import io
import json
import tarfile
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.dwca_conversion import RULE_VERSION, build_plan, convert, validate_decisions
from api.dwca_hierarchy import resolve_parents
from api.dwca_humboldt import ECO
from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive

PARENT_COLUMN = DWC + 'parentEventID'


def decisions_for(plan):
    return {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}


def node(event_id, parent, row):
    return {'event_id': event_id, 'parents': {parent}, 'rows': [row]}


def meta(core_terms, extensions=()):
    """meta.xml whose core id (column 0) is an archive key distinct from dwc:eventID."""
    def fields(terms):
        return ''.join(f'<field index="{i}" term="{term}"/>' for i, term in enumerate(terms, start=1))
    parts = ['<archive xmlns="http://rs.tdwg.org/dwc/text/">',
             f'<core rowType="{DWC}Event" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>event.csv</location></files>'
             f'<id index="0"/>{fields(core_terms)}</core>']
    for row_type, location, terms in extensions:
        parts.append(f'<extension rowType="{row_type}" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>{location}</location>'
                     f'</files><coreid index="0"/>{fields(terms)}</extension>')
    return (''.join(parts) + '</archive>').encode()


CORE_TERMS = [DWC + 'eventID', PARENT_COLUMN, DWC + 'eventCategory', DWC + 'eventDate']
OCCURRENCE_TERMS = [DWC + 'occurrenceID', DWC + 'scientificName', DWC + 'occurrenceStatus']
HUMBOLDT_TERMS = [ECO + 'targetTaxonomicScope', ECO + 'isTaxonomicScopeFullyReported', ECO + 'siteCount']
# Children precede their parents (forward references); archive keys k* differ from persistent eventIDs.
EVENTS = (b'key,eventID,parentEventID,eventCategory,eventDate\n'
          b'k4,visit-2,site-A,survey,2024-06-01\n'
          b'k3,visit-1,site-A,survey,2024-05-01\n'
          b'k2,site-A,project-1,survey,2024\n'
          b'k1,project-1,,survey,2024\n')
OCCURRENCES = (b'key,occurrenceID,scientificName,occurrenceStatus\n'
               b'k3,o1,Ixodes scapularis,present\nk3,o2,Amblyomma americanum,present\nk4,o3,Ixodes scapularis,absent\n')
HUMBOLDT = (b'key,targetTaxonomicScope,isTaxonomicScopeFullyReported,siteCount\n'
            b'k1,Ixodida,true,3\nk2,Ixodida,true,1\nk3,Ixodida,true,1\nk4,Ixodida,false,1\n')


def nested_files(events=EVENTS):
    extensions = [(DWC + 'Occurrence', 'occurrence.csv', OCCURRENCE_TERMS), (ECO + 'Event', 'humboldt.csv', HUMBOLDT_TERMS)]
    return {'meta.xml': meta(CORE_TERMS, extensions), 'event.csv': events, 'occurrence.csv': OCCURRENCES, 'humboldt.csv': HUMBOLDT}


def by_event_id(frames):
    events = frames['event']
    ids = dict(zip(events['event_pk'], events['eventID']))
    return {row['eventID']: ids.get(row.get('parentEvent_fk', ''), '') for _, row in events.iterrows()}


def parent_column(plan):
    return next(column for column in plan['columns'] if column['term'] == PARENT_COLUMN and column['table'] == 0)


class HierarchyHelperTests(SimpleTestCase):
    def test_forward_references_resolve_and_depth_is_counted(self):
        result = resolve_parents([node('c', 'b', 1), node('b', 'a', 2), node('a', '', 3)])
        self.assertEqual(result['links'], {0: 1, 1: 2})
        self.assertEqual((result['counts']['max_depth'], result['counts']['roots']), (2, 1))

    def test_each_invalid_relationship_withholds_every_link(self):
        cases = {
            'missing': [node('a', '', 1), node('b', 'a', 2), node('c', 'zzz', 3)],
            'ambiguous': [node('a', '', 1), node('a', '', 2), node('b', 'a', 3)],
            'self': [node('a', '', 1), node('b', 'b', 2)],
            'cycle': [node('a', 'c', 1), node('b', 'a', 2), node('c', 'b', 3), node('d', 'a', 4)],
            'inconsistent': [node('a', '', 1), {'event_id': 'b', 'parents': {'a', ''}, 'rows': [2, 3]}],
            'no-event-id': [node('a', '', 1), node('', 'a', 2)],
        }
        for kind, nodes in cases.items():
            with self.subTest(kind):
                result = resolve_parents(nodes)
                self.assertEqual([problem['problem'] for problem in result['problems']], [kind])
                self.assertEqual(result['links'], {})
        cycle = resolve_parents(cases['cycle'])['problems'][0]
        self.assertEqual((cycle['eventIDs'], cycle['rows']), (['a', 'c', 'b'], [1, 2, 3]))
        ambiguous = resolve_parents(cases['ambiguous'])['problems'][0]
        self.assertEqual(ambiguous['candidate_rows'], [1, 2])

    def test_values_are_exact_and_archive_keys_only_explain_failures(self):
        result = resolve_parents([node('a', '', 1), node('b', ' a', 2), node('c', 'k1', 3)], archive_keys={'k1'})
        self.assertEqual([problem['problem'] for problem in result['problems']], ['missing', 'missing'])
        self.assertIn('archive', result['problems'][1]['note'].lower())
        self.assertNotIn('note', result['problems'][0])

    def test_deep_chains_and_long_cycles_need_no_recursion(self):
        size = 20000
        chain = [node(f'e{i}', f'e{i - 1}' if i else '', i + 1) for i in reversed(range(size))]
        result = resolve_parents(chain)
        self.assertEqual((len(result['links']), result['counts']['max_depth']), (size - 1, size - 1))
        loop = [node(f'e{i}', f'e{(i + 1) % size}', i + 1) for i in range(size)]
        problems = resolve_parents(loop)['problems']
        self.assertEqual(len(problems), 1)
        self.assertEqual(len(problems[0]['eventIDs']), size)


class NestedEventConversionTests(SimpleTestCase):
    def test_nested_survey_archive_links_children_and_keeps_occurrences_on_their_own_events(self):
        archive = read_inputs([('nested.zip', source_zip(nested_files()))])
        plan = build_plan(archive)
        column = parent_column(plan)
        # One child weakens the parent's completeness claim for the same scope.
        self.assertEqual((column['default'], column['review']), ('parent-link', False))
        self.assertTrue(plan['scientific_hierarchy']['has_findings'])
        self.assertEqual(plan['event_hierarchy']['max_depth'], 2)
        self.assertEqual(plan['version'], RULE_VERSION)
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertTrue(report['validation']['valid'], report['validation'])
        self.assertEqual(by_event_id(frames), {'visit-2': 'site-A', 'visit-1': 'site-A', 'site-A': 'project-1', 'project-1': ''})
        # Exactly the source events: no fabricated parents, no roll-up of observations.
        self.assertEqual(len(frames['event']), 4)
        events = dict(zip(frames['event']['event_pk'], frames['event']['eventID']))
        self.assertEqual(sorted(events[key] for key in frames['occurrence']['event_fk']), ['visit-1', 'visit-1', 'visit-2'])
        self.assertEqual(frames['occurrence']['occurrenceStatus'].tolist(), ['present', 'present', 'absent'])
        # Each survey stays on its own event; parent completeness is the parent's own source row.
        surveys = {events[row['event_fk']]: row['survey_pk'] for _, row in frames['survey'].iterrows()}
        self.assertEqual(set(surveys), {'project-1', 'site-A', 'visit-1', 'visit-2'})
        targets = frames['survey-survey-target'].merge(frames['survey-target'], left_on='surveyTarget_fk', right_on='surveyTarget_pk')
        flags = {events[frames['survey'].set_index('survey_pk').loc[row['survey_fk'], 'event_fk']]: row['isSurveyTargetFullyReported']
                 for _, row in targets.iterrows()}
        self.assertEqual(flags, {'project-1': 'true', 'site-A': 'true', 'visit-1': 'true', 'visit-2': 'false'})
        hierarchy = report['event_hierarchy']
        self.assertEqual((hierarchy['decision'], hierarchy['linked_events']), ('parent-link', 3))
        self.assertEqual([(value['data_record'], value['archive_join_id'], value['parentEventID'], value['status'])
                          for value in hierarchy['source_values']],
                         [(1, 'k4', 'site-A', 'linked'), (2, 'k3', 'site-A', 'linked'), (3, 'k2', 'project-1', 'linked')])
        self.assertEqual({value['parent_source_row'] for value in hierarchy['source_values'][:2]}, {3})
        disposition = next(column for column in report['columns'] if column['term'] == PARENT_COLUMN)
        self.assertEqual((disposition['disposition'], disposition['mapped_rows'], disposition['retained_only_rows']), ('derived', 3, 0))

    def test_loose_files_link_forward_references_independently_of_upload_and_row_order(self):
        humboldt = b'eventID,siteCount\nvisit-1,1\nsite-A,1\n'
        forward = [('event.csv', b'eventID,parentEventID,eventCategory\nvisit-1,site-A,survey\nsite-A,,survey\n'),
                   ('humboldt.csv', humboldt)]
        backward = [('event.csv', b'eventID,parentEventID,eventCategory\nsite-A,,survey\nvisit-1,site-A,survey\n'),
                    ('humboldt.csv', humboldt)]
        results = []
        for inputs in (forward, list(reversed(forward)), backward):
            archive = read_inputs(inputs); plan = build_plan(archive)
            frames, report = convert(archive, plan, decisions_for(plan))
            self.assertTrue(report['validation']['valid'])
            self.assertEqual(by_event_id(frames), {'visit-1': 'site-A', 'site-A': ''})
            results.append((plan['id'], {name: frame.to_csv(index=False) for name, frame in frames.items()}))
        self.assertEqual(results[0], results[1])  # upload order changes nothing, including keys
        self.assertNotEqual(results[0][0], results[2][0])  # different source bytes, same relationships

    def test_conversion_is_reproducible(self):
        files = nested_files()
        outputs = []
        for _ in range(2):
            archive = read_inputs([('nested.zip', source_zip(files))]); plan = build_plan(archive)
            frames, report = convert(archive, plan, decisions_for(plan))
            outputs.append((plan['id'], json.dumps(report, sort_keys=True), {name: frame.to_csv(index=False) for name, frame in frames.items()}))
        self.assertEqual(outputs[0], outputs[1])

    def test_parent_values_naming_archive_keys_are_never_joined(self):
        events = (b'key,eventID,parentEventID,eventCategory,eventDate\n'
                  b'k1,project-1,,survey,2024\nk2,site-A,k1,survey,2024\n')
        archive = read_inputs([('meta.xml', meta(CORE_TERMS)), ('event.csv', events)])
        plan = build_plan(archive)
        column = parent_column(plan)
        self.assertEqual(([option['value'] for option in column['options']], column['default']), (['preserve'], 'preserve'))
        self.assertIn('meta.xml core id', json.dumps(plan['event_hierarchy']))
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertNotIn('parentEvent_fk', frames['event'])
        self.assertEqual(report['event_hierarchy']['source_values'][0]['status'], 'retained in originals')
        with self.assertRaisesMessage(ImportFailure, '1 missing'):
            validate_decisions(plan, {**decisions_for(plan), column['id']: 'parent-link'})

    def test_core_without_persistent_event_ids_cannot_link(self):
        terms = [PARENT_COLUMN, DWC + 'eventCategory']
        archive = read_inputs([('meta.xml', meta(terms)), ('event.csv', b'key,parentEventID,eventCategory\nk1,,survey\nk2,k1,survey\n')])
        plan = build_plan(archive)
        self.assertIn('no dwc:eventID', plan['event_hierarchy']['unsupported'])
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertNotIn('parentEvent_fk', frames['event'])
        self.assertIn('unsupported', report['event_hierarchy'])

    def test_invalid_relationships_offer_preservation_only_and_fail_clearly_when_forced(self):
        cases = {
            'missing': b'eventID,parentEventID,eventCategory\na,,survey\nb,a,survey\nc,elsewhere,survey\n',
            'ambiguous': b'eventID,parentEventID,eventCategory\na,,survey\na,,survey\nb,a,survey\n',
            'self': b'eventID,parentEventID,eventCategory\na,,survey\nb,b,survey\n',
            'cycle': b'eventID,parentEventID,eventCategory\na,b,survey\nb,a,survey\nc,a,survey\n',
        }
        for kind, events in cases.items():
            with self.subTest(kind):
                # Loose cores need unique eventIDs as join keys; a meta.xml archive key allows duplicate persistent IDs.
                rows = events.decode().splitlines()
                keyed = '\n'.join(['key,' + rows[0] + ',eventDate'] + [f'k{n},{row},2024' for n, row in enumerate(rows[1:])]) + '\n'
                archive = read_inputs([('meta.xml', meta([DWC + 'eventID', PARENT_COLUMN, DWC + 'eventCategory', DWC + 'eventDate'])),
                                       ('event.csv', keyed.encode())])
                plan = build_plan(archive)
                column = parent_column(plan)
                self.assertEqual([option['value'] for option in column['options']], ['preserve'])
                issue = next(issue for issue in [*plan['issues'], *plan.get('automatic_choices', [])] if issue['id'] == column['id'])
                self.assertIn(kind, issue['reason'])
                self.assertEqual(plan['event_hierarchy']['problem_kinds'], {kind: 1})
                decisions = decisions_for(plan)
                frames, report = convert(archive, plan, decisions)
                self.assertNotIn('parentEvent_fk', frames['event'])
                self.assertTrue(report['validation']['valid'])
                values = [value['parentEventID'] for value in report['event_hierarchy']['source_values']]
                self.assertEqual(values, [row.split(',')[1] for row in rows[1:] if row.split(',')[1]])
                with self.assertRaisesMessage(ImportFailure, 'cannot be linked faithfully'):
                    convert(archive, plan, {**decisions, column['id']: 'parent-link'})

    def test_explicit_preservation_emits_no_link_and_reports_every_value(self):
        archive = read_inputs([('nested.zip', source_zip(nested_files()))])
        plan = build_plan(archive); decisions = decisions_for(plan)
        decisions[parent_column(plan)['id']] = 'preserve'
        frames, report = convert(archive, plan, decisions)
        self.assertNotIn('parentEvent_fk', frames['event'])
        self.assertTrue(report['validation']['valid'])
        self.assertEqual([value['parentEventID'] for value in report['event_hierarchy']['source_values']], ['site-A', 'site-A', 'project-1'])
        self.assertEqual({value['status'] for value in report['event_hierarchy']['source_values']}, {'retained in originals'})
        disposition = next(column for column in report['columns'] if column['term'] == PARENT_COLUMN)
        self.assertEqual((disposition['target'], disposition['disposition'], disposition['nonempty']), ('preserve', 'retained-unmapped', 3))

    def test_survey_confirmation_is_not_propagated_to_parent_events(self):
        events = b'eventID,parentEventID\nsite-A,\nvisit-1,site-A\n'
        archive = read_inputs([('event.csv', events), ('humboldt.csv', b'eventID,siteCount\nvisit-1,1\n')])
        plan = build_plan(archive); decisions = decisions_for(plan)
        decisions.update({'event-category': 'occurrence', 'hum-category:1': 'confirm'})
        frames, report = convert(archive, plan, decisions)
        categories = dict(zip(frames['event']['eventID'], frames['event']['eventCategory']))
        self.assertEqual(categories, {'site-A': 'occurrence', 'visit-1': 'survey'})
        self.assertEqual(len(frames['survey']), 1)
        self.assertEqual(by_event_id(frames), {'site-A': '', 'visit-1': 'site-A'})

    def test_occurrence_extension_parent_values_are_retained_not_linked(self):
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne1,survey\n'),
                               ('occurrence.csv', b'eventID,occurrenceID,parentEventID,occurrenceStatus\ne1,o1,e0,present\n')])
        plan = build_plan(archive)
        column = next(column for column in plan['columns'] if column['term'] == PARENT_COLUMN)
        self.assertEqual([option['value'] for option in column['options']], ['preserve'])
        self.assertIn('Occurrence extension', column['parent_link_unavailable'])
        frames, report = convert(archive, plan, decisions_for(plan))
        self.assertNotIn('parentEvent_fk', frames['event'])


class OccurrenceCoreHierarchyTests(SimpleTestCase):
    CORE = (b'occurrenceID,eventID,parentEventID,occurrenceStatus\n'
            b'o1,visit-1,site-A,present\no2,visit-1,site-A,present\no3,site-A,,present\n')

    def test_links_only_between_events_established_by_reviewed_grouping(self):
        archive = read_inputs([('occurrence.csv', self.CORE)])
        plan = build_plan(archive); decisions = decisions_for(plan)
        column = parent_column(plan)
        self.assertEqual((column['default'], column['review']), ('preserve', True))
        frames, report = convert(archive, plan, decisions)
        self.assertNotIn('parentEvent_fk', frames['event'])
        decisions[column['id']] = 'parent-link'
        with self.assertRaisesMessage(ImportFailure, 'combined by supplied eventID'):
            convert(archive, plan, decisions)
        decisions['event-grain'] = 'by_id'
        frames, report = convert(archive, plan, decisions)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(len(frames['event']), 2)
        self.assertEqual(by_event_id(frames), {'visit-1': 'site-A', 'site-A': ''})
        self.assertEqual([value['status'] for value in report['event_hierarchy']['source_values']], ['linked', 'linked'])

    def test_inconsistent_or_missing_grouped_parents_are_not_linked(self):
        for content, kind in ((b'occurrenceID,eventID,parentEventID,occurrenceStatus\no1,v,s,present\no2,v,t,present\no3,s,,present\no4,t,,present\n', 'inconsistent'),
                              (b'occurrenceID,eventID,parentEventID,occurrenceStatus\no1,v,site-not-observed,present\n', 'missing')):
            with self.subTest(kind):
                archive = read_inputs([('occurrence.csv', content)])
                plan = build_plan(archive)
                self.assertEqual(plan['event_hierarchy']['problem_kinds'], {kind: 1})
                column = parent_column(plan)
                with self.assertRaisesMessage(ImportFailure, kind):
                    validate_decisions(plan, {**decisions_for(plan), column['id']: 'parent-link', 'event-grain': 'by_id'})


class SerializedHierarchyTests(SimpleTestCase):
    def test_serialized_package_validates_and_keeps_byte_identical_originals_and_provenance(self):
        files = nested_files()
        uploaded = source_zip(files)
        archive = read_inputs([('nested.zip', uploaded)])
        plan = build_plan(archive); frames, report = convert(archive, plan, decisions_for(plan))
        report_bytes = json.dumps(report, sort_keys=True).encode()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'converted.tar.gz'
            create_dwc_dp_archive(output, frames, title='Nested survey conversion', description='Nested events', include_eml=False,
                                  additional_files=[('source-originals.zip', source_zip(archive.files)), ('conversion-report.json', report_bytes)],
                                  declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])
            with tarfile.open(output) as package:
                members = {Path(member.name).name: member for member in package.getmembers() if member.isfile()}
                originals = package.extractfile(members['source-originals.zip']).read()
                stored_report = json.loads(package.extractfile(members['conversion-report.json']).read())
                event_csv = package.extractfile(members['event.csv']).read().decode()
        self.assertEqual(read_inputs([('originals.zip', originals)]).files, files)
        self.assertEqual(read_inputs([('originals.zip', originals)]).fingerprint, archive.fingerprint)
        self.assertIn('parentEvent_fk', event_csv.splitlines()[0])
        self.assertEqual(stored_report['event_hierarchy']['linked_events'], 3)
        self.assertEqual({(row['file'], row['data_record']) for row in stored_report['row_crosswalk'] if row['target_table'] == 'event'},
                         {('event.csv', n) for n in range(1, 5)})
        self.assertEqual(next(column for column in stored_report['columns'] if column['term'] == PARENT_COLUMN)['mapped_rows'], 3)
