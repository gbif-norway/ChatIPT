import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from api.conversion_jobs import apply_failure, classify_failure, process_next_conversion
from api.dwca_conversion import build_plan, convert, effective_decisions, option_status, validate_decisions
from api.dwca_germplasm import G, GEO
from api.dwca_humboldt import ECO
from api.dwca_import import ConversionError, ImportFailure, read_inputs
from api.models import CustomUser, DwcConversion
from api.test_dwca_extensions import data, source


def entry(plan, identifier):
    return next(item for item in [*plan['issues'], *plan['automatic_choices']] if item['id'] == identifier)


def answers(plan):
    return {item['id']: item['options'][0]['value'] for item in plan['issues']}


NBN = ('occurrenceID,sensitiveOccurrence\n' + 'o1,true\n' * 30).encode()


class IssuePolicyTests(SimpleTestCase):
    def test_every_issue_has_a_kind_option_flags_and_derived_authority(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,scientificName\na,e,Aus bus\nb,e,Aus bus\n')])
        plan = build_plan(archive)
        for item in [*plan['issues'], *plan['automatic_choices']]:
            self.assertIn('kind', item)
            self.assertIn(item['authority'], {'ai-reviewable', 'user-assertion'})
            self.assertTrue(all('assertion' in option for option in item['options']))
            self.assertFalse(any(option['assertion'] for option in item['options'] if option['value'] == 'preserve'))
        self.assertEqual(entry(plan, 'status:0')['authority'], 'user-assertion')
        self.assertEqual(entry(plan, 'loose-links')['authority'], 'user-assertion')
        # Splitting a repeated supplied eventID asserts separate events; combining does not.
        grain = {option['value']: option['assertion'] for option in entry(plan, 'event-grain')['options']}
        self.assertEqual(grain, {'per_row': True, 'by_id': False})
        self.assertEqual(entry(plan, 'event-grain')['authority'], 'ai-reviewable')

    def test_media_subject_options_are_assertions_but_unlinked_media_is_not(self):
        archive = source('multimedia.csv', ['http://purl.org/dc/terms/identifier'], [['e1', 'urn:media:1']])
        role = {option['value']: option['assertion'] for option in entry(build_plan(archive), 'table:1')['options']}
        self.assertTrue(role['media-event'])
        self.assertFalse(role['media-unlinked'])


class GroupedRowTests(SimpleTestCase):
    def test_identical_row_questions_become_one_group_with_member_overrides(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus\no1,present\n'), ('nbn.csv', NBN)])
        plan = build_plan(archive)
        group = entry(plan, 'row-group:1:0')
        self.assertEqual(group['count'], 30)
        self.assertEqual(group['rows'][:3], [1, 2, 3])
        self.assertEqual(len(plan['row_issues']), 30)
        self.assertFalse(any(item['id'].startswith('row:') for item in plan['issues']))
        self.assertIn('row-group:1:0', validate_decisions(plan, {}, require_complete=False))
        effective = effective_decisions(plan, {'row-group:1:0': 'preserve', 'row:1:4': 'convert'})
        self.assertEqual(effective['row:1:0'], 'preserve')
        self.assertEqual(effective['row:1:4'], 'convert')
        # Answering every member resolves the group without a group key.
        members = {member['id']: 'preserve' for member in plan['row_issues']}
        self.assertNotIn('row-group:1:0', validate_decisions(plan, members, require_complete=False))
        with self.assertRaisesMessage(ImportFailure, 'Unsupported mapping decision'):
            validate_decisions(plan, {'row:1:0': 'invented'}, require_complete=False)
        frames, report = convert(archive, plan, {**answers(plan), 'row-group:1:0': 'preserve'})
        self.assertEqual(len(report['preserved_extension_rows']), 30)
        self.assertTrue(all('reason' in row for row in report['preserved_extension_rows']))

    def test_group_ids_are_stable_and_part_of_the_plan_hash(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus\no1,present\n'), ('nbn.csv', NBN)])
        self.assertEqual(build_plan(archive)['id'], build_plan(archive)['id'])

    def test_scope_questions_group_only_identical_scope_claims(self):
        rows = [['e1', 'Aves'], ['e2', 'Aves'], ['e3', 'Mammalia']]
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne1,survey\ne2,survey\ne3,survey\n'),
                               ('humboldt.csv', data([DWC_EVENT_ID, ECO + 'targetTaxonomicScope'], rows))])
        plan = build_plan(archive)
        scopes = [item for item in plan['issues'] if item['kind'] == 'survey-completeness']
        self.assertEqual(sorted(item.get('count', 1) for item in scopes), [1, 2])
        group = next(item for item in scopes if item.get('members'))
        self.assertEqual(group['scope_values'], {ECO + 'targetTaxonomicScope': 'Aves'})
        self.assertEqual(group['authority'], 'user-assertion')


DWC_EVENT_ID = 'http://rs.tdwg.org/dwc/terms/eventID'


class RequirementTests(SimpleTestCase):
    def test_unsubmitted_defaults_are_checked_and_moved_back_to_review(self):
        # Separate surveys cannot share a supplied surveyID, so the automatic survey role needs input.
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne1,survey\ne2,survey\n'),
                               ('humboldt.csv', data([DWC_EVENT_ID, ECO + 'surveyID', ECO + 'siteCount'], [['e1', 's', '1'], ['e2', 's', '2']]))])
        plan = build_plan(archive)
        table = next(item for item in plan['issues'] if item['id'] == 'table:1')
        self.assertIn('surveyID values would identify several separate surveys', table['reason'])
        survey = next(column['id'] for column in plan['columns'] if column['term'] == ECO + 'surveyID')
        status = option_status(plan, {'table:1': 'humboldt-survey'})
        self.assertFalse(status['table:1']['humboldt-survey']['available'])
        self.assertTrue(option_status(plan, {'table:1': 'humboldt-survey', survey: 'preserve'})['table:1']['humboldt-survey']['available'])
        with self.assertRaises(ConversionError) as raised:
            validate_decisions(plan, {**answers(plan), 'table:1': 'humboldt-survey'})
        self.assertEqual(raised.exception.category, 'decision')
        self.assertIn(survey, raised.exception.decision_ids)
        frames, _ = convert(archive, plan, {**answers(plan), 'table:1': 'humboldt-survey', survey: 'preserve'})
        self.assertEqual(len(frames['survey']), 2)

    def test_preserved_tables_do_not_report_requirement_violations(self):
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne1,survey\ne2,survey\n'),
                               ('humboldt.csv', data([DWC_EVENT_ID, ECO + 'surveyID', ECO + 'siteCount'], [['e1', 's', '1'], ['e2', 's', '2']]))])
        plan = build_plan(archive)
        validate_decisions(plan, {**answers(plan), 'table:1': 'preserve'})

    def test_parent_links_on_occurrence_cores_require_combined_events(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,parentEventID,occurrenceStatus\na,e1,,present\nb,e2,e1,present\n')])
        plan = build_plan(archive)
        parent = next(column for column in plan['columns'] if column['term'].endswith('parentEventID'))
        status = option_status(plan, {'event-grain': 'per_row', parent['id']: 'parent-link'})
        self.assertFalse(status[parent['id']]['parent-link']['available'])


class FailureCategoryTests(SimpleTestCase):
    def test_categories_map_to_review_blocked_and_failed_states(self):
        conversion = DwcConversion(plan={'id': 'p'})
        apply_failure(conversion, ConversionError('x', category='conflict', decision_ids=['table:1']).as_conflict())
        self.assertEqual((conversion.status, conversion.retryable), ('review', False))
        apply_failure(conversion, ConversionError('x', category='conflict').as_conflict())
        self.assertEqual(conversion.status, 'blocked')  # No decision can remedy it.
        apply_failure(conversion, classify_failure(ImportFailure('Malformed CSV')))
        self.assertEqual(conversion.status, 'blocked')
        apply_failure(conversion, classify_failure(OSError('Storage down')))
        self.assertEqual((conversion.status, conversion.retryable), ('review', True))
        apply_failure(conversion, classify_failure(KeyError('bug')))
        self.assertEqual((conversion.status, conversion.retryable), ('failed', False))
        fresh = DwcConversion(plan={})
        apply_failure(fresh, classify_failure(OSError('Storage down')))
        self.assertEqual((fresh.status, fresh.retryable), ('failed', True))

    def test_extension_patch_conflicts_name_their_remedies(self):
        archive = source('measurement_trial.csv', [GEO + 'lat', GEO + 'lon', G + 'measurementTrailYear'], [['e1', '59.6', '10.7', '2025']],
                         ('event.csv', b'eventID,eventCategory,decimalLatitude\ne1,survey,60\n'))
        plan = build_plan(archive)
        with self.assertRaises(ConversionError) as raised:
            convert(archive, plan, answers(plan))
        self.assertEqual(raised.exception.category, 'conflict')
        self.assertIn('table:1', raised.exception.decision_ids)
        self.assertEqual(raised.exception.evidence['field'], 'decimalLatitude')


class SaveAndConflictAPITests(TransactionTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        storage = override_settings(STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': folder.name}},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        })
        storage.enable(); self.addCleanup(storage.disable)
        self.client = APIClient()
        self.client.force_authenticate(CustomUser.objects.create_user(username='tiered-owner'))
        response = self.client.post('/api/datasets/', {'workflow_type': 'dwca_conversion', 'files': [
            SimpleUploadedFile('event.csv', b'eventID,eventCategory,decimalLatitude\ne1,survey,60\n'),
            SimpleUploadedFile('measurement_trial.csv', data([DWC_EVENT_ID, GEO + 'lat', GEO + 'lon', G + 'measurementTrailYear'],
                                                             [['e1', '59.6', '10.7', '2025']])),
        ]}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        process_next_conversion()
        self.conversion = DwcConversion.objects.get(dataset_id=response.data['id'])
        self.url = f'/api/datasets/{response.data["id"]}/conversion/'

    def post(self, action, decisions):
        return self.client.post(self.url, {'action': action, 'plan_id': self.conversion.plan['id'], 'decisions': decisions}, format='json')

    def test_save_persists_without_a_job_and_conflicts_clear_when_a_remedy_changes(self):
        decisions = answers(self.conversion.plan)
        saved = self.post('save', decisions)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.data['unresolved'], [])
        self.assertIn('option_status', saved.data)
        self.assertEqual(self.post('convert', decisions).status_code, 202)
        process_next_conversion(); self.conversion.refresh_from_db()
        self.assertEqual(self.conversion.status, 'review')
        self.assertEqual(self.conversion.conflicts[0]['category'], 'conflict')
        self.assertIn('table:1', self.conversion.conflicts[0]['decision_ids'])
        state = self.client.get(self.url).data
        self.assertEqual(state['conflicts'], self.conversion.conflicts)
        unrelated = self.post('save', {**decisions, 'loose-links': 'confirm'})
        self.assertEqual(len(unrelated.data['conflicts']), 1)
        self.assertTrue(unrelated.data['error'])
        remedied = self.post('save', {**decisions, 'table:1': 'preserve'})
        self.assertEqual(remedied.data['conflicts'], [])
        self.assertEqual(remedied.data['error'], '')
        self.assertEqual(self.post('convert', {**decisions, 'table:1': 'preserve'}).status_code, 202)
        process_next_conversion(); self.conversion.refresh_from_db()
        self.assertEqual(self.conversion.status, 'complete')
        # The completed package reflects the converted choices; they cannot change underneath it.
        self.assertEqual(self.post('save', decisions).status_code, 409)
        self.assertEqual(self.post('review', {**decisions, 'table:1': 'preserve'}).status_code, 409)

    def test_rejected_decisions_return_a_structured_conflict(self):
        response = self.post('convert', {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['conflict']['category'], 'decision')
        self.assertTrue(response.data['conflict']['decision_ids'])


class CombinedMappingTests(SimpleTestCase):
    """Preflight follows convert(): columns sharing a target combine before groups are compared."""

    def test_complementary_identifier_columns_permit_combined_material(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,materialSampleID,materialEntityID,occurrenceStatus\n'
                                                  b'a,e,m1,,present\nb,e,,m1,present\n')])
        plan = build_plan(archive)
        self.assertIn('by_id', {option['value'] for option in entry(plan, 'material:0')['options']})
        decisions = {**answers(plan), 'material:0': 'by_id', 'event-grain': 'by_id'}
        validate_decisions(plan, decisions)
        frames, _ = convert(archive, plan, decisions)
        self.assertEqual(frames['material']['materialEntityID'].tolist(), ['m1'])

    def test_a_retained_media_row_does_not_block_its_table(self):
        from api.dwca_eol import DCT, XMP
        archive = source('eol_media.csv', [DCT + 'identifier', DCT + 'type', XMP + 'rights/UsageTerms', DWC + 'taxonID'],
                         [['o1', 'https://example.org/img', 'StillImage', 'CC BY', ''], ['o1', '', '', '', 'taxon:1']],
                         ('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus\no1,e1,present\n'))
        plan = build_plan(archive)
        row = next(member['id'] for member in [*plan['row_issues'], *({'id': item['id']} for item in plan['issues'])]
                   if member['id'] == 'row:1:1')
        self.assertIn('media-unlinked', {option['value'] for option in entry(plan, 'table:1')['options']})
        base = {**answers(plan), 'table:1': 'media-unlinked', 'row:1:0': 'convert'}
        self.assertTrue(option_status(plan, {**base, row: 'preserve'})['table:1']['media-unlinked']['available'])
        self.assertFalse(option_status(plan, {**base, row: 'convert'})['table:1']['media-unlinked']['available'])

    def test_core_material_match_does_not_require_the_accession_table(self):
        core = ('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus,materialEntityID\no1,e1,present,acc:1\n')
        archive = source('measurement_score.csv', [G + 'germplasmID', G + 'measurementTraitName', DWC + 'measurementValue'],
                         [['o1', 'acc:1', 'Height', '7']], core, other=(
                             ('germplasm_accession.csv', data([DWC + 'occurrenceID', G + 'germplasmID'], [['o1', 'acc:1']])),))
        plan = build_plan(archive)
        scores = next(index for index, table in enumerate(archive.tables) if table.name == 'measurement_score.csv')
        accession = next(index for index, table in enumerate(archive.tables) if table.name == 'germplasm_accession.csv')
        decisions = {**answers(plan), 'material:0': 'per_row', f'table:{scores}': 'germplasm-score-material', f'table:{accession}': 'preserve'}
        validate_decisions(plan, decisions)
        frames, _ = convert(archive, plan, decisions)
        self.assertEqual(len(frames['material-assertion']), 1)


DWC = 'http://rs.tdwg.org/dwc/terms/'


class ReviewFollowUpTests(SimpleTestCase):
    def test_filled_event_category_does_not_block_combining(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,eventCategory,occurrenceStatus\na,e,,present\nb,e,occurrence,present\n')])
        plan = build_plan(archive)
        self.assertTrue(option_status(plan, {'event-grain': 'by_id'}).get('event-grain', {}).get('by_id', {'available': True})['available'])
        frames, _ = convert(archive, plan, {**answers(plan), 'event-grain': 'by_id'})
        self.assertEqual(len(frames['event']), 1)

    def test_trait_ids_in_two_tables_link_when_one_table_is_retained(self):
        trait = [DWC + 'occurrenceID', G + 'measurementTraitID', G + 'measurementTraitName']
        archive = source('measurement_score.csv', [G + 'measurementTraitID', DWC + 'measurementType', DWC + 'measurementValue'],
                         [['o1', 'trait:1', 'Height', '7']], ('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus\no1,e1,present\n'),
                         other=(('measurement_trait.csv', data(trait, [['o1', 'trait:1', 'Height']])),
                                ('measurementtrait.txt', data(trait, [['o1', 'trait:1', 'Height']]))))
        plan = build_plan(archive)
        scores = next(index for index, table in enumerate(archive.tables) if table.name == 'measurement_score.csv')
        traits = [index for index, table in enumerate(archive.tables) if table.name.startswith('measurement') and 'trait' in table.name]
        self.assertIn('exact', {option['value'] for option in entry(plan, f'trait-link:{scores}')['options']})
        base = {**answers(plan), f'table:{scores}': 'germplasm-score-occurrence', f'trait-link:{scores}': 'exact'}
        self.assertFalse(option_status(plan, base)[f'trait-link:{scores}']['exact']['available'])
        retained = {**base, f'table:{traits[1]}': 'preserve'}
        self.assertTrue(option_status(plan, retained)[f'trait-link:{scores}']['exact']['available'])
        frames, _ = convert(archive, plan, retained)
        self.assertEqual(len(frames['protocol']), 1)

    def test_loose_headers_resolving_to_one_term_are_rejected(self):
        with self.assertRaisesMessage(ImportFailure, 'several headers for the same term'):
            read_inputs([('occurrence.csv', ('occurrenceID,' + DWC + 'occurrenceID\na,a\n').encode())])


class TypedMediaTests(SimpleTestCase):
    def test_a_media_row_whose_only_value_is_withheld_cannot_convert(self):
        plan = build_plan(source('multimedia.csv', ['http://purl.org/dc/terms/identifier', 'http://rs.tdwg.org/ac/terms/frameRate'],
                                 [['e1', 'urn:media:1', '25'], ['e1', '', 'fast']]))
        table = entry(plan, 'table:1')
        values = {option['value'] for option in table['options']} | {option['value'] for option in table.get('unavailable_options', [])}
        self.assertIn('media-unlinked', values)
        self.assertNotIn('media-unlinked', {option['value'] for option in table['options']})
