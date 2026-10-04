"""Scientific notices must survive source filtering and serialized conversion."""
import copy
import json
import tarfile
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.dwca_conversion import build_plan, convert, validate_decisions
from api.dwca_humboldt import ECO
from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive
from api.test_dwca_humboldt import table


def source(events, surveys=None, core_name='event.csv'):
    files = [(core_name, events)]
    if surveys is not None:
        files.append(('humboldt.csv', surveys))
    return read_inputs(files)


def decisions(plan):
    return {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}


def parent_column(plan):
    return next(column for column in plan['columns'] if column['term'] == DWC + 'parentEventID')


DATES = b'eventID,parentEventID,eventCategory,eventDate\nc,p,survey,2025-06\np,,survey,2024\n'


class ScientificConversionTests(SimpleTestCase):
    def test_temporal_conflict_warns_without_requiring_parent_link_review(self):
        archive = source(DATES)
        plan = build_plan(archive)
        column = parent_column(plan)
        self.assertFalse(column['review'])
        self.assertEqual(plan['scientific_hierarchy']['counts']['contradiction'], 1)
        chosen = decisions(plan)
        self.assertNotIn(column['id'], chosen)
        validate_decisions(plan, chosen)
        frames, report = convert(archive, plan, chosen)
        self.assertTrue(report['validation']['valid'])
        self.assertTrue(frames['event'].iloc[0]['parentEvent_fk'])
        audit = report['event_hierarchy']['scientific_consistency']
        self.assertTrue(audit['has_findings'])
        self.assertFalse(audit['review_required'])
        self.assertTrue(audit['supplied_links_retained'])
        conflict = next(check for check in audit['checks'] if check['status'] == 'contradiction')
        self.assertEqual((conflict['child_eventID'], conflict['parent_eventID']), ('c', 'p'))
        self.assertEqual(audit['events'][conflict['child']]['sources'][0]['source_row'], 1)

    def test_preserving_hierarchy_still_reports_original_scientific_findings(self):
        archive = source(DATES)
        plan = build_plan(archive)
        chosen = decisions(plan)
        chosen[parent_column(plan)['id']] = 'preserve'
        frames, report = convert(archive, plan, chosen)
        self.assertNotIn('parentEvent_fk', frames['event'])
        self.assertEqual(report['event_hierarchy']['linked_events'], 0)
        audit = report['event_hierarchy']['scientific_consistency']
        self.assertEqual(audit['decision'], 'preserve')
        self.assertFalse(audit['supplied_links_retained'])
        self.assertEqual(audit['counts']['contradiction'], 1)

    def test_preserved_date_column_cannot_hide_source_conflict(self):
        archive = source(DATES)
        plan = build_plan(archive)
        chosen = decisions(plan)
        chosen[next(c['id'] for c in plan['columns'] if c['term'] == DWC + 'eventDate')] = 'preserve'
        frames, report = convert(archive, plan, chosen)
        self.assertNotIn('eventDate', frames['event'])
        self.assertEqual(report['event_hierarchy']['scientific_consistency']['counts']['contradiction'], 1)

    def test_scope_flags_are_not_inherited_or_fixed_when_conflict_is_reported(self):
        archive = source(b'eventID,parentEventID,eventCategory\np,,survey\nc,p,survey\n', table(
            [DWC + 'eventID', ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'],
            [['p', 'Aves', 'true'], ['c', 'Aves', 'FALSE']]))
        plan = build_plan(archive)
        self.assertTrue(plan['scientific_hierarchy']['has_findings'])
        self.assertFalse(plan['scientific_hierarchy']['review_required'])
        frames, report = convert(archive, plan, decisions(plan))
        self.assertEqual(frames['survey-target']['isSurveyTargetFullyReported'].tolist(), ['true', 'FALSE'])
        self.assertNotIn('occurrence', frames)
        chosen = decisions(plan)
        for column in plan['columns']:
            if column['term'] == ECO + 'targetTaxonomicScope':
                chosen[column['id']] = 'preserve'
        _, filtered = convert(archive, plan, chosen)
        self.assertEqual(filtered['event_hierarchy']['scientific_consistency']['checks'],
                         report['event_hierarchy']['scientific_consistency']['checks'])

    def test_parent_incomplete_child_complete_is_allowed(self):
        archive = source(b'eventID,parentEventID,eventCategory\np,,survey\nc,p,survey\n', table(
            [DWC + 'eventID', ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'],
            [['p', 'Aves', 'false'], ['c', 'Aves', 'true']]))
        plan = build_plan(archive)
        self.assertFalse(plan['scientific_hierarchy']['review_required'])
        self.assertFalse(parent_column(plan)['review'])

    def test_multiple_surveys_warn_without_selecting_an_arbitrary_row(self):
        archive = source(b'eventID,parentEventID,eventCategory\np,,survey\nc,p,survey\n', table(
            [DWC + 'eventID', ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'],
            [['p', 'Aves', 'true'], ['p', 'Mammalia', 'true'], ['c', 'Aves', 'true']]))
        plan = build_plan(archive)
        self.assertFalse(parent_column(plan)['review'])
        self.assertTrue(plan['scientific_hierarchy']['has_findings'])
        _, report = convert(archive, plan, decisions(plan))
        checks = report['event_hierarchy']['scientific_consistency']['checks']
        self.assertTrue(any(check['status'] == 'incomparable' and check['kind'] == 'survey-ambiguous' for check in checks))

    def test_occurrence_group_source_generalization_ambiguity_prevents_spatial_claim(self):
        archive = source(table(
            ['occurrenceID', 'eventID', 'parentEventID', 'occurrenceStatus', 'decimalLatitude',
             'decimalLongitude', 'coordinateUncertaintyInMeters', 'geodeticDatum', 'dataGeneralizations'],
            [['o1', 'p', '', 'present', '0', '0', '10', 'WGS84', ''],
             ['o2', 'c', 'p', 'present', '50', '50', '10', 'WGS84', ''],
             ['o3', 'c', 'p', 'present', '50', '50', '10', 'WGS84', 'coordinates generalized']]), core_name='occurrence.csv')
        plan = build_plan(archive)
        self.assertTrue(plan['scientific_hierarchy']['has_findings'])
        self.assertNotIn('contradiction', plan['scientific_hierarchy']['counts'])
        self.assertTrue(any(check['kind'] == 'event-source-ambiguity'
                            for check in plan['scientific_hierarchy']['finding_sample']))
        chosen = decisions(plan)
        chosen['event-grain'] = 'by_id'
        chosen[parent_column(plan)['id']] = 'parent-link'
        generalized = next(c for c in plan['columns'] if c['term'] == DWC + 'dataGeneralizations')
        chosen[generalized['id']] = 'preserve'
        _, report = convert(archive, plan, chosen)
        check = next(c for c in report['event_hierarchy']['scientific_consistency']['checks'] if c['kind'] == 'spatial')
        self.assertEqual(check['status'], 'incomparable')
        self.assertEqual(check['evidence']['child'][DWC + 'decimalLatitude'], '50')
        self.assertEqual(check['evidence']['group_ambiguities'][0]['fields'][0]['values'], ['', 'coordinates generalized'])

    def test_remote_ancestor_identity_is_reported_with_its_own_source_row(self):
        archive = source(b'eventID,parentEventID,eventCategory,eventDate\n'
                         b'c,p,survey,2018-12\np,g,survey,2018\ng,,survey,2018-01-01/2018-06-30\n')
        plan = build_plan(archive)
        _, report = convert(archive, plan, decisions(plan))
        audit = report['event_hierarchy']['scientific_consistency']
        conflict = next(check for check in audit['checks'] if check['kind'] == 'temporal-ancestor')
        self.assertEqual((conflict['parent_eventID'], conflict['ancestor_eventID']), ('p', 'g'))
        self.assertEqual(audit['events'][conflict['ancestor']]['sources'][0]['source_row'], 3)

    def test_invalid_structure_never_gains_a_scientific_override(self):
        archive = source(b'eventID,parentEventID,eventCategory,eventDate\nc,missing,survey,2025\np,,survey,2024\n')
        plan = build_plan(archive)
        self.assertNotIn('scientific_hierarchy', plan)
        chosen = decisions(plan)
        chosen[parent_column(plan)['id']] = 'parent-link'
        with self.assertRaisesMessage(ImportFailure, 'cannot be linked'):
            convert(archive, plan, chosen)

    def test_report_and_originals_survive_serialized_package_validation(self):
        archive = source(DATES)
        plan = build_plan(archive)
        self.assertEqual(plan['id'], build_plan(archive)['id'])
        frames, report = convert(archive, plan, decisions(plan))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'scientific.tar.gz'
            create_dwc_dp_archive(output, frames, title='Scientific review', description='Explicit simulated review',
                include_eml=False, additional_files=[('conversion-report.json', json.dumps(report).encode()),
                    ('source-originals.zip', source_zip(archive.files))], declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])
            with tarfile.open(output) as package:
                member = next(item for item in package.getmembers() if item.name.endswith('conversion-report.json'))
                self.assertEqual(json.load(package.extractfile(member))['event_hierarchy']['scientific_consistency'],
                                 report['event_hierarchy']['scientific_consistency'])
        old_plan = copy.deepcopy(plan)
        old_plan['id'] = 'old-rules'
        with self.assertRaisesMessage(ImportFailure, 'mapping rules'):
            convert(archive, old_plan, decisions(plan))
