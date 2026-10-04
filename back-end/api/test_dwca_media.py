import csv
import io
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.dwca_conversion import build_plan, convert
from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.dwca_media import AC, DC, DCT, media_targets
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive


def tabular(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(headers); writer.writerows(rows)
    return stream.getvalue().encode()


def choices(plan):
    return {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}


def media_archive(headers, rows, core=None, name='audubon.csv'):
    core = core or ('occurrence.csv', b'occurrenceID,occurrenceStatus\no1,present\n')
    join = 'eventID' if core[0] == 'event.csv' else 'occurrenceID'
    return read_inputs([core, (name, tabular([DWC + join, *headers], rows))])


class MediaConversionTests(SimpleTestCase):
    def test_identifier_maps_only_to_media_identity_and_preserves_duplicate_ids(self):
        archive = media_archive([DCT + 'identifier', DCT + 'title'],
                                [['o1', 'urn:media:one', 'First'], ['o1', 'urn:media:one', 'Second']])
        plan = build_plan(archive)
        column = next(c for c in plan['columns'] if c['term'] == DCT + 'identifier')
        self.assertEqual([o['value'] for o in column['options']], ['media.mediaID', 'preserve'])
        frames, report = convert(archive, plan, choices(plan))
        self.assertEqual(len(frames['media']), 2)
        self.assertEqual(frames['media']['media_pk'].nunique(), 2)
        self.assertEqual(frames['media']['mediaID'].tolist(), ['urn:media:one'] * 2)
        self.assertNotIn('derivedFromMediaID', frames['media'])
        self.assertNotIn('usage-policy', frames)
        self.assertTrue(report['validation']['valid'])

    def test_shared_rights_and_provenance_combine_only_exact_descriptions(self):
        archive = media_archive([DCT + 'identifier', DCT + 'title', DC + 'rights', DC + 'creator'],
                                [['o1', 'urn:m:1', 'One', 'Rights A', 'A person'],
                                 ['o1', 'urn:m:2', 'Two', 'Rights A', 'A person'],
                                 ['o1', 'urn:m:3', 'Three', 'Rights B', 'A person']])
        plan = build_plan(archive); frames, report = convert(archive, plan, choices(plan))
        self.assertEqual(len(frames['media']), 3)
        self.assertEqual(len(frames['usage-policy']), 2)
        self.assertEqual(len(frames['provenance']), 1)
        self.assertNotIn('creatorID', frames['provenance'])
        self.assertNotIn('agent', frames)
        self.assertEqual(set(frames['occurrence-media']['occurrence_fk']), set(frames['occurrence']['occurrence_pk']))
        self.assertEqual(len([r for r in report['row_crosswalk'] if r['target_table'] == 'provenance']), 3)
        self.assertTrue(report['validation']['valid'])

    def test_simple_multimedia_event_links_and_literal_aliases_are_reviewed(self):
        archive = media_archive([DCT + 'identifier', DCT + 'format', DCT + 'creator', DCT + 'source'],
                                [['e1', 'https://example.org/image.jpg', 'image/jpeg', 'Photographer', 'A collection']],
                                core=('event.csv', b'eventID,eventCategory\ne1,survey\n'), name='multimedia.csv')
        plan = build_plan(archive)
        fields = {c['term']: c for c in plan['columns'] if c['table'] == 1}
        for term in (DCT + 'format', DCT + 'creator', DCT + 'source'):
            self.assertTrue(fields[term]['review'])
        frames, report = convert(archive, plan, choices(plan))
        self.assertEqual(frames['media'].iloc[0]['format'], 'image/jpeg')
        self.assertNotIn('formatIRI', frames['media'])
        self.assertEqual(frames['provenance'].iloc[0]['creator'], 'Photographer')
        self.assertNotIn('occurrence', frames)
        self.assertEqual(frames['event-media'].iloc[0]['event_fk'], frames['event'].iloc[0]['event_pk'])
        self.assertEqual(report['media_subjects'][0]['subject_basis'], 'reviewed_core_attachment')
        self.assertTrue(report['validation']['valid'])

    def test_media_content_and_specimen_signals_never_create_scientific_records(self):
        archive = media_archive([DCT + 'identifier', DWC + 'scientificName', DWC + 'decimalLatitude', AC + 'associatedSpecimenReference'],
                                [['o1', 'urn:m:1', 'Panthera leo', '-20', 'urn:specimen:1']])
        plan = build_plan(archive); decisions = choices(plan)
        issue = next(i for i in plan['issues'] if i['id'] == 'table:1')
        self.assertEqual(issue['subject_review_signals'], [AC + 'associatedSpecimenReference'])
        decisions['table:1'] = 'media-unlinked'
        frames, report = convert(archive, plan, decisions)
        self.assertNotIn('identification', frames)
        self.assertNotIn('material', frames)
        self.assertNotIn('occurrence-media', frames)
        self.assertNotIn('event-media', frames)
        self.assertNotIn('decimalLatitude', frames['event'])
        self.assertEqual(report['media_subjects'][0]['subject_basis'], 'explicitly_unlinked')
        retained = {c['term'] for c in report['columns'] if c['source_table'] == 'audubon.csv' and c['disposition'] == 'retained-unmapped'}
        self.assertTrue({DWC + 'scientificName', DWC + 'decimalLatitude', AC + 'associatedSpecimenReference'} <= retained)

    def test_mixed_iris_and_literals_or_generic_agent_ids_have_no_mapping(self):
        for term in (DCT + 'format', DCT + 'creator', DCT + 'source', DCT + 'rights'):
            with self.subTest(term=term):
                targets, reason = media_targets(term, ['urn:value:one', 'a literal'])
                self.assertEqual(targets, [])
                self.assertTrue(reason)
        self.assertEqual(media_targets(DWC + 'agentID', ['urn:agent:one'])[0], [])
        self.assertEqual(media_targets(AC + 'derivedFrom', ['a parent photograph'])[0], [])
        self.assertEqual(media_targets(AC + 'hasROI', ['urn:child:one'])[0], [])

    def test_conflicting_alias_columns_cannot_silently_overwrite(self):
        archive = media_archive([DCT + 'identifier', DCT + 'type', DC + 'type'], [['o1', 'urn:m:1', 'StillImage', 'Sound']])
        plan = build_plan(archive); decisions = choices(plan)
        with self.assertRaisesMessage(ImportFailure, 'supply different values for media.mediaType in 1 rows'):
            convert(archive, plan, decisions)
        alias = next(c for c in plan['columns'] if c['term'] == DC + 'type')
        decisions[alias['id']] = 'preserve'
        frames, report = convert(archive, plan, decisions)
        self.assertEqual(frames['media'].iloc[0]['mediaType'], 'StillImage')
        self.assertTrue(report['validation']['valid'])

    def test_media_without_mapped_values_is_preserved_instead_of_invented(self):
        archive = media_archive([DWC + 'scientificName'], [['o1', 'Panthera leo']])
        plan = build_plan(archive); decisions = choices(plan)
        table = next(item for item in [*plan['issues'], *plan['automatic_choices']] if item['id'] == 'table:1')
        self.assertTrue({option['value'] for option in table['unavailable_options']} >= {'media-occurrence', 'media-event', 'media-unlinked'})
        self.assertEqual(table.get('default'), 'preserve')
        frames, report = convert(archive, plan, decisions)
        self.assertNotIn('media', frames)

    def test_serialized_media_package_keeps_sources_and_all_relationships(self):
        archive = media_archive([DCT + 'identifier', DC + 'rights', DC + 'creator'], [['o1', 'urn:m:1', 'Rights', 'Creator']], name='images.csv')
        plan = build_plan(archive); frames, report = convert(archive, plan, choices(plan))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'converted.tar.gz'
            create_dwc_dp_archive(output, frames, title='Media conversion', description='Reviewed media', include_eml=False,
                                 additional_files=[('source-originals.zip', source_zip(archive.files)),
                                                   ('conversion-report.json', json.dumps(report).encode())],
                                 declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])
        again, again_report = convert(archive, plan, choices(plan))
        for name in frames:
            self.assertEqual(frames[name].to_dict('records'), again[name].to_dict('records'))
        self.assertEqual(report['media_subjects'], again_report['media_subjects'])

    def test_manifest_media_joins_internal_core_key_not_persistent_identifier(self):
        meta = ('<archive xmlns="http://rs.tdwg.org/dwc/text/">'
                '<core rowType="' + DWC + 'Occurrence" fieldsTerminatedBy="," ignoreHeaderLines="1">'
                '<files><location>occ.csv</location></files><id index="0"/>'
                '<field index="1" term="' + DWC + 'occurrenceID"/>'
                '<field term="' + DWC + 'occurrenceStatus" default="present"/></core>'
                '<extension rowType="' + AC + 'Multimedia" fieldsTerminatedBy="," ignoreHeaderLines="1">'
                '<files><location>media.csv</location></files><coreid index="0"/>'
                '<field index="1" term="' + DCT + 'identifier"/></extension></archive>').encode()
        archive = read_inputs([('meta.xml', meta), ('occ.csv', b'key,persistent\njoin-1,urn:occurrence:1\n'),
                               ('media.csv', b'coreid,identifier\njoin-1,urn:media:1\n')])
        plan = build_plan(archive); frames, report = convert(archive, plan, choices(plan))
        self.assertEqual(frames['occurrence'].iloc[0]['occurrenceID'], 'urn:occurrence:1')
        self.assertEqual(frames['media'].iloc[0]['mediaID'], 'urn:media:1')
        self.assertEqual(frames['occurrence-media'].iloc[0]['occurrence_fk'], frames['occurrence'].iloc[0]['occurrence_pk'])
        self.assertTrue(report['validation']['valid'])

    def test_out_of_range_media_measurement_warns_and_stays_in_originals(self):
        archive = media_archive([DCT + 'identifier', AC + 'xFrac'], [['o1', 'urn:m:1', '1.5']])
        plan = build_plan(archive); frames, report = convert(archive, plan, choices(plan))
        column = next(c for c in plan['columns'] if c['term'] == AC + 'xFrac')
        self.assertFalse(column['review'])
        self.assertTrue(any(w['id'] == column['id'] for w in plan['warnings']))
        self.assertTrue(report['validation']['valid'])
        self.assertNotIn('xFrac', frames['media'])
        self.assertEqual(report['withheld_values'][0]['value'], '1.5')
        self.assertEqual(next(c for c in report['columns'] if c['term'] == AC + 'xFrac')['disposition'], 'retained-unmapped')
