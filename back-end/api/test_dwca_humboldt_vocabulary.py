from django.test import SimpleTestCase

from api.dwca_conversion import build_plan, convert
from api.dwca_humboldt import AUDIT, COPIED, DIRECT, ECO, ECOIRI, IRI_SCOPE_TERMS, SCOPE_TERMS, humboldt_targets, is_iri
from api.dwca_import import ImportFailure
from api.test_dwca_humboldt import archive, choices

# hc term_versions.csv at 05ad2bb6, latest recommended versions outside the registered 57-field XML.
LITERAL = ['surveyID', 'surveyTargetID', 'surveyTargetType', 'surveyTargetValue', 'surveyTargetUnit',
           'includeOrExclude', 'isSurveyTargetFullyReported']
IRI = ['absentTaxa', 'compilationSourceTypes', 'compilationTypes', 'eventDurationUnit', 'excludedDegreeOfEstablishmentScope',
       'excludedGrowthFormScope', 'excludedHabitatScope', 'excludedLifeStageScope', 'excludedTaxonomicScope',
       'geospatialScopeAreaUnit', 'inventoryTypes', 'materialSampleTypes', 'nonTargetTaxa', 'protocolNames',
       'samplingEffortProtocol', 'samplingEffortUnit', 'samplingPerformedBy', 'targetDegreeOfEstablishmentScope',
       'targetGrowthFormScope', 'targetHabitatScope', 'targetLifeStageScope', 'targetTaxonomicScope',
       'taxonCompletenessProtocols', 'taxonCompletenessReported', 'totalAreaSampledUnit', 'surveyTargetType',
       'surveyTargetValue', 'surveyTargetUnit']
GBIF_AVES = 'https://www.gbif.org/species/212'


def column(plan, term):
    return next(item for item in plan['columns'] if item['term'] == term)


class VocabularyAuditTests(SimpleTestCase):
    def test_catalogue_covers_exactly_the_seven_literal_and_28_iri_properties(self):
        self.assertEqual(len(IRI), 28)
        self.assertEqual(set(AUDIT), {ECO + name for name in LITERAL} | {ECOIRI + name for name in IRI})
        self.assertFalse(set(AUDIT) & (set(DIRECT) | SCOPE_TERMS))
        for term, entry in AUDIT.items():
            self.assertTrue(entry['prerequisites'] and entry['reason'], term)
            self.assertEqual(entry['namespace'], 'ecoiri' if term.startswith(ECOIRI) else 'eco')

    def test_catalogue_dispositions_match_executable_targets(self):
        for term, entry in AUDIT.items():
            options, reason = humboldt_targets(term, [])
            with self.subTest(term):
                if entry['disposition'] == 'preserved':
                    self.assertEqual(options, [])
                    self.assertEqual(reason, entry['reason'])
                elif entry['disposition'] == 'reviewed-import':
                    self.assertTrue(options and reason)
                else:
                    self.assertEqual((options, reason), (['survey.surveyID'], None))
        # No blanket ecoiri: aliasing to literal fields.
        self.assertEqual({term for term in COPIED if term.startswith(ECOIRI)}, {ECOIRI + 'samplingPerformedBy'})
        self.assertEqual(COPIED[ECOIRI + 'samplingPerformedBy']['name'], 'samplingPerformedByID')

    def test_iri_shape_is_strict(self):
        for value in (GBIF_AVES, 'urn:lsid:marinespecies.org:taxname:1836', 'http://purl.obolibrary.org/obo/ENVO_00000260'):
            self.assertTrue(is_iri(value), value)
        for value in ('Aves', f'{GBIF_AVES} | https://www.gbif.org/species/359', f' {GBIF_AVES}', 'https://x y', ''):
            self.assertFalse(is_iri(value), value)


class SurveyIdTests(SimpleTestCase):
    def test_supplied_survey_id_is_copied_for_its_own_survey(self):
        source = archive([ECO + 'surveyID', ECO + 'siteCount'], [['e1', 'survey-2024-A', '2']])
        plan = build_plan(source)
        self.assertEqual((column(plan, ECO + 'surveyID')['default'], column(plan, ECO + 'surveyID')['review']), ('survey.surveyID', False))
        frames, report = convert(source, plan, choices(plan))
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['survey'].iloc[0]['surveyID'], 'survey-2024-A')
        self.assertNotEqual(frames['survey'].iloc[0]['survey_pk'], 'survey-2024-A')

    def test_repeated_survey_ids_need_merge_or_preservation(self):
        source = archive([ECO + 'surveyID', ECO + 'siteCount'], [['e1', 's1', '2'], ['e1', 's1', '2']])
        plan = build_plan(source)
        self.assertTrue(column(plan, ECO + 'surveyID')['review'])
        decisions = choices(plan)
        with self.assertRaisesMessage(ImportFailure, 'several separate surveys'):
            convert(source, plan, decisions)
        frames, _ = convert(source, plan, {**decisions, 'table:1': 'humboldt-merge'})
        self.assertEqual(frames['survey']['surveyID'].tolist(), ['s1'])
        frames, report = convert(source, plan, {**decisions, column(plan, ECO + 'surveyID')['id']: 'preserve'})
        self.assertNotIn('surveyID', frames['survey'])
        self.assertTrue(report['validation']['valid'])

    def test_bare_survey_id_header_is_not_inferred_from_its_basename(self):
        from api.dwca_import import DWC, read_inputs
        from api.test_dwca_humboldt import table
        source = read_inputs([('event.csv', b'eventID,eventCategory\ne1,survey\n'),
                              ('humboldt.csv', table([DWC + 'eventID', 'surveyID', ECO + 'siteCount'], [['e1', 's1', '2']]))])
        plan = build_plan(source)
        self.assertEqual(column(plan, 'header:surveyID')['default'], 'preserve')


class SurveyTargetPropertyTests(SimpleTestCase):
    def test_survey_target_properties_on_event_rows_are_retained_not_synthesized(self):
        terms = [ECO + name for name in LITERAL[1:]]
        source = archive(terms, [['e1', 't1', 'taxon', 'Aves', '', 'include', 'true']])
        plan = build_plan(source)
        for term in terms:
            item = column(plan, term)
            self.assertEqual(item['default'], 'preserve')
            if item['nonempty']:
                issue = next(issue for issue in [*plan['issues'], *plan.get('automatic_choices', [])] if issue['id'] == item['id'])
                self.assertIn('SurveyTarget', issue['reason'])
        frames, report = convert(source, plan, choices(plan))
        self.assertNotIn('survey-target', frames)
        self.assertNotIn('survey-target-descriptor', frames)
        self.assertEqual(len(frames['survey']), 1)
        self.assertTrue(report['validation']['valid'])


class IriScopeTests(SimpleTestCase):
    def test_iri_only_scope_becomes_iri_descriptors_after_column_review(self):
        source = archive([ECOIRI + 'targetTaxonomicScope', ECOIRI + 'excludedTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'],
                         [['e1', GBIF_AVES, 'https://www.gbif.org/species/1448', 'true']])
        plan = build_plan(source)
        self.assertTrue(column(plan, ECOIRI + 'targetTaxonomicScope')['review'])
        self.assertFalse(any(issue['id'].startswith('hum-scope:') for issue in plan['issues']))
        frames, report = convert(source, plan, choices(plan))
        self.assertTrue(report['validation']['valid'])
        descriptors = frames['survey-target-descriptor']
        self.assertEqual(descriptors['surveyTargetValueIRI'].tolist(), [GBIF_AVES, 'https://www.gbif.org/species/1448'])
        self.assertEqual(descriptors['includeOrExclude'].tolist(), ['include', 'exclude'])
        self.assertNotIn('surveyTargetValue', descriptors)
        self.assertEqual(frames['survey-target'].iloc[0]['isSurveyTargetFullyReported'], 'true')
        preserved = {item['id']: 'preserve' for item in plan['columns'] if item['term'].startswith(ECOIRI)}
        frames, report = convert(source, plan, {**choices(plan), **preserved})
        self.assertNotIn('survey-target', frames)

    def test_iri_scope_without_valid_flag_requires_an_explicit_assertion(self):
        source = archive([ECOIRI + 'targetHabitatScope'], [['e1', 'http://purl.obolibrary.org/obo/ENVO_00000260']])
        plan = build_plan(source)
        issue = next(issue for issue in [*plan['issues'], *plan.get('automatic_choices', [])] if issue['id'] == 'hum-scope:1:0')
        self.assertEqual([option['value'] for option in issue['options']], ['preserve', 'reported-true', 'reported-false'])
        frames, _ = convert(source, plan, choices(plan))
        self.assertNotIn('survey-target', frames)

    def test_mixed_literal_and_iri_scopes_keep_literal_behaviour_and_withhold_iris(self):
        source = archive([ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported', ECOIRI + 'targetTaxonomicScope'],
                         [['e1', 'Aves', 'true', GBIF_AVES]])
        plan = build_plan(source)
        self.assertFalse(any(issue['id'].startswith('hum-scope:') for issue in plan['issues']))
        frames, report = convert(source, plan, choices(plan))
        self.assertEqual(frames['survey-target-descriptor']['surveyTargetValue'].tolist(), ['Aves'])
        self.assertNotIn('surveyTargetValueIRI', frames['survey-target-descriptor'])
        withheld = {item['term']: item['reason'] for item in report['withheld_values']}
        self.assertIn('not paired', withheld[ECOIRI + 'targetTaxonomicScope'])

    def test_iri_lists_and_literals_are_not_split_or_converted(self):
        for value in (f'{GBIF_AVES} | https://www.gbif.org/species/359', 'Aves'):
            source = archive([ECOIRI + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported'], [['e1', value, 'true']])
            plan = build_plan(source)
            issue = next(issue for issue in [*plan['issues'], *plan.get('automatic_choices', [])] if issue['id'] == 'hum-scope:1:0')
            self.assertEqual([option['value'] for option in issue['options']], ['preserve'])
            frames, report = convert(source, plan, choices(plan))
            self.assertNotIn('survey-target', frames)
            self.assertIn(ECOIRI + 'targetTaxonomicScope', {item['term'] for item in report['withheld_values']})

    def test_preserving_literal_column_does_not_make_mixed_source_iri_only(self):
        source = archive([ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported', ECOIRI + 'targetTaxonomicScope'],
                         [['e1', 'Aves', 'true', GBIF_AVES]])
        plan = build_plan(source)
        decisions = {**choices(plan), column(plan, ECO + 'targetTaxonomicScope')['id']: 'preserve'}
        frames, report = convert(source, plan, decisions)
        self.assertNotIn('survey-target', frames)
        self.assertNotIn('survey-target-descriptor', frames)
        withheld = {item['term']: item['reason'] for item in report['withheld_values']}
        self.assertIn('not paired', withheld[ECOIRI + 'targetTaxonomicScope'])
        self.assertTrue(report['validation']['valid'])


class IriFieldTests(SimpleTestCase):
    def test_agent_iri_goes_to_identifier_field_and_literals_are_withheld(self):
        source = archive([ECO + 'samplingPerformedBy', ECOIRI + 'samplingPerformedBy'],
                         [['e1', 'KK Wall', 'https://orcid.org/0000-0002-1825-0097'], ['e1', 'JJ Green', 'JJ Green']])
        plan = build_plan(source)
        self.assertFalse(column(plan, ECOIRI + 'samplingPerformedBy')['review'])
        frames, report = convert(source, plan, choices(plan))
        self.assertEqual(frames['survey']['samplingPerformedBy'].tolist(), ['KK Wall', 'JJ Green'])
        self.assertEqual(frames['survey']['samplingPerformedByID'].tolist(), ['https://orcid.org/0000-0002-1825-0097', ''])
        self.assertEqual([item['source_row'] for item in report['withheld_values'] if item['term'] == ECOIRI + 'samplingPerformedBy'], [2])
        self.assertTrue(report['validation']['valid'])

    def test_iri_units_are_not_literal_units(self):
        source = archive([ECO + 'samplingEffortValue', ECOIRI + 'samplingEffortUnit'], [['e1', '10', 'http://qudt.org/vocab/unit/HR']])
        plan = build_plan(source)
        self.assertEqual(column(plan, ECOIRI + 'samplingEffortUnit')['default'], 'preserve')
        frames, report = convert(source, plan, choices(plan))
        self.assertNotIn('samplingEffortValue', frames['survey'])
        self.assertNotIn('samplingEffortUnit', frames['survey'])
        self.assertEqual({item['term'] for item in report['withheld_values']}, {ECO + 'samplingEffortValue'})

    def test_existing_scope_terms_are_unchanged(self):
        self.assertEqual(len(DIRECT), 43)
        self.assertEqual(len(SCOPE_TERMS), 14)
        self.assertEqual(len(IRI_SCOPE_TERMS), 10)
