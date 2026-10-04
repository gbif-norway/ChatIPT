"""Integration fixtures exercise source import, review, row links, reports and serialization."""
import csv
import io
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.dwca_conversion import SUPPORTED_EXTENSIONS, build_plan, convert, validate_decisions
from api.dwca_eol import DCT, XMP, EOL_MEDIA
from api.dwca_germplasm import G, GEO
from api.dwca_legacy import BMDE, NXF
from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive


def data(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream); writer.writerow(headers); writer.writerows(rows)
    return stream.getvalue().encode()


EVENT = ('event.csv', b'eventID,eventCategory\ne1,survey\n')
OCCURRENCE = ('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus,materialEntityID\no1,e1,present,m1\n')


def source(name, terms, rows, core=EVENT, other=()):
    join = DWC + ('eventID' if core[0] == 'event.csv' else 'occurrenceID')
    return read_inputs([core, (name, data([join, *terms], rows)), *other])


def review(archive):
    plan = build_plan(archive)
    return plan, {item['id']: item['options'][0]['value'] for item in plan['issues']}


class ExtensionIntegrationTests(SimpleTestCase):
    def test_twenty_families_have_runtime_paths(self):
        self.assertEqual(len(SUPPORTED_EXTENSIONS), 20)

    def test_incomplete_scores_and_identifier_only_traits_preserve_only_affected_rows(self):
        archive = source('measurement_score.csv', [DWC + 'measurementType', DWC + 'measurementValue'],
                         [['e1', 'Height', '7'], ['e1', 'Height', '']], other=(
                             ('measurement_trait.csv', data([DWC + 'eventID', G + 'measurementTraitID', G + 'measurementTraitName'],
                                                            [['e1', 'trait:1', 'Height'], ['e1', 'trait:2', '']])),))
        plan, choices = review(archive); frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['event-assertion']), 1)
        self.assertEqual(len(frames['protocol']), 1)
        self.assertEqual(len(report['preserved_extension_rows']), 2)
        self.assertIn('supplied measurement', next(r for r in report['preserved_extension_rows'] if r['source_table'] == 'measurement_score.csv')['reason'])
        self.assertEqual(next(c for c in report['columns'] if c['term'] == DWC + 'measurementType')['retained_only_rows'], 1)

    def test_eol_media_subject_and_missing_details_are_explicit(self):
        archive = source('eol_media.csv', [DCT + 'identifier', DCT + 'type', XMP + 'rights/UsageTerms', DWC + 'taxonID'],
                         [['o1', 'https://example.org/img', 'StillImage', 'CC BY', 'taxon:1']], OCCURRENCE)
        plan, choices = review(archive)
        self.assertEqual(choices['row:1:0'], 'preserve')
        frames, report = convert(archive, plan, choices)
        self.assertNotIn('media', frames)
        self.assertEqual(len(report['preserved_extension_rows']), 1)
        choices['row:1:0'] = 'convert'; choices['table:1'] = 'media-unlinked'
        frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['media'].iloc[0]['mediaID'], 'https://example.org/img')
        self.assertNotIn('occurrence-media', frames)
        self.assertEqual(report['media_subjects'][0]['subject_basis'], 'explicitly_unlinked')

    def test_eol_text_cannot_be_forced_into_media(self):
        archive = source('eol_media.csv', [DCT + 'identifier', DCT + 'type'], [['e1', 'text-1', 'Text']])
        plan, choices = review(archive)
        self.assertEqual(next(i for i in [*plan['issues'], *plan.get('automatic_choices', [])] if i['id'] == 'row:1:0')['options'][0]['value'], 'preserve')
        choices['row:1:0'] = 'convert'
        with self.assertRaisesMessage(ImportFailure, 'Unsupported mapping decision'):
            convert(archive, plan, choices)

    def test_eol_full_citation_structured_fields_require_approval_and_keep_multiplicity(self):
        archive = source('eol_references.csv', [DCT + 'identifier', 'http://eol.org/schema/reference/full_reference', DCT + 'title'],
                         [['e1', 'ref:1', 'A full citation', 'A title'], ['e1', 'ref:1', 'A full citation', 'A title']])
        plan, choices = review(archive)
        title = next(c for c in plan['columns'] if c['term'] == DCT + 'title')
        self.assertTrue(title['review'])
        choices[title['id']] = 'preserve'
        identifier = next(c for c in plan['columns'] if c['term'] == DCT + 'identifier')
        choices[identifier['id']] = 'preserve'
        frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['bibliographic-resource']), 2)
        self.assertNotIn('title', frames['bibliographic-resource'])
        self.assertNotIn('relationshipType', frames['event-reference'])

    def test_missing_eol_identifier_preserves_row(self):
        archive = source('eol_references.csv', [DCT + 'title'], [['e1', 'A title']])
        plan, choices = review(archive); frames, report = convert(archive, plan, choices)
        self.assertNotIn('bibliographic-resource', frames)
        self.assertEqual(report['columns'][-1]['disposition'], 'retained-unmapped')

    def test_accession_material_requires_review_and_creates_only_statements(self):
        archive = source('germplasm_accession.csv', [G + 'germplasmID', G + 'biologicalStatus', G + 'purdyPedigree', GEO + 'lat'],
                         [['o1', 'acc:1', 'Landrace', 'A/B', '60']], OCCURRENCE)
        plan, choices = review(archive)
        with self.assertRaisesMessage(ImportFailure, 'explicitly approved material'):
            convert(archive, plan, choices)
        choices['material:0'] = 'per_row'
        frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['material']), 1)
        self.assertEqual(len(frames['material-assertion']), 2)
        self.assertEqual(frames['material-identifier'].iloc[0]['identifier'], 'acc:1')
        self.assertNotIn('decimalLatitude', frames['event'])
        self.assertNotIn('agent', frames)
        self.assertEqual(next(c for c in report['columns'] if c['term'] == GEO + 'lat')['disposition'], 'retained-unmapped')

    def test_score_material_exact_identifier_and_optional_trait_protocol(self):
        archive = source('measurement_score.csv', [G + 'germplasmID', G + 'measurementTraitID', G + 'measurementTraitName', DWC + 'measurementValue'],
                         [['o1', 'acc:1', 'trait:1', 'Height', '7']], OCCURRENCE, other=(
                             ('measurement_trait.csv', data([DWC + 'occurrenceID', G + 'measurementTraitID', G + 'measurementTraitName'], [['o1', 'trait:1', 'Height']])),
                             ('germplasm_accession.csv', data([DWC + 'occurrenceID', G + 'germplasmID'], [['o1', 'acc:1']]))))
        plan, choices = review(archive); choices['material:0'] = 'per_row'
        choices[next(issue['id'] for issue in [*plan['issues'], *plan.get('automatic_choices', [])] if issue['id'].startswith('trait-link:'))] = 'exact'
        frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        score = frames['material-assertion'].iloc[0]
        self.assertEqual(score['materialEntity_fk'], frames['material'].iloc[0]['materialEntity_pk'])
        self.assertEqual(score['assertionProtocol_fk'], frames['protocol'].iloc[0]['protocol_pk'])
        self.assertEqual(score['verbatimAssertionType'], 'Height')
        self.assertEqual(next(c for c in report['columns'] if c['term'] == DWC + 'measurementValue')['mapped_rows'], 1)
        self.assertEqual(next(c for c in report['columns'] if c['term'] == G + 'measurementTraitID' and c['source_table'] == 'measurement_score.csv')['disposition'], 'derived')
        next(table for table in archive.tables if table.name == 'measurement_score.csv').rows[0][1] = 'wrong-accession'
        plan, choices = review(archive); choices['material:0'] = 'per_row'
        with self.assertRaisesMessage(ImportFailure, 'must exactly match an identifier'):
            convert(archive, plan, choices)

    def test_score_protocol_missing_or_ambiguous_id_never_links(self):
        for protocols in ([], [['o1', 'trait:1', 'Height'], ['o1', 'trait:1', 'Height']]):
            other = [('measurement_trait.csv', data([DWC + 'occurrenceID', G + 'measurementTraitID', G + 'measurementTraitName'], protocols))] if protocols else []
            archive = source('measurement_score.csv', [G + 'measurementTraitID', DWC + 'measurementType', DWC + 'measurementValue'], [['o1', 'trait:1', 'Height', '7']], OCCURRENCE, other)
            plan, choices = review(archive); choices['table:1'] = 'germplasm-score-occurrence'; choices['trait-link:1'] = 'exact'
            with self.assertRaisesMessage(ImportFailure, 'exactly match one converted'):
                convert(archive, plan, choices)

    def test_score_subject_retargets_and_type_contradictions_fail(self):
        archive = source('measurement_score.csv', [DWC + 'measurementType', DWC + 'measurementValue', G + 'measurementTraitName'], [['e1', 'Height', '7', 'Height']])
        plan, choices = review(archive); frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['event-assertion'].iloc[0]['assertionValue'], '7')
        self.assertEqual(next(c for c in report['columns'] if c['term'] == DWC + 'measurementValue')['target'], 'event-assertion.assertionValue')
        archive.tables[1].rows[0][-1] = 'Mass'; plan, choices = review(archive)
        with self.assertRaisesMessage(ImportFailure, 'disagree'):
            convert(archive, plan, choices)

    def test_trial_patches_existing_event_and_serialized_package_validates(self):
        archive = source('measurement_trial.csv', [G + 'measurementTrailID', G + 'measurementTrailYear', GEO + 'lat', GEO + 'lon', G + 'measurementTrailReport'],
                         [['e1', 'trial:1', '2025', '59.6', '10.7', 'Trial report']])
        plan, choices = review(archive); frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['event']), 1)
        self.assertEqual(frames['event'].iloc[0]['decimalLatitude'], '59.6')
        self.assertNotIn('geodeticDatum', frames['event'])
        self.assertEqual(frames['event-identifier'].iloc[0]['identifier'], 'trial:1')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'converted.tar.gz'
            create_dwc_dp_archive(output, frames, title='Trial', description='Trial conversion', include_eml=False,
                additional_files=[('source-originals.zip', source_zip(archive.files)), ('conversion-report.json', json.dumps(report).encode())], declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])

    def test_trial_conflicting_core_coordinates_or_date_never_overwrite(self):
        for core in (('event.csv', b'eventID,eventCategory,decimalLatitude\ne1,survey,60\n'),
                     ('event.csv', b'eventID,eventCategory,eventDate\ne1,survey,2024-03-01\n')):
            archive = source('measurement_trial.csv', [GEO + 'lat', GEO + 'lon', G + 'measurementTrailYear'], [['e1', '59.6', '10.7', '2025']], core)
            plan, choices = review(archive)
            with self.assertRaisesMessage(ImportFailure, 'conflicts'):
                convert(archive, plan, choices)

    def test_bmde_groups_unpivot_without_merging_and_event_values_patch(self):
        archive = source('bmde.csv', [BMDE + 'MeasurementType1', BMDE + 'MeasurementValue1', BMDE + 'MeasurementUnit1',
                                     BMDE + 'MeasurementType2', BMDE + 'MeasurementValue2', BMDE + 'MeasurementUnit2',
                                     BMDE + 'UTMZone', BMDE + 'UTMEasting', BMDE + 'UTMNorthing', BMDE + 'TimeObservationsStarted'],
                         [['o1', 'Mass', '7', 'g', 'Mass', '7', 'g', '17T', '630084', '4833438', '6.5']], OCCURRENCE)
        plan, choices = review(archive); frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['occurrence-assertion']), 2)
        self.assertEqual(frames['event'].iloc[0]['eventTime'], '06:30')
        self.assertEqual(frames['event'].iloc[0]['verbatimCoordinates'], '17T 630084 4833438')
        self.assertNotIn('decimalLatitude', frames['event'])
        self.assertEqual(next(c for c in report['columns'] if c['term'] == BMDE + 'MeasurementValue2')['mapped_rows'], 1)

    def test_bmde_event_core_cannot_invent_occurrence_and_subminute_times_stay_verbatim(self):
        archive = source('bmde.csv', [BMDE + 'MeasurementType1', BMDE + 'MeasurementValue1', BMDE + 'SurveyAreaIdentifier', BMDE + 'TimeObservationsStarted'], [['e1', 'Mass', '7', 'site:1', '6.123']])
        plan, choices = review(archive); frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertNotIn('occurrence', frames)
        self.assertNotIn('eventTime', frames['event'])
        self.assertEqual(frames['event'].iloc[0]['siteNumber'], 'site:1')
        self.assertEqual(next(c for c in report['columns'] if c['term'] == BMDE + 'TimeObservationsStarted')['disposition'], 'retained-unmapped')

    def test_bmde_delete_and_noobs_rows_cannot_assert_absence(self):
        for term, value in ((BMDE + 'LastModifiedAction', 'DELETE'), (BMDE + 'NoObservations', 'NoObs')):
            archive = source('bmde.csv', [term, BMDE + 'MeasurementType1', BMDE + 'MeasurementValue1'], [['o1', value, 'Mass', '7']], OCCURRENCE)
            plan, choices = review(archive); frames, report = convert(archive, plan, choices)
            self.assertNotIn('occurrence-assertion', frames)
            self.assertEqual(frames['occurrence'].iloc[0]['occurrenceStatus'], 'present')
            self.assertEqual(len(report['preserved_extension_rows']), 1)

    def test_nbn_date_codes_preserve_precision_and_sensitive_requires_choice(self):
        archive = source('nbn.csv', [NXF + 'eventDateTypeCode', NXF + 'eventDateStart', NXF + 'eventDateEnd', NXF + 'sensitiveOccurrence', NXF + 'gridReference', NXF + 'gridReferenceType', NXF + 'gridReferencePrecision'],
                         [['e1', 'Y', '2025-01-01', '2025-12-31', 'true', 'SD4261', 'BNG', '10000']])
        plan, choices = review(archive); frames, report = convert(archive, plan, choices)
        self.assertNotIn('eventDate', frames['event'])
        choices['row:1:0'] = 'convert'; frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['event'].iloc[0]['eventDate'], '2025-01-01/2025-12-31')
        self.assertNotIn('coordinateUncertaintyInMeters', frames['event'])
        self.assertNotIn('informationWithheld', frames['event'])
        self.assertNotIn('decimalLatitude', frames['event'])

    def test_nbn_unsupported_or_invalid_dates_withheld_without_hiding_other_values(self):
        for code, start, end in (('U', '2025-01-01', '2025-12-31'), ('Y', '2025-03-01', '2025-12-31')):
            archive = source('nbn.csv', [NXF + 'eventDateTypeCode', NXF + 'eventDateStart', NXF + 'eventDateEnd', NXF + 'sensitiveOccurrence', NXF + 'gridReference'], [['e1', code, start, end, 'false', 'SD4261']])
            plan, choices = review(archive); choices['row:1:0'] = 'convert'; frames, report = convert(archive, plan, choices)
            self.assertTrue(report['validation']['valid'])
            self.assertNotIn('eventDate', frames['event'])
            self.assertEqual(frames['event'].iloc[0]['verbatimCoordinates'], 'SD4261')
            self.assertEqual(len(report['withheld_values']), 3)
            self.assertEqual(next(c for c in report['columns'] if c['term'] == NXF + 'eventDateStart')['mapped_rows'], 0)

    def test_nbn_cannot_overwrite_a_conflicting_event_date(self):
        archive = source('nbn.csv', [NXF + 'eventDateTypeCode', NXF + 'eventDateStart', NXF + 'eventDateEnd', NXF + 'sensitiveOccurrence'], [['e1', 'D', '2025-02-01', '2025-02-01', 'false']], ('event.csv', b'eventID,eventCategory,eventDate\ne1,survey,2025-01-01\n'))
        plan, choices = review(archive)
        with self.assertRaisesMessage(ImportFailure, 'conflicts'):
            convert(archive, plan, choices)

    def test_preserving_extension_disposes_all_fields_and_suppresses_its_reviews(self):
        archive = source('measurement_score.csv', [DWC + 'measurementType', DWC + 'measurementValue'], [['e1', 'Height', '7']])
        plan, choices = review(archive)
        choices = {key: value for key, value in choices.items() if not key.startswith('column:1:')}
        choices['table:1'] = 'preserve'
        self.assertEqual(validate_decisions(plan, choices), [])
        frames, report = convert(archive, plan, choices)
        self.assertNotIn('event-assertion', frames)
        self.assertTrue(all(c['disposition'] == 'retained-unmapped' for c in report['columns'] if c['source_table'] == 'measurement_score.csv'))

    def test_combined_occurrence_extensions_export_with_all_links_and_originals(self):
        archive = source('measurement_score.csv', [G + 'germplasmID', G + 'measurementTraitID', DWC + 'measurementType', DWC + 'measurementValue'],
                         [['o1', 'acc:1', 'trait:1', 'Height', '7']], OCCURRENCE, other=(
                             ('germplasm_accession.csv', data([DWC + 'occurrenceID', G + 'germplasmID', G + 'biologicalStatus'], [['o1', 'acc:1', 'Landrace']])),
                             ('measurement_trait.csv', data([DWC + 'occurrenceID', G + 'measurementTraitID', G + 'measurementTraitName'], [['o1', 'trait:1', 'Height']])),
                             ('bmde.csv', data([DWC + 'occurrenceID', BMDE + 'MeasurementType1', BMDE + 'MeasurementValue1'], [['o1', 'Mass', '3']])),
                             ('eol_media.csv', data([DWC + 'occurrenceID', DCT + 'identifier', DCT + 'type', XMP + 'rights/UsageTerms'], [['o1', 'https://example.org/photo', 'StillImage', 'CC BY']])),
                             ('eol_references.csv', data([DWC + 'occurrenceID', DCT + 'identifier', DCT + 'title'], [['o1', 'ref:1', 'A reference']])),
                         ))
        plan, choices = review(archive); choices['material:0'] = 'per_row'
        choices[next(issue['id'] for issue in [*plan['issues'], *plan.get('automatic_choices', [])] if issue['id'].startswith('trait-link:'))] = 'exact'
        frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertTrue({'material-assertion', 'material-identifier', 'protocol', 'occurrence-assertion', 'occurrence-media', 'occurrence-reference'} <= frames.keys())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'converted.tar.gz'
            create_dwc_dp_archive(output, frames, title='Combined conversion', description='Reviewed extensions', include_eml=False,
                additional_files=[('source-originals.zip', source_zip(archive.files)), ('conversion-report.json', json.dumps(report).encode())], declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])
