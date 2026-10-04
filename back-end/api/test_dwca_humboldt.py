import csv
import io
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.dwca_conversion import build_plan, convert
from api.dwca_humboldt import DIRECT, ECO, SCOPE_TERMS
from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive


def table(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream); writer.writerow(headers); writer.writerows(rows)
    return stream.getvalue().encode()


def archive(headers, rows, core=None):
    core = core or ('event.csv', b'eventID,eventCategory\ne1,survey\n')
    join = 'eventID' if core[0] == 'event.csv' else 'occurrenceID'
    return read_inputs([core, ('humboldt.csv', table([DWC + join, *headers], rows))])


def choices(plan):
    return {item['id']: item['options'][0]['value'] for item in plan['issues']}


class HumboldtConversionTests(SimpleTestCase):
    def test_all_57_terms_have_declared_direct_or_scope_paths(self):
        self.assertEqual(len(DIRECT), 43)
        self.assertEqual(len(SCOPE_TERMS), 14)

    def test_valid_fields_and_single_dimension_scope_copy_without_extra_review(self):
        source = archive([ECO + 'siteCount', ECO + 'samplingEffortValue', ECO + 'samplingEffortUnit',
                          ECO + 'targetTaxonomicScope', ECO + 'excludedTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'],
                         [['e1', '3', '5e1', 'observerMinutes', 'Aves', 'Columba livia', 'TRUE']])
        plan = build_plan(source)
        self.assertFalse(any(item['id'].startswith('hum-scope:') for item in plan['issues']))
        frames, report = convert(source, plan, choices(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['survey'].iloc[0]['siteCount'], '3')
        self.assertEqual(frames['survey'].iloc[0]['samplingEffortValue'], '5e1')
        self.assertEqual(frames['survey-target'].iloc[0]['isSurveyTargetFullyReported'], 'TRUE')
        self.assertEqual(frames['survey-target-descriptor']['includeOrExclude'].tolist(), ['include', 'exclude'])
        self.assertNotIn('samplingEffortProtocol_fk', frames['survey'])
        self.assertNotIn('protocol', frames)
        self.assertEqual(report['withheld_values'], [])

    def test_bad_typed_values_are_withheld_per_row_and_good_values_survive(self):
        source = archive([ECO + 'siteCount', ECO + 'isAbsenceReported'],
                         [['e1', '3', 'true'], ['e1', '3.0', 'yes'], ['e1', '0', 'false']])
        plan = build_plan(source); frames, report = convert(source, plan, choices(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['survey']['siteCount'].tolist(), ['3', '', ''])
        self.assertEqual(frames['survey']['isAbsenceReported'].tolist(), ['true', '', 'false'])
        self.assertEqual(len(report['withheld_values']), 3)
        column = next(c for c in report['columns'] if c['term'] == ECO + 'siteCount')
        self.assertEqual((column['mapped_rows'], column['retained_only_rows']), (1, 2))

    def test_unit_and_flag_contradictions_withhold_only_affected_values(self):
        source = archive([ECO + 'eventDurationValue', ECO + 'hasVouchers', ECO + 'voucherInstitutions', ECO + 'reportedWeather'],
                         [['e1', '2', 'false', 'Museum', 'Rain']])
        plan = build_plan(source); frames, report = convert(source, plan, choices(plan))
        self.assertEqual(frames['survey'].iloc[0]['reportedWeather'], 'Rain')
        self.assertNotIn('eventDurationValue', frames['survey'])
        self.assertNotIn('hasVouchers', frames['survey'])
        self.assertNotIn('voucherInstitutions', frames['survey'])
        self.assertEqual(len(report['withheld_values']), 3)
        self.assertTrue(report['validation']['valid'])

    def test_habitat_requires_reviewed_completeness_never_defaults_it(self):
        source = archive([ECO + 'targetHabitatScope'], [['e1', 'oak savannah']])
        plan = build_plan(source); decisions = choices(plan)
        frames, report = convert(source, plan, decisions)
        self.assertNotIn('survey-target', frames)
        self.assertEqual(report['columns'][-1]['disposition'], 'retained-unmapped')
        decisions['hum-scope:1:0'] = 'reported-false'
        frames, report = convert(source, plan, decisions)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['survey-target'].iloc[0]['isSurveyTargetFullyReported'], 'false')
        self.assertEqual(frames['survey-target-descriptor'].iloc[0]['surveyTargetType'], 'habitat')

    def test_combined_scope_records_an_explicit_assertion_and_keeps_source_flags(self):
        source = archive([ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported',
                          ECO + 'targetLifeStageScope', ECO + 'isLifeStageScopeFullyReported'],
                         [['e1', 'Aves', 'true', 'adult', 'false']])
        plan = build_plan(source); decisions = choices(plan); decisions['hum-scope:1:0'] = 'reported-false'
        frames, report = convert(source, plan, decisions)
        self.assertEqual(len(frames['survey-target']), 1)
        self.assertEqual(frames['survey-target-descriptor']['surveyTargetType'].tolist(), ['taxon', 'lifeStage'])
        self.assertEqual(len(report['withheld_values']), 2)
        self.assertNotIn('occurrence', frames)
        self.assertTrue(report['validation']['valid'])

    def test_bad_scope_lists_cannot_be_forced_through_a_conversion_option(self):
        for value in ('Aves | ', 'Aves | Aves', ' Aves'):
            source = archive([ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'], [['e1', value, 'true']])
            plan = build_plan(source)
            issue = next(i for i in [*plan['issues'], *plan.get('automatic_choices', [])] if i['id'] == 'hum-scope:1:0')
            self.assertEqual([o['value'] for o in issue['options']], ['preserve'])
            frames, report = convert(source, plan, choices(plan))
            self.assertNotIn('survey-target', frames)
            self.assertTrue(report['withheld_values'])

    def test_duplicate_source_rows_preserve_multiplicity_unless_merge_is_selected(self):
        source = archive([ECO + 'siteCount'], [['e1', '3'], ['e1', '3']])
        plan = build_plan(source); decisions = choices(plan)
        frames, report = convert(source, plan, decisions)
        self.assertEqual(len(frames['survey']), 2)
        decisions['table:1'] = 'humboldt-merge'
        frames, report = convert(source, plan, decisions)
        self.assertEqual(len(frames['survey']), 1)
        self.assertEqual(len([r for r in report['row_crosswalk'] if r['target_table'] == 'survey']), 2)

    def test_conflicting_surveys_cannot_merge(self):
        source = archive([ECO + 'siteCount'], [['e1', '3'], ['e1', '4']])
        plan = build_plan(source); decisions = choices(plan); decisions['table:1'] = 'humboldt-merge'
        with self.assertRaisesMessage(ImportFailure, 'identical source values'):
            convert(source, plan, decisions)

    def test_occurrence_subject_requires_complete_identical_grouping_and_category_review(self):
        core = ('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus\no1,e1,present\no2,e1,present\n')
        source = archive([ECO + 'siteCount'], [['o1', '3'], ['o2', '3']], core)
        plan = build_plan(source); decisions = choices(plan); decisions['event-grain'] = 'by_id'
        with self.assertRaisesMessage(ImportFailure, 'reviewed survey events'):
            convert(source, plan, decisions)
        decisions['hum-category:1'] = 'confirm'
        frames, report = convert(source, plan, decisions)
        self.assertEqual(len(frames['survey']), 1)
        self.assertEqual(frames['event'].iloc[0]['eventCategory'], 'survey')
        self.assertNotIn('surveyTarget_fk', frames['occurrence'])
        self.assertTrue(report['validation']['valid'])
        partial = archive([ECO + 'siteCount'], [['o1', '3']], core)
        partial_plan = build_plan(partial); partial_choices = choices(partial_plan)
        partial_choices.update({'event-grain': 'by_id', 'hum-category:1': 'confirm'})
        with self.assertRaisesMessage(ImportFailure, 'complete identical coverage'):
            convert(partial, partial_plan, partial_choices)

    def test_supplied_non_survey_category_cannot_be_overwritten(self):
        source = archive([ECO + 'siteCount'], [['e1', '3']], ('event.csv', b'eventID,eventCategory\ne1,occurrence\n'))
        plan = build_plan(source); decisions = choices(plan)
        frames, report = convert(source, plan, decisions)
        self.assertNotIn('survey', frames)
        self.assertEqual(frames['event'].iloc[0]['eventCategory'], 'occurrence')
        self.assertTrue(any('cannot be overwritten' in item['reason'] for item in report['warnings']))
        with self.assertRaisesMessage(ImportFailure, 'cannot be overwritten'):
            convert(source, plan, {**decisions, 'table:1': 'humboldt-survey'})

    def test_serialized_survey_package_has_valid_targets_and_retains_sources(self):
        source = archive([ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'], [['e1', 'Aves', 'true']])
        plan = build_plan(source); frames, report = convert(source, plan, choices(plan))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'converted.tar.gz'
            create_dwc_dp_archive(output, frames, title='Survey conversion', description='Reviewed survey', include_eml=False,
                additional_files=[('source-originals.zip', source_zip(source.files)), ('conversion-report.json', json.dumps(report).encode())],
                declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])
