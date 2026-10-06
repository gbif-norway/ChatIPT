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

    def test_individual_count_becomes_individual_quantity_with_row_level_accounting(self):
        archive = occurrence(b'occurrenceID,individualCount,organismQuantity,organismQuantityType,occurrenceStatus\n'
                             b'one,7,,,present\nzero,0,,,present\nexplicit,4,2,pairs,present\n'
                             b'unit-only,3,,nests,present\nbad,1.5,,,present\nsame,5,5,individuals,present\n')
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        rows = frames['occurrence'].set_index('occurrenceID')
        self.assertEqual((rows.loc['one', 'organismQuantity'], rows.loc['one', 'organismQuantityType']), ('7', 'individuals'))
        self.assertEqual((rows.loc['zero', 'organismQuantity'], rows.loc['zero', 'organismQuantityType']), ('0', 'individuals'))
        self.assertEqual(rows.loc['zero', 'occurrenceStatus'], 'present')
        self.assertEqual((rows.loc['explicit', 'organismQuantity'], rows.loc['explicit', 'organismQuantityType']), ('2', 'pairs'))
        self.assertEqual(rows.loc['unit-only', 'organismQuantityType'], 'nests')
        self.assertEqual(rows.loc['unit-only', 'organismQuantity'], '')
        self.assertEqual(rows.loc['bad', 'organismQuantity'], '')
        # Beside a different supplied quantity the raw count becomes an assertion; an equal individuals quantity already carries it.
        assertions = frames['occurrence-assertion']
        self.assertEqual(assertions['occurrence_fk'].tolist(), [rows.loc['explicit', 'occurrence_pk'], rows.loc['unit-only', 'occurrence_pk']])
        self.assertEqual(assertions[['assertionType', 'assertionTypeIRI', 'assertionValue', 'assertionUnit']].values.tolist(), [
            ['individualCount', DWC + 'individualCount', '4', 'individuals'], ['individualCount', DWC + 'individualCount', '3', 'individuals']])
        disposition = next(item for item in report['columns'] if item['term'] == DWC + 'individualCount')
        self.assertEqual(disposition['disposition'], 'derived')
        self.assertEqual((disposition['mapped_rows'], disposition['retained_only_rows']), (5, 1))
        self.assertEqual(disposition['derived_routes'], {'quantity_pair': 2, 'assertion': 2, 'same_as_supplied_quantity': 1})
        self.assertEqual(disposition['target_counts'], {'occurrence.organismQuantity': 2, 'occurrence-assertion.assertionValue': 2})
        self.assertEqual(disposition['retained_reasons'], {'invalid_nonnegative_integer': 1})
        self.assertIn('source individualCount remains in the originals', disposition['mapping_rule'])
        self.assertEqual(disposition['derived_value_examples'], [
            {'source_row': 1, 'source_value': '7', 'target_table': 'occurrence', 'target_row': 1,
             'target_fields': {'organismQuantity': '7', 'organismQuantityType': 'individuals'}},
            {'source_row': 2, 'source_value': '0', 'target_table': 'occurrence', 'target_row': 2,
             'target_fields': {'organismQuantity': '0', 'organismQuantityType': 'individuals'}},
            {'source_row': 3, 'source_value': '4', 'target_table': 'occurrence-assertion', 'target_row': 1,
             'target_fields': {'assertionType': 'individualCount', 'assertionValue': '4', 'assertionUnit': 'individuals'}},
            {'source_row': 4, 'source_value': '3', 'target_table': 'occurrence-assertion', 'target_row': 2,
             'target_fields': {'assertionType': 'individualCount', 'assertionValue': '3', 'assertionUnit': 'individuals'}},
        ])
        self.assertEqual(disposition['derived_value_examples_omitted'], 0)
        self.assertTrue(report['validation']['valid'])

    def test_individual_count_provenance_examples_are_bounded(self):
        rows = b''.join(f'{n},1,present\n'.encode() for n in range(25))
        archive = occurrence(b'occurrenceID,individualCount,occurrenceStatus\n' + rows)
        plan = build_plan(archive)
        _, report = convert(archive, plan, decisions_for(plan))
        item = next(column for column in report['columns'] if column['term'] == DWC + 'individualCount')
        self.assertEqual(item['mapped_rows'], 25)
        self.assertEqual(len(item['derived_value_examples']), 20)
        self.assertEqual(item['derived_value_examples_omitted'], 5)

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

    def test_invalid_country_code_and_identification_date_are_withheld(self):
        archive = occurrence(
            b'occurrenceID,countryCode,dateIdentified,minimumElevationInMeters,occurrenceStatus\n'
            b'one,Norway,0-0-0,0,present\n'
            b'two,NO,2024-01-01,0,present\n')
        plan = build_plan(archive)
        country_issue = next(issue for issue in plan['issues'] if issue['id'].startswith('country-label:'))
        choices = decisions_for(plan)
        choices[country_issue['id']] = 'event.country'
        frames, report = convert(archive, plan, choices)
        self.assertEqual(len(report['withheld_values']), 1)
        self.assertEqual(report['withheld_values'][0]['value'], '0-0-0')
        self.assertEqual(frames['event'].iloc[0]['country'], 'Norway')
        self.assertEqual(frames['event'].iloc[0].get('countryCode', ''), '')
        self.assertEqual(frames['event'].iloc[1]['countryCode'], 'NO')
        self.assertEqual(list(frames['event']['minimumElevationInMeters']), ['0', '0'])
        country_disposition = next(item for item in report['columns'] if item['term'] == DWC + 'countryCode')
        self.assertEqual(country_disposition['target_counts'], {'event.country': 1, 'event.countryCode': 1})
        self.assertTrue(report['validation']['valid'])

    def test_reviewed_country_label_can_describe_water_body(self):
        archive = occurrence(b'occurrenceID,countryCode,occurrenceStatus\na,Mediterranean Sea,present\n')
        plan = build_plan(archive)
        issue = next(item for item in plan['issues'] if item['id'].startswith('country-label:'))
        decisions = {**decisions_for(plan), issue['id']: 'event.waterBody'}
        frames, report = convert(archive, plan, decisions)
        self.assertEqual(frames['event'].iloc[0]['waterBody'], 'Mediterranean Sea')
        self.assertNotIn('countryCode', frames['event'])
        self.assertEqual(report['columns'][1]['target_counts'], {'event.waterBody': 1})
        self.assertTrue(report['validation']['valid'])

    def test_preserved_country_column_does_not_require_its_value_reviews(self):
        archive = occurrence(b'occurrenceID,countryCode,occurrenceStatus\na,Norway,present\n')
        plan = build_plan(archive)
        decisions = decisions_for(plan)
        for issue in plan['issues']:
            if issue['id'].startswith('country-label:'):
                decisions.pop(issue['id'])
        column = next(item for item in plan['columns'] if item['term'] == DWC + 'countryCode')
        decisions[column['id']] = 'preserve'
        frames, report = convert(archive, plan, decisions)
        self.assertNotIn('countryCode', frames['event'])
        self.assertEqual(report['reviewed_value_routes'][0]['target'], 'inactive: source column preserved')
        self.assertTrue(report['validation']['valid'])

    def test_age_like_event_remarks_require_review_before_occurrence_mapping(self):
        archive = occurrence(
            b'occurrenceID,eventRemarks,occurrenceStatus\n'
            b'a,ad,present\nb,juv.,present\nc,1 juv.,present\nd,sampled by hand,present\n')
        plan = build_plan(archive)
        issues = {item['source_value']: item for item in plan['issues'] if item['id'].startswith('age-remark:')}
        self.assertEqual(set(issues), {'ad', 'juv.', '1 juv.'})
        choices = decisions_for(plan)
        choices[issues['ad']['id']] = 'occurrence.lifeStage'
        choices[issues['1 juv.']['id']] = 'occurrence.occurrenceRemarks'
        frames, report = convert(archive, plan, choices)
        self.assertEqual(frames['occurrence'].iloc[0]['lifeStage'], 'ad')
        self.assertEqual(frames['occurrence'].iloc[2]['occurrenceRemarks'], '1 juv.')
        self.assertEqual(frames['event'].iloc[1]['eventRemarks'], 'juv.')
        self.assertEqual(frames['event'].iloc[3]['eventRemarks'], 'sampled by hand')
        item = next(column for column in report['columns'] if column['term'] == DWC + 'eventRemarks')
        self.assertEqual(item['target_counts'], {
            'event.eventRemarks': 2, 'occurrence.lifeStage': 1, 'occurrence.occurrenceRemarks': 1})
        self.assertTrue(report['validation']['valid'])

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
        # Two row events inside one event that keeps the shared eventID.
        self.assertEqual(len(frames['event']), 3); self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['event']['eventID'].tolist(), ['e', '', ''])

    def test_separate_row_events_keep_a_shared_event_id_on_one_parent_event(self):
        """Camera streams whose detections share an eventID but not a timestamp (production dataset 564)."""
        stream = b'Camera13Stream1_2024-01-02T13-45-44_2024-01-02T13-50-45'
        archive = occurrence(b'occurrenceID,eventID,eventDate,basisOfRecord,organismQuantity,organismQuantityType,scientificName,occurrenceStatus\n'
                             b'c312ab1d,' + stream + b',2024-01-02 12:47:11.290311+00:00,MachineObservation,1,individuals,Larus marinus,present\n'
                             b'd41f8e02,' + stream + b',2024-01-02 12:48:03.104220+00:00,MachineObservation,2,individuals,Larus sp.,present\n'
                             b'e7a0c311,Camera13Stream1_2024-01-03T09-46-02_2024-01-03T09-51-02,2024-01-03 08:47:40.000000+00:00,MachineObservation,1,individuals,Larus fuscus,present\n')
        plan = build_plan(archive)
        grain = next(issue for issue in plan['issues'] if issue['id'] == 'event-grain')
        self.assertIn('inside one event for each shared eventID', grain['options'][0]['label'])
        self.assertFalse(option_status(plan, {**decisions_for(plan), 'event-grain': 'by_id'})['event-grain']['by_id']['available'])
        frames, report = convert(archive, plan, {**decisions_for(plan), 'event-grain': 'per_row'})
        events = frames['event']
        supplied = events[events['eventID'] != '']['eventID']
        self.assertEqual(sorted(supplied), sorted([stream.decode(), 'Camera13Stream1_2024-01-03T09-46-02_2024-01-03T09-51-02']))
        parent = events[events['eventID'] == stream.decode()].iloc[0]
        self.assertEqual((parent['eventDate'], parent['parentEvent_fk'], parent['eventCategory']), ('', '', 'occurrence'))
        children = events[events['parentEvent_fk'] == parent['event_pk']]
        self.assertEqual(sorted(children['eventDate']), ['2024-01-02 12:47:11.290311+00:00', '2024-01-02 12:48:03.104220+00:00'])
        self.assertEqual(set(frames['occurrence'].set_index('occurrenceID').loc[['c312ab1d', 'd41f8e02'], 'event_fk']), set(children['event_pk']))
        # An eventID used once stays on its own row event.
        single = events[events['eventID'] == 'Camera13Stream1_2024-01-03T09-46-02_2024-01-03T09-51-02'].iloc[0]
        self.assertEqual((single['eventDate'], single['parentEvent_fk']), ('2024-01-03 08:47:40.000000+00:00', ''))
        self.assertEqual(report['shared_event_ids']['parent_events'], 1)
        self.assertEqual(report['shared_event_ids']['row_events'], 2)
        self.assertTrue(report['validation']['valid'])

    def test_emof_vocabulary_identifiers_reach_the_assertion_iri_fields(self):
        """NERC P01/S10/P06 identifiers on eMoF rows (production dataset 566)."""
        event = b'Nord-1983-02-07-5-SC'
        archive = read_inputs([
            ('event.csv', b'eventID,eventCategory,eventDate\n' + event + b',survey,1983-02-07\n'),
            ('occurrence.csv', b'eventID,occurrenceID,basisOfRecord,occurrenceStatus,scientificName\n'
                               + event + b',Nord-1983-02-07-5-SC-6,MaterialSample,present,Calanus finmarchicus\n'),
            ('extendedmeasurementorfact.csv',
             b'eventID,occurrenceID,measurementType,measurementTypeID,measurementValue,measurementValueID,measurementUnit,measurementUnitID\n'
             + event + b',NA,Mesh size,http://vocab.nerc.ac.uk/collection/P01/current/MSHSIZE1/,180,NA,micrometers,http://vocab.nerc.ac.uk/collection/P06/current/UMIC/\n'
             + event + b',Nord-1983-02-07-5-SC-6,sex,http://vocab.nerc.ac.uk/collection/P01/current/ENTSEX01/,F,'
                       b'http://vocab.nerc.ac.uk/collection/S10/current/S102/,not applicable,https://vocab.nerc.ac.uk/collection/P06/current/XXXX/\n'
             + event + b',Nord-1983-02-07-5-SC-6,sex,http://vocab.nerc.ac.uk/collection/P01/current/ENTSEX01/,M,'
                       b'S103,not applicable,https://vocab.nerc.ac.uk/collection/P06/current/XXXX/\n')])
        plan = build_plan(archive)
        emof = next(index for index, table in enumerate(archive.tables) if table.name == 'extendedmeasurementorfact.csv')
        targets = {column['term'].rsplit('/', 1)[1]: column['default'] for column in plan['columns'] if column['table'] == emof}
        self.assertEqual((targets['measurementTypeID'], targets['measurementValueID'], targets['measurementUnitID']),
                         ('occurrence-assertion.assertionTypeIRI', 'occurrence-assertion.assertionValueIRI', 'occurrence-assertion.assertionUnitIRI'))
        self.assertFalse(any(column.get('unmapped') for column in plan['columns'] if column['table'] == emof))
        frames, report = convert(archive, plan, decisions_for(plan))
        mesh = frames['event-assertion'].iloc[0]
        self.assertEqual((mesh['assertionTypeIRI'], mesh.get('assertionValueIRI', ''), mesh['assertionUnitIRI']),
                         ('http://vocab.nerc.ac.uk/collection/P01/current/MSHSIZE1/', '', 'http://vocab.nerc.ac.uk/collection/P06/current/UMIC/'))
        sex = frames['occurrence-assertion'].iloc[0]
        self.assertEqual((sex['assertionValue'], sex['assertionValueIRI']), ('F', 'http://vocab.nerc.ac.uk/collection/S10/current/S102/'))
        # "NA" is an empty cell, counted once for the column; other non-IRI text is withheld with its source row.
        self.assertEqual([(cell['term'].rsplit('/', 1)[1], cell['value'], cell['source_row']) for cell in report['withheld_values']],
                         [('measurementValueID', 'S103', 3)])
        self.assertEqual(frames['occurrence-assertion'].iloc[1].get('assertionValueIRI', ''), '')
        value_ids = next(column for column in report['columns'] if column['term'].endswith('/measurementValueID'))
        self.assertEqual((value_ids['mapped_rows'], value_ids['retained_only_rows'], value_ids['empty_placeholder']), (1, 2, 1))
        # The plan notice counts only the real non-IRI text, not the placeholder.
        self.assertEqual(next(column for column in plan['columns'] if column['term'].endswith('/measurementValueID'))['incompatible_values'],
                         {'occurrence-assertion.assertionValueIRI': 1})
        from api.dwca_value_ledger import build_value_disposition_ledger
        ledger = {row['source_term'].rsplit('/', 1)[1]: row for row in build_value_disposition_ledger(plan, report)['source_terms']
                  if row['source_table_index'] == emof}
        self.assertEqual((ledger['measurementValueID']['withheld_invalid_values'], ledger['measurementValueID']['empty_placeholder_values'],
                          ledger['measurementValueID']['originals_only_values']), (1, 1, 1))
        self.assertTrue(report['validation']['valid'])

    def test_identification_qualifiers_follow_the_name_in_verbatim_identification(self):
        """Qualifiers supplied beside the name (production datasets 567 and 568)."""
        archive = occurrence(b'occurrenceID,basisOfRecord,scientificName,identificationQualifier,occurrenceStatus\n'
                             b'urn:uuid:719cbdc1,PreservedSpecimen,Iguana sp.,?,present\n'
                             b'urn:uuid:83a496fb,PreservedSpecimen,"Tropidolaemus subannulatus Gray, 1842",cf.,present\n'
                             b'urn:uuid:4f2a1c90,PreservedSpecimen,Rana cf. arvalis,cf.,present\n'
                             b'd51a5613,PreservedSpecimen,Microcalanus,spp.,present\n'
                             b'f4702da8,PreservedSpecimen,Aglantha digitale,,present\n')
        plan = build_plan(archive)
        qualifier = next(column for column in plan['columns'] if column['term'] == DWC + 'identificationQualifier')
        self.assertEqual((qualifier['verbatim_copy'], qualifier['verbatim_role']), ('occurrence.verbatimIdentification', 'qualifier'))
        self.assertNotIn('unmapped', qualifier)
        frames, report = convert(archive, plan, decisions_for(plan))
        rows = frames['occurrence'].set_index('occurrenceID')
        self.assertEqual(rows['verbatimIdentification'].tolist(), [
            'Iguana sp. ?', 'Tropidolaemus subannulatus Gray, 1842 cf.', 'Rana cf. arvalis', 'Microcalanus spp.', 'Aglantha digitale'])
        # scientificName never receives a qualifier.
        self.assertEqual((rows.loc['urn:uuid:719cbdc1', 'scientificName'], rows.loc['d51a5613', 'scientificName']), ('Iguana sp.', 'Microcalanus'))
        item = next(column for column in report['columns'] if column['term'] == DWC + 'identificationQualifier')
        self.assertEqual((item['target'], item['disposition'], item['mapped_rows'], item['retained_only_rows']),
                         ('derived verbatim copy → occurrence.verbatimIdentification', 'derived', 4, 0))
        self.assertTrue(report['validation']['valid'])

    def test_qualifiers_that_name_a_part_go_before_it_on_identification_extensions(self):
        """Darwin Core's own identificationQualifier and verbatimIdentification examples, on an Identification extension."""
        archive = read_inputs([
            ('occurrence.csv', b'occurrenceID,occurrenceStatus\no1,present\no2,present\no3,present\no4,present\no5,present\n'),
            ('identification.csv', b'occurrenceID,scientificName,identificationQualifier\n'
                                   b'o1,Quercus agrifolia var. oxyadenia (Torr.) J.T. Howell,aff. agrifolia var. oxyadenia\n'
                                   b'o2,Quercus agrifolia var. oxyadenia,cf. var. oxyadenia\n'
                                   b'o3,Pachyporidae?,?\n'
                                   b'o4,Quercus robur,aff. agrifolia\n'
                                   b'o5,Peromyscus,sp.\n')])
        plan = build_plan(archive)
        qualifier = next(column for column in plan['columns'] if column['term'] == DWC + 'identificationQualifier')
        self.assertEqual(qualifier['verbatim_copy'], 'identification.verbatimIdentification')
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertEqual(frames['identification']['verbatimIdentification'].tolist(), [
            'Quercus aff. agrifolia var. oxyadenia (Torr.) J.T. Howell', 'Quercus agrifolia cf. var. oxyadenia',
            'Pachyporidae?', 'Quercus robur', 'Peromyscus sp.'])
        item = next(column for column in report['columns'] if column['term'] == DWC + 'identificationQualifier')
        # The qualifier whose part is not in the name builds no text and stays in the originals.
        self.assertEqual((item['target'], item['mapped_rows'], item['retained_only_rows']),
                         ('derived verbatim copy → identification.verbatimIdentification', 4, 1))
        self.assertEqual(item['retained_reasons'], {'qualifier_not_placeable': 1})
        self.assertTrue(report['validation']['valid'])

    def test_a_supplied_verbatim_identification_is_not_rewritten_with_the_qualifier(self):
        archive = occurrence(b'occurrenceID,scientificName,verbatimIdentification,identificationQualifier,occurrenceStatus\n'
                             b'a,Themisto abyssorum,T. aby.,cf.,present\nb,Calanus,,spp.,present\n')
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertEqual(frames['occurrence']['verbatimIdentification'].tolist(), ['T. aby.', 'Calanus spp.'])
        item = next(column for column in report['columns'] if column['term'] == DWC + 'identificationQualifier')
        self.assertEqual((item['mapped_rows'], item['retained_only_rows']), (1, 1))
        self.assertEqual(item['retained_reasons'], {'verbatim_identification_supplied': 1})

    def test_placeholder_event_ids_are_not_repeated_on_separate_row_events(self):
        archive = occurrence(b'occurrenceID,eventID,eventDate,occurrenceStatus\n'
                             b'a,NA,2024-01-02,present\nb,NA,2024-01-03,present\nc,cast-1,2024-01-04,present\nd,cast-1,2024-01-05,present\n')
        plan = build_plan(archive)
        frames, report = convert(archive, plan, {**decisions_for(plan), 'event-grain': 'per_row'})
        events = frames['event']
        self.assertEqual(sorted(events['eventID']), ['', '', '', '', 'cast-1'])
        # The NA rows keep separate events without an eventID or a parent; the originals keep "NA".
        unidentified = events[events['eventDate'].isin(['2024-01-02', '2024-01-03'])]
        self.assertEqual((unidentified['eventID'].tolist(), unidentified['parentEvent_fk'].tolist()), (['', ''], ['', '']))
        self.assertEqual(report['shared_event_ids']['parent_events'], 1)
        column = next(item for item in report['columns'] if item['term'] == DWC + 'eventID')
        self.assertEqual((column['mapped_rows'], column['retained_only_rows'], column['empty_placeholder']), (2, 2, 2))
        self.assertEqual(report['withheld_values'], [])
        self.assertTrue(report['validation']['valid'])

    def test_depths_within_one_event_become_child_events(self):
        """One cast sampling several depths (production dataset 546)."""
        archive = occurrence(b'occurrenceID,eventID,parentEventID,eventDate,decimalLatitude,decimalLongitude,minimumDepthInMeters,maximumDepthInMeters,occurrenceStatus\n'
                             b'a,cast-1,cruise,2025-01-01,60,10,0,10,present\nb,cast-1,cruise,2025-01-01,60,10,0,10,present\n'
                             b'c,cast-1,cruise,2025-01-01,60,10,50,60,present\nd,cast-2,cruise,2025-01-02,61,11,5,5,present\n'
                             b'e,cruise,,2025-01-01/2025-01-02,60,10,,,present\n')
        plan = build_plan(archive)
        grain = next(issue for issue in plan['issues'] if issue['id'] == 'event-grain')
        self.assertIn('by_id_depth', [option['value'] for option in grain['options']])
        self.assertTrue(next(option for option in grain['options'] if option['value'] == 'by_id_depth')['assertion'])
        parent = next(column['id'] for column in plan['columns'] if column['term'] == DWC + 'parentEventID')
        decisions = {**decisions_for(plan), 'event-grain': 'by_id', parent: 'parent-link'}
        status = option_status(plan, decisions)['event-grain']
        self.assertFalse(status['by_id']['available'])
        self.assertTrue(status['by_id_depth']['available'])
        frames, report = convert(archive, plan, {**decisions, 'event-grain': 'by_id_depth'})
        self.assertTrue(report['validation']['valid'], report['validation'])
        events = frames['event']
        combined = events[events['eventID'] != ''].set_index('eventID')
        self.assertEqual(sorted(combined.index), ['cast-1', 'cast-2', 'cruise'])
        self.assertTrue((combined[['minimumDepthInMeters', 'maximumDepthInMeters']] == '').all().all())
        self.assertEqual(combined.loc['cast-1', 'parentEvent_fk'], combined.loc['cruise', 'event_pk'])
        children = events[events['eventID'] == '']
        self.assertEqual(sorted(zip(children['minimumDepthInMeters'], children['maximumDepthInMeters'])),
                         [('', ''), ('0', '10'), ('5', '5'), ('50', '60')])
        self.assertEqual(set(children['eventDate']), {''})
        occurrences = frames['occurrence'].set_index('occurrenceID')
        child = events.set_index('event_pk')
        self.assertEqual(occurrences.loc['a', 'event_fk'], occurrences.loc['b', 'event_fk'])
        self.assertNotEqual(occurrences.loc['a', 'event_fk'], occurrences.loc['c', 'event_fk'])
        self.assertEqual(child.loc[occurrences.loc['c', 'event_fk'], 'parentEvent_fk'], combined.loc['cast-1', 'event_pk'])
        self.assertEqual(report['depth_events']['combined_events'], 3)
        self.assertEqual(report['depth_events']['depth_events'], 4)
        # Without a mapped depth column there is nothing to split by.
        depths = {column['id']: 'preserve' for column in plan['columns'] if column['term'].endswith('DepthInMeters')}
        self.assertFalse(option_status(plan, {**decisions, **depths, 'event-grain': 'by_id_depth'})['event-grain']['by_id_depth']['available'])

    def test_depth_children_follow_source_depths_and_material_stays_within_one(self):
        archive = occurrence(b'occurrenceID,eventID,materialSampleID,minimumDepthInMeters,maximumDepthInMeters,occurrenceStatus\n'
                             b'a,e1,m1,5,20,present\nb,e1,m1,5,20,present\nc,e1,m2,10,20,present\nd,e1,m3,NA,20,present\nf,e1,m4,bad,20,present\n')
        plan = build_plan(archive)
        decisions = {**decisions_for(plan), 'event-grain': 'by_id_depth', 'material:0': 'by_id'}
        self.assertTrue(option_status(plan, decisions)['material:0']['by_id']['available'])
        minimum = next(column['id'] for column in plan['columns'] if column['term'] == DWC + 'minimumDepthInMeters')
        frames, report = convert(archive, plan, {**decisions, minimum: 'preserve'})
        self.assertTrue(report['validation']['valid'], report['validation'])
        # Retained or invalid depths still keep each source depth its own event.
        self.assertEqual(report['depth_events']['depth_events'], 4)
        self.assertEqual(len(frames['material']), 4)
        spanning = occurrence(b'occurrenceID,eventID,materialSampleID,minimumDepthInMeters,occurrenceStatus\n'
                              b'a,e1,m1,5,present\nb,e1,m1,10,present\n')
        plan = build_plan(spanning)
        decisions = {**decisions_for(plan), 'event-grain': 'by_id_depth', 'material:0': 'by_id'}
        self.assertFalse(option_status(plan, decisions)['material:0']['by_id']['available'])

    def test_extension_event_details_patch_the_combined_event_not_a_depth_child(self):
        nxf = 'http://rs.nbn.org.uk/dwc/nxf/0.1/terms/'
        meta = ('<archive xmlns="http://rs.tdwg.org/dwc/text/">'
                '<core rowType="' + DWC + 'Occurrence" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>occ.csv</location></files><id index="0"/>'
                '<field index="1" term="' + DWC + 'occurrenceID"/><field index="2" term="' + DWC + 'eventID"/>'
                '<field index="3" term="' + DWC + 'minimumDepthInMeters"/><field index="4" term="' + DWC + 'eventDate"/>'
                '<field term="' + DWC + 'occurrenceStatus" default="present"/></core>'
                '<extension rowType="' + nxf + 'nxfOccurrence" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>nbn.csv</location></files><coreid index="0"/>'
                '<field index="1" term="' + nxf + 'eventDateTypeCode"/><field index="2" term="' + nxf + 'eventDateStart"/>'
                '<field index="3" term="' + nxf + 'eventDateEnd"/></extension></archive>').encode()

        def source(second, core_date=b''):
            return read_inputs([('meta.xml', meta), ('occ.csv', b'id,occurrenceID,eventID,minimumDepthInMeters,eventDate\nr1,a,e1,5,' + core_date +
                                                       b'\nr2,b,e1,10,' + core_date + b'\n'),
                                ('nbn.csv', b'id,code,start,end\nr1,D,2015-06-03,2015-06-03\nr2,D,' + second + b',' + second + b'\n')])

        def prepare(archive, grain='by_id_depth'):
            plan = build_plan(archive)
            return plan, {**decisions_for(plan), 'event-grain': grain, 'row-group:1:0': 'convert', 'table:1': 'nbn-context'}

        archive = source(b'2015-06-03')
        frames, report = convert(archive, *prepare(archive))
        events = frames['event']
        self.assertEqual(events.set_index('eventID').loc['e1', 'eventDate'], '2015-06-03')
        self.assertEqual(set(events[events['eventID'] == '']['eventDate']), {''})
        # Different extension dates for one combined event are found before conversion.
        archive = source(b'2015-07-01')
        plan, decisions = prepare(archive)
        status = option_status(plan, decisions)
        self.assertFalse(status['event-grain']['by_id_depth']['available'])
        self.assertIn('same event', status['event-grain']['by_id_depth']['reasons'][0])
        self.assertFalse(status['table:1']['nbn-context']['available'])
        with self.assertRaises(ImportFailure): validate_decisions(plan, decisions)
        frames, report = convert(archive, plan, {**decisions, 'event-grain': 'per_row'})
        self.assertTrue(report['validation']['valid'])
        # So is an extension date that differs from the core's own eventDate, whatever the event grain.
        archive = source(b'2015-06-03', b'2015-06-04')
        plan, decisions = prepare(archive, 'per_row')
        self.assertFalse(option_status(plan, decisions)['table:1']['nbn-context']['available'])
        date = next(column['id'] for column in plan['columns'] if column['term'] == DWC + 'eventDate')
        self.assertTrue(option_status(plan, {**decisions, date: 'preserve'})['table:1']['nbn-context']['available'])
        self.assertTrue(option_status(plan, {**decisions, 'row-group:1:0': 'preserve'})['table:1']['nbn-context']['available'])
        frames, report = convert(archive, plan, {**decisions, date: 'preserve'})
        # Per-row events hold the patched dates; the event for their shared eventID holds only that identity.
        events = frames['event']
        self.assertEqual(set(events[events['parentEvent_fk'] != '']['eventDate']), {'2015-06-03'})
        self.assertEqual(events[events['parentEvent_fk'] == '']['eventID'].tolist(), ['e1'])

    def test_depth_children_are_offered_only_when_an_event_spans_depths(self):
        archive = occurrence(b'occurrenceID,eventID,minimumDepthInMeters,occurrenceStatus\na,e1,5,present\nb,e1,5,present\nc,e2,7,present\n')
        grain = next(item for item in [*build_plan(archive)['issues'], *build_plan(archive)['automatic_choices']] if item['id'] == 'event-grain')
        self.assertNotIn('by_id_depth', [option['value'] for option in grain['options']])

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

    def test_preserved_specimen_catalog_triples_create_material_records(self):
        archive = occurrence(
            b'occurrenceID,basisOfRecord,institutionCode,collectionCode,catalogNumber,preparations,occurrenceStatus\n'
            b'one,PreservedSpecimen,RMZ,Araneae,1,fluid,present\n'
            b'two,PreservedSpecimen,RMZ,Araneae,2,fluid,present\n')
        plan = build_plan(archive)
        automatic = {choice['id']: choice['default'] for choice in plan['automatic_choices']}
        self.assertEqual(automatic['material:0'], 'per_row')
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['material']), 2)
        self.assertEqual(set(frames['material']['catalogNumber']), {'1', '2'})
        self.assertEqual(set(frames['material']['preparations']), {'fluid'})
        self.assertEqual(set(frames['material']['evidenceForOccurrenceID']), {'one', 'two'})
        self.assertNotIn('materialEntityID', frames['material'])

    def test_specimen_material_needs_review_when_catalog_identity_repeats(self):
        archive = occurrence(
            b'occurrenceID,basisOfRecord,institutionCode,collectionCode,catalogNumber,occurrenceStatus\n'
            b'one,PreservedSpecimen,RMZ,Araneae,1,present\n'
            b'two,PreservedSpecimen,RMZ,Araneae,1,present\n')
        plan = build_plan(archive)
        self.assertNotIn('material:0', {choice['id'] for choice in plan['automatic_choices']})
        self.assertIn('material:0', {issue['id'] for issue in plan['issues']})
        frames, _ = convert(archive, plan, decisions_for(plan))
        self.assertNotIn('material', frames)

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
        self.assertIn('table:1', {item['id'] for item in plan['issues']})
        with self.assertRaisesRegex(ImportFailure, 'Unsupported mapping decision'):
            validate_decisions(plan, {**decisions_for(plan), 'table:1': 'declared-assertions'})

    def test_unmatched_na_reference_uses_event_subject_without_erasing_original(self):
        archive = read_inputs([('event.csv', b'eventID,parentEventID,eventCategory\nparent,NA,survey\nchild,parent,survey\n'),
            ('extendedmeasurementorfact.csv', b'eventID,occurrenceID,measurementType,measurementValue\nchild,occ,length,5\nchild,NA,temperature,20\n'),
            ('occurrence.csv', b'eventID,occurrenceID,occurrenceStatus\nchild,occ,present\n')])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['occurrence-assertion']), 1)
        self.assertEqual(len(frames['event-assertion']), 1)
        self.assertEqual(report['event_hierarchy']['linked_events'], 1)
        self.assertEqual(report['event_hierarchy']['source_values'][0]['parentEventID'], 'NA')
        self.assertEqual(report['event_hierarchy']['source_values'][0]['status'], 'retained in originals')

    def test_placeholder_ids_do_not_create_structural_links_even_when_both_sides_match(self):
        archive = read_inputs([('event.csv', b'eventID,parentEventID,eventCategory\nNA,,survey\nchild,NA,survey\n'),
            ('extendedmeasurementorfact.csv', b'eventID,occurrenceID,measurementType,measurementValue\nchild,NA,length,5\n'),
            ('occurrence.csv', b'eventID,occurrenceID,occurrenceStatus\nchild,NA,present\n')])
        plan = build_plan(archive)
        self.assertIn('table:1', {issue['id'] for issue in plan['issues']})
        self.assertNotIn('table:1', {choice['id'] for choice in plan['automatic_choices']})
        with self.assertRaises(ImportFailure):
            convert(archive, plan, {})
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(report['event_hierarchy']['linked_events'], 0)
        self.assertNotIn('occurrence-assertion', frames)
        self.assertEqual(len(frames['event-assertion']), 1)
        self.assertEqual(report['event_hierarchy']['source_values'][0]['withheld_reason'], 'empty-reference-token')

    def test_loose_file_placeholder_keys_do_not_join_extension_rows(self):
        cases = [
            ([('event.csv', b'eventID,eventCategory\nNA,survey\ne2,survey\n'),
              ('occurrence.csv', b'eventID,occurrenceStatus\nNA,present\nNA,present\n')], 2),
            ([('occurrence.csv', b'occurrenceID,occurrenceStatus\nNA,present\nother,present\n'),
              ('extendedmeasurementorfact.csv', b'occurrenceID,measurementType,measurementValue\nNA,length,5\n')], 1),
        ]
        for files, dropped in cases:
            with self.subTest(core=files[0][0]):
                with self.assertRaisesRegex(ImportFailure, 'ambiguous core link'):
                    read_inputs(files)
                archive = read_inputs(files, drop_unlinked_extension_rows=True)
                self.assertEqual(archive.dropped_extension_rows[0]['rows'], dropped)

    def test_manifest_placeholder_core_keys_do_not_join_extension_rows(self):
        extension = '<extension rowType="http://rs.iobis.org/obis/terms/ExtendedMeasurementOrFact" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>facts.csv</location></files><coreid index="0"/><field index="1" term="' + DWC + 'measurementValue"/></extension>'
        files = [('meta.xml', manifest(extension=extension)),
                 ('occ.csv', b'key,occurrenceID\nNA,persistent\n'),
                 ('facts.csv', b'key,value\nNA,5\n')]
        with self.assertRaisesRegex(ImportFailure, 'ambiguous core link'):
            read_inputs(files)

    def test_placeholder_assertion_id_with_no_core_id_keeps_occurrence_subject(self):
        extension = '<extension rowType="http://rs.iobis.org/obis/terms/ExtendedMeasurementOrFact" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>facts.csv</location></files><coreid index="0"/><field index="1" term="' + DWC + 'occurrenceID"/><field index="2" term="' + DWC + 'measurementValue"/></extension>'
        meta = ('<archive xmlns="http://rs.tdwg.org/dwc/text/"><core rowType="' + DWC + 'Occurrence" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>occ.csv</location></files><id index="0"/><field term="' + DWC + 'occurrenceStatus" default="present"/></core>' + extension + '</archive>').encode()
        archive = read_inputs([('meta.xml', meta), ('occ.csv', b'key\njoin\n'), ('facts.csv', b'key,occurrenceID,value\njoin,NA,5\n')])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['occurrence-assertion']), 1)
        self.assertNotIn('event-assertion', frames)
        archive.tables[1].rows[0][0] = 'real-id'
        plan = build_plan(archive)
        self.assertIn('table:1', {issue['id'] for issue in plan['issues']})
        self.assertNotIn('table:1', {choice['id'] for choice in plan['automatic_choices']})

    def test_placeholder_identifiers_cannot_merge_events_or_material(self):
        archive = occurrence(b'occurrenceID,eventID,materialSampleID,occurrenceStatus\na,NA,NA,present\nb,NA,NA,present\n')
        plan = build_plan(archive)
        grain = next(item for item in [*plan['issues'], *plan['automatic_choices']] if item['id'] == 'event-grain')
        self.assertEqual(grain['default'], 'per_row')
        self.assertNotIn('by_id', {option['value'] for option in grain['options']})
        self.assertFalse(option_status(plan, {**decisions_for(plan), 'material:0': 'by_id'})['material:0']['by_id']['available'])
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['event']), 2)

    def test_single_agent_ids_create_records_without_pairing_lists_or_conflicts(self):
        archive = occurrence(b'occurrenceID,recordedBy,recordedByID,identifiedBy,identifiedByID,occurrenceStatus\n'
            b'o1,"Smith, Alice",https://example.org/alice,"Smith, Alice",https://example.org/alice,present\n'
            b'o2,Alice Smith,https://example.org/alice,,NA,present\n'
            b'o3,Bob | Carol,https://example.org/bob | https://example.org/carol,,https://example.org/id,present\n'
            b'o4,Bob|Carol,https://example.org/bob,,,present\n'
            b'o5,Bob|Carol,https://example.org/bob;https://example.org/carol,,,present\n'
            b'o6,"Bob, Carol","https://example.org/bob,https://example.org/carol",,,present\n'
            b'o7,Smith,Smith:John,,,present\n')
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'])
        agents = frames['agent'].set_index('agentID')
        # Only o3's | list of IDs is split (by the agent role builder); other lists are not.
        self.assertEqual(set(agents.index), {'https://example.org/alice', 'https://example.org/id', 'https://example.org/bob',
                                             'https://example.org/carol'})
        self.assertEqual(agents.loc['https://example.org/alice', 'preferredAgentName'], '')
        self.assertEqual(agents.loc['https://example.org/bob', 'preferredAgentName'], '')
        # Carol appears only in a list, and list positions do not pair names with IDs.
        self.assertEqual(agents.loc['https://example.org/carol', 'preferredAgentName'], '')
        self.assertEqual(report['agent_mapping']['non_single_id_cells'], 4)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'agents.tar.gz'
            create_dwc_dp_archive(output, frames, 'Agents', 'Explicit source agents')
            self.assertTrue(validate_dwc_dp_archive(output)['valid'])

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

    def upload_with_eml(self, title='', description=''):
        eml = (b'<eml><dataset><title>  Forest   birds </title><abstract><para>Point counts   in forest.</para></abstract>'
               b'<creator><organizationName>Field Team</organizationName></creator>'
               b'<keywordSet><keyword>birds</keyword></keywordSet>'
               b'<intellectualRights>https://creativecommons.org/licenses/by/4.0/</intellectualRights>'
               b'<citation><para>Forest birds dataset</para></citation></dataset></eml>')
        files = [SimpleUploadedFile('meta.xml', manifest().replace(b'<archive ', b'<archive metadata="metadata.xml" ')),
                 SimpleUploadedFile('metadata.xml', eml), SimpleUploadedFile('occ.csv', b'join,occurrenceID\na,persistent\n')]
        response = self.client.post('/api/datasets/', {'workflow_type': 'dwca_conversion', 'title': title,
                                                       'description': description, 'files': files}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        return Dataset.objects.get(pk=response.data['id'])

    def test_inspect_fills_blank_metadata_from_declared_eml_and_reports_provenance(self):
        dataset = self.upload_with_eml()
        self.assertTrue(process_next_conversion())
        dataset.refresh_from_db()
        conversion = dataset.conversion
        self.assertEqual(dataset.title, 'Forest birds')
        self.assertEqual(dataset.description, 'Point counts in forest.')
        response = self.client.post(f'/api/datasets/{dataset.pk}/conversion/',
                                    {'plan_id': conversion.plan['id'], 'decisions': decisions_for(conversion.plan)}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertTrue(process_next_conversion())
        conversion.refresh_from_db()
        self.assertEqual(conversion.status, 'complete', conversion.error)
        self.assertEqual(conversion.report['metadata']['title_source'], 'eml')
        self.assertEqual(conversion.report['metadata']['description_source'], 'eml')
        self.assertIn('eml', conversion.report['metadata'])
        self.assertEqual(conversion.report['metadata']['descriptor_fields_from_source_eml'],
                         ['contributors', 'keywords', 'licenses'])
        self.assertEqual(conversion.report['metadata']['source_only']['citation'], 'Forest birds dataset')
        self.assertIn('source_terms', conversion.report['value_disposition'])
        with conversion.output_file.open('rb') as stream, tarfile.open(fileobj=stream, mode='r:gz') as output:
            descriptor = json.load(output.extractfile('datapackage.json'))
            package_report = json.load(output.extractfile('conversion-report.json'))
        self.assertEqual(descriptor['keywords'], ['birds'])
        self.assertEqual(descriptor['licenses'][0]['name'], 'cc-by-4.0')
        self.assertEqual(descriptor['contributors'][0]['title'], 'Field Team')
        self.assertIn('value_disposition', package_report)
        self.assertIn('semantic_value_audit', package_report)

    def test_inspect_does_not_overwrite_user_metadata(self):
        # A user title identical to the EML title is still the user's, recorded at inspection.
        dataset = self.upload_with_eml('Forest birds', 'My description')
        self.assertTrue(process_next_conversion())
        dataset.refresh_from_db()
        self.assertEqual(dataset.title, 'Forest birds')
        self.assertEqual(dataset.description, 'My description')
        conversion = dataset.conversion
        response = self.client.post(f'/api/datasets/{dataset.pk}/conversion/',
                                    {'plan_id': conversion.plan['id'], 'decisions': decisions_for(conversion.plan)}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertTrue(process_next_conversion())
        conversion.refresh_from_db()
        self.assertEqual(conversion.report['metadata']['title_source'], 'user')
        self.assertEqual(conversion.report['metadata']['description_source'], 'user')

    def test_separate_queue_review_export_and_authenticated_download(self):
        dataset = self.upload()
        self.assertEqual(dataset.agent_set.count(), 0); self.assertIsNone(dataset.next_agent())
        self.assertTrue(process_next_conversion()); dataset.conversion.refresh_from_db()
        conversion = dataset.conversion; self.assertEqual(conversion.status, 'review')
        response = self.client.post(f'/api/datasets/{dataset.pk}/conversion/', {'plan_id': conversion.plan['id'], 'decisions': decisions_for(conversion.plan)}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertTrue(process_next_conversion()); conversion.refresh_from_db()
        self.assertEqual(conversion.status, 'complete', conversion.error); self.assertTrue(dataset.package_ready)
        self.assertEqual(conversion.report['metadata']['title_source'], 'none')
        self.assertEqual(conversion.report['metadata']['description_source'], 'none')
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
