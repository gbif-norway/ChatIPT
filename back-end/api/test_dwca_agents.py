import tempfile
from collections import Counter
from pathlib import Path

import pandas as pd
from django.test import SimpleTestCase

from api.dwca_agents import ROLE_FIELDS, RoleField, build_agent_roles, composite_name_reason
from api.dwca_conversion import _key, build_plan, convert
from api.dwca_import import read_inputs
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive, validate_dwc_dp_resources

ORCID = 'https://orcid.org/0000-0002-1825-0097'


def converted(content):
    archive = read_inputs([('occurrence.csv', content)])
    plan = build_plan(archive)
    frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
    assert report['validation']['valid'], report['validation']
    # Exercise the helper independently of the graph now emitted by convert().
    frames = {name: frame for name, frame in frames.items() if not name.endswith('-agent-role')}
    if 'agent' in frames:
        identified = frames['agent'][frames['agent']['agentID'].astype(bool)] if 'agentID' in frames['agent'] else pd.DataFrame()
        if identified.empty:
            frames.pop('agent')
        else:
            frames['agent'] = identified.reset_index(drop=True)
    return archive, frames


def merged(frames, result):
    rows = {name: frame.to_dict('records') for name, frame in frames.items()}
    for table, added in result.tables().items():
        rows.setdefault(table, []).extend(added)
    return {name: pd.DataFrame(table_rows).fillna('') for name, table_rows in rows.items()}


class AgentRoleTests(SimpleTestCase):
    def test_converter_emits_agent_roles_with_source_crosswalk(self):
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,recordedByID,identifiedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,' + ORCID.encode() + b',NTNU University Museum,2025-01-01,present\n')])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
        self.assertEqual(len(frames['agent']), 1)
        self.assertEqual(len(frames['occurrence-agent-role']), 1)
        self.assertEqual(report['agent_roles']['roles_created'], {'occurrence-agent-role': 1})
        self.assertEqual(report['agent_roles']['unlinked_name_only'], 1)
        self.assertEqual(len([row for row in report['row_crosswalk'] if row['target_table'] == 'occurrence-agent-role']), 1)
        self.assert_package_valid(frames)

    def test_converter_shares_only_a_confirmed_exact_name(self):
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,2025-01-01,present\n'
            b'o2,e2,Ola Nordmann,2025-01-02,present\n')])
        plan = build_plan(archive)
        issue = next(item for item in plan['automatic_choices'] if item['id'].startswith('agent-share:'))
        self.assertEqual(issue['default'], 'separate')
        self.assertTrue(next(option for option in issue['options'] if option['value'] == 'shared')['assertion'])
        choices = {item['id']: item['options'][0]['value'] for item in plan['issues']}
        separate, separate_report = convert(archive, plan, choices)
        self.assertNotIn('agent', separate)
        self.assertNotIn('occurrence-agent-role', separate)
        self.assertEqual(separate_report['agent_roles']['unlinked_name_only'], 2)
        shared, report = convert(archive, plan, {**choices, issue['id']: 'shared'})
        self.assertEqual(len(shared['agent']), 1)
        self.assertEqual(len(shared['occurrence-agent-role']), 2)
        self.assertEqual(set(shared['occurrence-agent-role']['agent_fk']), set(shared['agent']['agent_pk']))
        self.assertEqual(report['agent_roles']['agents_created']['shared_name'], 1)
        self.assert_package_valid(shared)

    def assert_package_valid(self, frames):
        validation = validate_dwc_dp_resources(frames)
        self.assertTrue(validation['valid'], validation['errors'])
        self.assertFalse([warning for warning in validation['warnings'] if "resource 'agent'" in warning])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'agents.tar.gz'
            create_dwc_dp_archive(output, frames, 'Agents', 'Agent roles', include_eml=False)
            archive_validation = validate_dwc_dp_archive(output, require_eml=False)
            self.assertTrue(archive_validation['valid'], archive_validation['errors'])

    def test_unconfirmed_name_only_values_remain_in_text_fields(self):
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,identifiedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,NTNU University Museum,2025-01-01,present\n'
            b'o2,e2,Ola Nordmann,NTNU University Museum,2025-01-02,present\n'
            b'o3,e3, Ola Nordmann ,NTNU University Museum,2025-01-03,present\n')
        result = build_agent_roles(frames, lambda *parts: _key(archive, *parts))
        self.assertEqual(result.rows, [])
        self.assertEqual([(item['name'], item['mentions']) for item in result.report['repeated_names']],
                         [('NTNU University Museum', 3), ('Ola Nordmann', 3)])
        self.assertEqual(result.report['agents_created'], {'shared_name': 0, 'explicit_id': 0})
        self.assertEqual(result.report['unlinked_name_only'], 6)
        # The mapped name fields are unchanged and the result is deterministic.
        self.assertEqual(frames['occurrence']['recordedBy'].tolist()[2].strip(), 'Ola Nordmann')
        self.assertEqual(build_agent_roles(frames, lambda *parts: _key(archive, *parts)).rows, result.rows)
        self.assert_package_valid(merged(frames, result))

    def test_shared_identity_is_only_asserted_for_named_values(self):
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,identifiedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,NTNU University Museum,2025-01-01,present\n'
            b'o2,e2,Ola Nordmann,NTNU University Museum,2025-01-02,present\n')
        result = build_agent_roles(frames, lambda *parts: _key(archive, *parts),
                                   shared_names=['Ola Nordmann', 'Ola Nordman'])
        tables = result.tables()
        collectors = [agent for agent in tables['agent'] if agent['preferredAgentName'] == 'Ola Nordmann']
        self.assertEqual(len(collectors), 1)
        roles = pd.DataFrame(tables['occurrence-agent-role'])
        recorded = roles[roles['agentRole'] == 'recordedBy']
        self.assertEqual(set(recorded['agent_fk']), {collectors[0]['agent_pk']})
        self.assertEqual(len(recorded), 2)
        self.assertEqual(set(roles['agentRole']), {'recordedBy'})
        self.assertEqual(result.report['unlinked_name_only'], 2)
        self.assertEqual(result.report['shared_names_unused'], ['Ola Nordman'])
        self.assertEqual([item['name'] for item in result.report['repeated_names']], ['NTNU University Museum'])
        self.assert_package_valid(merged(frames, result))

    def test_composite_and_placeholder_names_are_reported_not_split(self):
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,"Hansen, P. & Olsen, K.",2025-01-01,present\n'
            b'o2,e1,P. Hansen | K. Olsen,2025-01-01,present\n'
            b'o3,e1,"Hansen, P.; Olsen, K.",2025-01-01,present\n'
            b'o4,e1,"Per Hansen, Kari Olsen",2025-01-01,present\n'
            b'o5,e1,Hansen et al.,2025-01-01,present\n'
            b'o6,e1,unknown,2025-01-01,present\n'
            b'o7,e1,"Hansen, P.",2025-01-01,present\n'
            b'o8,e1,P. Hansen,2025-01-01,present\n'
            b'o9,e1,"Hansen, P",2025-01-01,present\n')
        result = build_agent_roles(frames, lambda *parts: _key(archive, *parts))
        self.assertEqual(result.rows, [])
        self.assertEqual(result.report['skipped'], {'comma_list': 1, 'conjunction': 1, 'delimited_list': 2,
                                                    'incomplete_group': 1, 'placeholder': 1})
        self.assertEqual(result.report['fields']['occurrence.recordedBy']['unlinked_name_only'], 3)
        skipped = {item['value']: (item['reason'], item['first_row']) for item in result.report['skipped_values']}
        self.assertEqual(skipped['P. Hansen | K. Olsen'], ('delimited_list', 2))
        self.assertEqual(skipped['Per Hansen, Kari Olsen'], ('comma_list', 4))
        # Spelling variants are surfaced for review without creating identities.
        self.assertEqual(result.report['variant_groups'], [['Hansen, P', 'Hansen, P.', 'P. Hansen']])
        self.assert_package_valid(merged(frames, result))

    def test_explicit_ids_reuse_converter_agents_and_pair_only_one_to_one(self):
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,recordedByID,eventDate,occurrenceStatus\n'
            b'o1,e1,Alice Smith,' + ORCID.encode() + b',2025-01-01,present\n'
            b'o2,e2,,' + ORCID.encode() + b',2025-01-02,present\n'
            b'o3,e3,Bob | Carol,https://example.org/bob | https://example.org/carol | https://example.org/dan,2025-01-03,present\n'
            b'o4,e4,Bob & Carol,https://example.org/bob,2025-01-04,present\n')
        alice = frames['agent'].set_index('agentID').loc[ORCID, 'agent_pk']
        key = lambda *parts: _key(archive, *parts)  # noqa: E731
        result = build_agent_roles(frames, key)
        self.assertNotIn('agent', result.tables())
        roles = pd.DataFrame(result.tables()['occurrence-agent-role'])
        self.assertEqual(roles['agent_fk'].tolist(), [alice, alice])
        self.assertEqual(result.report['explicit_id_agents_reused'], 1)
        self.assertEqual(result.report['skipped'], {'id_name_count_mismatch': 1, 'id_not_single_iri': 1})
        self.assert_package_valid(merged(frames, result))
        # Without the converter's rows, the same key yields the same agent and the unique safe name.
        standalone = build_agent_roles({'occurrence': frames['occurrence']}, key)
        self.assertEqual(standalone.tables()['agent'],
                         [{'agent_pk': alice, 'agentID': ORCID, 'preferredAgentName': 'Alice Smith'}])
        self.assertEqual(standalone.report['agents_created']['explicit_id'], 1)

    def test_confirmed_name_shares_one_agent_across_whitespace_variants(self):
        # ds572 had 4,209 Agent rows for one collector, one per mention. A confirmed name is one Agent.
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,"Straumfors, Per",2025-01-01,present\n'
            b'o2,e2,"Straumfors, Per",2025-01-02,present\n'
            b'o3,e3," Straumfors,  Per ",2025-01-03,present\n')
        result = build_agent_roles(frames, lambda *parts: _key(archive, *parts), shared_names=['Straumfors,  Per'])
        tables = result.tables()
        self.assertEqual([agent['preferredAgentName'] for agent in tables['agent']], ['Straumfors, Per'])
        roles = pd.DataFrame(tables['occurrence-agent-role'])
        self.assertEqual(len(roles), 3)
        self.assertEqual(set(roles['occurrence_fk']), set(frames['occurrence']['occurrence_pk']))
        self.assertEqual(result.report['agents_created'], {'shared_name': 1, 'explicit_id': 0})
        self.assertEqual((result.report['unlinked_name_only'], result.report['shared_names_unused']), (0, []))
        # The mapped name field keeps its source text.
        self.assertEqual(frames['occurrence']['recordedBy'].tolist()[2], ' Straumfors,  Per ')
        self.assert_package_valid(merged(frames, result))

    def test_whitespace_variants_form_one_sharing_question(self):
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,"Straumfors, Per",2025-01-01,present\n'
            b'o2,e2,"Straumfors,  Per",2025-01-02,present\n')])
        plan = build_plan(archive)
        issues = [item for item in plan['automatic_choices'] if item['id'].startswith('agent-share:')]
        self.assertEqual([(item['source_value'], item['count']) for item in issues], [('Straumfors, Per', 2)])
        frames, report = convert(archive, plan, {**{issue['id']: issue['options'][0]['value'] for issue in plan['issues']},
                                                 issues[0]['id']: 'shared'})
        self.assertEqual(len(frames['agent']), 1)
        self.assertEqual(len(frames['occurrence-agent-role']), 2)

    def test_pipe_lists_paired_with_id_lists_are_split_in_order(self):
        # ds560: 25 recordedBy/recordedByID list pairs were skipped as id_not_single_iri.
        horvath, bryn, liahjell = ('https://orcid.org/0000-0002-6017-5385', 'https://orcid.org/0000-0003-4712-8266',
                                   'https://orcid.org/0009-0000-2845-7836')
        archive = read_inputs([('occurrence.csv', (
            'occurrenceID,eventID,recordedBy,recordedByID,eventDate,occurrenceStatus\n'
            f'o1,e1,Peter Horvath,{horvath},2025-01-01,present\n'
            f'o2,e2,Peter Horvath | Gunnar Thorsen Liahjell,{horvath} | {liahjell},2025-01-02,present\n'
            f'o3,e3,Anders Bryn|Gunnar Thorsen Liahjell,{bryn}|{liahjell},2025-01-03,present\n'
            f'o4,e4,Anders Bryn | Gunnar Thorsen Liahjell,{bryn},2025-01-04,present\n').encode())])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
        agents = frames['agent'].set_index('agentID')
        self.assertEqual(sorted(agents.index), sorted([horvath, bryn, liahjell]))
        self.assertEqual(agents.loc[liahjell, 'preferredAgentName'], 'Gunnar Thorsen Liahjell')
        roles = frames['occurrence-agent-role']
        occurrence = dict(zip(frames['occurrence']['occurrenceID'], frames['occurrence']['occurrence_pk']))
        for source, expected in (('o2', [horvath, liahjell]), ('o3', [bryn, liahjell])):
            listed = roles[roles['occurrence_fk'] == occurrence[source]].sort_values('agentRoleOrder')
            self.assertEqual(listed['agent_fk'].tolist(), [agents.loc[identifier, 'agent_pk'] for identifier in expected])
            self.assertEqual(listed['agentRoleOrder'].astype(int).tolist(), [1, 2])
        agent_roles = report['agent_roles']
        self.assertEqual(agent_roles['fields']['occurrence.recordedBy']['linked_by_id_list'], 2)
        # A list paired with a single ID is still a count mismatch and gets no role.
        self.assertEqual(agent_roles['skipped'], {'id_name_count_mismatch': 1})
        self.assertNotIn(occurrence['o4'], set(roles['occurrence_fk']))
        self.assert_package_valid(frames)

    def test_empty_by_id_columns_with_name_only_agents_export(self):
        # ds568 failed at export: empty recordedByID/identifiedByID columns beside an agent table
        # without agentID made the descriptor declare a key to a missing field.
        archive = read_inputs([('occurrence.txt',
            b'occurrenceID\tbasisOfRecord\tcatalogNumber\trecordedBy\trecordedByID\tidentifiedBy\tidentifiedByID\t'
            b'eventDate\tscientificName\n'
            b'urn:catalog:NHMO:J:1\tPreservedSpecimen\t1\tAnfinnsen, Martin T.\t\tPerlini, Enrico Maria\t\t1911-06-01\tSalmo trutta\n'
            b'urn:catalog:NHMO:J:2\tPreservedSpecimen\t2\tAnfinnsen, Martin T.\t\tPerlini, Enrico Maria\t\t1911-06-02\tSalmo trutta\n'
            b'urn:catalog:NHMO:J:3\tPreservedSpecimen\t3\tCollett, Robert\t\t\t\t1880\tEsox lucius\n'
            b'urn:catalog:NHMO:J:4\tPreservedSpecimen\t4\t\t\t\t\t1880\tEsox lucius\n')])
        plan = build_plan(archive)
        choices = {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}
        # By default name-only mentions stay text, so there is no agent table at all.
        unlinked, _ = convert(archive, plan, choices)
        self.assertNotIn('agent', unlinked)
        self.assert_package_valid(unlinked)
        # Confirmed names create agents without agentID beside the empty *ByID columns: the 568 shape.
        sharing = {item['id']: 'shared' for item in plan['automatic_choices'] if item['id'].startswith('agent-share:')}
        self.assertEqual(len(sharing), 2)
        frames, report = convert(archive, plan, {**choices, **sharing})
        self.assertTrue(report['validation']['valid'], report['validation']['errors'])
        self.assertNotIn('agentID', frames['agent'])
        self.assertEqual(sorted(frames['agent']['preferredAgentName']), ['Anfinnsen, Martin T.', 'Perlini, Enrico Maria'])
        self.assertEqual(Counter(frames['occurrence-agent-role']['agentRole']), {'recordedBy': 2, 'identifiedBy': 2})
        self.assert_package_valid(frames)
        with tempfile.TemporaryDirectory() as directory:
            descriptor = create_dwc_dp_archive(Path(directory) / 'ds568.tar.gz', frames, 'ds568', 'Agents', include_eml=False)
        occurrence = next(resource for resource in descriptor['resources'] if resource['name'] == 'occurrence')
        declared = [key['fields'] for keys in ('foreignKeys', 'weakForeignKeys') for key in occurrence['schema'].get(keys, [])]
        self.assertNotIn('recordedByID', declared)
        self.assertNotIn('identifiedByID', declared)

    def test_role_order_continues_existing_roles_and_repeat_calls_add_nothing(self):
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,recordedByID,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,' + ORCID.encode() + b',2025-01-01,present\n')
        key = lambda *parts: _key(archive, *parts)  # noqa: E731
        occurrence = frames['occurrence'].loc[0, 'occurrence_pk']
        frames['agent'] = pd.concat([frames['agent'], pd.DataFrame([{'agent_pk': 'existing', 'preferredAgentName': 'Kari Nordmann'}])], ignore_index=True)
        frames['occurrence-agent-role'] = pd.DataFrame([{'occurrence_fk': occurrence, 'agent_fk': 'existing',
                                                         'agentRole': 'recordedBy', 'agentRoleOrder': 1}])
        result = build_agent_roles(frames, key)
        self.assertEqual([row['agentRoleOrder'] for row in result.tables()['occurrence-agent-role']], [2])
        combined = merged(frames, result)
        self.assert_package_valid(combined)
        again = build_agent_roles(combined, key)
        self.assertEqual(again.rows, [])
        self.assertEqual(again.report['roles_already_present'], 1)

    def test_role_rows_without_order_fail_schema_validation(self):
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,recordedByID,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,' + ORCID.encode() + b',2025-01-01,present\n')
        combined = merged(frames, build_agent_roles(frames, lambda *parts: _key(archive, *parts)))
        validation = validate_dwc_dp_resources({**combined, 'occurrence-agent-role':
                                                combined['occurrence-agent-role'].drop(columns='agentRoleOrder')})
        self.assertIn("Resource 'occurrence-agent-role' is missing required field 'agentRoleOrder'.",
                      validation['errors'])

    def test_role_fields_follow_the_vendored_role_tables(self):
        self.assertEqual(ROLE_FIELDS[('material', 'collectedBy')],
                         RoleField('material', 'collectedBy', 'collectedByID', 'materialEntity_pk',
                                   'material-agent-role', 'materialEntity_fk', 'collectedBy'))
        self.assertIn(('identification', 'identifiedBy'), ROLE_FIELDS)
        self.assertIn(('event', 'eventConductedBy'), ROLE_FIELDS)
        # Assertion tables have no role table; their assertionBy stays a mapped value only.
        self.assertNotIn(('occurrence-assertion', 'assertionBy'), ROLE_FIELDS)
        with self.assertRaises(ValueError):
            build_agent_roles({}, str, fields=[('occurrence-assertion', 'assertionBy')])

    def test_single_name_forms_are_not_flagged(self):
        for value in ('Smith, J. R.', 'John Smith, Jr.', "O'Neil", 'van der Berg', 'Ole-Martin Ås',
                      'NTNU University Museum', 'Linnaeus'):
            self.assertIsNone(composite_name_reason(value), value)
        # Conservative: a compound surname with a conjunction is skipped and reported, never split.
        self.assertEqual(composite_name_reason('Ortega y Gasset'), 'conjunction')
        self.assertEqual(composite_name_reason('12345'), 'not_a_name')
