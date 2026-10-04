import io
import json
import tempfile
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.dwca_conversion import build_plan, convert, option_status, validate_decisions
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive
from api.models import CustomUser, Dataset, DwcConversion, DwcConversionJob
from api.conversion_jobs import process_next_conversion


def occurrence(content=None):
    return read_inputs([('occurrence.csv', content or b'occurrenceID,eventID,scientificName,eventDate,occurrenceStatus\nsource-1,e1,Apus apus,2025-01-01,present\n')])


def decisions_for(plan):
    return {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}


def manifest(fields='', extension=''):
    return ('<archive xmlns="http://rs.tdwg.org/dwc/text/">'
            '<core rowType="' + DWC + 'Occurrence" fieldsTerminatedBy="," fieldsEnclosedBy="&quot;" ignoreHeaderLines="1">'
            '<files><location>occ.csv</location></files><id index="0"/>'
            '<field index="1" term="' + DWC + 'occurrenceID"/>'
            '<field term="' + DWC + 'occurrenceStatus" default="present"/>' + fields + '</core>' + extension + '</archive>').encode()


class ArchiveTests(SimpleTestCase):
    def test_manifest_defaults_and_internal_ids_are_distinct(self):
        archive = read_inputs([('meta.xml', manifest()), ('occ.csv', b'id,occurrenceID\njoin-1,persistent-1\n')])
        self.assertEqual(archive.tables[0].ids, ['join-1'])
        self.assertEqual(archive.tables[0].rows, [['persistent-1', 'present']])
        plan = build_plan(archive); frames, report = convert(archive, plan, decisions_for(plan))
        self.assertEqual(frames['occurrence'].iloc[0]['occurrenceID'], 'persistent-1')
        self.assertNotEqual(frames['occurrence'].iloc[0]['occurrence_pk'], 'persistent-1')
        self.assertTrue(report['validation']['valid'])

    def test_extension_joins_internal_key_not_persistent_id(self):
        extension = '<extension rowType="' + DWC + 'Identification" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>identification.csv</location></files><coreid index="0"/><field index="1" term="' + DWC + 'scientificName"/></extension>'
        archive = read_inputs([('meta.xml', manifest(extension=extension)), ('occ.csv', b'id,occurrenceID\njoin-1,persistent-1\n'),
                               ('identification.csv', b'link,name\njoin-1,Apus apus\njoin-1,Apus pallidus\n')])
        plan = build_plan(archive); frames, report = convert(archive, plan, decisions_for(plan))
        self.assertEqual(len(frames['identification']), 2)
        self.assertEqual(set(frames['identification']['occurrence_fk']), {frames['occurrence'].iloc[0]['occurrence_pk']})
        self.assertNotIn('isAcceptedIdentification', frames['identification'].columns)

    def test_zip_round_trip_preserves_every_original_byte(self):
        inputs = {'nested/meta.xml': manifest(), 'nested/occ.csv': b'id,occurrenceID\njoin-1,persistent-1\n', 'notes.bin': b'\x00\xff'}
        archive = read_inputs([('test.zip', source_zip(inputs))])
        self.assertEqual(archive.files, inputs)

    def test_loose_upload_order_does_not_change_plan_keys_or_rows(self):
        inputs = [('occurrence.csv', b'occurrenceID,occurrenceStatus\na,present\n'),
                  ('identificationhistory.csv', b'occurrenceID,scientificName\na,Apus apus\n')]
        first = read_inputs(inputs); second = read_inputs(reversed(inputs))
        first_plan = build_plan(first); second_plan = build_plan(second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first_plan['id'], second_plan['id'])
        first_frames, first_report = convert(first, first_plan, decisions_for(first_plan))
        second_frames, second_report = convert(second, second_plan, decisions_for(second_plan))
        self.assertEqual(first_report, second_report)
        self.assertEqual({name: df.to_csv(index=False) for name, df in first_frames.items()},
                         {name: df.to_csv(index=False) for name, df in second_frames.items()})

    def test_zip_entry_order_keeps_internal_keys_and_checksums(self):
        inputs = [('occurrence.csv', b'occurrenceID,occurrenceStatus\na,present\n'),
                  ('identificationhistory.csv', b'occurrenceID,scientificName\na,Apus apus\n')]
        def zipped(items):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                for name, content in items: archive.writestr(name, content)
            return stream.getvalue()
        first = read_inputs([('archive.zip', zipped(inputs))]); second = read_inputs([('archive.zip', zipped(reversed(inputs)))])
        self.assertEqual(first.fingerprint, second.fingerprint)
        first_plan = build_plan(first); second_plan = build_plan(second)
        # Exact uploaded ZIP bytes differ and are recorded, but parsed content and converted keys agree.
        self.assertEqual(first_plan['files'], second_plan['files'])
        first_frames, _ = convert(first, first_plan, decisions_for(first_plan))
        second_frames, _ = convert(second, second_plan, decisions_for(second_plan))
        self.assertEqual({name: df.to_csv(index=False) for name, df in first_frames.items()},
                         {name: df.to_csv(index=False) for name, df in second_frames.items()})

    def test_zip_rejects_unsafe_duplicate_and_symlink_paths(self):
        for path in ('../occurrence.csv', '/occurrence.csv', 'C:/occurrence.csv'):
            with self.subTest(path=path), self.assertRaises(ImportFailure):
                read_inputs([('test.zip', source_zip({path: b'hello'}))])
        with self.assertRaises(ImportFailure):
            read_inputs([('test.zip', source_zip({'occurrence.csv': b'one', 'Occurrence.csv': b'two'}))])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            info = zipfile.ZipInfo('occurrence.csv'); info.external_attr = 0o120777 << 16
            archive.writestr(info, '../outside')
        with self.assertRaises(ImportFailure): read_inputs([('test.zip', buffer.getvalue())])

    def test_no_xml_entities(self):
        with self.assertRaises(ImportFailure):
            read_inputs([('meta.xml', b'<!DOCTYPE archive [<!ENTITY x SYSTEM "file:///etc/passwd">]><archive/>')])

    def test_duplicate_and_dangling_ids_block(self):
        with self.assertRaises(ImportFailure): occurrence(b'occurrenceID\na\na\n')
        with self.assertRaises(ImportFailure):
            read_inputs([('occurrence.csv', b'occurrenceID\na\n'), ('identification.csv', b'occurrenceID,scientificName\nb,Apus apus\n')])

    def test_multiline_single_row_and_zero_values_are_retained(self):
        archive = occurrence(b'occurrenceID,occurrenceRemarks,decimalLatitude,occurrenceStatus\na,"line one\nline two",0,present\n')
        plan = build_plan(archive); frames, _ = convert(archive, plan, decisions_for(plan))
        self.assertEqual(frames['event'].iloc[0]['decimalLatitude'], '0')
        self.assertEqual(frames['occurrence'].iloc[0]['occurrenceRemarks'], 'line one\nline two')

    def test_missing_status_is_reviewed_not_assumed(self):
        plan = build_plan(occurrence(b'occurrenceID,scientificName\na,Apus apus\n'))
        self.assertIn('status:0', [issue['id'] for issue in plan['issues']])
        with self.assertRaises(ImportFailure): validate_decisions(plan, {})

    def test_invalid_coordinate_tokens_warn_and_copy_only_compatible_cells(self):
        archive = occurrence(b'occurrenceID,decimalLatitude,decimalLongitude,occurrenceStatus\nvalid,0,10.5,present\ninvalid,NA,NA,present\n')
        plan = build_plan(archive)
        coordinates = [c for c in plan['columns'] if c['term'] in {DWC + 'decimalLatitude', DWC + 'decimalLongitude'}]
        self.assertTrue(all(c['review'] is False for c in coordinates))
        self.assertTrue(all(next(iter(c['incompatible_values'].values())) == 1 for c in coordinates))
        decisions = decisions_for(plan)
        self.assertTrue(all(c['id'] not in decisions for c in coordinates))
        self.assertTrue(all(any(w['id'] == c['id'] for w in plan['warnings']) for c in coordinates))
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['event']['decimalLatitude'].tolist(), ['0', ''])
        self.assertEqual(frames['event']['decimalLongitude'].tolist(), ['10.5', ''])
        self.assertEqual([cell['value'] for cell in report['withheld_values']], ['NA', 'NA'])
        self.assertEqual(archive.files['occurrence.csv'], b'occurrenceID,decimalLatitude,decimalLongitude,occurrenceStatus\nvalid,0,10.5,present\ninvalid,NA,NA,present\n')
        for term in (DWC + 'decimalLatitude', DWC + 'decimalLongitude'):
            column = next(c for c in report['columns'] if c['term'] == term)
            self.assertEqual((column['mapped_rows'], column['retained_only_rows']), (1, 1))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'coordinates.tar.gz'
            create_dwc_dp_archive(output, frames, 'Reviewed coordinates', '', include_eml=False,
                additional_files=[('source-originals.zip', source_zip(archive.files)), ('conversion-report.json', json.dumps(report).encode())], declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])

    def test_coordinate_bounds_nonfinite_values_and_integer_lexical_forms_are_retained(self):
        archive = occurrence(b'occurrenceID,decimalLatitude,decimalLongitude,year,occurrenceStatus\ninvalid,91,Infinity,2025.0,present\nvalid,-90,180,2025,present\n')
        plan = build_plan(archive); frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['event']['decimalLatitude'].tolist(), ['', '-90'])
        self.assertEqual(frames['event']['decimalLongitude'].tolist(), ['', '180'])
        self.assertEqual(frames['event']['year'].tolist(), ['', '2025'])
        self.assertEqual(len(report['withheld_values']), 3)

    def test_preserving_typed_column_retains_whole_column_without_claiming_cells_were_mapped(self):
        archive = occurrence(b'occurrenceID,decimalLatitude,occurrenceStatus\na,NA,present\nb,10,present\n')
        plan = build_plan(archive); decisions = decisions_for(plan)
        column = next(c for c in plan['columns'] if c['term'] == DWC + 'decimalLatitude')
        decisions[column['id']] = 'preserve'
        frames, report = convert(archive, plan, decisions)
        self.assertNotIn('decimalLatitude', frames['event'])
        self.assertEqual(report['withheld_values'], [])
        self.assertEqual(next(c for c in report['columns'] if c['term'] == DWC + 'decimalLatitude')['disposition'], 'retained-unmapped')

    def test_suspicious_dates_warn_and_are_never_repaired(self):
        archive = occurrence(b'occurrenceID,eventDate,occurrenceStatus\na,2015.0,present\nb,NA,present\nc,2015-01-01,present\n')
        plan = build_plan(archive)
        column = next(c for c in plan['columns'] if c['term'] == DWC + 'eventDate')
        self.assertFalse(column['review'])
        self.assertTrue(any(w['id'] == column['id'] for w in plan['warnings']))
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['event']['eventDate'].tolist(), ['2015.0', 'NA', '2015-01-01'])
        decisions = decisions_for(plan); decisions[column['id']] = 'preserve'
        frames, _ = convert(archive, plan, decisions)
        self.assertNotIn('eventDate', frames['event'])

    def test_malformed_term_is_preserved(self):
        archive = read_inputs([('meta.xml', manifest('<field index="2" term="http://https://bad/scientificName"/>')), ('occ.csv', b'id,occurrenceID,bad\nx,o,original\n')])
        plan = build_plan(archive)
        self.assertEqual(plan['columns'][-1]['default'], 'preserve')

    def test_unknown_extension_is_not_attached_by_row_position(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus\na,present\n'), ('mystery.csv', b'label\nunknown\n')])
        plan = build_plan(archive); frames, report = convert(archive, plan, decisions_for(plan))
        self.assertEqual(set(frames), {'event', 'occurrence'})
        self.assertEqual(report['columns'][-1]['disposition'], 'retained-unmapped')

    def test_missing_loose_core_identifier_cannot_accidentally_match_row_numbers(self):
        archive = read_inputs([('occurrence.csv', b'scientificName,occurrenceStatus\nApus apus,present\n'),
                               ('identification.csv', b'occurrenceID,scientificName\n1,Apus pallidus\n')])
        self.assertEqual(archive.tables[1].row_type, '')
        plan = build_plan(archive); frames, _ = convert(archive, plan, decisions_for(plan))
        self.assertNotIn('identification', frames)

    def test_grouping_requires_consistent_event_values(self):
        archive = occurrence(b'occurrenceID,eventID,eventDate,occurrenceStatus\na,e,2025-01-01,present\nb,e,2025-01-02,present\n')
        plan = build_plan(archive); decisions = decisions_for(plan); decisions['event-grain'] = 'by_id'
        self.assertFalse(option_status(plan, decisions)['event-grain']['by_id']['available'])
        with self.assertRaisesRegex(ImportFailure, 'disagree within 1 eventID groups'): convert(archive, plan, decisions)
        # Availability follows the effective decisions: retaining eventDate makes combining valid.
        date = next(column['id'] for column in plan['columns'] if column['term'] == DWC + 'eventDate')
        self.assertTrue(option_status(plan, {**decisions, date: 'preserve'})['event-grain']['by_id']['available'])
        frames, report = convert(archive, plan, {**decisions, date: 'preserve'})
        self.assertEqual(len(frames['event']), 1); self.assertTrue(report['validation']['valid'])
        decisions['event-grain'] = 'per_row'
        frames, report = convert(archive, plan, decisions)
        self.assertEqual(len(frames['event']), 2); self.assertTrue(report['validation']['valid'])

    def test_keys_are_deterministic_and_invalid_or_stale_decisions_fail(self):
        archive = occurrence(); plan = build_plan(archive)
        first, _ = convert(archive, plan, decisions_for(plan)); second, _ = convert(archive, plan, decisions_for(plan))
        self.assertEqual(first['occurrence'].to_csv(index=False), second['occurrence'].to_csv(index=False))
        with self.assertRaises(ImportFailure): validate_decisions(plan, {'event-grain': 'invent'})
        plan['id'] = 'stale'
        with self.assertRaises(ImportFailure): convert(archive, plan, decisions_for(plan))

    def test_authorship_in_scientific_name_triggers_review(self):
        plan = build_plan(occurrence(b'occurrenceID,scientificName,occurrenceStatus\na,Apus apus (Linnaeus),present\n'))
        self.assertTrue(any(issue['id'] == 'column:0:1' for issue in plan['issues']))

    def test_export_includes_declared_originals_and_report_without_invented_eml(self):
        archive = occurrence(); plan = build_plan(archive); frames, _ = convert(archive, plan, decisions_for(plan))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'package.tar.gz'
            descriptor = create_dwc_dp_archive(path, frames, 'Original data', '', include_eml=False,
                additional_files=[('source-originals.zip', source_zip(archive.files)), ('conversion-report.json', b'{}')], declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(path, require_eml=False)['valid'])
            self.assertFalse(validate_dwc_dp_archive(path)['valid'])
            self.assertIn('source-originals', {resource['name'] for resource in descriptor['resources']})

    def test_molecular_records_retain_multiplicity_and_deduplicate_exact_descriptions(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus\na,present\nb,present\n'),
                               ('dnaderiveddata.csv', b'occurrenceID,DNA_sequence,target_gene\na,ACGT,12S\nb,ACGT,12S\n')])
        plan = build_plan(archive); frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid']); self.assertEqual(len(frames['nucleotide-analysis']), 2)
        self.assertEqual(len(frames['nucleotide-sequence']), 1); self.assertEqual(len(frames['molecular-protocol']), 1)
        self.assertNotIn('materialEntity_fk', frames['nucleotide-analysis'])

    def test_material_evidence_uses_persistent_occurrence_id(self):
        archive = occurrence(b'occurrenceID,materialSampleID,occurrenceStatus\npersistent-occ,physical-sample,present\n')
        plan = build_plan(archive); decisions = decisions_for(plan); decisions['material:0'] = 'per_row'
        frames, report = convert(archive, plan, decisions)
        self.assertTrue(report['validation']['valid']); self.assertEqual(frames['material'].iloc[0]['evidenceForOccurrenceID'], 'persistent-occ')
        self.assertEqual(frames['material'].iloc[0]['materialEntityID'], 'physical-sample')

    def test_material_merging_blocks_conflicting_descriptions(self):
        archive = occurrence(b'occurrenceID,eventID,materialSampleID,catalogNumber,occurrenceStatus\na,e,m,catalog-a,present\nb,e,m,catalog-b,present\n')
        plan = build_plan(archive); decisions = decisions_for(plan); decisions.update({'material:0': 'by_id', 'event-grain': 'by_id'})
        with self.assertRaisesRegex(ImportFailure, 'disagree within 1 material identifier groups'): convert(archive, plan, decisions)

    def test_event_extensions_and_explicit_assertion_subjects(self):
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne,survey\n'),
            ('extendedmeasurementorfact.csv', b'eventID,occurrenceID,measurementType,measurementValue\ne,o,length,5\ne,,temperature,20\n'),
            ('occurrence.csv', b'eventID,occurrenceID,occurrenceStatus\ne,o,present\n')])
        plan = build_plan(archive); frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid']); self.assertEqual(len(frames['occurrence-assertion']), 1); self.assertEqual(len(frames['event-assertion']), 1)
        archive.tables[1].rows[0][1] = 'dangling'
        plan = build_plan(archive)
        issue = next(item for item in [*plan['issues'], *plan['automatic_choices']] if item['id'] == 'table:1')
        self.assertIn('declared-assertions', {option['value'] for option in issue['unavailable_options']})
        with self.assertRaisesRegex(ImportFailure, 'Unsupported mapping decision'):
            validate_decisions(plan, {**decisions_for(plan), 'table:1': 'declared-assertions'})

    def test_assertion_identifier_must_agree_with_core_attachment(self):
        extension = '<extension rowType="http://rs.iobis.org/obis/terms/ExtendedMeasurementOrFact" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>facts.csv</location></files><coreid index="0"/><field index="1" term="' + DWC + 'occurrenceID"/><field index="2" term="' + DWC + 'measurementValue"/></extension>'
        archive = read_inputs([('meta.xml', manifest(extension=extension)), ('occ.csv', b'key,occurrenceID\njoin,persistent\n'), ('facts.csv', b'join,occurrenceID,value\njoin,other,5\n')])
        plan = build_plan(archive)
        issue = next(item for item in [*plan['issues'], *plan['automatic_choices']] if item['id'] == 'table:1')
        self.assertEqual([option['value'] for option in issue['unavailable_options']], ['occurrence-assertion'])
        self.assertIn('attached core row', issue['unavailable_options'][0]['reason'])


class ConversionAPITests(TransactionTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': self.folder.name}},
                                                            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
        self.settings_override.enable(); self.addCleanup(self.settings_override.disable); self.addCleanup(self.folder.cleanup)
        self.user = CustomUser.objects.create_user(username='conversion-owner')
        self.client = APIClient(); self.client.force_authenticate(self.user)

    def upload(self):
        with patch('api.helpers.discord_bot.send_discord_message') as discord:
            response = self.client.post('/api/datasets/', {'workflow_type': 'dwca_conversion', 'files': [SimpleUploadedFile('occurrence.csv', b'occurrenceID,occurrenceStatus\na,present\n')]}, format='multipart')
            self.assertEqual(response.status_code, 201, response.data); discord.assert_not_called()
        return Dataset.objects.get(pk=response.data['id'])

    def test_separate_queue_review_export_and_authenticated_download(self):
        dataset = self.upload()
        self.assertEqual(dataset.agent_set.count(), 0); self.assertIsNone(dataset.next_agent())
        self.assertTrue(process_next_conversion()); dataset.conversion.refresh_from_db()
        conversion = dataset.conversion; self.assertEqual(conversion.status, 'review')
        response = self.client.post(f'/api/datasets/{dataset.pk}/conversion/', {'plan_id': conversion.plan['id'], 'decisions': decisions_for(conversion.plan)}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertTrue(process_next_conversion()); conversion.refresh_from_db()
        self.assertEqual(conversion.status, 'complete', conversion.error); self.assertTrue(dataset.package_ready)
        self.assertEqual(dataset.agent_set.count(), 0)
        response = self.client.get(f'/api/datasets/{dataset.pk}/conversion-download/')
        self.assertEqual(response.status_code, 200); response.close()
        other = CustomUser.objects.create_user(username='someone-else'); self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f'/api/datasets/{dataset.pk}/conversion/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/datasets/{dataset.pk}/conversion-download/').status_code, 404)

    def test_stale_plan_duplicate_job_and_source_mutation_rejected(self):
        dataset = self.upload()
        self.assertEqual(self.client.post(f'/api/datasets/{dataset.pk}/conversion/', {}, format='json').status_code, 409)
        process_next_conversion()
        self.assertEqual(self.client.post(f'/api/datasets/{dataset.pk}/conversion/', {'plan_id': 'stale'}, format='json').status_code, 409)
        self.assertEqual(self.client.patch(f'/api/datasets/{dataset.pk}/', {'workflow_type': 'publication'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(f'/api/user-files/{dataset.user_files.first().pk}/').status_code, 400)

    def test_rejected_inputs_do_not_leave_a_dataset(self):
        response = self.client.post('/api/datasets/', {'workflow_type': 'dwca_conversion', 'files': [SimpleUploadedFile('occurrence.csv', b'occurrenceID\na\na\n')]}, format='multipart')
        self.assertEqual(response.status_code, 400); self.assertEqual(Dataset.objects.count(), 0)

    def test_uploaded_zip_is_preserved_exactly_and_deletion_cleans_files(self):
        original = source_zip({'meta.xml': manifest(), 'occ.csv': b'join,occurrenceID\na,persistent\n'})
        response = self.client.post('/api/datasets/', {'workflow_type': 'dwca_conversion', 'files': [SimpleUploadedFile('archive.zip', original)]}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        dataset = Dataset.objects.get(pk=response.data['id']); process_next_conversion()
        conversion = DwcConversion.objects.get(dataset=dataset)
        self.client.post(f'/api/datasets/{dataset.pk}/conversion/', {'plan_id': conversion.plan['id'], 'decisions': decisions_for(conversion.plan)}, format='json')
        process_next_conversion(); conversion.refresh_from_db()
        self.assertEqual(conversion.status, 'complete', conversion.error)
        with conversion.output_file.open('rb') as stream, tarfile.open(fileobj=stream, mode='r:gz') as archive:
            self.assertEqual(archive.extractfile('uploaded-archive.zip').read(), original)
        output_path = conversion.output_file.path; source_path = dataset.user_files.first().file.path
        self.assertEqual(self.client.delete(f'/api/datasets/{dataset.pk}/').status_code, 204)
        self.assertFalse(Path(output_path).exists()); self.assertFalse(Path(source_path).exists())

    def test_storage_failure_releases_job_and_keeps_sources_for_retry(self):
        dataset = self.upload(); process_next_conversion(); conversion = DwcConversion.objects.get(dataset=dataset)
        self.client.post(f'/api/datasets/{dataset.pk}/conversion/', {'plan_id': conversion.plan['id'], 'decisions': decisions_for(conversion.plan)}, format='json')
        with patch('django.db.models.fields.files.FieldFile.save', side_effect=OSError('Storage is unavailable')):
            self.assertTrue(process_next_conversion())
        conversion.refresh_from_db()
        self.assertEqual(conversion.status, 'review'); self.assertIn('Storage is unavailable', conversion.error)
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists()); self.assertTrue(dataset.user_files.exists())

