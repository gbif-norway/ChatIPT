import os
import datetime
import gzip
import io
import json
import tarfile
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, override_settings
import openpyxl
import pandas as pd
import yaml
from .helpers.publish import (
    assert_case_insensitive_unique_identifier,
    make_eml,
    parse_newick_to_tree,
    parse_nexus_to_tree,
    parse_newick_tip_labels,
    parse_nexus_tip_labels,
    register_dataset_and_endpoint,
    _sanitize_dataframe_for_utf8_export,
    validate_dwca_archive,
)
from .agent_tools import (
    BasicValidationForSomeDwCTerms,
    ExportDwcDp,
    GetDarwinCoreInfo,
    SetEML,
    LogBugWithDeveloper,
    SetBasicMetadata,
    SetAgentTaskToComplete,
    RequestUserInput,
    SubmitDwcDpAccounting,
    PreviewDwcDpDescriptor,
    UploadDwCA,
    normalize_event_date,
    _dwc_dp_resources_from_mapping,
    _project_dwca_from_dwc_dp_resources,
)
from .helpers.openai_helpers import (
    CompatAssistantMessage,
    CompatFunctionCall,
    CompatToolCall,
    _attach_pdf_files_to_latest_user_message,
    _functions_to_responses_tools,
    _messages_to_responses_input,
    _response_to_compat_message,
    create_response_message,
)
from .models import Agent, Dataset, Message, Table, Task, UserFile
from .serializers import DatasetListSerializer, DatasetSerializer
from .accounting import build_source_accounting_snapshot, current_accounting_status
from .dwc_dp_specs import (
    DWC_DP_SCHEMA_REVISION,
    RESERVED_TABLE_NAMES,
    VENDORED_SCHEMA_ERRORS,
    build_dwc_dp_explorer_model,
    build_datapackage_descriptor,
    create_dwc_dp_archive,
    export_dwc_dp_package,
    validate_datapackage_descriptor,
    validate_dwc_dp_archive,
    validate_dwc_dp_resources,
)


class DwcDpSpecTests(SimpleTestCase):
    def _resources(self):
        return {
            'event': pd.DataFrame([
                {
                    'event_pk': 'event-1',
                    'eventID': 'source-event-1',
                    'eventCategory': 'occurrence',
                    'eventDate': '2025-04-26',
                    'country': 'Norway',
                },
            ]),
            'occurrence': pd.DataFrame([
                {
                    'occurrence_pk': 'occ-1',
                    'occurrenceID': 'source-occ-1',
                    'event_fk': 'event-1',
                    'scientificName': 'Apus apus',
                    'occurrenceStatus': 'present',
                },
            ]),
        }

    def test_validates_event_occurrence_resources_and_builds_descriptor(self):
        resources = self._resources()

        validation = validate_dwc_dp_resources(resources)
        self.assertTrue(validation['valid'], validation)
        self.assertEqual(validation['schema']['revision'], DWC_DP_SCHEMA_REVISION)

        descriptor = build_datapackage_descriptor(
            resources,
            title='Smoke test',
            description='DwC-DP smoke test',
        )
        self.assertEqual(
            descriptor['profile'],
            'http://rs.tdwg.org/dwc-dp/1.0/dwc-dp-profile.json',
        )
        self.assertEqual(descriptor['dwcDpSchema']['revision'], DWC_DP_SCHEMA_REVISION)
        self.assertEqual(len(descriptor['dwcDpSchema']['sha256']), 64)
        self.assertEqual(validate_datapackage_descriptor(descriptor), [])
        occurrence = next(resource for resource in descriptor['resources'] if resource['name'] == 'occurrence')
        self.assertEqual(occurrence['path'], 'occurrence.csv.gz')
        self.assertEqual(occurrence['schema']['primaryKey'], 'occurrence_pk')
        self.assertEqual(occurrence['schema']['weakPrimaryKey'], 'occurrenceID')
        self.assertEqual(
            occurrence['schema']['foreignKeys'][0]['reference']['resource'],
            'event',
        )
        self.assertEqual(
            occurrence['schema']['foreignKeys'][0]['reference']['fields'],
            'event_pk',
        )

    def test_vendored_snapshot_has_all_post_review_schemas(self):
        self.assertEqual(VENDORED_SCHEMA_ERRORS, ())
        self.assertEqual(len(RESERVED_TABLE_NAMES), 79)
        self.assertIn('survey-survey-target', RESERVED_TABLE_NAMES)
        self.assertIn('survey-target-descriptor', RESERVED_TABLE_NAMES)

    def test_rejects_old_identifier_based_relationship_shape(self):
        resources = {
            'event': pd.DataFrame([
                {'eventID': 'event-1', 'eventCategory': 'occurrence'},
            ]),
            'occurrence': pd.DataFrame([
                {'occurrenceID': 'occ-1', 'eventID': 'event-1', 'occurrenceStatus': 'present'},
            ]),
        }

        validation = validate_dwc_dp_resources(resources)

        self.assertFalse(validation['valid'])
        self.assertTrue(any("event_pk" in error for error in validation['errors']))
        self.assertTrue(any("event_fk" in error for error in validation['errors']))
        self.assertTrue(any("not defined" in error and "eventID" in error for error in validation['errors']))

    def test_validates_types_and_column_name_hygiene(self):
        resources = {
            'event': pd.DataFrame([
                {
                    ' event_pk': 'event-1',
                    'eventCategory': 'occurrence',
                    'year': 2025.5,
                },
            ]),
            'survey-target': pd.DataFrame([
                {'surveyTarget_pk': 'target-1', 'isSurveyTargetFullyReported': 'yes'},
            ]),
        }

        validation = validate_dwc_dp_resources(resources)

        self.assertFalse(validation['valid'])
        self.assertTrue(any("surrounding whitespace" in error for error in validation['errors']))
        self.assertTrue(any("integer values" in error for error in validation['errors']))
        self.assertTrue(any("boolean values" in error for error in validation['errors']))

    def test_enforces_strong_foreign_keys_but_not_weak_foreign_keys(self):
        event = pd.DataFrame([
            {
                'event_pk': 'event-1',
                'eventCategory': 'occurrence',
                'eventConductedByID': 'external-agent-id',
            },
        ])
        self.assertTrue(validate_dwc_dp_resources({'event': event})['valid'])

        occurrence = pd.DataFrame([
            {'occurrence_pk': 'occ-1', 'event_fk': 'missing-event', 'occurrenceStatus': 'present'},
        ])
        validation = validate_dwc_dp_resources({'event': event, 'occurrence': occurrence})
        self.assertFalse(validation['valid'])
        self.assertTrue(any("not found in 'event.event_pk'" in error for error in validation['errors']))

    def test_descriptor_includes_weak_relationships_when_target_table_is_present(self):
        resources = {
            'agent': pd.DataFrame([{'agent_pk': 'agent-1', 'agentID': 'source-agent-1'}]),
            'event': pd.DataFrame([
                {
                    'event_pk': 'event-1',
                    'eventID': 'source-event-1',
                    'eventCategory': 'occurrence',
                    'eventConductedByID': 'source-agent-1',
                },
            ]),
        }

        descriptor = build_datapackage_descriptor(resources)
        event = next(resource for resource in descriptor['resources'] if resource['name'] == 'event')

        self.assertEqual(event['schema']['weakPrimaryKey'], 'eventID')
        weak_fk = event['schema']['weakForeignKeys'][0]
        self.assertEqual(weak_fk['fields'], 'eventConductedByID')
        self.assertEqual(weak_fk['reference']['fields'], 'agentID')

    def test_creates_rooted_archive_with_gzipped_csv_resources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / 'package.tar.gz'
            create_dwc_dp_archive(
                archive_path,
                self._resources(),
                title='Smoke test',
                description='DwC-DP smoke test',
            )

            with tarfile.open(archive_path, 'r:gz') as archive:
                self.assertEqual(
                    sorted(archive.getnames()),
                    ['datapackage.json', 'eml.xml', 'event.csv.gz', 'occurrence.csv.gz'],
                )
                descriptor = json.load(archive.extractfile('datapackage.json'))
                occurrence_csv = gzip.decompress(archive.extractfile('occurrence.csv.gz').read()).decode('utf-8')

        self.assertEqual(descriptor['dwcDpSchema']['revision'], DWC_DP_SCHEMA_REVISION)
        self.assertIn('occurrence_pk,occurrenceID,event_fk', occurrence_csv)

    def test_archive_includes_and_validates_ancillary_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / 'package.tar.gz'
            create_dwc_dp_archive(
                archive_path,
                self._resources(),
                title='Tree smoke test',
                description='DwC-DP with supporting tree',
                additional_files=[('example.tre', b'(A,B);')],
            )

            validation = validate_dwc_dp_archive(
                archive_path,
                expected_resources=self._resources(),
                expected_additional_files=['example.tre'],
            )
            with tarfile.open(archive_path, 'r:gz') as archive:
                names = archive.getnames()

        self.assertTrue(validation['valid'], validation)
        self.assertIn('example.tre', names)

    def test_rejects_invalid_utf8_before_archive_creation(self):
        resources = self._resources()
        resources['occurrence'].loc[0, 'scientificName'] = 'collector\udc92s record'

        validation = validate_dwc_dp_resources(resources)

        self.assertFalse(validation['valid'])
        self.assertTrue(any('cannot be encoded as UTF-8' in error for error in validation['errors']))

    def test_publication_safety_and_semantic_checks_are_warnings(self):
        resources = {
            'event': pd.DataFrame([{
                'event_pk': 'event-1',
                'eventCategory': 'occurrence',
                'dataGeneralizations': (
                    'Coordinates generalized from 32.2340, -102.2340 to whole degrees.'
                ),
            }]),
            'event-assertion': pd.DataFrame([{
                'event_fk': 'event-1',
                'assertionType': 'unused flag',
                'assertionValue': '',
            }]),
            'resource-relationship': pd.DataFrame([{
                'subjectResourceID': 'occ-1',
                'relationshipType': 'preys on',
                'relatedResourceID': 'occ-2',
            }]),
        }

        validation = validate_dwc_dp_resources(resources)

        self.assertTrue(validation['valid'], validation)
        self.assertTrue(any('precise coordinate pairs' in warning for warning in validation['warnings']))
        self.assertTrue(any('blank assertionValue' in warning for warning in validation['warnings']))
        self.assertTrue(any('organism-interaction' in warning for warning in validation['warnings']))

    def test_bdq_inspired_checks_report_cross_field_and_vocabulary_warnings(self):
        resources = {
            'event': pd.DataFrame([{
                'event_pk': 'event-1',
                'eventCategory': 'occurrence',
                'eventDate': '2023-02-28',
                'year': 2024,
                'month': 2,
                'day': 30,
                'countryCode': 'no',
                'decimalLatitude': 0,
                'decimalLongitude': 0,
                'geodeticDatum': '',
                'minimumDepthInMeters': 20,
                'maximumDepthInMeters': 10,
                'minimumElevationInMeters': 100,
                'maximumElevationInMeters': 50,
            }]),
            'occurrence': pd.DataFrame([{
                'occurrence_pk': 'occ-1',
                'event_fk': 'event-1',
                'occurrenceStatus': 'Present',
            }]),
        }

        validation = validate_dwc_dp_resources(resources)
        warnings = '\n'.join(validation['warnings'])

        self.assertTrue(validation['valid'], validation)
        for test_id in (
            'VALIDATION_COORDINATES_NOTZERO',
            'VALIDATION_GEODETICDATUM_NOTEMPTY',
            'VALIDATION_MINDEPTH_LESSTHAN_MAXDEPTH',
            'VALIDATION_MINELEVATION_LESSTHAN_MAXELEVATION',
            'VALIDATION_COUNTRYCODE_STANDARD',
            'VALIDATION_OCCURRENCESTATUS_STANDARD',
            'VALIDATION_DAY_INRANGE',
            'VALIDATION_EVENT_CONSISTENT',
        ):
            self.assertIn(test_id, warnings)

    def test_bdq_inspired_checks_accept_valid_and_inapplicable_values(self):
        resources = {
            'event': pd.DataFrame([
                {
                    'event_pk': 'event-1',
                    'eventCategory': 'occurrence',
                    'eventDate': '2024-02-29',
                    'year': 2024,
                    'month': 2,
                    'day': 29,
                    'startDayOfYear': 60,
                    'endDayOfYear': 60,
                    'countryCode': 'NO',
                    'decimalLatitude': 0,
                    'decimalLongitude': 10,
                    'geodeticDatum': 'WGS84',
                    'minimumDepthInMeters': 0,
                    'maximumDepthInMeters': 10,
                    'minimumElevationInMeters': -20,
                    'maximumElevationInMeters': 100,
                },
                {
                    'event_pk': 'event-2',
                    'eventCategory': 'occurrence',
                    'year': 2024,
                    'day': 29,
                    'countryCode': 'XZ',
                },
            ]),
            'occurrence': pd.DataFrame([
                {
                    'occurrence_pk': 'occ-1',
                    'event_fk': 'event-1',
                    'occurrenceStatus': 'absent',
                },
                {
                    'occurrence_pk': 'occ-2',
                    'event_fk': 'event-2',
                    'occurrenceStatus': 'present',
                },
            ]),
        }

        validation = validate_dwc_dp_resources(resources)

        self.assertTrue(validation['valid'], validation)
        self.assertFalse(
            any(warning.startswith('BDQ-inspired') for warning in validation['warnings']),
            validation,
        )

    @patch('api.dwc_dp_specs.upload_file')
    @patch('minio.Minio')
    def test_uploads_package_with_gzip_content_type(self, minio_class, upload_file_mock):
        env = {
            'MINIO_URI': 'storage.example.org',
            'MINIO_ACCESS_KEY': 'key',
            'MINIO_SECRET_KEY': 'secret',
            'MINIO_BUCKET': 'bucket',
            'MINIO_BUCKET_FOLDER': 'packages',
        }
        with patch.dict(os.environ, env):
            url = export_dwc_dp_package(
                self._resources(),
                title='Smoke test',
                description='DwC-DP smoke test',
            )

        self.assertTrue(url.startswith('https://storage.example.org/bucket/packages/dwc-dp-'))
        self.assertEqual(upload_file_mock.call_args.kwargs['content_type'], 'application/gzip')
        minio_class.assert_called_once()

    def test_duplicate_resource_mapping_is_reported_without_overwriting(self):
        first = SimpleNamespace(id=1, title='event', df=self._resources()['event'])
        second = SimpleNamespace(id=2, title='event', df=self._resources()['event'])
        dataset = SimpleNamespace(table_set=SimpleNamespace(all=lambda: [first, second]))

        resources, errors = _dwc_dp_resources_from_mapping(dataset, None)

        self.assertEqual(list(resources), ['event'])
        self.assertEqual(len(errors), 1)
        self.assertIn('mapped more than once', errors[0])

    def test_dwca_projection_joins_enforced_keys_and_restores_dwc_identifiers(self):
        projected, core_type = _project_dwca_from_dwc_dp_resources(self._resources())

        self.assertEqual(core_type.value, 'occurrence')
        self.assertEqual(projected.loc[0, 'occurrenceID'], 'source-occ-1')
        self.assertEqual(projected.loc[0, 'eventID'], 'source-event-1')
        self.assertEqual(projected.loc[0, 'eventDate'], '2025-04-26')
        self.assertNotIn('occurrence_pk', projected.columns)
        self.assertNotIn('event_fk', projected.columns)


class EmlGenerationTests(SimpleTestCase):
    def test_eml_template_is_well_formed(self):
        template_path = os.path.join(os.path.dirname(__file__), 'templates', 'eml.xml')
        with open(template_path, 'rb') as fh:
            xml_bytes = fh.read()
        # Should parse without error
        ET.fromstring(xml_bytes)

    def test_make_eml_generates_parseable_xml(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Test Dataset',
            description='Plain text abstract',
            user=DummyUser(),
            eml_extra={
                'geographic_scope': 'Norway. Coordinate bounds: lat 58 to 71, lon 4 to 31.',
                'temporal_scope': '2024-01-01/2024-12-31',
                'taxonomic_scope': 'Coleoptera',
                'methodology': 'Trap sampling',
                'users': [
                    {'first_name': 'Alice', 'last_name': 'Smith', 'orcid': '0000-0001-2345-6789'}
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        # dataset exists
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)
        # title
        title = dataset.find('title')
        self.assertIsNotNone(title)
        self.assertEqual(title.text, 'Test Dataset')
        # language
        language = dataset.find('language')
        self.assertIsNotNone(language)
        self.assertEqual(language.text, 'eng')
        # pubDate
        self.assertIsNotNone(dataset.find('pubDate'))
        # abstract para
        abstract_para = dataset.find('abstract/para')
        self.assertIsNotNone(abstract_para)
        self.assertEqual(abstract_para.text, 'Plain text abstract')
        # contact with email
        contact_email = dataset.find('contact/electronicMailAddress')
        self.assertIsNotNone(contact_email)
        self.assertEqual(contact_email.text, 'alice@example.org')
        # coverage blocks present
        coverage = dataset.find('coverage')
        self.assertIsNotNone(coverage)
        self.assertIsNotNone(coverage.find('geographicCoverage/geographicDescription'))
        self.assertEqual(
            coverage.find('geographicCoverage/boundingCoordinates/westBoundingCoordinate').text,
            '4',
        )
        # methods description present
        self.assertIsNotNone(dataset.find('methods/methodStep/description/para'))

    def test_make_eml_uses_each_supported_gbif_license(self):
        expected = {
            "CC0 1.0": "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
            "CC BY 4.0": "http://creativecommons.org/licenses/by/4.0/legalcode",
            "CC BY-NC 4.0": "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
        }

        for license_name, license_url in expected.items():
            with self.subTest(license_name=license_name):
                root = ET.fromstring(
                    make_eml("Licensed data", "Description", eml_extra={"license": license_name})
                )
                link = root.find("dataset/intellectualRights/para/ulink")
                self.assertIsNotNone(link)
                self.assertEqual(link.attrib["url"], license_url)
                self.assertTrue(link.findtext("citetitle"))

    @patch("api.helpers.publish.requests.post")
    def test_gbif_registration_uses_canonical_license_url(self, post_mock):
        post_mock.side_effect = [
            SimpleNamespace(status_code=201, json=lambda: "dataset-key"),
            SimpleNamespace(status_code=201, json=lambda: {}),
        ]

        register_dataset_and_endpoint(
            "Licensed data",
            "Description",
            "https://example.org/archive.zip",
            "CC BY-NC 4.0",
        )

        dataset_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(
            dataset_payload["license"],
            "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
        )

    def test_make_eml_maps_manuscript_fields_and_creators(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='PDF-derived dataset',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'manuscript_doi': '10.1234/abcd.1',
                'dataset_citation': 'Doe J, Roe R (2025) Example manuscript.',
                'manuscript_title': 'Example manuscript',
                'journal': 'Journal of Examples',
                'publication_year': 2025,
                'users': [
                    {'first_name': 'Jane', 'last_name': 'Doe', 'email': 'jane@example.org'},
                    {'first_name': 'Richard', 'last_name': 'Roe', 'email': 'richard@example.org'},
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        creators = dataset.findall('creator')
        self.assertEqual(len(creators), 2)
        self.assertEqual(creators[0].find('individualName/givenName').text, 'Jane')
        self.assertEqual(creators[1].find('individualName/givenName').text, 'Richard')
        self.assertEqual(len(dataset.findall('project/personnel')), 0)

    def test_make_eml_normalizes_orcid_url_to_identifier_and_directory(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = 'https://orcid.org/0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='ORCID normalization',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'users': [
                    {
                        'first_name': 'Jane',
                        'last_name': 'Doe',
                        'email': 'jane@example.org',
                        'orcid': 'https://orcid.org/0000-0002-1825-0097',
                    },
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        creator_user_id = dataset.find('creator/userId')
        self.assertIsNotNone(creator_user_id)
        self.assertEqual(creator_user_id.get('directory'), 'https://orcid.org/')
        self.assertEqual(creator_user_id.text, '0000-0002-1825-0097')

        metadata_provider_user_id = dataset.find('metadataProvider/userId')
        self.assertIsNotNone(metadata_provider_user_id)
        self.assertEqual(metadata_provider_user_id.get('directory'), 'https://orcid.org/')
        self.assertEqual(metadata_provider_user_id.text, '0000-0001-2345-6789')

        contact_email = dataset.find('contact/electronicMailAddress')
        self.assertIsNotNone(contact_email)
        self.assertEqual(contact_email.text, 'alice@example.org')

    def test_make_eml_adds_project_personnel_when_project_title_is_set(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Project-backed dataset',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'project_title': 'Arctic Deep Survey',
                'users': [
                    {'first_name': 'Jane', 'last_name': 'Doe', 'email': 'jane@example.org'},
                    {'first_name': 'Richard', 'last_name': 'Roe', 'email': 'richard@example.org'},
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        self.assertEqual(dataset.find('project/title').text, 'Arctic Deep Survey')
        personnel_nodes = dataset.findall('project/personnel')
        self.assertEqual(len(personnel_nodes), 2)
        self.assertEqual(personnel_nodes[0].find('individualName/givenName').text, 'Jane')
        self.assertEqual(personnel_nodes[1].find('individualName/givenName').text, 'Richard')
        self.assertIsNone(personnel_nodes[0].find('electronicMailAddress'))
        self.assertIsNone(personnel_nodes[0].find('userId'))
        self.assertEqual(personnel_nodes[0].find('role').text, 'metadataProvider')

    def test_make_eml_keeps_creator_before_metadata_provider_when_users_override_primary_creator(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Ordered creators',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'users': [
                    {'first_name': 'Jane', 'last_name': 'Doe', 'email': 'jane@example.org'},
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        child_tags = [child.tag for child in list(dataset)]
        creator_index = child_tags.index('creator')
        metadata_provider_index = child_tags.index('metadataProvider')
        self.assertLess(creator_index, metadata_provider_index)

    def test_make_eml_builds_taxonomic_classification_from_scope(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Taxonomy',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'taxonomic_scope': 'Plantae, Solanaceae',
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        self.assertEqual(
            dataset.find('coverage/taxonomicCoverage/generalTaxonomicCoverage').text,
            'Plantae, Solanaceae',
        )
        first_classification = dataset.find('coverage/taxonomicCoverage/taxonomicClassification')
        self.assertIsNotNone(first_classification)
        self.assertEqual(first_classification.find('taxonRankName').text, 'kingdom')
        self.assertEqual(first_classification.find('taxonRankValue').text, 'Plantae')

        self.assertIsNone(first_classification.find('taxonomicClassification'))
        classifications = dataset.findall('coverage/taxonomicCoverage/taxonomicClassification')
        self.assertEqual(len(classifications), 2)
        self.assertEqual(classifications[1].find('taxonRankName').text, 'family')
        self.assertEqual(classifications[1].find('taxonRankValue').text, 'Solanaceae')

    def test_make_eml_uses_structured_taxonomic_keywords(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Structured taxonomy',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'taxonomic_scope': 'Plants and fungi in source data.',
                'taxonomic_keywords': [
                    {'rank': 'kingdom', 'scientificName': 'Plantae'},
                    {'rank': 'family', 'scientificName': 'Solanaceae', 'commonName': 'nightshades'},
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        classifications = dataset.findall('coverage/taxonomicCoverage/taxonomicClassification')
        self.assertEqual(len(classifications), 2)
        self.assertEqual(classifications[0].find('taxonRankValue').text, 'Plantae')
        self.assertEqual(classifications[1].find('commonName').text, 'nightshades')

    def test_make_eml_does_not_turn_taxonomic_prose_into_classifications(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Taxonomic prose',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'taxonomic_scope': (
                    'Plantae, Fabaceae. The dataset contains specimen-linked occurrence records '
                    'for multiple genera, with emphasis on Errazurizia and related taxa.'
                ),
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        classifications = dataset.findall('coverage/taxonomicCoverage/taxonomicClassification')
        self.assertEqual(len(classifications), 1)
        self.assertEqual(classifications[0].find('taxonRankValue').text, 'Plantae')

    def test_make_eml_orders_agent_email_before_user_id(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Ordered agent',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={},
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        creator_tags = [child.tag for child in list(dataset.find('creator'))]
        self.assertLess(creator_tags.index('electronicMailAddress'), creator_tags.index('userId'))
        contact_tags = [child.tag for child in list(dataset.find('contact'))]
        self.assertLess(contact_tags.index('electronicMailAddress'), contact_tags.index('userId'))

    def test_make_eml_omits_geographic_coverage_without_bounds(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='No geographic bounds',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'geographic_scope': 'Norway',
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        self.assertIsNone(dataset.find('coverage/geographicCoverage'))

    def test_make_eml_extracts_geographic_bounds_from_scope_text(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Geographic bounds',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'geographic_scope': (
                    'Coordinates range from 28.55056 S to 44.49629167 N latitude '
                    'and from 112.71712 W to 70.80929 W longitude.'
                ),
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        bounds = dataset.find('coverage/geographicCoverage/boundingCoordinates')
        self.assertIsNotNone(bounds)
        self.assertEqual(bounds.find('westBoundingCoordinate').text, '-112.71712')
        self.assertEqual(bounds.find('eastBoundingCoordinate').text, '-70.80929')
        self.assertEqual(bounds.find('northBoundingCoordinate').text, '44.496292')
        self.assertEqual(bounds.find('southBoundingCoordinate').text, '-28.55056')

    def test_make_eml_accepts_structured_geographic_bounds_with_zero_values(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Structured geographic bounds',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'geographic_scope': 'Equator and prime meridian test area.',
                'geographic_bounds': {
                    'west': 0,
                    'east': 10,
                    'north': 5,
                    'south': 0,
                },
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        bounds = dataset.find('coverage/geographicCoverage/boundingCoordinates')
        self.assertIsNotNone(bounds)
        self.assertEqual(bounds.find('westBoundingCoordinate').text, '0')
        self.assertEqual(bounds.find('southBoundingCoordinate').text, '0')

    def test_make_eml_omits_template_coverage_when_all_scopes_are_empty(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='No coverage',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'geographic_scope': None,
                'temporal_scope': None,
                'taxonomic_scope': None,
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)
        self.assertIsNone(dataset.find('coverage'))

    def test_make_eml_normalizes_temporal_textual_range(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Temporal text range',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'temporal_scope': '17 - 22 October 2025',
            },
        )

        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        begin = dataset.find('coverage/temporalCoverage/rangeOfDates/beginDate/calendarDate')
        end = dataset.find('coverage/temporalCoverage/rangeOfDates/endDate/calendarDate')
        self.assertIsNotNone(begin)
        self.assertIsNotNone(end)
        self.assertEqual(begin.text, '2025-10-17')
        self.assertEqual(end.text, '2025-10-22')

    def test_make_eml_extracts_iso_dates_from_free_text_temporal_scope(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Temporal free text',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'temporal_scope': (
                    'Field observations appear span 2025-09-18 to 2025-09-23 based on project naming.'
                ),
            },
        )

        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        begin = dataset.find('coverage/temporalCoverage/rangeOfDates/beginDate/calendarDate')
        end = dataset.find('coverage/temporalCoverage/rangeOfDates/endDate/calendarDate')
        self.assertIsNotNone(begin)
        self.assertIsNotNone(end)
        self.assertEqual(begin.text, '2025-09-18')
        self.assertEqual(end.text, '2025-09-23')


class IdentifierValidationTests(SimpleTestCase):
    def test_case_insensitive_identifier_uniqueness_passes_for_unique_values(self):
        df = pd.DataFrame({'occurrenceID': ['abc-1', 'Abc-2', 'ABC-3']})
        assert_case_insensitive_unique_identifier(df, 'occurrenceID')

    def test_case_insensitive_identifier_uniqueness_fails_for_collisions(self):
        df = pd.DataFrame({'occurrenceID': ['1764255881670-v4s75yh73|185À', '1764255881670-v4s75yh73|185à']})
        with self.assertRaises(ValueError):
            assert_case_insensitive_unique_identifier(df, 'occurrenceID')

    def test_case_insensitive_identifier_uniqueness_ignores_empty_values(self):
        df = pd.DataFrame({'occurrenceID': ['', None, 'x-1']})
        assert_case_insensitive_unique_identifier(df, 'occurrenceID')


class PhylogenyParsingTests(SimpleTestCase):
    """Tests for phylogenetic tree parsing and matching functions."""
    
    def test_parse_nexus_with_translate_block(self):
        """Test parsing NEXUS file with TRANSLATE block."""
        nexus_content = """#NEXUS
BEGIN TREES;
	Title Acacia;
	LINK Taxa = Taxa;
	TRANSLATE
        DBT01_clean	Berneuxia_thibetica_01,
        DBT03_clean	Berneuxia_thibetica_02,
        Berneuxia_thibetica	Berneuxia_thibetica_P,
        S7451102	Cyrilla_racemiflora,
        DDH04_clean	Diapensia_himalaica_01;
    TREE tree_1 = (DBT01_clean,DBT03_clean);
END;"""
        
        tip_labels = parse_nexus_tip_labels(nexus_content)
        
        # Should extract translated names from TRANSLATE block
        expected_labels = [
            'Berneuxia_thibetica_01',
            'Berneuxia_thibetica_02',
            'Berneuxia_thibetica_P',
            'Cyrilla_racemiflora',
            'Diapensia_himalaica_01'
        ]
        
        self.assertEqual(set(tip_labels), set(expected_labels))
        self.assertEqual(len(tip_labels), len(expected_labels))
    
    def test_parse_nexus_from_example_file(self):
        """Test parsing the actual example NEXUS file."""
        nexus_path = os.path.join(
            os.path.dirname(__file__),
            'templates', 'examples', 'gaynor_et_al_v3', 'above50_genes.nex'
        )
        
        with open(nexus_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        tip_labels = parse_nexus_tip_labels(content)
        
        # Should extract all translated tip labels
        self.assertGreater(len(tip_labels), 0)
        
        # Check for specific expected labels from the file
        expected_labels = [
            'Berneuxia_thibetica_01',
            'Berneuxia_thibetica_02',
            'Berneuxia_thibetica_P',
            'Cyrilla_racemiflora',
            'Diapensia_himalaica_01',
            'Diapensia_himalaica_02',
            'Diapensia_himalaica_P',
            'Diapensia_lapponica_01',
            'Diapensia_lapponica_02',
            'Diapensia_lapponica_03',
            'Diapensia_obovata_01',
            'Diapensia_obovata_02',
            'Diapensia_purpurea_01',
            'Diapensia_purpurea_P',
            'Diapensia_wardii_P',
            'Galax_urceolata_01',
            'Galax_urceolata_02',
            'Pyxidanthera_barbulata_01',
            'Pyxidanthera_barbulata_02',
            'Pyxidanthera_brevifolia_01',
            'Pyxidanthera_brevifolia_02',
            'Schizocodon_ilicifolius_01',
            'Schizocodon_soldanelloides_01',
            'Schizocodon_soldanelloides_02',
            'Shortia_galacifolia_01',
            'Shortia_galacifolia_02',
            'Shortia_rotundifolia_01',
            'Shortia_sinensis_01',
            'Shortia_sinensis_P',
            'Shortia_uniflora_01',
            'Shortia_uniflora_02',
        ]
        
        # Check that all expected labels are present
        for expected in expected_labels:
            self.assertIn(expected, tip_labels, f"Expected label {expected} not found")
        
        # Should have exactly 31 labels
        self.assertEqual(len(tip_labels), 31)
    
    def test_parse_newick_simple(self):
        """Test parsing a simple Newick format tree."""
        newick_content = "(A:0.1,B:0.2,(C:0.3,D:0.4):0.5);"
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        expected = ['A', 'B', 'C', 'D']
        self.assertEqual(set(tip_labels), set(expected))
        self.assertEqual(len(tip_labels), len(expected))
    
    def test_parse_newick_with_underscores(self):
        """Test parsing Newick with labels containing underscores."""
        newick_content = "(Berneuxia_thibetica:0.1,Diapensia_himalaica:0.2);"
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        expected = ['Berneuxia_thibetica', 'Diapensia_himalaica']
        self.assertEqual(set(tip_labels), set(expected))
    
    def test_parse_newick_complex(self):
        """Test parsing a more complex Newick tree."""
        newick_content = "((A:0.1,B:0.2)100:0.3,(C:0.4,D:0.5)90:0.6);"
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        expected = ['A', 'B', 'C', 'D']
        self.assertEqual(set(tip_labels), set(expected))
    
    def test_parse_nexus_no_translate_block(self):
        """Test parsing NEXUS file without TRANSLATE block."""
        nexus_content = """#NEXUS
BEGIN TREES;
    TREE tree_1 = (A:0.1,B:0.2,(C:0.3,D:0.4):0.5);
END;"""
        
        tip_labels = parse_nexus_tip_labels(nexus_content)
        
        expected = ['A', 'B', 'C', 'D']
        self.assertEqual(set(tip_labels), set(expected))
    
    def test_parse_newick_handles_quotes(self):
        """Test that Newick parser handles various label formats."""
        # Labels might have quotes or special characters
        newick_content = '("Species A":0.1,"Species B":0.2);'
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        # Should extract labels even with quotes
        # Note: current implementation may not handle quotes, but should not crash
        self.assertIsInstance(tip_labels, list)

    def test_parse_newick_preserves_nonstandard_unquoted_spaces(self):
        newick_content = (
            '(Prosopidastrum mexicanum RM17061 Mexico,'
            'Dichrostachys cinerea ERR7618736);'
        )

        self.assertEqual(
            parse_newick_tip_labels(newick_content),
            [
                'Prosopidastrum mexicanum RM17061 Mexico',
                'Dichrostachys cinerea ERR7618736',
            ],
        )
        tree = parse_newick_to_tree(newick_content)
        self.assertEqual(
            [child["name"] for child in tree["children"]],
            [
                "Prosopidastrum mexicanum RM17061 Mexico",
                "Dichrostachys cinerea ERR7618736",
            ],
        )

    def test_parse_nexus_translate_preserves_quoted_spaces(self):
        nexus_content = """#NEXUS
BEGIN TREES;
TRANSLATE
  1 'Species one voucher A',
  2 'Species two voucher B';
TREE tree_1 = (1:0.1,2:0.2);
END;"""

        self.assertEqual(
            parse_nexus_tip_labels(nexus_content),
            ['Species one voucher A', 'Species two voucher B'],
        )
        tree = parse_nexus_to_tree(nexus_content)
        self.assertEqual(
            [child["name"] for child in tree["children"]],
            ['Species one voucher A', 'Species two voucher B'],
        )


class GetDarwinCoreInfoTests(SimpleTestCase):
    def test_summary_response_lists_sections_only(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier. Examples: abc",
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
            "Event": {
                "eventID": "An identifier for the event. Examples: EVT-1",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo().run()

        self.assertIn("Darwin Core quick reference sections:", response)
        self.assertIn("- Occurrence (2 terms)", response)
        self.assertIn("- Event (1 terms)", response)
        self.assertNotIn("The specific nature of the data record", response)

    def test_section_response_returns_all_terms_and_includes_examples_by_default(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier for the occurrence. Examples: 1234",
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(section="Occurrence").run()

        self.assertIn("Occurrence (2 terms total):", response)
        self.assertIn("- occurrenceID: An identifier for the occurrence. Examples: 1234", response)
        self.assertIn("- basisOfRecord: The specific nature of the data record. Examples: HumanObservation", response)

    def test_section_response_can_strip_examples_when_requested(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier for the occurrence. Examples: 1234",
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(section="Occurrence", include_examples=False).run()

        self.assertIn("Occurrence (2 terms total):", response)
        self.assertIn("- occurrenceID: An identifier for the occurrence.", response)
        self.assertIn("- basisOfRecord: The specific nature of the data record.", response)
        self.assertNotIn("Examples:", response)

    def test_section_response_applies_optional_limit(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier for the occurrence.",
                "basisOfRecord": "The specific nature of the data record.",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(section="Occurrence", max_terms=1).run()

        self.assertIn("Occurrence (2 terms total):", response)
        self.assertIn("- occurrenceID: An identifier for the occurrence.", response)
        self.assertIn("... 1 more terms not shown. Increase `max_terms` to see more.", response)

    def test_term_lookup_can_include_examples(self):
        sample_reference = {
            "Occurrence": {
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(terms=["basisOfRecord"], include_examples=True).run()

        self.assertIn("Darwin Core term lookup results:", response)
        self.assertIn("basisOfRecord (Occurrence): The specific nature of the data record. Examples: HumanObservation", response)


class EventDateNormalizationTests(SimpleTestCase):
    def test_preserves_partial_date_precision_and_interval_precision(self):
        self.assertEqual(normalize_event_date("2010"), "2010")
        self.assertEqual(normalize_event_date("2010-07"), "2010-07")
        self.assertEqual(normalize_event_date("2010/2011"), "2010/2011")
        self.assertEqual(normalize_event_date("2010-07/2011-03"), "2010-07/2011-03")

    def test_normalizes_only_explicit_textual_precision(self):
        self.assertEqual(normalize_event_date("March 2010"), "2010-03")
        self.assertEqual(normalize_event_date("11 March 2010"), "2010-03-11")
        self.assertIsNone(normalize_event_date("03/04/2010"))

    def test_normalizes_compact_and_spreadsheet_date_values(self):
        self.assertEqual(normalize_event_date("20250211"), "2025-02-11")
        self.assertEqual(
            normalize_event_date("2025-02-11 00:00:00"),
            "2025-02-11",
        )
        self.assertEqual(
            normalize_event_date(pd.Timestamp("2025-02-11 00:00:00")),
            "2025-02-11",
        )
        self.assertIsNone(normalize_event_date("20250230"))

    def test_normalizes_real_datetimes_to_iso_without_dropping_time(self):
        self.assertEqual(
            normalize_event_date("2025-02-11T00:00:00"),
            "2025-02-11T00:00:00",
        )
        self.assertEqual(
            normalize_event_date("2025-02-11 12:34:56"),
            "2025-02-11T12:34:56",
        )
        self.assertEqual(
            normalize_event_date("2025-02-11T00:00:00Z"),
            "2025-02-11T00:00:00+00:00",
        )

    def test_basic_validation_does_not_invent_date_components(self):
        df = pd.DataFrame(
            {"eventDate": ["2010", "2010/2011", "March 2010", "not known"]}
        )

        normalized, failed, future = BasicValidationForSomeDwCTerms(
            agent_id=1
        ).validate_and_format_event_dates(df)

        self.assertEqual(
            normalized["eventDate"].tolist(),
            ["2010", "2010/2011", "2010-03", "not known"],
        )
        self.assertEqual(failed, [3])
        self.assertEqual(future, [])


class SetEMLTemporalInferenceTests(SimpleTestCase):
    def test_infer_temporal_bounds_from_eventdate_column(self):
        df = pd.DataFrame(
            {
                "eventDate": [
                    "2019-01-15",
                    "2020-02",
                    "2018",
                    "2007-03-01/2008-05-11",
                ]
            }
        )

        bounds = SetEML._infer_temporal_bounds_from_df(df)

        self.assertEqual(bounds[0].isoformat(), "2007-03-01")
        self.assertEqual(bounds[1].isoformat(), "2020-02-29")

    def test_infer_temporal_bounds_from_year_month_day_columns(self):
        df = pd.DataFrame(
            {
                "year": ["2011", "2013", "2012"],
                "month": ["", "6", "2"],
                "day": ["", "", "29"],
            }
        )

        bounds = SetEML._infer_temporal_bounds_from_df(df)

        self.assertEqual(bounds[0].isoformat(), "2011-01-01")
        self.assertEqual(bounds[1].isoformat(), "2013-06-30")

    def test_resolve_temporal_scope_replaces_today_placeholder_with_inferred_range(self):
        today = datetime.date.today()
        today_text = today.strftime("%d %B %Y")
        provided_scope = f"{today_text} - {today_text}"
        inferred_scope = "2018-01-01/2020-12-31"

        resolved_scope, note = SetEML._resolve_temporal_scope(provided_scope, None, inferred_scope)

        self.assertEqual(resolved_scope, inferred_scope)
        self.assertIn("adjusted from placeholder", note)

    def test_resolve_temporal_scope_keeps_non_placeholder_value(self):
        provided_scope = "2015-01-01/2016-12-31"
        inferred_scope = "2018-01-01/2020-12-31"

        resolved_scope, note = SetEML._resolve_temporal_scope(provided_scope, None, inferred_scope)

        self.assertEqual(resolved_scope, provided_scope)
        self.assertIsNone(note)

    def test_resolve_temporal_scope_keeps_existing_non_placeholder_when_not_provided(self):
        existing_scope = "2001-01-01/2003-12-31"
        inferred_scope = "2018-01-01/2020-12-31"

        resolved_scope, note = SetEML._resolve_temporal_scope(None, existing_scope, inferred_scope)

        self.assertEqual(resolved_scope, existing_scope)
        self.assertIsNone(note)

    def test_infer_geographic_scope_from_country_and_coordinates(self):
        class DummyTable:
            def __init__(self, df):
                self.df = df

        class DummyTableSet:
            def __init__(self, tables):
                self._tables = tables

            def all(self):
                return self._tables

        class DummyDataset:
            def __init__(self, tables):
                self.table_set = DummyTableSet(tables)

        dataset = DummyDataset(
            [
                DummyTable(
                    pd.DataFrame(
                        {
                            "country": ["Norway", "Sweden", "Norway"],
                            "decimalLatitude": ["60.1", "61.2", "59.9"],
                            "decimalLongitude": ["10.0", "11.5", "9.8"],
                        }
                    )
                )
            ]
        )

        inferred = SetEML._infer_geographic_scope_from_dataset(dataset)

        self.assertIn("Countries:", inferred)
        self.assertIn("Norway", inferred)
        self.assertIn("Sweden", inferred)
        self.assertIn("Coordinate bounds:", inferred)

    def test_infer_taxonomic_scope_from_taxonomic_columns(self):
        class DummyTable:
            def __init__(self, df):
                self.df = df

        class DummyTableSet:
            def __init__(self, tables):
                self._tables = tables

            def all(self):
                return self._tables

        class DummyDataset:
            def __init__(self, tables):
                self.table_set = DummyTableSet(tables)

        dataset = DummyDataset(
            [
                DummyTable(
                    pd.DataFrame(
                        {
                            "family": ["Felidae", "Canidae", "Felidae"],
                        }
                    )
                )
            ]
        )

        inferred = SetEML._infer_taxonomic_scope_from_dataset(dataset)

        self.assertIn("Families:", inferred)
        self.assertIn("Felidae", inferred)
        self.assertIn("Canidae", inferred)

    def test_infer_taxonomic_keywords_from_taxonomic_columns(self):
        class DummyTable:
            def __init__(self, df):
                self.df = df

        class DummyTableSet:
            def __init__(self, tables):
                self._tables = tables

            def all(self):
                return self._tables

        class DummyDataset:
            def __init__(self, tables):
                self.table_set = DummyTableSet(tables)

        dataset = DummyDataset(
            [
                DummyTable(
                    pd.DataFrame(
                        {
                            "kingdom": ["Animalia", "Animalia"],
                            "family": ["Felidae", "Canidae"],
                        }
                    )
                )
            ]
        )

        inferred = SetEML._infer_taxonomic_keywords_from_dataset(dataset)

        self.assertIn({"rank": "kingdom", "scientificName": "Animalia"}, inferred)
        self.assertIn({"rank": "family", "scientificName": "Felidae"}, inferred)
        self.assertIn({"rank": "family", "scientificName": "Canidae"}, inferred)

    def test_infer_methodology_from_sampling_protocol(self):
        class DummyTable:
            def __init__(self, df):
                self.df = df

        class DummyTableSet:
            def __init__(self, tables):
                self._tables = tables

            def all(self):
                return self._tables

        class DummyDataset:
            def __init__(self, tables):
                self.table_set = DummyTableSet(tables)

        dataset = DummyDataset(
            [
                DummyTable(
                    pd.DataFrame(
                        {
                            "samplingProtocol": ["Camera trap", "Camera trap"],
                        }
                    )
                )
            ]
        )

        inferred = SetEML._infer_methodology_from_dataset(dataset)
        self.assertEqual(inferred, "Camera trap")


class ExcelWorkbookRepairTests(SimpleTestCase):
    @staticmethod
    def _build_workbook_bytes():
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "template"
        sheet["A1"] = "column"
        sheet["A2"] = "value"
        sheet["A1"].comment = openpyxl.comments.Comment("sample comment", "tester")

        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def _inject_invalid_font_family_values(workbook_bytes):
        source = io.BytesIO(workbook_bytes)
        output = io.BytesIO()

        with zipfile.ZipFile(source, "r") as source_zip:
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as output_zip:
                for info in source_zip.infolist():
                    data = source_zip.read(info.filename)
                    if info.filename.endswith(".xml"):
                        xml = data.decode("utf-8")
                        xml = xml.replace('family val="2"', 'family val="34"')
                        data = xml.encode("utf-8")
                    output_zip.writestr(info, data)

        return output.getvalue()

    def test_loader_repairs_out_of_range_font_family_values(self):
        from .models import UserFile

        valid_workbook = self._build_workbook_bytes()
        broken_workbook = self._inject_invalid_font_family_values(valid_workbook)

        with self.assertRaises(ValueError):
            openpyxl.load_workbook(io.BytesIO(broken_workbook))

        workbook = UserFile._load_workbook_with_xml_repair(broken_workbook)
        self.assertEqual(workbook.sheetnames, ["template"])

    def test_sanitizer_is_noop_for_valid_workbook(self):
        from .models import UserFile

        workbook_bytes = self._build_workbook_bytes()
        sanitized_bytes, modified = UserFile._sanitize_excel_xml_font_families(workbook_bytes)

        self.assertFalse(modified)
        self.assertEqual(workbook_bytes, sanitized_bytes)


class LogBugWithDeveloperTests(SimpleTestCase):
    @patch("api.agent_tools.discord_bot.send_discord_message")
    def test_uses_discord_user_id_for_direct_mention(self, send_discord_message_mock):
        with patch.dict(os.environ, {"DISCORD_DEVELOPER_USER_ID": "1234567890"}, clear=False):
            result = LogBugWithDeveloper(
                message="Python tool failed with unexpected KeyError",
                agent_id=44,
                urgent=True,
            ).run()

        self.assertIn("Bug report sent", result)
        sent_message = send_discord_message_mock.call_args.args[0]
        sent_allowed_mentions = send_discord_message_mock.call_args.kwargs["allowed_mentions"]

        self.assertIn("<@1234567890>", sent_message)
        self.assertIn("Agent ID: 44", sent_message)
        self.assertIn("Python tool failed with unexpected KeyError", sent_message)
        self.assertEqual(sent_allowed_mentions, {"parse": [], "users": ["1234567890"]})

    @patch("api.agent_tools.discord_bot.send_discord_message")
    def test_falls_back_to_plain_rkian_tag_without_user_id(self, send_discord_message_mock):
        with patch.dict(os.environ, {"DISCORD_DEVELOPER_USER_ID": "", "DISCORD_DEVELOPER_HANDLE": "@_rkian"}, clear=False):
            result = LogBugWithDeveloper(message="Validation response parsing failed").run()

        self.assertIn("Bug report sent", result)
        sent_message = send_discord_message_mock.call_args.args[0]

        self.assertIn("@_rkian", sent_message)
        self.assertIn("Validation response parsing failed", sent_message)
        self.assertNotIn("allowed_mentions", send_discord_message_mock.call_args.kwargs)


class ResponsesAdapterCompatibilityTests(SimpleTestCase):
    class _Message:
        def __init__(self, openai_obj):
            self.openai_obj = openai_obj

    @override_settings(OPENAI_MODEL="gpt-5.4", OPENAI_REASONING_EFFORT="high")
    @patch("api.helpers.openai_helpers.query_responses_api")
    def test_default_request_uses_gpt_5_4_with_high_reasoning(self, query_mock):
        query_mock.return_value = SimpleNamespace(
            id="resp_1",
            status="completed",
            output=[],
            output_text="Done.",
        )

        create_response_message(
            [self._Message({"role": "user", "content": "Transform this."})],
            [],
        )

        request = query_mock.call_args.args[0]
        self.assertEqual(request["model"], "gpt-5.4")
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertNotIn("temperature", request)

    def test_messages_are_mapped_to_responses_input_with_tool_history(self):
        messages = [
            self._Message({"role": "system", "content": "You are a helper."}),
            self._Message({"role": "user", "content": "Clean my table"}),
            self._Message(
                {
                    "role": "assistant",
                    "content": "I will run Python.",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "Python",
                                "arguments": "{\"code\":\"print(1)\"}",
                            },
                        }
                    ],
                }
            ),
            self._Message({"role": "tool", "tool_call_id": "call_abc", "content": "1"}),
        ]

        items = _messages_to_responses_input(messages)

        self.assertEqual(items[0], {"role": "system", "content": "You are a helper."})
        self.assertEqual(items[1], {"role": "user", "content": "Clean my table"})
        self.assertEqual(items[2], {"role": "assistant", "content": "I will run Python."})
        self.assertEqual(
            items[3],
            {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "Python",
                "arguments": "{\"code\":\"print(1)\"}",
            },
        )
        self.assertEqual(
            items[4],
            {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": "1",
            },
        )

    def test_pdf_file_inputs_attach_to_latest_user_message(self):
        items = [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "Read the manuscript."},
            {"role": "assistant", "content": "I can inspect it."},
        ]

        updated = _attach_pdf_files_to_latest_user_message(
            items,
            [{"type": "input_file", "file_id": "file_123", "filename": "paper.pdf"}],
        )

        self.assertEqual(updated[0], items[0])
        self.assertEqual(updated[2], items[2])
        self.assertEqual(updated[1]["role"], "user")
        self.assertEqual(updated[1]["content"][0], {"type": "input_text", "text": "Read the manuscript."})
        self.assertEqual(updated[1]["content"][1]["type"], "input_text")
        self.assertIn("attached", updated[1]["content"][1]["text"])
        self.assertEqual(updated[1]["content"][2], {"type": "input_file", "file_id": "file_123"})

    @patch("api.helpers.openai_helpers._resolve_openai_file_id_from_user_file_id")
    def test_user_message_pdf_attachments_are_hydrated_from_message_metadata(self, resolve_file_id_mock):
        resolve_file_id_mock.return_value = "file_pdf_1"
        messages = [
            self._Message({"role": "system", "content": "You are a helper."}),
            self._Message(
                {
                    "role": "user",
                    "content": "Please use this PDF.",
                    "pdf_attachments": [{"user_file_id": 101, "filename": "paper.pdf"}],
                }
            ),
        ]

        items = _messages_to_responses_input(messages)

        self.assertEqual(items[0], {"role": "system", "content": "You are a helper."})
        self.assertEqual(items[1]["role"], "user")
        self.assertEqual(items[1]["content"][0], {"type": "input_text", "text": "Please use this PDF."})
        self.assertEqual(items[1]["content"][1], {"type": "input_file", "file_id": "file_pdf_1"})
        resolve_file_id_mock.assert_called_once_with(101)

    def test_responses_output_is_mapped_back_to_legacy_message_shape(self):
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_xyz",
                    name="Python",
                    arguments="{\"code\":\"print(2)\"}",
                ),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="Done.")],
                ),
            ],
            output_text="Done.",
        )

        compat_message = _response_to_compat_message(response)

        self.assertEqual(compat_message.role, "assistant")
        self.assertEqual(compat_message.content, "Done.")
        self.assertEqual(len(compat_message.tool_calls), 1)
        self.assertEqual(compat_message.tool_calls[0].id, "call_xyz")
        self.assertEqual(compat_message.tool_calls[0].function.name, "Python")
        self.assertEqual(compat_message.tool_calls[0].function.arguments, "{\"code\":\"print(2)\"}")
        self.assertEqual(
            compat_message.dict(),
            {
                "role": "assistant",
                "content": "Done.",
                "tool_calls": [
                    {
                        "id": "call_xyz",
                        "type": "function",
                        "function": {
                            "name": "Python",
                            "arguments": "{\"code\":\"print(2)\"}",
                        },
                    }
                ],
            },
        )

    def test_functions_are_mapped_to_responses_tool_schema(self):
        class DummyFunction:
            @classmethod
            def openai_schema(cls):
                return {
                    "name": "DummyFunction",
                    "description": "A test helper function.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "foo": {"type": "string"},
                        },
                        "required": ["foo"],
                    },
                }

        tools = _functions_to_responses_tools([DummyFunction])
        self.assertEqual(
            tools,
            [
                {
                    "type": "function",
                    "name": "DummyFunction",
                    "description": "A test helper function.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "foo": {"type": "string"},
                        },
                        "required": ["foo"],
                    },
                }
            ],
        )

    def test_set_basic_metadata_schema_keeps_title_and_description_properties(self):
        schema = SetBasicMetadata.openai_schema()
        parameters = schema["parameters"]
        properties = parameters["properties"]

        self.assertIn("agent_id", properties)
        self.assertIn("title", properties)
        self.assertIn("description", properties)
        self.assertNotIn("suitable_for_publication_on_gbif", properties)

    def test_upload_dwca_schema_exposes_extension_table_mapping_values(self):
        schema = UploadDwCA.openai_schema()
        extension_tables = schema["parameters"]["properties"]["extension_tables"]
        object_schema = next(
            item for item in extension_tables["anyOf"] if item.get("type") == "object"
        )

        self.assertIn("additionalProperties", object_schema)
        self.assertEqual(
            object_schema["additionalProperties"]["$ref"],
            "#/$defs/DarwinCoreExtensionType",
        )
        self.assertIn("DarwinCoreExtensionType", schema["parameters"]["$defs"])


class DwcaExportSanitizationTests(SimpleTestCase):
    def test_surrogateescape_text_is_safe_for_utf8_export(self):
        df = pd.DataFrame({"occurrenceID": ["occ-1"], "fieldNotes": ["collector\udc92s note"]})

        sanitized = _sanitize_dataframe_for_utf8_export(df)
        value = sanitized.loc[0, "fieldNotes"]

        self.assertEqual(value, "collector’s note")
        value.encode("utf-8")

    def test_exact_archive_validation_detects_malformed_rows(self):
        meta = """<?xml version="1.0" encoding="UTF-8"?>
<archive xmlns="http://rs.tdwg.org/dwc/text/">
  <core encoding="UTF-8" fieldsTerminatedBy="\\t" fieldsEnclosedBy="&quot;"
        ignoreHeaderLines="1" rowType="http://rs.tdwg.org/dwc/terms/Occurrence">
    <files><location>occurrence.txt</location></files>
    <id index="0"/>
    <field index="1" term="http://rs.tdwg.org/dwc/terms/scientificName"/>
  </core>
</archive>"""
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("meta.xml", meta)
                archive.writestr("eml.xml", "<eml/>")
                archive.writestr(
                    "occurrence.txt",
                    "occurrenceID\tscientificName\nocc-1\tAcer rubrum\textra\n",
                )

            result = validate_dwca_archive(archive_path)

        self.assertFalse(result["valid"])
        self.assertTrue(any("field count" in error for error in result["errors"]))


class DatasetSummarySerializerTests(TestCase):
    def test_package_explorer_model_uses_current_schema_and_reports_link_coverage(self):
        dataset = Dataset.objects.create(title="Explorer")
        Table.objects.create(
            dataset=dataset,
            title="event",
            df=pd.DataFrame(
                [
                    {"event_pk": "event-1", "eventCategory": "occurrence"},
                    {"event_pk": "event-2", "eventCategory": "occurrence"},
                ]
            ),
        )
        Table.objects.create(
            dataset=dataset,
            title="occurrence",
            df=pd.DataFrame(
                [
                    {"occurrence_pk": "occ-1", "event_fk": "event-1", "occurrenceStatus": "present"},
                    {"occurrence_pk": "occ-2", "event_fk": "missing", "occurrenceStatus": "present"},
                    {"occurrence_pk": "occ-3", "event_fk": "", "occurrenceStatus": "present"},
                ]
            ),
        )

        model = build_dwc_dp_explorer_model(dataset)

        self.assertEqual({node["id"] for node in model["nodes"]}, {"event", "occurrence"})
        occurrence = next(node for node in model["nodes"] if node["id"] == "occurrence")
        self.assertEqual(occurrence["rowCount"], 3)
        self.assertTrue(next(field for field in occurrence["fields"] if field["name"] == "occurrence_pk")["primary"])

        relationship = next(
            edge
            for edge in model["edges"]
            if edge["source"] == "occurrence" and edge["target"] == "event"
        )
        self.assertEqual(relationship["predicate"], "happened during")
        self.assertEqual(relationship["sourceFields"], ["event_fk"])
        self.assertEqual(relationship["targetFields"], ["event_pk"])
        self.assertEqual(relationship["populatedRows"], 2)
        self.assertEqual(relationship["linkedRows"], 1)
        self.assertEqual(relationship["unmatchedRows"], 1)
        self.assertEqual(relationship["blankRows"], 1)

    def test_reports_relational_resource_counts_without_calling_the_sum_records(self):
        dataset = Dataset.objects.create(title="Relational counts")
        Table.objects.create(
            dataset=dataset,
            title="source working copy",
            df=pd.DataFrame({"id": range(7)}),
        )
        for title, rows in {
            "event": 2,
            "occurrence": 5,
            "material": 3,
            "identification": 4,
        }.items():
            Table.objects.create(
                dataset=dataset,
                title=title,
                df=pd.DataFrame({"id": range(rows)}),
            )

        data = DatasetListSerializer(dataset).data

        self.assertEqual(
            data["counts"]["resources"],
            {"event": 2, "identification": 4, "material": 3, "occurrence": 5},
        )
        self.assertEqual(data["counts"]["package_rows"], 14)
        self.assertEqual(data["counts"]["source_rows"], 7)
        self.assertEqual(data["record_count"], 5)

    @patch("api.accounting.current_accounting_status", return_value={"valid": True})
    def test_ready_packages_have_ready_status_and_complete_applicable_progress(self, _accounting):
        Task.objects.create(name=Dataset.MANUSCRIPT_TASK_NAME, text="PDF", order=1)
        Task.objects.create(name="Data transformation", text="Transform", order=2)
        Task.objects.create(name="Phylogenetic tree linking", text="Tree", order=3)
        Task.objects.create(name="Final Review & Publication", text="Publish", order=4)
        Task.objects.create(name="Data maintenance", text="Maintain", order=5)
        dataset = Dataset.objects.create(
            dwc_dp_url="https://example.org/package.tar.gz",
            dwca_url="https://example.org/archive.zip",
            dwc_dp_validation={"valid": True},
        )

        list_data = DatasetListSerializer(dataset).data
        detail_data = DatasetSerializer(dataset).data

        self.assertTrue(dataset.package_ready)
        self.assertEqual(list_data["status"], "ready")
        self.assertEqual(list_data["progress"], {"done": 2, "total": 2})
        self.assertTrue(list_data["package_ready"])
        self.assertTrue(detail_data["package_ready"])
        self.assertEqual(
            detail_data["dwc_dp_standard"]["profile"],
            "http://rs.tdwg.org/dwc-dp/1.0/dwc-dp-profile.json",
        )
        self.assertEqual(detail_data["dwc_dp_standard"]["schema"]["version"], "0.1")

    def test_status_distinguishes_preparing_from_needs_input(self):
        task = Task.objects.create(name="Data exploration", text="Explore", order=1)
        preparing_dataset = Dataset.objects.create(title="Preparing")
        preparing_agent = Agent.objects.create(
            dataset=preparing_dataset,
            task=task,
            busy_thinking=True,
        )
        Message.objects.create(
            agent=preparing_agent,
            openai_obj={"role": Message.Role.ASSISTANT, "content": "Working"},
        )

        needs_input_dataset = Dataset.objects.create(title="Needs input")
        needs_input_agent = Agent.objects.create(dataset=needs_input_dataset, task=task)
        Message.objects.create(
            agent=needs_input_agent,
            openai_obj={"role": Message.Role.ASSISTANT, "content": "Which sheet should I use?"},
        )

        self.assertEqual(DatasetListSerializer(preparing_dataset).data["status"], "preparing")
        self.assertEqual(DatasetListSerializer(needs_input_dataset).data["status"], "needs_input")

    @patch("api.accounting.current_accounting_status", return_value={"valid": False})
    def test_stale_accounting_prevents_package_ready_status(self, _accounting):
        dataset = Dataset.objects.create(
            dwc_dp_url="https://example.org/package.tar.gz",
            dwca_url="https://example.org/archive.zip",
            dwc_dp_validation={"valid": True},
        )

        data = DatasetListSerializer(dataset).data

        self.assertFalse(dataset.package_ready)
        self.assertNotEqual(data["status"], "ready")
        self.assertFalse(data["package_ready"])


class TaskFunctionTests(TestCase):
    def test_accounting_tool_is_available_whenever_package_tables_can_change(self):
        for index, task_name in enumerate(
            [
                "Data transformation",
                "Data validation and refinement",
                "Final Review & Publication",
                "Data maintenance",
            ],
            start=1,
        ):
            with self.subTest(task_name=task_name):
                task = Task.objects.create(name=task_name, text=task_name, order=index)
                self.assertIn(SubmitDwcDpAccounting, task.functions)


class DwcDpAccountingTests(TestCase):
    def _make_transformed_dataset(self):
        dataset = Dataset.objects.create(title="Accounting test")
        source = Table.objects.create(
            dataset=dataset,
            title="source.csv",
            df=pd.DataFrame(
                {
                    "occurrenceID": ["occ-1", "occ-2", "occ-3"],
                    "locality": ["Oslo", "Oslo", "Bergen"],
                }
            ),
        )
        dataset.source_accounting_snapshot = build_source_accounting_snapshot(dataset)
        dataset.save(update_fields=["source_accounting_snapshot"])
        source_id = source.id
        source.delete()
        Table.objects.create(
            dataset=dataset,
            title="event",
            df=pd.DataFrame(
                [
                    {"event_pk": "event-1", "locality": "Oslo"},
                    {"event_pk": "event-2", "locality": "Bergen"},
                ]
            ),
        )
        Table.objects.create(
            dataset=dataset,
            title="occurrence",
            df=pd.DataFrame(
                [
                    {"occurrence_pk": "pk-1", "occurrenceID": "occ-1", "event_fk": "event-1"},
                    {"occurrence_pk": "pk-2", "occurrenceID": "occ-2", "event_fk": "event-1"},
                    {"occurrence_pk": "pk-3", "occurrenceID": "occ-3", "event_fk": "event-2"},
                ]
            ),
        )
        return dataset, source_id

    def _valid_sources(self, source_id):
        return [
            {
                "source_table_id": source_id,
                "rows_accounted": 3,
                "omissions": [],
                "coverage_notes": "All three source IDs occur once in occurrence; locality is deduplicated for event.",
                "dispositions": [
                    {
                        "target_table": "occurrence",
                        "operation": "direct",
                        "source_rows_used": 3,
                        "target_rows_contributed": 3,
                    },
                    {
                        "target_table": "event",
                        "operation": "deduplicated",
                        "source_rows_used": 3,
                        "target_rows_contributed": 2,
                    },
                ],
                "resource_routes": [
                    {
                        "target_table": "occurrence",
                        "field_mappings": {0: "occurrenceID"},
                    },
                    {
                        "target_table": "event",
                        "field_mappings": {1: "locality"},
                    },
                ],
            }
        ]

    def test_transformation_agent_snapshots_source_counts_once(self):
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        dataset = Dataset.objects.create()
        Table.objects.create(
            dataset=dataset,
            title="source.csv",
            df=pd.DataFrame(
                {
                    "id": ["a", "b", "c"],
                    "notes": [None, "", "kept"],
                    "empty": [None, "", None],
                }
            ),
        )

        agent = task.create_agent_with_system_messages(dataset)
        dataset.refresh_from_db()

        snapshot = dataset.source_accounting_snapshot
        self.assertEqual(snapshot["tables"][0]["row_count"], 3)
        self.assertEqual(snapshot["tables"][0]["columns"][0]["populated_values"], 3)
        self.assertEqual(snapshot["tables"][0]["columns"][1]["populated_values"], 1)
        self.assertEqual(len(snapshot["tables"][0]["columns"]), 2)
        system_content = agent.message_set.get(openai_obj__role="system").openai_obj["content"]
        self.assertIn('"source_table_id"', system_content)
        self.assertNotIn('"name": "empty"', system_content)
        self.assertNotIn("&#x27;source_table_id&#x27;", system_content)
        self.assertIn("mechanical completeness guard, not a semantic classifier", system_content)
        self.assertIn("valid accounting result does not by itself prove", system_content)
        self.assertIn("Do not repeat source titles, row counts", system_content)

        Table.objects.create(dataset=dataset, title="later", df=pd.DataFrame({"x": [1]}))
        task.create_agent_with_system_messages(dataset)
        dataset.refresh_from_db()
        self.assertEqual(len(dataset.source_accounting_snapshot["tables"]), 1)

    def test_submit_accounting_accepts_split_and_deduplicated_paths(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)

        result = json.loads(
            SubmitDwcDpAccounting(agent_id=agent.id, sources=self._valid_sources(source_id)).run()
        )

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["summary"]["source_rows"], 3)
        self.assertEqual(result["summary"]["dispositions"], 2)
        dataset.refresh_from_db()
        self.assertNotIn("verification", dataset.dwc_dp_accounting)
        self.assertIn("signature", dataset.dwc_dp_accounting)
        self.assertIn("resources", dataset.dwc_dp_accounting)
        self.assertTrue(current_accounting_status(dataset)["valid"])

    def test_submit_accounting_accepts_grouped_resource_field_mappings(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        sources = self._valid_sources(source_id)
        sources[0]["resource_routes"] = [
            {
                "target_table": "occurrence",
                "field_mappings": {0: "occurrenceID", 1: "occurrenceID"},
            }
        ]

        result = json.loads(
            SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run()
        )

        self.assertTrue(result["valid"], result)
        dataset.refresh_from_db()
        columns = dataset.dwc_dp_accounting["declaration"]["sources"][0]["columns"]
        self.assertEqual([column["source_column_index"] for column in columns], [0, 1])
        self.assertEqual(
            [column["destinations"][0]["source_values"] for column in columns],
            [3, 3],
        )

    def test_submit_accounting_rejects_duplicate_or_unknown_column_routes(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        sources = self._valid_sources(source_id)
        sources[0]["resource_routes"].append(
            {
                "target_table": "occurrence",
                "field_mappings": {0: "occurrenceID"},
            }
        )

        duplicate_result = json.loads(
            SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run()
        )
        sources = self._valid_sources(source_id)
        sources[0]["resource_routes"][0]["field_mappings"] = {99: "occurrenceID"}
        empty_result = json.loads(
            SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run()
        )

        self.assertFalse(duplicate_result["valid"])
        self.assertTrue(
            any("repeats the same destination" in error for error in duplicate_result["errors"])
        )
        self.assertFalse(empty_result["valid"])
        self.assertTrue(
            any("unknown or has no populated" in error for error in empty_result["errors"])
        )

    def test_accounting_tool_schema_exposes_only_compact_server_expanded_fields(self):
        schema_text = json.dumps(SubmitDwcDpAccounting.openai_schema())

        self.assertIn("source_column_indexes", schema_text)
        self.assertIn("field_mappings", schema_text)
        self.assertIn("resource_routes", schema_text)
        self.assertIn("metadata_routes", schema_text)
        self.assertIn("omitted_column_routes", schema_text)
        self.assertNotIn("source_table_title", schema_text)
        self.assertNotIn("source_populated_values", schema_text)
        self.assertNotIn("target_table_rows", schema_text)
        self.assertIn("eml.geographic_scope", schema_text)

    def test_submit_accounting_expands_server_owned_counts_and_rejects_missing_fields(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        sources = self._valid_sources(source_id)
        sources[0]["resource_routes"][0]["field_mappings"][0] = "catalogNumber"

        result = json.loads(SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run())

        self.assertFalse(result["valid"])
        self.assertTrue(any("catalogNumber" in error for error in result["errors"]))

        sources[0]["resource_routes"][0]["field_mappings"][0] = "occurrenceID"
        valid_result = json.loads(
            SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run()
        )
        dataset.refresh_from_db()

        self.assertTrue(valid_result["valid"], valid_result)
        declaration = dataset.dwc_dp_accounting["declaration"]["sources"][0]
        self.assertEqual(declaration["source_table_title"], "source.csv")
        self.assertEqual(declaration["source_rows"], 3)
        self.assertEqual(
            declaration["dispositions"][0]["target_table_rows"],
            3,
        )
        self.assertEqual(declaration["columns"][0]["source_column"], "occurrenceID")
        self.assertEqual(declaration["columns"][0]["source_populated_values"], 3)

    def test_submit_accounting_rejects_declared_coverage_without_actual_paths(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        sources = self._valid_sources(source_id)
        for disposition in sources[0]["dispositions"]:
            disposition["source_rows_used"] = 0
            disposition["target_rows_contributed"] = 0
        for route in sources[0]["resource_routes"]:
            route["source_values"] = {
                column_index: 0
                for column_index in route["field_mappings"]
            }

        result = json.loads(SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run())

        self.assertFalse(result["valid"])
        self.assertTrue(any("cannot account for 3 unique rows" in error for error in result["errors"]))
        self.assertTrue(any("snapshot has 3 populated values" in error for error in result["errors"]))

    def test_metadata_destination_must_point_to_populated_dataset_metadata(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        sources = self._valid_sources(source_id)
        sources[0]["resource_routes"] = sources[0]["resource_routes"][:1]
        sources[0]["metadata_routes"] = [
            {
                "metadata_field": "eml.license",
                "source_column_indexes": [1],
            }
        ]

        missing_result = json.loads(SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run())
        dataset.eml = {"license": "CC BY 4.0"}
        dataset.save(update_fields=["eml"])
        populated_result = json.loads(SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run())

        self.assertFalse(missing_result["valid"])
        self.assertTrue(any("eml.license" in error for error in missing_result["errors"]))
        self.assertTrue(populated_result["valid"], populated_result)

    def test_measurements_can_be_accounted_to_event_assertion_without_occurrence_links(self):
        dataset = Dataset.objects.create(title="Event measurements")
        source = Table.objects.create(
            dataset=dataset,
            title="measurements.csv",
            df=pd.DataFrame({"eventID": ["e1", "e2"], "measurementValue": [12.0, 14.0]}),
        )
        dataset.source_accounting_snapshot = build_source_accounting_snapshot(dataset)
        dataset.save(update_fields=["source_accounting_snapshot"])
        source_id = source.id
        source.delete()
        Table.objects.create(
            dataset=dataset,
            title="event-assertion",
            df=pd.DataFrame(
                {
                    "eventAssertion_pk": ["a1", "a2"],
                    "event_fk": ["e1", "e2"],
                    "measurementValue": [12.0, 14.0],
                }
            ),
        )
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        sources = [{
            "source_table_id": source_id,
            "rows_accounted": 2,
            "omissions": [],
            "coverage_notes": "Both rows join directly to events; no occurrence link is present or needed.",
            "dispositions": [{
                "target_table": "event-assertion",
                "operation": "direct",
                "source_rows_used": 2,
                "target_rows_contributed": 2,
            }],
            "resource_routes": [
                {
                    "target_table": "event-assertion",
                    "field_mappings": {0: "event_fk", 1: "measurementValue"},
                },
            ],
        }]

        result = json.loads(SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run())

        self.assertTrue(result["valid"], result)

    def test_explicit_row_and_column_omissions_are_valid_warnings(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        sources = self._valid_sources(source_id)
        sources[0]["rows_accounted"] = 2
        sources[0]["omissions"] = [{"rows": 1, "reason": "One row has no usable identifier."}]
        for disposition in sources[0]["dispositions"]:
            disposition["source_rows_used"] = 2
            disposition["target_rows_contributed"] = 2
        sources[0]["resource_routes"] = sources[0]["resource_routes"][:1]
        sources[0]["omitted_column_routes"] = [
            {
                "source_column_indexes": [1],
                "reason": "Locality is intentionally withheld.",
            }
        ]

        result = json.loads(SubmitDwcDpAccounting(agent_id=agent.id, sources=sources).run())

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["summary"]["explicitly_omitted_rows"], 1)
        self.assertEqual(result["summary"]["explicitly_omitted_columns"], 1)
        self.assertEqual(result["summary"]["explicitly_omitted_values"], 3)
        self.assertEqual(len(result["warnings"]), 2)

    def test_completion_rechecks_accounting_after_target_changes(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        SubmitDwcDpAccounting(agent_id=agent.id, sources=self._valid_sources(source_id)).run()
        occurrence = dataset.table_set.get(title="occurrence")
        occurrence.df = occurrence.df.iloc[:2].copy()
        occurrence.save()

        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        agent.refresh_from_db()
        self.assertIsNone(agent.completed_at)
        self.assertIn("accounting passes", result)
        self.assertIn("table has 2", result)

    def test_forged_verification_flag_cannot_make_accounting_valid(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        SubmitDwcDpAccounting(agent_id=agent.id, sources=self._valid_sources(source_id)).run()
        dataset.refresh_from_db()
        receipt = dataset.dwc_dp_accounting
        receipt["verification"] = {"valid": True, "errors": []}
        receipt["declaration"]["sources"][0]["dispositions"][0]["target_table_rows"] = 99
        dataset.dwc_dp_accounting = receipt
        dataset.save(update_fields=["dwc_dp_accounting"])

        status = current_accounting_status(dataset)
        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        self.assertFalse(status["valid"])
        self.assertTrue(any("not authentic" in error for error in status["errors"]))
        self.assertIn("table has 3", " ".join(status["errors"]))
        self.assertIn("accounting passes", result)

    @patch("api.agent_tools.export_dwc_dp_package")
    def test_export_refuses_a_stale_accounting_receipt(self, export_mock):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Final Review & Publication", text="Publish", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        SubmitDwcDpAccounting(agent_id=agent.id, sources=self._valid_sources(source_id)).run()
        occurrence = dataset.table_set.get(title="occurrence")
        occurrence.df.loc[0, "occurrenceID"] = "changed-after-accounting"
        occurrence.save()

        result = ExportDwcDp(agent_id=agent.id).run()

        self.assertIn("accounting is not current", result)
        self.assertIn("resource tables changed", result)
        export_mock.assert_not_called()

    def test_completion_succeeds_with_current_valid_accounting(self):
        dataset, source_id = self._make_transformed_dataset()
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        SubmitDwcDpAccounting(agent_id=agent.id, sources=self._valid_sources(source_id)).run()

        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        agent.refresh_from_db()
        self.assertIsNotNone(agent.completed_at)
        self.assertIn("Task marked as complete", result)

    def test_transformation_completion_removes_empty_resources(self):
        dataset, source_id = self._make_transformed_dataset()
        Table.objects.create(dataset=dataset, title="material", df=pd.DataFrame(columns=["material_pk"]))
        task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        SubmitDwcDpAccounting(agent_id=agent.id, sources=self._valid_sources(source_id)).run()

        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        self.assertIn("Task marked as complete", result)
        self.assertFalse(dataset.table_set.filter(title="material").exists())

    def test_refinement_completion_removes_non_package_staging_tables(self):
        dataset, source_id = self._make_transformed_dataset()
        Table.objects.create(dataset=dataset, title="join scratch", df=pd.DataFrame({"x": [1]}))
        task = Task.objects.create(name="Data validation and refinement", text="Validate", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)
        SubmitDwcDpAccounting(agent_id=agent.id, sources=self._valid_sources(source_id)).run()

        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        self.assertIn("Task marked as complete", result)
        self.assertFalse(dataset.table_set.filter(title="join scratch").exists())
        self.assertEqual(
            set(dataset.table_set.values_list("title", flat=True)),
            {"event", "occurrence"},
        )


class DwcDpAssertionRoutingPromptTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "tasks.yaml"
        fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        cls.task_text = {
            item["fields"]["name"]: item["fields"]["text"]
            for item in fixture
        }

    def test_transformation_prompt_delegates_semantic_routing_to_model(self):
        text = self.task_text["Data transformation"]

        self.assertIn("MODEL-LED ASSERTION ROUTING", text)
        self.assertIn("complete dataset context", text)
        self.assertIn("Do not use or invent a hard-coded keyword/term classifier", text)
        self.assertIn("exact source-to-target join", text)
        self.assertIn("explicit, structurally inferred, or unresolved", text)
        self.assertIn("save a concise version of the assertion-routing plan", text)
        self.assertIn("validation agent can independently reconstruct and challenge it", text)

    def test_transformation_prompt_prefers_a_minimum_sufficient_package(self):
        text = self.task_text["Data transformation"]

        self.assertIn("MINIMUM SUFFICIENT PACKAGE", text)
        self.assertIn("smallest valid DwC-DP package", text)
        self.assertIn("Do not create a resource merely because DwC-DP provides one", text)
        self.assertIn("one short evidence-based justification for every proposed resource", text)
        self.assertIn("ordinary bird observations", text)
        self.assertIn("usually need only event and occurrence", text)
        self.assertIn("not a hard resource-count rule", text)

    def test_transformation_prompt_has_a_narrow_deterministic_fast_path(self):
        text = self.task_text["Data transformation"]

        self.assertIn("DETERMINISTIC EXACT-MATCH FAST PATH", text)
        self.assertIn("headers exactly match fields confirmed by GetDwcDpTableInfo", text)
        self.assertIn("not similar-looking names or semantic synonyms", text)
        self.assertIn("still require the model-led contextual review", text)

    def test_refinement_prompt_challenges_unnecessary_complexity(self):
        text = self.task_text["Data validation and refinement"]

        self.assertIn("Minimum sufficient package", text)
        self.assertIn("unnecessary wrapper entities", text)
        self.assertIn("duplicated one-per-row protocols/agents", text)
        self.assertIn("assertion rows that only repeat a native field", text)
        self.assertIn("Collapse synthetic wrapper/child hierarchies", text)
        self.assertIn("remove identification rows that contain only keys", text)

    def test_transformation_prompt_preserves_date_precision_and_avoids_empty_identifications(self):
        text = self.task_text["Data transformation"]

        self.assertIn("EVENT AND IDENTIFICATION MODELLING", text)
        self.assertIn("Do not manufacture one wrapper event plus one child event per occurrence", text)
        self.assertIn("One current scientificName per occurrence normally stays in occurrence", text)
        self.assertIn("Never create identification rows containing only identification_pk", text)
        self.assertIn("DATE PRECISION", text)
        self.assertIn("Never use today's month or day as a parser default", text)
        self.assertIn("normalize_event_date", text)

    def test_transformation_prompt_requires_assertion_coverage_and_join_checks(self):
        text = self.task_text["Data transformation"]

        self.assertIn("Never discard a row because `occurrenceID`", text)
        self.assertIn("temporary source-row key", text)
        self.assertIn("unmatched and multiply matched rows", text)
        self.assertIn("examples of investigation, not fixed mappings", text)
        self.assertIn("Count that source row once for unique source-row coverage", text)

    def test_transformation_prompt_asks_only_about_material_domain_ambiguity(self):
        text = self.task_text["Data transformation"]

        self.assertIn("WHEN TO ASK THE USER ABOUT AMBIGUOUS DATA", text)
        self.assertIn("Perform a bounded initial assessment first", text)
        self.assertIn("materially change the package", text)
        self.assertIn("concrete row counts and representative examples", text)
        self.assertIn("current best interpretation", text)
        self.assertIn("Do not ask the user to select a Darwin Core table", text)
        self.assertIn("Do not ask merely because a value is unusual", text)
        self.assertIn("If the user cannot answer, do not invent a resolution", text)

    def test_transformation_prompt_bounds_investigation_and_context_output(self):
        text = self.task_text["Data transformation"]

        self.assertIn("DECISION-FIRST AND CONTEXT DISCIPLINE", text)
        self.assertIn("Every exploratory Python call must resolve", text)
        self.assertIn("exploration budget of at most three read-only Python calls", text)
        self.assertIn("There is no fourth read-only inspection call", text)
        self.assertIn("show at most five representative records across the entire call", text)
        self.assertIn("never print a complete dataframe, wide row, or long list of records", text)
        self.assertIn("Do not attempt every possible inference first", text)
        self.assertIn("stop exploring representative records", text)
        self.assertIn("GetDwcDpTableInfo(include_fields=false)", text)
        self.assertIn("full field list for only one resource per assistant turn", text)
        self.assertIn("max_fields no higher than 40", text)
        self.assertIn("use that lookup to write the resource now", text)
        self.assertIn("compact planning summary followed by one field-level lookup", text)
        self.assertIn("Do not inspect resources speculatively", text)
        self.assertIn("Treat source accounting as a bounded declaration", text)
        self.assertIn("give every populated source column index at least one route", text)
        self.assertIn("Group resource field mappings by target table", text)
        self.assertIn("metadata indexes by metadata field", text)
        self.assertIn("A source column may have multiple routes", text)
        self.assertIn("Do not repeat source titles, source row counts", text)
        self.assertIn("Choose metadata destinations from the tool's accepted explicit paths", text)
        self.assertIn("correct all reported paths together and resubmit", text)
        self.assertNotIn(
            "inspect every source column, populated and missing identifier patterns",
            text,
        )

    def test_refinement_prompt_rechecks_semantics_and_mechanical_coverage(self):
        text = self.task_text["Data validation and refinement"]

        self.assertIn("Reconstruct and challenge the model-led routing", text)
        self.assertIn("Inspect complete context rather than classifying from keywords", text)
        self.assertIn("Never assume blank `occurrenceID` means the row is irrelevant", text)
        self.assertIn("ask the user one focused question about the underlying data", text)
        self.assertIn("never ask the user to choose a DwC-DP mapping", text)
        self.assertIn("unresolved source rows must never disappear silently", text)
        self.assertIn("independently test the specific record linkage", text)


class SetAgentTaskToCompleteTests(TestCase):
    def test_manuscript_task_can_complete_without_tables(self):
        task = Task.objects.create(
            name=Dataset.MANUSCRIPT_TASK_NAME,
            text="Review manuscript",
            order=1,
        )
        dataset = Dataset.objects.create(source_mode=Dataset.SourceMode.PDF_ONLY)
        agent = Agent.objects.create(dataset=dataset, task=task)

        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        agent.refresh_from_db()
        self.assertIsNotNone(agent.completed_at)
        self.assertIn("Task marked as complete", result)

    def test_table_dependent_tasks_cannot_complete_without_tables(self):
        task_names = [
            "Data transformation",
            "Data validation and refinement",
            "Final Review & Publication",
        ]

        for index, task_name in enumerate(task_names, start=1):
            with self.subTest(task_name=task_name):
                task = Task.objects.create(name=task_name, text=task_name, order=index)
                dataset = Dataset.objects.create(source_mode=Dataset.SourceMode.PDF_ONLY)
                agent = Agent.objects.create(dataset=dataset, task=task)

                result = SetAgentTaskToComplete(agent_id=agent.id).run()

                agent.refresh_from_db()
                self.assertIsNone(agent.completed_at)
                self.assertIn(f"Cannot complete '{task_name}' while this dataset has no tables", result)

    def test_table_dependent_task_can_complete_with_tables(self):
        task = Task.objects.create(name="Final Review & Publication", text="Review data", order=1)
        dataset = Dataset.objects.create(source_mode=Dataset.SourceMode.PDF_ONLY)
        Table.objects.create(dataset=dataset, title="occurrence", df=pd.DataFrame([{"scientificName": "Acer"}]))
        agent = Agent.objects.create(dataset=dataset, task=task)

        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        agent.refresh_from_db()
        self.assertIsNotNone(agent.completed_at)
        self.assertIn("Task marked as complete", result)

    def test_empty_reserved_resource_is_removed_and_cannot_complete(self):
        task = Task.objects.create(name="Final Review & Publication", text="Review data", order=1)
        dataset = Dataset.objects.create(source_mode=Dataset.SourceMode.PDF_ONLY)
        Table.objects.create(dataset=dataset, title="occurrence", df=pd.DataFrame(columns=["occurrence_pk"]))
        agent = Agent.objects.create(dataset=dataset, task=task)

        result = SetAgentTaskToComplete(agent_id=agent.id).run()

        agent.refresh_from_db()
        self.assertIsNone(agent.completed_at)
        self.assertFalse(dataset.table_set.exists())
        self.assertIn("while this dataset has no tables", result)


class DwcDpDescriptorPreviewTests(TestCase):
    def test_preview_returns_complete_compact_json(self):
        dataset = Dataset.objects.create(
            title="Bird observations",
            description="D" * 4000,
        )
        Table.objects.create(
            dataset=dataset,
            title="event",
            df=pd.DataFrame([
                {
                    "event_pk": "event-1",
                    "eventID": "source-event-1",
                    "eventCategory": "occurrence",
                    "eventDate": "2025-04-26",
                    "country": "Norway",
                }
            ]),
        )
        task = Task.objects.create(name="Final Review & Publication", text="Review", order=1)
        agent = Agent.objects.create(dataset=dataset, task=task)

        result = json.loads(PreviewDwcDpDescriptor(agent_id=agent.id).run())

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["resource_count"], 1)
        self.assertEqual(result["resources"][0]["name"], "event")
        self.assertEqual(result["resources"][0]["row_count"], 1)
        self.assertIn("event_pk", result["resources"][0]["fields"])
        self.assertTrue(result["description_truncated"])
        self.assertEqual(len(result["description"]), 2000)


class AgentPdfAttachmentTests(TestCase):
    class FakeResponseMessage:
        tool_calls = []

        def dict(self):
            return {"role": "assistant", "content": "Done."}

    @patch("api.models.create_response_message")
    def test_next_message_does_not_attach_follow_up_pdf_twice(self, create_response_message_mock):
        captured_pdf_files = []

        def fake_create_response_message(*args, **kwargs):
            captured_pdf_files.extend(list(kwargs.get("pdf_user_files") or []))
            return self.FakeResponseMessage()

        create_response_message_mock.side_effect = fake_create_response_message
        task = Task.objects.create(name=Dataset.MANUSCRIPT_TASK_NAME, text="Review manuscript", order=1)
        dataset = Dataset.objects.create(source_mode=Dataset.SourceMode.PDF_ONLY)
        agent = Agent.objects.create(dataset=dataset, task=task)
        Message.objects.create(agent=agent, openai_obj={"role": "assistant", "content": "Please upload the PDF."})
        user_file = UserFile.objects.create(
            dataset=dataset,
            file="user_files/paper.pdf",
        )
        Message.objects.bulk_create([
            Message(
                agent=agent,
                openai_obj={
                    "role": "user",
                    "content": "Here is the PDF.",
                    "pdf_attachments": [{"user_file_id": user_file.id, "filename": "paper.pdf"}],
                },
            )
        ])

        agent.next_message()

        self.assertEqual(captured_pdf_files, [])


class AgentWorkflowActionTests(TestCase):
    def _agent_with_user_message(self):
        task = Task.objects.create(
            name="Workflow action test",
            text="Complete the workflow action test.",
            order=1,
        )
        Task.objects.create(
            name="Data maintenance",
            text="Remain available for follow-up.",
            order=2,
        )
        dataset = Dataset.objects.create(title="Workflow test", description="Test")
        agent = Agent.create_with_system_message(dataset=dataset, task=task, tables=[])
        Message.objects.bulk_create([
            Message(
                agent=agent,
                openai_obj={"role": "user", "content": "Please process this."},
            )
        ])
        return agent

    @patch("api.models.create_response_message")
    def test_no_action_is_retried_and_completed_without_user_continue(
        self,
        create_response_message_mock,
    ):
        agent = self._agent_with_user_message()
        create_response_message_mock.side_effect = [
            CompatAssistantMessage(content="The work is complete."),
            CompatAssistantMessage(
                tool_calls=[
                    CompatToolCall(
                        id="call-complete",
                        function=CompatFunctionCall(
                            name="SetAgentTaskToComplete",
                            arguments=json.dumps({"agent_id": agent.id}),
                        ),
                    )
                ],
            ),
        ]

        agent.next_message()

        agent.refresh_from_db()
        self.assertEqual(create_response_message_mock.call_count, 2)
        self.assertIsNotNone(agent.completed_at)
        retry_items = create_response_message_mock.call_args.kwargs["additional_input_items"]
        self.assertIn("took no action", retry_items[-1]["content"])
        self.assertFalse(
            Message.objects.filter(
                agent=agent,
                openai_obj__role="user",
                openai_obj__content__icontains="continue",
            ).exists()
        )

    @patch("api.models.create_response_message")
    def test_request_user_input_creates_terminal_visible_questions(
        self,
        create_response_message_mock,
    ):
        agent = self._agent_with_user_message()
        create_response_message_mock.return_value = CompatAssistantMessage(
            tool_calls=[
                CompatToolCall(
                    id="call-question",
                    function=CompatFunctionCall(
                        name="RequestUserInput",
                        arguments=json.dumps({
                            "agent_id": agent.id,
                            "questions": [
                                {
                                    "question": "Are depths recorded in metres?",
                                    "context": "The source file does not specify a unit.",
                                },
                                {
                                    "question": "Should zero counts mean absence?",
                                },
                            ],
                        }),
                    ),
                )
            ],
        )

        messages = agent.next_message()

        agent.refresh_from_db()
        self.assertIsNone(agent.completed_at)
        self.assertEqual(create_response_message_mock.call_count, 1)
        self.assertEqual(messages[-1].role, Message.Role.ASSISTANT)
        self.assertIn("1. Are depths recorded in metres?", messages[-1].openai_obj["content"])
        self.assertIn("2. Should zero counts mean absence?", messages[-1].openai_obj["content"])
        self.assertIsNone(agent.next_message())

    def test_request_user_input_schema_allows_multiple_questions(self):
        schema = RequestUserInput.openai_schema()

        self.assertEqual(
            schema["parameters"]["properties"]["questions"]["maxItems"],
            10,
        )
