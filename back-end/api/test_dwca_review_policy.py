"""Review is for interpretation; source copies and safe retention are automatic."""
from django.test import SimpleTestCase

from api.dwca_conversion import build_plan, convert, validate_decisions
from api.dwca_humboldt import ECO, ECOIRI
from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.test_dwca_conversion import manifest
from api.test_dwca_hierarchy import meta, nested_files
from api.test_dwca_humboldt import table


def choices(plan):
    return {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}


def column(plan, term):
    return next(item for item in plan['columns'] if item['term'] == term)


class StreamlinedReviewTests(SimpleTestCase):
    def test_declared_occurrence_archive_converts_without_a_review_decision(self):
        fields = ''.join(f'<field index="{i}" term="{DWC}{term}"/>' for i, term in enumerate(
            ['identifiedBy', 'identificationRemarks', 'habitat', 'organismQuantity', 'organismQuantityType', 'organismID'], 2))
        data = b'key,occurrenceID,identifiedBy,identificationRemarks,habitat,organismQuantity,organismQuantityType,organismID\nk,o,A collector,As supplied,forest,15,reads,organism-1\n'
        archive = read_inputs([('meta.xml', manifest(fields)), ('occ.csv', data)])
        plan = build_plan(archive)
        self.assertEqual(plan['issues'], [])
        frames, report = convert(archive, plan, {})
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['occurrence'].iloc[0]['identifiedBy'], 'A collector')
        self.assertEqual(frames['occurrence'].iloc[0]['organismQuantityType'], 'reads')
        self.assertEqual(frames['event'].iloc[0]['habitat'], 'forest')
        self.assertNotIn('identification', frames)
        self.assertNotIn('organism', frames)
        self.assertEqual(report['decisions'], {})
        self.assertEqual(report['effective_decisions']['event-grain'], 'per_row')
        self.assertEqual(archive.files['occ.csv'], data)

    def test_unknown_columns_are_retained_without_an_unanswerable_prompt(self):
        archive = read_inputs([('meta.xml', manifest('<field index="2" term="urn:unknown:term"/>')),
                               ('occ.csv', b'key,occurrenceID,unknown\nk,o,untouched\n')])
        plan = build_plan(archive)
        self.assertEqual(plan['issues'], [])
        frames, report = convert(archive, plan, {})
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(report['columns'][-1]['disposition'], 'retained-unmapped')
        self.assertTrue(any(warning['id'] == column(plan, 'urn:unknown:term')['id'] for warning in report['warnings']))
        self.assertEqual(archive.files['occ.csv'], b'key,occurrenceID,unknown\nk,o,untouched\n')

    def test_nested_surveys_preserve_flags_and_links_with_no_user_choices(self):
        archive = read_inputs([('nested.zip', source_zip(nested_files()))])
        plan = build_plan(archive)
        self.assertEqual(plan['issues'], [])
        frames, report = convert(archive, plan, {})
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(report['event_hierarchy']['linked_events'], 3)
        self.assertTrue(report['event_hierarchy']['scientific_consistency']['has_findings'])
        self.assertEqual(frames['survey-target']['isSurveyTargetFullyReported'].tolist(), ['true', 'true', 'true', 'false'])
        self.assertEqual(report['decisions'], {})

    def test_automatic_extension_role_can_be_overridden_by_preservation(self):
        archive = read_inputs([('nested.zip', source_zip(nested_files()))])
        plan = build_plan(archive)
        index = next(i for i, table_profile in enumerate(plan['tables']) if table_profile['row_type'] == ECO + 'Event')
        frames, report = convert(archive, plan, {f'table:{index}': 'preserve'})
        self.assertNotIn('survey', frames)
        self.assertEqual(report['decisions'], {f'table:{index}': 'preserve'})
        self.assertTrue(all(item['disposition'] == 'retained-unmapped' for item in report['columns'] if item['source_table'] == 'humboldt.csv'))
        self.assertTrue(report['event_hierarchy']['scientific_consistency']['has_findings'])

    def test_automatic_retention_suppresses_decisions_for_the_retained_table(self):
        extension = '<extension rowType="' + DWC + 'Identification" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>id.csv</location></files><coreid index="0"/><field index="1" term="' + DWC + 'scientificName"/></extension>'
        archive = read_inputs([('meta.xml', meta([DWC + 'eventID', DWC + 'eventCategory']).replace(b'</archive>', extension.encode() + b'</archive>')),
            ('event.csv', b'key,eventID,eventCategory\nk,e,survey\n'), ('id.csv', b'key,name\nk,Apus apus (Linnaeus)\n')])
        plan = build_plan(archive)
        validate_decisions(plan, {})
        frames, report = convert(archive, plan, {})
        self.assertNotIn('identification', frames)
        self.assertTrue(report['validation']['valid'])

    def test_missing_status_and_zero_quantity_are_never_resolved_automatically(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,organismQuantity,organismQuantityType\no,0,reads\n')])
        plan = build_plan(archive)
        status = next(issue for issue in plan['issues'] if issue['id'] == 'status:0')
        self.assertIn('1 rows', status['reason'])
        self.assertEqual({option['value'] for option in status['options']}, {'present', 'absent'})
        chosen = choices(plan)
        chosen.pop('status:0')
        with self.assertRaisesMessage(ImportFailure, 'Resolve'):
            convert(archive, plan, chosen)
        chosen['status:0'] = 'absent'
        frames, _ = convert(archive, plan, chosen)
        self.assertEqual(frames['occurrence'].iloc[0]['occurrenceStatus'], 'absent')
        self.assertEqual(frames['occurrence'].iloc[0]['organismQuantity'], '0')

    def test_event_grouping_and_missing_category_stay_explicit(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus\na,e,present\nb,e,present\n')])
        plan = build_plan(archive)
        self.assertIn('event-grain', {issue['id'] for issue in plan['issues']})
        event = read_inputs([('event.csv', b'eventID\ne\n')])
        self.assertIn('event-category', {issue['id'] for issue in build_plan(event)['issues']})

    def test_unique_occurrence_event_ids_retain_parent_links_without_merging(self):
        fields = ''.join(f'<field index="{i}" term="{DWC}{term}"/>' for i, term in enumerate(['eventID', 'parentEventID'], 2))
        archive = read_inputs([('meta.xml', manifest(fields)),
            ('occ.csv', b'key,occurrenceID,eventID,parentEventID\nk1,o1,p,\nk2,o2,c,p\n')])
        plan = build_plan(archive)
        self.assertEqual(plan['issues'], [])
        frames, report = convert(archive, plan, {})
        self.assertEqual(len(frames['event']), 2)
        self.assertEqual(report['event_hierarchy']['linked_events'], 1)
        self.assertEqual(report['effective_decisions']['event-grain'], 'by_id')

    def test_incomplete_occurrence_event_ids_never_offer_impossible_grouped_surveys(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus\no1,e,present\no2,,present\n'),
            ('humboldt.csv', table([DWC + 'occurrenceID', ECO + 'siteCount'], [['o1', '2']]))])
        plan = build_plan(archive)
        self.assertNotIn('event-grain', {issue['id'] for issue in plan['issues']})
        role = next(item for item in plan['automatic_choices'] if item['id'] == 'table:1')
        self.assertEqual(role['default'], 'preserve')
        frames, _ = convert(archive, plan, choices(plan))
        self.assertEqual(len(frames['event']), 2)
        self.assertNotIn('survey', frames)

    def test_missing_and_repeated_ids_keep_the_context_split_explicit(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus\na,e,present\nb,e,present\nc,,present\n')])
        plan = build_plan(archive)
        grain = next(issue for issue in plan['issues'] if issue['id'] == 'event-grain')
        self.assertEqual([option['value'] for option in grain['options']], ['per_row'])
        chosen = choices(plan)
        chosen.pop('event-grain')
        with self.assertRaisesMessage(ImportFailure, 'Resolve'):
            convert(archive, plan, chosen)
        chosen['event-grain'] = 'per_row'
        chosen[column(plan, DWC + 'eventID')['id']] = 'preserve'
        frames, _ = convert(archive, plan, chosen)
        self.assertEqual(len(frames['event']), 3)
        self.assertNotIn('eventID', frames['event'])

    def test_non_survey_category_choice_is_rejected_before_the_conversion_job(self):
        archive = read_inputs([('event.csv', b'eventID\ne\n'),
            ('humboldt.csv', table([DWC + 'eventID', ECO + 'siteCount'], [['e', '2']]))])
        plan = build_plan(archive)
        chosen = choices(plan)
        chosen['event-category'] = 'occurrence'
        with self.assertRaisesMessage(ImportFailure, 'Choose survey'):
            validate_decisions(plan, chosen)
        chosen['table:1'] = 'preserve'
        validate_decisions(plan, chosen)
        frames, _ = convert(archive, plan, chosen)
        self.assertNotIn('survey', frames)

    def test_missing_unlinked_category_does_not_block_already_classified_surveys(self):
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne,survey\nunlinked,\n'),
            ('humboldt.csv', table([DWC + 'eventID', ECO + 'siteCount'], [['e', '2']]))])
        plan = build_plan(archive)
        chosen = {**choices(plan), 'event-category': 'occurrence'}
        validate_decisions(plan, chosen)
        frames, _ = convert(archive, plan, chosen)
        self.assertEqual(frames['event']['eventCategory'].tolist(), ['survey', 'occurrence'])
        self.assertEqual(len(frames['survey']), 1)

    def test_combined_scope_completeness_stays_explicit_after_automatic_role(self):
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne,survey\n'), ('humboldt.csv', table(
            [DWC + 'eventID', ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported',
             ECO + 'targetHabitatScope', ECO + 'isHabitatScopeFullyReported'], [['e', 'Aves', 'true', 'forest', 'false']]))])
        plan = build_plan(archive)
        scope = next(issue for issue in plan['issues'] if issue['id'].startswith('hum-scope:'))
        chosen = choices(plan)
        chosen.pop(scope['id'])
        with self.assertRaisesMessage(ImportFailure, 'Resolve'):
            convert(archive, plan, chosen)
        frames, _ = convert(archive, plan, {**chosen, scope['id']: 'reported-false'})
        self.assertEqual(frames['survey-target'].iloc[0]['isSurveyTargetFullyReported'], 'false')

    def test_iri_sampling_agent_keeps_identifier_role_without_extra_confirmation(self):
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne,survey\n'),
            ('humboldt.csv', table([DWC + 'eventID', ECOIRI + 'samplingPerformedBy'], [['e', 'https://orcid.org/0000-0001-2345-6789']]))])
        plan = build_plan(archive)
        self.assertFalse(column(plan, ECOIRI + 'samplingPerformedBy')['review'])
        frames, _ = convert(archive, plan, choices(plan))
        self.assertEqual(frames['survey'].iloc[0]['samplingPerformedByID'], 'https://orcid.org/0000-0001-2345-6789')
        self.assertNotIn('samplingPerformedBy', frames['survey'])

    def test_rank_subgenus_hybrid_and_cultivar_names_copy_without_repair(self):
        names = ['Aves', 'Aus (Bus) cus', 'Salix alba × Salix fragilis', '×Triticosecale',
                 'Bellis perennis subsp. minor', "Rosa 'Peace'", 'Tobacco mosaic virus', 'Puma concolor couguar']
        fields = '<field index="2" term="' + DWC + 'scientificName"/>'
        archive = read_inputs([('meta.xml', manifest(fields)), ('occ.csv', table(['key', 'occurrenceID', 'name'],
            [[str(i), f'o{i}', name] for i, name in enumerate(names)]))])
        plan = build_plan(archive)
        self.assertEqual(plan['issues'], [])
        frames, _ = convert(archive, plan, {})
        self.assertEqual(frames['occurrence']['scientificName'].tolist(), names)

    def test_author_or_qualifier_is_not_hidden_by_a_hybrid_or_rank_marker(self):
        for name in ['Apus apus (Linnaeus, 1758)', 'Quercus robur L.', 'Aus cf. bus', 'Aus sp.',
                     'Aus bus de Vries', 'Aus bus ex Smith', 'Aus bus?', 'Salix alba × Salix fragilis L.']:
            with self.subTest(name=name):
                archive = read_inputs([('occurrence.csv', table(['occurrenceID', 'scientificName', 'occurrenceStatus'], [['o', name, 'present']]))])
                plan = build_plan(archive)
                self.assertTrue(column(plan, DWC + 'scientificName')['review'])
                self.assertIn(column(plan, DWC + 'scientificName')['id'], {issue['id'] for issue in plan['issues']})

    def test_invalid_half_coordinate_is_reported_without_inventing_a_point(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus,decimalLatitude,decimalLongitude\no,present,NA,10\n')])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, choices(plan))
        self.assertEqual(frames['event'].iloc[0]['decimalLongitude'], '10')
        self.assertNotIn('decimalLatitude', frames['event'])
        notice = next(warning for warning in report['warnings'] if warning['id'] == 'coordinate-pair:0')
        self.assertEqual(notice['invalid_pairs'], 1)
        self.assertIn('withheld as invalid', notice['reason'])
        self.assertEqual(report['withheld_values'][0]['source_row'], 1)
        self.assertEqual(report['withheld_values'][0]['value'], 'NA')
