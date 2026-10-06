import tempfile
from collections import Counter
from pathlib import Path

import pandas as pd
from django.test import SimpleTestCase

from api.dwca_agents import (NAME_AGENT_REMARK, ROLE_FIELDS, RoleField, build_agent_roles, composite_name_reason,
                             split_agent_ids)
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
        self.assertEqual(len(frames['agent']), 2)
        self.assertEqual(len(frames['occurrence-agent-role']), 2)
        self.assertEqual(report['agent_roles']['roles_created'], {'occurrence-agent-role': 2})
        self.assertEqual(report['agent_roles']['agents_created'], {'name': 1, 'explicit_id': 0})
        self.assertEqual(len([row for row in report['row_crosswalk'] if row['target_table'] == 'occurrence-agent-role']), 2)
        self.assert_package_valid(frames)

    def test_exact_names_share_one_agent_by_default_and_can_be_kept_as_text(self):
        # ds563: 1,229 Agent rows for 'OceanPro AS', one per mention.
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,eventDate,occurrenceStatus\n'
            + b''.join(b'o%d,e%d,OceanPro AS,2025-01-01,present\n' % (n, n) for n in range(1, 6))
            + b'o6,e6,Kari Nordmann,2025-01-02,present\n')])
        plan = build_plan(archive)
        automatic = {item['id']: item for item in plan['automatic_choices']}
        self.assertFalse([item for item in plan['issues'] if item['id'].startswith(('agent-share:', 'agent-names'))])
        everyone = automatic['agent-names']
        self.assertEqual((everyone['default'], everyone['count'], everyone['names']), ('shared', 6, 2))
        self.assertIn('Linked 6 mentions of 2 names', everyone['reason'])
        share = next(item for item in automatic.values() if item['id'].startswith('agent-share:'))
        self.assertEqual((share['default'], share['source_value']), ('shared', 'OceanPro AS'))
        self.assertIn("Linked the 5 mentions of 'OceanPro AS' to one agent", share['reason'])
        self.assertFalse(any(option['assertion'] for item in (everyone, share) for option in item['options']))
        choices = {item['id']: item['options'][0]['value'] for item in plan['issues']}
        frames, report = convert(archive, plan, choices)
        self.assertEqual(sorted(frames['agent']['preferredAgentName']), ['Kari Nordmann', 'OceanPro AS'])
        self.assertEqual(len(frames['occurrence-agent-role']), 6)
        self.assertEqual(report['agent_roles']['agents_created'], {'name': 2, 'explicit_id': 0})
        self.assert_package_valid(frames)
        # Keeping one name as text only removes its agent and roles.
        one, one_report = convert(archive, plan, {**choices, share['id']: 'separate'})
        self.assertEqual(one['agent']['preferredAgentName'].tolist(), ['Kari Nordmann'])
        self.assertEqual(len(one['occurrence-agent-role']), 1)
        self.assertEqual(one_report['agent_roles']['unlinked_name_only'], 5)
        # One control keeps every name without an identifier as text only.
        none, none_report = convert(archive, plan, {**choices, 'agent-names': 'text'})
        self.assertNotIn('agent', none)
        self.assertNotIn('occurrence-agent-role', none)
        self.assertEqual(none_report['agent_roles']['unlinked_name_only'], 6)
        self.assert_package_valid(none)

    def assert_package_valid(self, frames):
        validation = validate_dwc_dp_resources(frames)
        self.assertTrue(validation['valid'], validation['errors'])
        self.assertFalse([warning for warning in validation['warnings'] if "resource 'agent'" in warning])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'agents.tar.gz'
            create_dwc_dp_archive(output, frames, 'Agents', 'Agent roles', include_eml=False)
            archive_validation = validate_dwc_dp_archive(output, require_eml=False)
            self.assertTrue(archive_validation['valid'], archive_validation['errors'])

    def test_repeated_name_only_values_share_one_agent_per_exact_name(self):
        # ds572 had 4,209 Agent rows for one collector, one per mention.
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,identifiedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,"Straumfors, Per",NTNU University Museum,2025-01-01,present\n'
            b'o2,e2,"Straumfors, Per",NTNU University Museum,2025-01-02,present\n'
            b'o3,e3," Straumfors,  Per ",NTNU University Museum,2025-01-03,present\n')
        result = build_agent_roles(frames, lambda *parts: _key(archive, *parts))
        tables = result.tables()
        roles = pd.DataFrame(tables['occurrence-agent-role'])
        self.assertEqual(sorted(agent['preferredAgentName'] for agent in tables['agent']),
                         ['NTNU University Museum', 'Straumfors, Per'])
        self.assertTrue(all(not agent.get('agentID') and agent['agentRemarks'] == NAME_AGENT_REMARK
                            for agent in tables['agent']))
        # Role rows stay per mention.
        self.assertEqual(Counter(roles['agentRole']), {'recordedBy': 3, 'identifiedBy': 3})
        self.assertEqual(set(roles['agentRoleOrder']), {1})
        self.assertEqual(set(roles['occurrence_fk']), set(frames['occurrence']['occurrence_pk']))
        self.assertEqual([(item['name'], item['mentions']) for item in result.report['repeated_names']],
                         [('NTNU University Museum', 3), ('Straumfors, Per', 3)])
        self.assertEqual(result.report['agents_created'], {'name': 2, 'explicit_id': 0})
        # The mapped name fields are unchanged and the result is deterministic.
        self.assertEqual(frames['occurrence']['recordedBy'].tolist()[2], ' Straumfors,  Per ')
        self.assertEqual(build_agent_roles(frames, lambda *parts: _key(archive, *parts)).rows, result.rows)
        self.assert_package_valid(merged(frames, result))

    def test_names_kept_as_text_get_no_agent(self):
        archive, frames = converted(
            b'occurrenceID,eventID,recordedBy,identifiedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,NTNU University Museum,2025-01-01,present\n'
            b'o2,e2,Ola Nordmann,NTNU University Museum,2025-01-02,present\n')
        key = lambda *parts: _key(archive, *parts)  # noqa: E731
        result = build_agent_roles(frames, key, unlinked_names=['Ola  Nordmann', 'Ola Nordman'])
        tables = result.tables()
        self.assertEqual([agent['preferredAgentName'] for agent in tables['agent']], ['NTNU University Museum'])
        self.assertEqual(set(pd.DataFrame(tables['occurrence-agent-role'])['agentRole']), {'identifiedBy'})
        self.assertEqual(result.report['unlinked_name_only'], 2)
        self.assertEqual(result.report['unlinked_names_unused'], ['Ola Nordman'])
        self.assert_package_valid(merged(frames, result))
        self.assertEqual(build_agent_roles(frames, key, link_names=False).rows, [])

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
        self.assertEqual(result.report['skipped'], {'comma_list': 1, 'conjunction': 1, 'delimited_list': 2,
                                                    'incomplete_group': 1, 'placeholder': 1})
        self.assertEqual(result.report['fields']['occurrence.recordedBy']['name'], 3)
        skipped = {item['value']: (item['reason'], item['first_row']) for item in result.report['skipped_values']}
        self.assertEqual(skipped['P. Hansen | K. Olsen'], ('delimited_list', 2))
        self.assertEqual(skipped['Per Hansen, Kari Olsen'], ('comma_list', 4))
        # Spelling variants are surfaced for review but stay separate agents.
        self.assertEqual(sorted(agent['preferredAgentName'] for agent in result.tables()['agent']),
                         ['Hansen, P', 'Hansen, P.', 'P. Hansen'])
        self.assertEqual(result.report['variant_groups'], [['Hansen, P', 'Hansen, P.', 'P. Hansen']])
        self.assert_package_valid(merged(frames, result))

    def test_placeholder_names_never_become_agents(self):
        for value in ('unknown', 'Ukjent', 'NA', 'n/a', 'anon', 'Anon.', 'anonymous', '-', '?', 'none', 'NULL',
                      'not recorded', 'Not  Recorded', 'unknown.', 'N.N.',
                      # Bracketed, role-noun and abbreviated forms (review of 8626525).
                      '[unknown]', '(unknown)', '[Ukjent]', 'Unknown collector', 'Unknown observer',
                      'Anonymous collector', 'Ukjent samler', 'Samler ukjent', 'Ikke angitt', 'Ikke kjent', 's. n.',
                      's.n', 'N.N', 'unknown?', '?unknown', 'Indet.', '<NA>', '"Unknown"', 'Collector'):
            self.assertEqual(composite_name_reason(value), 'placeholder', value)
        for value in ('Nordmann, Ola', 'N. Nordmann', 'Ole Ukjentsen', 'Indetta Hansen', 'NTNU University Museum'):
            self.assertIsNone(composite_name_reason(value), value)
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Ukjent,2025-01-01,present\no2,e2,Ukjent,2025-01-02,present\no3,e3,NA,2025-01-03,present\n')])
        plan = build_plan(archive)
        self.assertFalse([item for item in plan['automatic_choices'] if item['id'].startswith(('agent-share:', 'agent-names'))])
        frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
        self.assertNotIn('agent', frames)
        self.assertEqual(report['agent_roles']['skipped'], {'placeholder': 3})

    def test_doubtful_names_stay_text(self):
        for value in ('Rosaag?', 'Umlauf ?', 'Zernich?', 'Flage?, Gudmund', 'Ø.O.(Ørjan Olsen?)'):
            self.assertEqual(composite_name_reason(value), 'uncertain', value)
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Rosaag?,2025-01-01,present\no2,e2,Rosaag?,2025-01-02,present\no3,e3,Rosaag,2025-01-03,present\n')])
        plan = build_plan(archive)
        names = next(item for item in plan['automatic_choices'] if item['id'] == 'agent-names')
        self.assertEqual((names['count'], names['names']), (1, 1))
        frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
        self.assertEqual(frames['agent']['preferredAgentName'].tolist(), ['Rosaag'])
        self.assertEqual(report['agent_roles']['skipped'], {'uncertain': 2})

    def test_missing_value_ids_count_as_empty(self):
        # recordedByID=NA used to skip the mention as id_not_single_iri and lose its name agent.
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,recordedByID,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,NA,2025-01-01,present\no2,e2,Ola Nordmann,,2025-01-02,present\n')])
        plan = build_plan(archive)
        names = next(item for item in plan['automatic_choices'] if item['id'] == 'agent-names')
        self.assertEqual((names['count'], names['names']), (2, 1))
        frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
        self.assertEqual(frames['agent']['preferredAgentName'].tolist(), ['Ola Nordmann'])
        self.assertEqual(len(frames['occurrence-agent-role']), 2)
        self.assertEqual(report['agent_roles']['skipped'], {})
        self.assertEqual(report['agent_mapping']['non_single_id_cells'], 0)

    def test_name_choice_counts_match_the_linked_mentions_and_raise_a_notice(self):
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventID,recordedBy,identifiedBy,eventDate,occurrenceStatus\n'
            b'o1,e1,Ola Nordmann,Kari Nordmann,2025-01-01,present\no2,e2,Ola Nordmann,,2025-01-02,present\n')])
        plan = build_plan(archive)
        names = next(item for item in plan['automatic_choices'] if item['id'] == 'agent-names')
        notice = next(item for item in plan['warnings'] if item['id'] == 'agent-names')
        self.assertIn('Linked 3 mentions of 2 names', notice['reason'])
        self.assertEqual(notice['reason'], names['reason'])
        choices = {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}
        frames, report = convert(archive, plan, choices)
        linked = sum(stats['name'] for stats in report['agent_roles']['fields'].values())
        self.assertEqual((linked, len(frames['agent'])), (names['count'], names['names']))
        self.assertIn('agent-names', {item.get('id') for item in report['warnings']})
        # A column remapped to another agent role was counted too, so its names keep their choice.
        recorded = next(column for column in plan['columns'] if column['term'].endswith('/recordedBy'))
        other = next(option['value'] for option in recorded['options']
                     if option['value'] != recorded['default'] and option['value'].endswith(('By', 'eventConductedBy')))
        remapped, remapped_report = convert(archive, plan, {**choices, recorded['id']: other})
        self.assertEqual(sum(stats['name'] for stats in remapped_report['agent_roles']['fields'].values()), names['count'])

    def test_explicit_ids_decide_identity_not_names(self):
        other = 'https://orcid.org/0000-0001-5109-3700'
        archive = read_inputs([('occurrence.csv', (
            'occurrenceID,eventID,recordedBy,recordedByID,eventDate,occurrenceStatus\n'
            f'o1,e1,Ola Nordmann,{ORCID},2025-01-01,present\n'
            f'o2,e2,Ola Nordmann,{other},2025-01-02,present\n'
            f'o3,e3,Ola Nordmann,,2025-01-03,present\n').encode())])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
        # Two different ORCIDs with the same name stay two agents; the name-only mention gets its own
        # name agent and is never merged into either ID.
        agents = frames['agent']
        self.assertEqual(sorted(agents['agentID']), ['', other, ORCID])
        self.assertEqual(set(agents['preferredAgentName']), {'Ola Nordmann'})
        roles = frames['occurrence-agent-role']
        self.assertEqual(len(roles), 3)
        self.assertEqual(len(set(roles['agent_fk'])), 3)
        self.assertEqual(report['agent_roles']['agents_created']['name'], 1)
        # The name agent that shares its name with ID agents is reported.
        self.assertEqual(report['agent_roles']['name_agents_matching_id_agents'], 1)
        self.assertEqual(report['agent_roles']['name_agents_matching_id_agents_examples'], ['Ola Nordmann'])
        self.assert_package_valid(frames)

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


    def test_id_lists_link_one_agent_per_iri_without_positional_names(self):
        # ds560: 25 recordedBy/recordedByID list pairs were skipped as id_not_single_iri.
        horvath, bryn, liahjell = ('https://orcid.org/0000-0002-6017-5385', 'https://orcid.org/0000-0003-4712-8266',
                                   'https://orcid.org/0009-0000-2845-7836')
        archive = read_inputs([('occurrence.csv', (
            'occurrenceID,eventID,recordedBy,recordedByID,eventDate,occurrenceStatus\n'
            f'o1,e1,Peter  Horvath,{horvath},2025-01-01,present\n'
            f'o2,e2,Peter Horvath | Gunnar Thorsen Liahjell,{horvath} | {liahjell},2025-01-02,present\n'
            # Names listed in a different order from the IDs: no name is taken from a position.
            f'o3,e3,Gunnar Thorsen Liahjell|Anders Bryn,{bryn}|{liahjell},2025-01-03,present\n'
            f'o4,e4,Anders Bryn | Gunnar Thorsen Liahjell,{bryn},2025-01-04,present\n'
            f'o5,e5,,{horvath} | {bryn},2025-01-05,present\n'
            f'o6,e6,Peter Horvath | | Anders Bryn,{horvath} | | {bryn},2025-01-06,present\n'
            f'o7,e7,Peter Horvath | Peter Horvath,{horvath} | {horvath},2025-01-07,present\n'
            f'o8,e8,Peter Horvath | Anders Bryn,0000-0002-6017-5385 | 0000-0003-4712-8266,2025-01-08,present\n'
            f'o9,e9,Peter Horvath,{horvath},2025-01-09,present\n'
            ).encode())])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, {issue['id']: issue['options'][0]['value'] for issue in plan['issues']})
        agents = frames['agent'].set_index('agentID')
        self.assertEqual(sorted(agents.index), sorted([horvath, bryn, liahjell]))
        # Horvath stands alone in o1 and o9 (whitespace collapsed, so one name); Bryn and Liahjell never
        # stand alone, so they get no name.
        self.assertEqual(agents['preferredAgentName'].to_dict(), {horvath: 'Peter Horvath', bryn: '', liahjell: ''})
        roles = frames['occurrence-agent-role']
        occurrence = dict(zip(frames['occurrence']['occurrenceID'], frames['occurrence']['occurrence_pk']))
        # agentRoleOrder is the source ID order.
        for source, expected in (('o2', [horvath, liahjell]), ('o3', [bryn, liahjell]), ('o5', [horvath, bryn])):
            listed = roles[roles['occurrence_fk'] == occurrence[source]].sort_values('agentRoleOrder')
            self.assertEqual(listed['agent_fk'].tolist(), [agents.loc[identifier, 'agent_pk'] for identifier in expected])
            self.assertEqual(listed['agentRoleOrder'].astype(int).tolist(), [1, 2])
        agent_roles = report['agent_roles']
        self.assertEqual(agent_roles['fields']['occurrence.recordedBy']['linked_by_id_list'], 3)
        # A name list beside one ID, an empty segment, a repeated ID and bare ORCIDs are not split.
        self.assertEqual(agent_roles['skipped'], {'id_name_count_mismatch': 1, 'id_not_single_iri': 3})
        for source in ('o4', 'o6', 'o7', 'o8'):
            self.assertNotIn(occurrence[source], set(roles['occurrence_fk']))
        # Only the unsplit list cells count as non-single ID cells.
        self.assertEqual(report['agent_mapping']['non_single_id_cells'], 3)
        self.assert_package_valid(frames)

    def test_split_agent_ids_edge_cases(self):
        from api.dwca_conversion import _single_agent_iri as single
        a, b = 'https://orcid.org/0000-0002-6017-5385', 'https://orcid.org/0000-0003-4712-8266'
        self.assertEqual(split_agent_ids('A | B', f'{a} | {b}', single), [a, b])
        self.assertEqual(split_agent_ids('B | A', f'{a}|{b}', single), [a, b])
        self.assertEqual(split_agent_ids('', f'{a}|{b}', single), [a, b])
        self.assertIsNone(split_agent_ids('A', a, single))                    # not a list
        self.assertIsNone(split_agent_ids('A | B', f'{a} | ', single))          # empty segment
        self.assertIsNone(split_agent_ids('A | A', f'{a} | {a}', single))       # duplicate ID
        self.assertIsNone(split_agent_ids('A | B', '0000-0002-6017-5385 | 0000-0003-4712-8266', single))  # bare ORCIDs
        self.assertIsNone(split_agent_ids('A | B | C', f'{a} | {b}', single))   # count mismatch
        self.assertIsNone(split_agent_ids('A & B | C', f'{a} | {b}', single))   # composite name

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
        frames, report = convert(archive, plan, choices)
        self.assertTrue(report['validation']['valid'], report['validation']['errors'])
        self.assertNotIn('agentID', frames['agent'])
        self.assertEqual(sorted(frames['agent']['preferredAgentName']),
                         ['Anfinnsen, Martin T.', 'Collett, Robert', 'Perlini, Enrico Maria'])
        self.assertEqual(Counter(frames['occurrence-agent-role']['agentRole']), {'recordedBy': 3, 'identifiedBy': 2})
        self.assert_package_valid(frames)
        with tempfile.TemporaryDirectory() as directory:
            descriptor = create_dwc_dp_archive(Path(directory) / 'ds568.tar.gz', frames, 'ds568', 'Agents', include_eml=False)
        occurrence = next(resource for resource in descriptor['resources'] if resource['name'] == 'occurrence')
        declared = [key['fields'] for keys in ('foreignKeys', 'weakForeignKeys') for key in occurrence['schema'].get(keys, [])]
        self.assertNotIn('recordedByID', declared)
        self.assertNotIn('identifiedByID', declared)
        # With every name kept as text there is no agent table, and the package is still valid.
        unlinked, _ = convert(archive, plan, {**choices, 'agent-names': 'text'})
        self.assertNotIn('agent', unlinked)
        self.assert_package_valid(unlinked)

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
