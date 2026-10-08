"""Scientific-name checks for archive conversion: parsing, COL matching, review decisions and the convert-time overlay.

All HTTP is mocked; nothing here reaches GBIF or ChecklistBank.
"""
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from django.test import SimpleTestCase, override_settings

from api import conversion_names as names
from api import taxon_matching
from api.conversion_jobs import process_next_conversion
from api.dwca_import import read_inputs
from api.models import DwcConversion, DwcConversionJob, Table
from api.taxon_matching import TaxonServiceError
from api import conversion_chat
from api.test_conversion_review import AI, QUERY, ConversionTestCase, answer, reply, requested
from api.dwca_conversion import build_plan, convert
from api.test_dwca_conversion import DWC, decisions_for, manifest

RELEASE = {'checklistKey': 'col-key', 'alias': 'COL26.6 XR', 'checklistBankDatasetKey': '315557'}
ID_MATCHES = json.loads((Path(__file__).parent / 'testdata' / 'col_v2_id_matches.json').read_text())
# The converter copies each source name to verbatimIdentification itself.
OCCURRENCE = (b'occurrenceID,scientificName,kingdom,taxonRank,scientificNameAuthorship\n'
              b'a,Aus bus L.,Animalia,,\n'
              b'b,Aus bus L.,Animalia,,\n'
              b'c,Cus dus (Smith) Jones 1900,Plantae,species,Jones\n'
              b'd,Eus sp.,,,\n')


def parser_item(complete, canonical, marker=None, type='SCIENTIFIC', parsed=True, authorship=None):
    return {'scientificName': complete, 'type': type, 'parsed': parsed, 'parsedPartially': False, 'canonicalName': canonical,
            'canonicalNameWithMarker': canonical, 'canonicalNameComplete': complete, 'authorship': authorship,
            **({'rankMarker': marker} if marker else {})}


PARSED = {
    'Aus bus L.': parser_item('Aus bus L.', 'Aus bus', 'sp.', authorship='L.'),
    'Cus dus (Smith) Jones 1900': parser_item('Cus dus (Smith) Jones, 1900', 'Cus dus', 'sp.', authorship='Jones'),
    'Eus sp.': parser_item('Eus spec.', 'Eus spec.', type='INFORMAL'),
}


def col_summary(match_type, key=None, name=None, authorship='', rank='SPECIES', alternatives=(), status='ACCEPTED'):
    payload = {'diagnostics': {'matchType': match_type, 'confidence': 97, 'alternatives': list(alternatives)}}
    if key:
        payload.update({'usage': {'key': key, 'name': f'{name} {authorship}'.strip(), 'canonicalName': name, 'authorship': authorship,
                                  'rank': rank, 'status': status}, 'classification': []})
    return taxon_matching.summarize_match(payload)


def alternative(key, name, authorship='', rank='SPECIES'):
    return {'usage': {'key': key, 'name': name, 'canonicalName': name, 'authorship': authorship, 'rank': rank, 'status': 'ACCEPTED'},
            'diagnostics': {'matchType': 'VARIANT', 'confidence': 80}}


MATCHES = {
    'Aus bus L.': col_summary('EXACT', 'COL-AUS', 'Aus bus', 'L.'),
    'Cus dus (Smith) Jones 1900': col_summary('VARIANT', 'COL-CUS', 'Cus dus', '(Smith, 1900)',
                                              alternatives=[alternative('COL-CUS2', 'Cus dux', 'Jones')]),
    'Eus': col_summary('EXACT', 'COL-EUS', 'Eus', 'Smith', rank='GENUS'),
}


def fake_match(queries, deadline=None, **kwargs):
    return [MATCHES.get(query['scientificName']) or col_summary('NONE') for query in queries]


def fake_parse(labels, deadline=None):
    return [names.summarize_parse(label, PARSED[label]) for label in labels]


def mocked(match=fake_match, parse=fake_parse):
    return [patch.object(names, 'match_col', side_effect=match), patch.object(names, 'parse_names', side_effect=parse),
            patch.object(names, 'col_release', return_value=RELEASE)]


class Mocks:
    """Enter the service patches for a block and expose the mocks."""
    def __init__(self, match=fake_match, parse=fake_parse):
        self.patches = mocked(match, parse)

    def __enter__(self):
        self.match, self.parse, self.release = [item.start() for item in self.patches]
        return self

    def __exit__(self, *exc):
        for item in self.patches:
            item.stop()


class ParseTests(SimpleTestCase):
    def test_full_authorship_is_taken_from_the_complete_name_and_split_is_lossless(self):
        parsed = names.summarize_parse('Abies alba subsp. alpina (L.) Mill., 1768', {
            'type': 'SCIENTIFIC', 'parsed': True, 'parsedPartially': False, 'authorship': 'Mill.', 'rankMarker': 'subsp.',
            'canonicalName': 'Abies alba alpina', 'canonicalNameWithMarker': 'Abies alba subsp. alpina',
            'canonicalNameComplete': 'Abies alba subsp. alpina (L.) Mill., 1768'})
        self.assertEqual((parsed['canonical'], parsed['authorship'], parsed['rank']),
                         ('Abies alba subsp. alpina', '(L.) Mill., 1768', 'subspecies'))
        self.assertTrue(parsed['usable'] and parsed['lossless'] and parsed['splits'])

    def test_a_parse_that_renames_the_text_is_usable_but_not_lossless(self):
        parsed = names.summarize_parse('blue whale', parser_item('Blue whale', 'Blue whale', 'sp.'))
        self.assertTrue(parsed['usable'])
        self.assertFalse(parsed['lossless'])
        dropped = names.summarize_parse('Carabus (Morphocarabus) kruberi Fischer, 1823',
                                        parser_item('Carabus kruberi Fischer, 1823', 'Carabus kruberi', 'sp.'))
        self.assertFalse(dropped['lossless'])

    def test_informal_and_unparsed_names_are_not_usable(self):
        self.assertFalse(names.summarize_parse('Eus sp.', PARSED['Eus sp.'])['usable'])
        self.assertFalse(names.summarize_parse('x', parser_item('x', 'x', parsed=False))['usable'])
        self.assertFalse(names.summarize_parse('x', None)['usable'])

    def test_parse_names_posts_one_batch_to_the_gbif_parser(self):
        response = MagicMock(status_code=200)
        response.json.return_value = [PARSED['Aus bus L.'], PARSED['Cus dus (Smith) Jones 1900']]
        with patch('api.taxon_matching.requests.request', return_value=response) as request:
            result = names.parse_names(['Aus bus L.', 'Cus dus (Smith) Jones 1900'])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ('POST', names.GBIF_PARSER_URL))
        self.assertEqual(request.call_args.kwargs['json'], ['Aus bus L.', 'Cus dus (Smith) Jones 1900'])
        self.assertEqual([item['canonical'] for item in result], ['Aus bus', 'Cus dus'])
        self.assertEqual(names.parse_names([]), [])

    def test_parser_result_count_must_match(self):
        response = MagicMock(status_code=200)
        response.json.return_value = [PARSED['Aus bus L.']]
        with patch('api.taxon_matching.requests.request', return_value=response):
            with self.assertRaises(TaxonServiceError):
                names.parse_names(['Aus bus L.', 'Cus dus'])

    def test_parser_outage_raises_a_service_error(self):
        with patch('api.taxon_matching.requests.request', side_effect=taxon_matching.requests.ConnectionError('down')), \
                patch('api.taxon_matching.time.sleep'):
            with self.assertRaises(TaxonServiceError):
                names.parse_names(['Aus bus L.'])


class CollectTests(SimpleTestCase):
    def collect(self, content, plan_id='plan', **kwargs):
        return names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': plan_id})

    def test_distinct_labels_are_counted_with_consistent_context_only(self):
        state = self.collect(b'occurrenceID,scientificName,kingdom,family,taxonRank\n'
                             b'a,Aus  bus L.,Animalia,Fam,Species\nb,Aus bus L.,Animalia,Other,\nc,Cus dus,,,\nd,,Animalia,,\n')
        self.assertEqual((state['plan_id'], state['status']), ('plan', 'pending'))
        aus, cus = state['labels']
        self.assertEqual((aus['label'], aus['rows'], aus['tables']), ('Aus bus L.', 2, {'occurrence': 2}))
        # The rows agree on kingdom but not family, so only kingdom is a matching hint.
        self.assertEqual(aus['hints'], {'kingdom': 'Animalia'})
        self.assertEqual(aus['source_rank'], 'species')
        self.assertEqual((cus['hints'], cus['source_rank'], cus['rows']), ({}, None, 1))

    def test_qualifiers_are_recorded_and_extra_labels_are_counted_not_dropped_silently(self):
        with patch.object(names, 'MAX_LABELS', 1):
            state = self.collect(b'occurrenceID,scientificName\na,Aus bus\nb,Aus bus\nc,Zus sp.\n')
        self.assertEqual([record['label'] for record in state['labels']], ['Aus bus'])
        self.assertEqual(state['truncated'], 1)
        self.assertEqual(self.collect(b'occurrenceID,scientificName\na,Zus sp.\n')['labels'][0]['qualifier'], 'sp.')

    def test_archives_without_names_have_nothing_to_check(self):
        state = self.collect(b'occurrenceID,locality\na,Oslo\n')
        self.assertEqual((state['status'], state['labels']), ('none', []))

    def test_source_ids_require_one_external_uri_and_qualifiers_are_collected_from_rows(self):
        state = self.collect(b'occurrenceID,scientificName,scientificNameID,taxonID,identificationQualifier\n'
                             b'a,Consistent, urn:lsid:worms:1 ,,sp\n'
                             b'b,Consistent,urn:lsid:worms:1,,sp\n'
                             b'c,Local,local-id,,\n'
                             b'd,Conflict,https://example.org/one,https://example.org/taxon-a,\n'
                             b'e,Conflict,https://example.org/two,https://example.org/taxon-b,\n'
                             b'f,Blank,,,\n')
        records = {record['label']: record for record in state['labels']}
        self.assertEqual(records['Consistent']['source_ids'], {'scientificNameID': 'urn:lsid:worms:1'})
        self.assertEqual(records['Consistent']['source_qualifiers'], ['sp.'])
        self.assertEqual(records['Consistent']['source_qualifier_rows'], 2)
        self.assertFalse(records['Consistent']['qualifiers_truncated'])
        self.assertEqual(records['Local']['source_ids'], {})
        self.assertEqual(records['Conflict']['source_ids'], {})
        self.assertEqual(records['Blank']['source_ids'], {})

    def test_source_qualifier_values_are_bounded_and_report_truncation(self):
        rows = ''.join(f'a{i},Shared,,,{qualifier}\n' for i, qualifier in enumerate(('a', 'b', 'c', 'd', 'e', 'f')))
        state = self.collect(('occurrenceID,scientificName,scientificNameID,taxonID,identificationQualifier\n' + rows).encode())
        record = state['labels'][0]
        self.assertEqual(record['source_qualifiers'], ['a.', 'b.', 'c.', 'd.', 'e.'])
        self.assertEqual(record['source_qualifier_rows'], 6)
        self.assertTrue(record['qualifiers_truncated'])

    def test_mixed_uncertain_source_qualifiers_allow_blank_rows(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,scientificName,identificationQualifier\n'
                                                  b'a,Pseudocalanus,sp.\nb,Pseudocalanus,spp.\nc,Pseudocalanus,\n')])
        record = names.collect_state(archive, {'id': 'plan'})['labels'][0]
        self.assertEqual(record['source_qualifiers'], ['sp.', 'spp.'])
        self.assertEqual(record['source_qualifier_rows'], 2)
        self.assertEqual(names.qualifier_kind(record), 'uncertain')

    def test_source_qualifiers_are_deduplicated_after_normalizing_case_and_periods(self):
        values = ('sp', 'Sp', 'SP.', 'spp', 'Spp', 'indet')
        rows = ''.join(f'a{i},Shared,{value}\n' for i, value in enumerate(values))
        archive = read_inputs([('occurrence.csv', ('occurrenceID,scientificName,identificationQualifier\n' + rows).encode())])
        record = names.collect_state(archive, {'id': 'plan'})['labels'][0]
        self.assertEqual(record['source_qualifiers'], ['indet.', 'sp.', 'spp.'])
        self.assertEqual(record['source_qualifier_counts'], {'indet.': 1, 'sp.': 3, 'spp.': 2})
        self.assertFalse(record['qualifiers_truncated'])
        self.assertEqual(names.qualifier_kind(record), 'uncertain')


class CheckChunkTests(SimpleTestCase):
    def fixture_match(self, queries, deadline=None, **kwargs):
        entries = {json.dumps(item['query'], sort_keys=True): item for item in ID_MATCHES}
        return [taxon_matching.summarize_match(entries[json.dumps(query, sort_keys=True)]['response']) for query in queries]

    def item(self, label, ids=None, match=False, parse=True):
        return {'label': label, 'query': {'scientificName': label}, 'qualifier': None, 'ids': ids or {},
                'match': match, 'parse': parse}

    def test_source_ids_promote_only_matching_exact_name_results(self):
        items = [self.item('Calanus', {'scientificNameID': 'urn:lsid:marinespecies.org:taxname:104152'}),
                 self.item('Chaetognatha', {'scientificNameID': 'urn:lsid:marinespecies.org:taxname:2081'})]
        with patch.object(names, 'match_col', side_effect=self.fixture_match) as matcher:
            results, error = names.check_chunk(items, deadline=None)
        self.assertIsNone(error)
        for label in ('Calanus', 'Chaetognatha'):
            result = results[label]['match']
            self.assertEqual(result['usage']['scientificName'], label)
            self.assertEqual(result['disambiguatedBy'], ['scientificNameID'])
            self.assertEqual(result['nameMatch']['status'], 'higher_rank')
        self.assertEqual(len(matcher.call_args_list), 2)
        self.assertEqual(matcher.call_args_list[1].args[0], [
            {'scientificName': 'Calanus', 'scientificNameID': 'urn:lsid:marinespecies.org:taxname:104152'},
            {'scientificName': 'Chaetognatha', 'scientificNameID': 'urn:lsid:marinespecies.org:taxname:2081'}])

    def test_higher_rank_match_without_source_id_stays_higher_rank(self):
        with patch.object(names, 'match_col', side_effect=self.fixture_match):
            results, error = names.check_chunk([self.item('Calanus'), self.item('Chaetognatha')], deadline=None)
        self.assertIsNone(error)
        for label in ('Calanus', 'Chaetognatha'):
            self.assertEqual(results[label]['match']['status'], 'higher_rank')
            self.assertNotIn('idCheck', results[label]['match'])

    def test_id_not_found_stays_a_check_and_exact_names_skip_step_two(self):
        items = [self.item('Oncaea', {'taxonID': 'urn:lsid:marinespecies.org:taxname:128690)'}),
                 self.item('Parathemisto libellula', {'scientificNameID': 'urn:lsid:marinespecies.org:taxname:156528'}),
                 self.item('Metridia longa', {'taxonID': 'urn:lsid:marinespecies.org:taxname:104632'})]
        with patch.object(names, 'match_col', side_effect=self.fixture_match) as matcher:
            results, error = names.check_chunk(items, deadline=None)
        self.assertIsNone(error)
        self.assertEqual(results['Oncaea']['match']['idCheck']['outcome'], 'not_found')
        self.assertEqual(results['Oncaea']['match']['idCheck']['issues'], ['TAXON_ID_NOT_FOUND'])
        self.assertEqual(results['Parathemisto libellula']['match']['usage']['scientificName'], 'Parathemisto libellula')
        self.assertEqual(results['Metridia longa']['match']['usage']['scientificName'], 'Metridia longa')
        # Only Oncaea (higher rank by name) is matched again with its ID; the exact names never are.
        self.assertEqual(len(matcher.call_args_list), 2)
        self.assertEqual(matcher.call_args_list[1].args[0],
                         [{'scientificName': 'Oncaea', 'taxonID': 'urn:lsid:marinespecies.org:taxname:128690)'}])

    def test_higher_rank_labels_can_be_promoted_by_source_ids(self):
        items = [self.item('Polychaeta', {'taxonID': 'urn:lsid:marinespecies.org:taxname:883'}),
                 self.item('Isopoda', {'taxonID': 'urn:lsid:marinespecies.org:taxname:1131'})]
        with patch.object(names, 'match_col', side_effect=self.fixture_match):
            results, error = names.check_chunk(items, deadline=None)
        self.assertIsNone(error)
        for label in ('Polychaeta', 'Isopoda'):
            self.assertEqual(results[label]['match']['usage']['scientificName'], label)
            self.assertEqual(results[label]['match']['disambiguatedBy'], ['taxonID'])

    def test_step_two_failure_discards_matches_but_keeps_parses(self):
        calls = 0
        def failing_match(queries, deadline=None, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [taxon_matching.summarize_match(ID_MATCHES[0]['response'])]
            raise TaxonServiceError('step two is unavailable')
        parsed = names.summarize_parse('Calanus', parser_item('Calanus', 'Calanus'))
        with patch.object(names, 'match_col', side_effect=failing_match), patch.object(names, 'parse_names', return_value=[parsed]):
            results, error = names.check_chunk([self.item('Calanus', {'scientificNameID': 'urn:lsid:marinespecies.org:taxname:104152'}, parse=False)],
                                               deadline=None)
        self.assertEqual(str(error), 'step two is unavailable')
        self.assertNotIn('match', results.get('Calanus', {}))
        self.assertEqual(results['Calanus']['parsed'], parsed)


def frames(**overrides):
    base = {'occurrence': pd.DataFrame([
        {'occurrence_pk': 'o1', 'scientificName': 'Aus bus L.', 'verbatimIdentification': 'Aus bus L.', 'scientificNameAuthorship': '', 'taxonRank': ''},
        {'occurrence_pk': 'o2', 'scientificName': 'Aus bus L.', 'verbatimIdentification': 'Aus  bus L.', 'scientificNameAuthorship': 'Linnaeus', 'taxonRank': 'species'},
        {'occurrence_pk': 'o3', 'scientificName': '', 'verbatimIdentification': 'Eus sp.', 'scientificNameAuthorship': '', 'taxonRank': ''},
        {'occurrence_pk': 'o4', 'scientificName': '', 'verbatimIdentification': 'Unreviewed', 'scientificNameAuthorship': '', 'taxonRank': ''},
    ]), 'identification': pd.DataFrame([
        {'identification_pk': 'i1', 'occurrence_fk': 'o1', 'scientificName': 'Aus bus L.'},
        {'identification_pk': 'i4', 'occurrence_fk': 'o4', 'scientificName': ''},
    ])}
    return {**base, **overrides}


def review(decisions, status='complete'):
    return {'plan_id': 'plan', 'status': status, 'error': '', 'col_release': RELEASE, 'truncated': 0, 'decisions': decisions,
            'labels': [{'label': 'Aus bus L.', 'rows': 3}, {'label': 'Eus sp.', 'rows': 1}, {'label': 'Unreviewed', 'rows': 1}]}


def apply(frames, state, sources=None):
    """Overlay with each row's source name taken from its verbatimIdentification column unless given."""
    if sources is None:
        sources = {table: frame['verbatimIdentification'].tolist() if 'verbatimIdentification' in frame.columns else [''] * len(frame)
                   for table, frame in frames.items()}
    return names.apply_name_decisions(frames, state, sources)


def decision(kind, name, authorship=None, rank=None, **extra):
    return {'decision': kind, 'source': extra.pop('source', 'parser'), 'scientificName': name, 'scientificNameAuthorship': authorship,
            'taxonRank': rank, 'by': 'user', 'at': '2026-10-04T10:00:00+00:00', **extra}


class OverlayTests(SimpleTestCase):
    def test_a_parsed_split_sets_name_and_fills_only_blank_authorship_and_rank(self):
        source = frames()
        before = copy.deepcopy({key: value.to_dict('records') for key, value in source.items()})
        result, section = apply(source, review({'Aus bus L.': decision('parsed', 'Aus bus', 'L.', 'species')}))
        occurrence = result['occurrence'].to_dict('records')
        self.assertEqual([row['scientificName'] for row in occurrence], ['Aus bus', 'Aus bus', '', ''])
        # Supplied authorship is never overwritten; a blank cell is filled.
        self.assertEqual([row['scientificNameAuthorship'] for row in occurrence], ['L.', 'Linnaeus', '', ''])
        self.assertEqual([row['taxonRank'] for row in occurrence], ['species', 'species', '', ''])
        # Whitespace-different labels match, and verbatimIdentification is never touched.
        self.assertEqual(occurrence[1]['verbatimIdentification'], 'Aus  bus L.')
        self.assertEqual(result['occurrence']['verbatimIdentification'].tolist(), source['occurrence']['verbatimIdentification'].tolist())
        # An identification row with no verbatimIdentification of its own is left untouched, never borrowed from its occurrence.
        pd.testing.assert_frame_equal(result['identification'], source['identification'])
        self.assertEqual({key: value.to_dict('records') for key, value in source.items()}, before)  # inputs are not mutated
        entry = section['entries'][0]
        self.assertEqual((entry['rows'], entry['authorshipKept']), ({'occurrence': 2}, 1))

    def test_identification_rows_with_their_own_verbatim_text_are_matched_directly(self):
        identification = pd.DataFrame([{'identification_pk': 'i1', 'occurrence_fk': 'o4', 'verbatimIdentification': 'Aus bus L.',
                                        'scientificName': '', 'scientificNameAuthorship': ''}])
        result, _ = apply(frames(identification=identification), review({'Aus bus L.': decision('parsed', 'Aus bus', 'L.')}))
        row = result['identification'].iloc[0]
        self.assertEqual((row['scientificName'], row['scientificNameAuthorship'], row['verbatimIdentification']), ('Aus bus', 'L.', 'Aus bus L.'))

    def test_identification_rows_follow_their_own_name_not_their_occurrences(self):
        occurrence = pd.DataFrame([{'occurrence_pk': 'o1', 'scientificName': '', 'verbatimIdentification': 'Species A'}])
        identification = pd.DataFrame([
            {'identification_pk': 'i1', 'occurrence_fk': 'o1', 'scientificName': '', 'verbatimIdentification': 'Species B'},
            {'identification_pk': 'i2', 'occurrence_fk': 'o1', 'scientificName': '', 'verbatimIdentification': ''},
            {'identification_pk': 'i3', 'occurrence_fk': 'o1', 'scientificName': '', 'verbatimIdentification': 'Species A'}])
        state = {'plan_id': 'plan', 'status': 'complete', 'labels': [{'label': 'Species A'}, {'label': 'Species B'}], 'truncated': 0}

        def names_for(decisions):
            result, section = apply({'occurrence': occurrence, 'identification': identification},
                                                         {**state, 'decisions': decisions})
            return result['occurrence']['scientificName'].tolist(), result['identification']['scientificName'].tolist(), section

        only_a = names_for({'Species A': decision('parsed', 'Species Alpha')})
        self.assertEqual(only_a[:2], (['Species Alpha'], ['', '', 'Species Alpha']))  # the "Species B" record is not rewritten to A
        only_b = names_for({'Species B': decision('parsed', 'Species Beta')})
        self.assertEqual(only_b[:2], ([''], ['Species Beta', '', '']))  # approving B reaches its own record, not the occurrence
        self.assertEqual(only_b[2]['entries'][0]['rows'], {'identification': 1})

    def test_keep_fills_blank_names_only_and_empty_clears(self):
        result, _ = apply(frames(), review({
            'Aus bus L.': decision('keep', None, source='verbatim'), 'Eus sp.': decision('empty', '', source='none')}))
        occurrence = result['occurrence']
        self.assertEqual(occurrence['scientificName'].tolist(), ['Aus bus L.', 'Aus bus L.', '', ''])
        blank = frames()
        blank['occurrence'].loc[0, 'scientificName'] = ''
        blank['occurrence'].loc[1, 'scientificName'] = ''
        result, _ = apply(blank, review({'Aus bus L.': decision('keep', None, source='verbatim')}))
        # The supplied text fills a blank scientificName exactly as written, including whitespace.
        self.assertEqual(result['occurrence']['scientificName'].tolist()[:2], ['Aus bus L.', 'Aus  bus L.'])
        cleared = frames()
        result, section = apply(cleared, review({'Aus bus L.': decision('empty', '', source='none')}))
        self.assertEqual(result['occurrence']['scientificName'].tolist()[:2], ['', ''])
        # No name is published, so neither is an authorship or rank that belonged to it.
        self.assertEqual(result['occurrence']['scientificNameAuthorship'].tolist()[:2], ['', ''])
        self.assertEqual(result['occurrence']['taxonRank'].tolist()[:2], ['', ''])
        self.assertEqual(section['entries'][0]['ranksReplaced'], {'species': 1})

    def test_col_decisions_carry_provenance_into_the_report(self):
        col = decision('col', 'Aus bus', 'L.', 'species', source='col', usageId='COL-AUS', matchType='EXACT',
                       checklist={'checklistKey': 'col-key', 'alias': 'COL26.6 XR'}, taxonomicStatus='accepted')
        _, section = apply(frames(), review({'Aus bus L.': col}))
        entry = section['entries'][0]
        self.assertEqual((entry['decision'], entry['source'], entry['colUsageId'], entry['checklist']['alias']),
                         ('col', 'col', 'COL-AUS', 'COL26.6 XR'))
        self.assertEqual((section['labels'], section['reviewed'], section['unreviewed'], section['decision_counts']),
                         (3, 1, 2, {'col': 1}))
        self.assertEqual(section['checklist'], RELEASE)
        self.assertIn('not taxonID', section['policy'])

    def test_unreviewed_labels_and_missing_tables_leave_frames_unchanged(self):
        source = frames()
        result, section = apply(source, review({}))
        for key in source:
            pd.testing.assert_frame_equal(result[key], source[key])
        self.assertEqual((section['reviewed'], section['entries']), (0, []))
        self.assertEqual(apply({'event': pd.DataFrame([{'event_pk': 'e'}])}, review({'Aus bus L.': decision('parsed', 'Aus bus')}))[1]['entries'][0]['rows'], {})
        passthrough, none = apply(source, {})
        self.assertIs(passthrough, source)
        self.assertIsNone(none)

    def test_columns_missing_from_the_frame_are_added_when_the_table_has_the_field(self):
        occurrence = pd.DataFrame([{'occurrence_pk': 'o1', 'verbatimIdentification': 'Aus bus L.'}])
        result, _ = apply({'occurrence': occurrence}, review({'Aus bus L.': decision('parsed', 'Aus bus', 'L.', 'species')}))
        row = result['occurrence'].iloc[0]
        self.assertEqual((row['scientificName'], row['scientificNameAuthorship'], row['taxonRank']), ('Aus bus', 'L.', 'species'))
        self.assertNotIn('scientificName', occurrence.columns)

    def test_public_report_drops_only_the_entries(self):
        _, section = apply(frames(), review({'Aus bus L.': decision('parsed', 'Aus bus')}))
        public = names.public_report({'name_review': section, 'resources': {}})
        self.assertNotIn('entries', public['name_review'])
        self.assertEqual(public['name_review']['reviewed'], 1)
        self.assertEqual(names.public_report({'resources': {}}), {'resources': {}})


class DecisionTests(SimpleTestCase):
    def state(self):
        return {'col_release': RELEASE}

    def record(self):
        return {'label': 'Cus dus', 'parsed': names.summarize_parse('Cus dus (Smith, 1900)', parser_item('Cus dus (Smith, 1900)', 'Cus dus')),
                'match': names.compact_match(MATCHES['Cus dus (Smith) Jones 1900'])}

    def test_decisions_are_self_contained_snapshots(self):
        record = self.record()
        parsed = names.build_decision(record, {'decision': 'parsed'}, self.state())
        self.assertEqual((parsed['source'], parsed['scientificName'], parsed['scientificNameAuthorship']), ('parser', 'Cus dus', '(Smith, 1900)'))
        col = names.build_decision(record, {'decision': 'col'}, self.state(), by='bulk:auto')
        self.assertEqual((col['usageId'], col['checklist'], col['by'], col['matchType']), ('COL-CUS', {'checklistKey': 'col-key', 'alias': 'COL26.6 XR'},
                                                                                         'bulk:auto', 'VARIANT'))
        other = names.build_decision(record, {'decision': 'alternative', 'usage_id': 'COL-CUS2', 'confirm_coarser': True}, self.state())
        self.assertEqual((other['scientificName'], other['usageId']), ('Cus dux', 'COL-CUS2'))

    def test_impossible_decisions_are_rejected(self):
        record = self.record()
        for spec in ({'decision': 'nonsense'}, None, {'decision': 'alternative', 'usage_id': 'unknown'}, {'decision': 'alternative'}):
            with self.assertRaises(names.NameDecisionError):
                names.build_decision(record, spec, self.state())
        with self.assertRaises(names.NameDecisionError):
            names.build_decision({'label': 'x', 'parsed': {'usable': False}, 'match': {}}, {'decision': 'parsed'}, self.state())
        with self.assertRaises(names.NameDecisionError):
            names.build_decision({'label': 'x', 'parsed': {}, 'match': {'usage': None}}, {'decision': 'col'}, self.state())


@override_settings(CONVERSION_NAME_CHECKS_ENABLED=True)
class NamesCase(ConversionTestCase):
    files = [('occurrence.csv', OCCURRENCE)]

    def run_names(self, **kwargs):
        with Mocks(**kwargs) as mocks:
            self.assertTrue(process_next_conversion())
        return mocks

    def inspected(self):
        process_next_conversion()  # inspect; names are collected from the archive and a names job is chained
        return self.conversion

    def state(self, query=''):
        return self.client.get(self.url + query).data

    def reviewed(self):
        self.inspected()
        self.run_names()
        return self.conversion


class NameJobTests(NamesCase):
    def test_inspect_chains_a_names_job_that_parses_and_matches_each_label(self):
        conversion = self.inspected()
        self.assertEqual(conversion.status, 'review')
        self.assertEqual(conversion.name_review['status'], 'pending')
        self.assertEqual([record['label'] for record in conversion.name_review['labels']],
                         ['Aus bus L.', 'Cus dus (Smith) Jones 1900', 'Eus sp.'])
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'names')
        # The conversion stays editable while names are checked.
        self.assertEqual((self.state()['name_review']['status'], self.state()['name_review']['checking']), ('pending', True))
        mocks = self.run_names()
        conversion = self.conversion
        self.assertEqual(conversion.status, 'review')
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists())
        state = conversion.name_review
        self.assertEqual((state['status'], state['error'], state['runs']), ('complete', '', 1))
        self.assertEqual(state['col_release'], RELEASE)
        # The qualified label bypasses parsing and is matched by its genus stem.
        self.assertEqual(mocks.parse.call_args.args[0], ['Aus bus L.', 'Cus dus (Smith) Jones 1900'])
        self.assertEqual(mocks.match.call_args.args[0][0], {'kingdom': 'Animalia', 'scientificName': 'Aus bus L.'})
        self.assertEqual(mocks.match.call_args.args[0][2], {'scientificName': 'Eus'})
        public = self.state()['name_review']
        self.assertEqual((public['summary']['checked'], public['checking']), (3, False))
        aus, cus, eus = public['labels']
        self.assertEqual((aus['match']['status'], aus['parsed']['canonical'], aus['kind'], aus['eligible']), ('exact', 'Aus bus', 'auto', ['col', 'parsed', 'keep']))
        # A variant match and a parse that rewrites the supplied text are offered, but never in bulk.
        self.assertEqual((cus['match']['status'], cus['kind'], cus['offers']), ('variant', 'spelling', {'parsed': True, 'col': True}))
        self.assertEqual((eus['qualifier'], eus['parsed']['usable'], eus['parsed']['reason'], eus['kind'], eus['eligible']),
                         ('sp.', False, 'qualifier', 'uncertain', ['stem', 'keep']))
        self.assertEqual(next(group for group in public['summary']['groups'] if group['id'] == 'auto')['options'][0]['eligible'], 1)
        self.assertEqual(next(group for group in public['summary']['groups'] if group['id'] == 'uncertain')['options'][0]['eligible'], 1)
        self.assertEqual(public['checklist'], 'COL26.6 XR')

    def test_unfinished_labels_continue_in_a_later_run(self):
        self.inspected()
        calls = []

        def limited(queries, deadline=None, **kwargs):
            calls.append(len(queries))
            if len(calls) == 2:
                raise TaxonServiceError('the time budget for this matching run was used up')
            return fake_match(queries)

        with patch.object(names, 'CHUNK', 2):
            self.run_names(match=limited)
        conversion = self.conversion
        self.assertEqual((conversion.name_review['status'], conversion.name_review['error']), ('incomplete', ''))
        # The first chunk was saved; the job stays queued to continue the rest.
        self.assertEqual(sum(names.checked(record) for record in conversion.name_review['labels']), 2)
        job = DwcConversionJob.objects.get(conversion=conversion)
        self.assertEqual((job.action, job.claimed_at), ('names', None))
        with patch.object(names, 'CHUNK', 2):
            mocks = self.run_names(match=limited)
        conversion = self.conversion
        self.assertEqual((conversion.name_review['status'], conversion.name_review['runs']), ('complete', 2))
        self.assertEqual(len(mocks.match.call_args.args[0]), 1)  # only the unchecked label is matched again
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists())

    def test_runs_stop_continuing_after_the_limit(self):
        self.inspected()
        with patch.object(names, 'MAX_RUNS', 1):
            self.run_names(match=MagicMock(side_effect=TaxonServiceError('the time budget used up')))
        conversion = self.conversion
        self.assertEqual(conversion.name_review['status'], 'incomplete')
        self.assertIn('Check again', conversion.name_review['error'])
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists())

    def test_service_failure_is_recorded_and_never_blocks_conversion(self):
        self.inspected()
        self.run_names(match=MagicMock(side_effect=TaxonServiceError('https://api.gbif.org unreachable')))
        conversion = self.conversion
        self.assertEqual((conversion.status, conversion.name_review['status']), ('review', 'error'))
        self.assertIn('unreachable', conversion.name_review['error'])
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists())
        self.assertEqual(self.state()['name_review']['error'], conversion.name_review['error'])
        # Conversion proceeds without any name decisions, exactly as the converter produced it.
        self.assertEqual(self.post('convert', decisions=decisions_for(conversion.plan)).status_code, 202)
        process_next_conversion()
        conversion = self.conversion
        self.assertEqual(conversion.status, 'complete', conversion.error)
        self.assertEqual(conversion.report['name_review']['reviewed'], 0)
        self.assertEqual(conversion.report['name_review']['status'], 'error')

    def test_an_unexpected_failure_is_recorded_without_breaking_the_conversion(self):
        self.inspected()
        self.run_names(match=MagicMock(side_effect=RuntimeError('boom')))
        conversion = self.conversion
        self.assertEqual((conversion.status, conversion.name_review['status']), ('review', 'error'))
        self.assertNotIn('boom', conversion.name_review['error'])
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists())

    def test_parser_failure_still_keeps_the_col_matches_and_retries_only_the_parse(self):
        self.inspected()
        self.run_names(parse=MagicMock(side_effect=TaxonServiceError('parser unreachable')))
        conversion = self.conversion
        self.assertEqual(conversion.name_review['status'], 'error')
        labels = conversion.name_review['labels']
        self.assertTrue(all('match' in record for record in labels))
        self.assertEqual([('parsed' in record) for record in labels], [False, False, True])  # the qualified label needs no parser
        self.post('check_names')
        mocks = self.run_names()
        self.assertEqual(self.conversion.name_review['status'], 'complete')
        mocks.match.assert_not_called()

    def test_disabled_checks_do_not_chain_a_job_but_can_be_requested(self):
        with override_settings(CONVERSION_NAME_CHECKS_ENABLED=False):
            conversion = self.inspected()
        self.assertEqual(conversion.name_review['status'], 'pending')
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists())
        response = self.post('check_names')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'names')
        self.assertEqual(self.post('check_names').status_code, 409)
        self.run_names()
        self.assertEqual(self.conversion.name_review['status'], 'complete')

    def test_check_again_collects_labels_for_a_conversion_inspected_before_name_checks(self):
        self.inspected()
        self.run_names()
        DwcConversion.objects.filter(pk=self.conversion.pk).update(name_review={})
        self.assertEqual(self.state()['name_review']['status'], 'none')
        self.assertEqual(self.post('check_names').status_code, 202)
        self.run_names()
        self.assertEqual((self.conversion.name_review['status'], len(self.conversion.name_review['labels'])), ('complete', 3))

    def test_check_again_with_refresh_rechecks_undecided_labels_only(self):
        self.inspected()
        self.run_names()
        self.post('names', name_decisions={'Aus bus L.': {'decision': 'keep'}})
        self.assertEqual(self.post('check_names', refresh=True).status_code, 202)
        mocks = self.run_names()
        self.assertEqual([query['scientificName'] for query in mocks.match.call_args.args[0]], ['Cus dus (Smith) Jones 1900'])
        self.assertIn('Aus bus L.', self.conversion.name_review['decisions'])

    def test_convert_supersedes_a_running_names_job(self):
        conversion = self.inspected()
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'names')
        response = self.post('convert', decisions=decisions_for(conversion.plan))
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'convert')
        self.assertEqual(self.conversion.name_review['status'], 'incomplete')
        with Mocks() as mocks:
            process_next_conversion()
        mocks.match.assert_not_called()
        conversion = self.conversion
        self.assertEqual(conversion.status, 'complete', conversion.error)
        # Labels that were never checked leave the converter's output unchanged.
        self.assertEqual(conversion.report['name_review']['reviewed'], 0)
        self.assertEqual(conversion.report['name_review']['checked'], 0)

    def test_state_pages_the_labels_and_filters_undecided_ones(self):
        self.inspected()
        self.run_names()
        self.post('names', name_decisions={'Aus bus L.': {'decision': 'keep'}})
        everything = self.state('?names_limit=2')['name_review']
        self.assertEqual((everything['page'], [item['label'] for item in everything['labels']]),
                         ({'offset': 0, 'limit': 2, 'total': 3, 'view': 'all', 'group': None}, ['Aus bus L.', 'Cus dus (Smith) Jones 1900']))
        second = self.state('?names_limit=2&names_offset=2')['name_review']
        self.assertEqual([item['label'] for item in second['labels']], ['Eus sp.'])
        pending = self.state('?names_view=pending')['name_review']
        self.assertEqual((pending['page']['total'], pending['summary']['decided']), (1, 2))
        self.assertEqual([item['label'] for item in pending['labels']], ['Cus dus (Smith) Jones 1900'])
        self.assertEqual(self.state('?names_limit=99999&names_offset=junk')['name_review']['page'],
                         {'offset': 0, 'limit': names.MAX_PAGE_SIZE, 'total': 3, 'view': 'all', 'group': None})


class NameDecisionAPITests(NamesCase):
    def test_decisions_are_saved_as_snapshots_and_can_be_withdrawn(self):
        self.reviewed()
        response = self.post('names', name_decisions={
            'Aus bus L.': {'decision': 'col'}, 'Cus dus (Smith) Jones 1900': {'decision': 'alternative', 'usage_id': 'COL-CUS2', 'confirm_coarser': True},
            'Eus sp.': {'decision': 'empty'}})
        self.assertEqual(response.status_code, 200, response.data)
        decisions = self.conversion.name_review['decisions']
        self.assertEqual((decisions['Aus bus L.']['scientificName'], decisions['Aus bus L.']['usageId']), ('Aus bus', 'COL-AUS'))
        self.assertEqual(decisions['Cus dus (Smith) Jones 1900']['scientificName'], 'Cus dux')
        self.assertEqual(response.data['name_review']['summary']['decided'], 3)
        self.assertEqual(response.data['name_review']['labels'][0]['decision']['source'], 'col')
        self.post('names', name_decisions={'Eus sp.': None})
        self.assertNotIn('Eus sp.', self.conversion.name_review['decisions'])
        # Deciding every name settled the scientificName fallback; withdrawing a decision withdraws that system answer,
        # so the question is asked again instead of leaving the undecided name without a scientificName.
        self.assertEqual(self.conversion.decisions, {})
        self.assertIn('column:0:1', self.state()['unresolved'])
        # An answer the user gave stands when names are decided and withdrawn again.
        self.assertEqual(self.post('save', changes={'column:0:1': 'preserve'}).status_code, 200)
        self.post('names', name_decisions={'Eus sp.': {'decision': 'keep'}})
        self.post('names', name_decisions={'Eus sp.': None})
        self.assertEqual(self.conversion.decisions, {'column:0:1': 'preserve'})
        self.assertEqual(self.state()['decision_sources']['column:0:1']['source'], 'user')

    def test_bulk_actions_decide_only_pending_labels_they_may(self):
        self.reviewed()
        self.post('names', name_decisions={'Cus dus (Smith) Jones 1900': {'decision': 'keep'}})
        response = self.post('names', bulk={'group': 'auto', 'decision': 'col'})
        self.assertEqual(response.status_code, 200)
        decisions = self.conversion.name_review['decisions']
        # Exact and resolvable uncertain labels are already decided; the spelling is not in the auto group.
        self.assertEqual(sorted(decisions), ['Aus bus L.', 'Cus dus (Smith) Jones 1900', 'Eus sp.'])
        self.assertEqual((decisions['Aus bus L.']['decision'], decisions['Aus bus L.']['by']), ('col', 'bulk:auto'))
        self.assertEqual(decisions['Cus dus (Smith) Jones 1900']['decision'], 'keep')  # an earlier decision is never replaced
        self.assertEqual(self.post('names', bulk={'group': 'auto', 'decision': 'parsed'}).data['name_review']['summary']['decided'], 3)
        self.post('names', name_decisions={'Aus bus L.': None, 'Cus dus (Smith) Jones 1900': None})
        # Only a split that rebuilds the supplied text exactly is accepted in bulk.
        self.post('names', bulk={'group': 'auto', 'decision': 'parsed'})
        decisions = self.conversion.name_review['decisions']
        self.assertEqual(sorted(decisions), ['Aus bus L.', 'Eus sp.'])
        self.assertEqual((decisions['Aus bus L.']['decision'], decisions['Aus bus L.']['scientificNameAuthorship']), ('parsed', 'L.'))
        self.assertEqual((decisions['Eus sp.']['decision'], decisions['Eus sp.']['by']), ('stem', 'auto:uncertain'))

    def test_group_filter_bulk_batch_undo_and_auto_undo_api(self):
        self.reviewed()
        grouped = self.state('?names_view=group&names_group=auto&names_q=Aus')['name_review']
        self.assertEqual((grouped['page']['group'], grouped['page']['total']), ('auto', 1))
        self.assertEqual(grouped['labels'][0]['group'], 'auto')
        applied = self.post('names', bulk={'group': 'auto', 'decision': 'col'})
        self.assertEqual(applied.status_code, 200)
        batch_id = applied.data['name_review']['summary']['last_batch']['id']
        undone = self.post('names', undo_batch=batch_id)
        self.assertEqual(undone.status_code, 200)
        self.assertIsNone(undone.data['name_review']['summary']['last_batch'])
        self.assertEqual(undone.data['name_review']['summary']['batches'], [])
        self.assertEqual(self.post('names', undo_auto='auto').status_code, 200)
        self.assertGreater(self.conversion.name_review['auto_declined'].__len__(), 0)
        # Each batch that can still be undone is listed, so an earlier one keeps its Undo.
        first = self.post('names', bulk={'group': 'auto', 'decision': 'parsed'}).data['name_review']['summary']
        self.assertEqual([batch['group'] for batch in first['batches']], ['auto'])
        self.assertNotIn('changes', first['batches'][0])

    def test_a_fully_automatic_check_settles_the_fallback_and_declines_survive_a_recheck(self):
        def all_exact(queries, deadline=None, **kwargs):
            exact = {'Cus dus': col_summary('EXACT', 'COL-CUS', 'Cus dus', 'Jones')}
            return [exact.get(query['scientificName'].split(' (')[0]) or MATCHES.get(query['scientificName']) or col_summary('NONE')
                    for query in queries]
        self.inspected()
        with override_settings(CONVERSION_NAME_CHECKS_ENABLED=True):
            self.run_names(match=all_exact)
            review = self.conversion.name_review
            self.assertEqual({label: decision['by'] for label, decision in review['decisions'].items()},
                             {'Aus bus L.': 'auto:exact', 'Cus dus (Smith) Jones 1900': 'auto:exact', 'Eus sp.': 'auto:uncertain'})
            # Nobody clicked anything, and the scientificName fallback question settled itself.
            self.assertEqual(self.conversion.decisions, {'column:0:1': 'preserve'})
            # Undo all brings the question back; a re-check never accepts the declined names again.
            self.assertEqual(self.post('names', undo_auto='auto').status_code, 200)
            self.assertNotIn('column:0:1', self.conversion.decisions)
            self.assertEqual(self.post('check_names', refresh=True).status_code, 202)
            self.run_names(match=all_exact)
            review = self.conversion.name_review
            self.assertEqual(sorted(review['auto_declined']), ['Aus bus L.', 'Cus dus (Smith) Jones 1900'])
            self.assertEqual(sorted(review['decisions']), ['Eus sp.'])

    def test_invalid_decisions_are_rejected_without_saving_anything(self):
        self.reviewed()
        for body in ({'name_decisions': {'Unknown name': {'decision': 'keep'}}},
                     {'name_decisions': {'Aus bus L.': {'decision': 'keep'}, 'Eus sp.': {'decision': 'parsed'}}},
                     {'name_decisions': {'Aus bus L.': {'decision': 'alternative', 'usage_id': 'nope'}}},
                     {'name_decisions': ['Aus bus L.']}, {'bulk': 'everything'}):
            response = self.post('names', **body)
            self.assertEqual(response.status_code, 400, body)
        self.assertEqual({label: decision['by'] for label, decision in self.conversion.name_review['decisions'].items()},
                         {'Aus bus L.': 'auto:exact', 'Eus sp.': 'auto:uncertain'})

    def test_stale_plan_conflicts_and_decisions_outside_review_are_refused(self):
        self.reviewed()
        stale = self.client.post(self.url, {'action': 'names', 'plan_id': 'old', 'bulk': 'exact_col'}, format='json')
        self.assertEqual(stale.status_code, 409)
        self.post('names', name_decisions={'Aus bus L.': {'decision': 'keep'}})
        self.assertEqual(self.post('convert', decisions=decisions_for(self.conversion.plan)).status_code, 202)
        process_next_conversion()
        self.assertEqual(self.conversion.status, 'complete')
        for body in ({'name_decisions': {'Aus bus L.': None}}, {'bulk': 'exact_col'}):
            self.assertEqual(self.post('names', **body).status_code, 409)
        self.assertEqual(self.post('check_names').status_code, 409)
        self.assertIn('Aus bus L.', self.conversion.name_review['decisions'])

    def test_decisions_may_be_saved_while_names_are_still_being_checked(self):
        self.inspected()
        self.assertEqual(self.post('names', name_decisions={'Eus sp.': {'decision': 'empty'}}).status_code, 200)
        self.assertEqual(DwcConversionJob.objects.get(conversion=self.conversion).action, 'names')
        self.run_names()
        # The worker merged its results without losing the decision made meanwhile.
        state = self.conversion.name_review
        self.assertEqual((state['status'], set(state['decisions'])), ('complete', {'Aus bus L.', 'Eus sp.'}))

    def test_convert_applies_only_reviewed_names_and_records_provenance(self):
        self.reviewed()
        self.post('names', name_decisions={
            'Aus bus L.': {'decision': 'col'},
            'Cus dus (Smith) Jones 1900': {'decision': 'parsed'},
            'Eus sp.': {'decision': 'keep'}})
        self.assertEqual(self.post('convert', decisions=decisions_for(self.conversion.plan)).status_code, 202)
        process_next_conversion()
        conversion = self.conversion
        self.assertEqual(conversion.status, 'complete', conversion.error)
        occurrence = Table.objects.get(dataset_id=self.dataset_id, title='occurrence').df.set_index('occurrenceID')
        self.assertEqual(occurrence.loc['a', 'scientificName'], 'Aus bus')
        self.assertEqual(occurrence.loc['a', 'scientificNameAuthorship'], 'L.')
        self.assertEqual(occurrence.loc['b', 'scientificName'], 'Aus bus')
        # The parsed split fills nothing over the supplied authorship "Jones".
        self.assertEqual((occurrence.loc['c', 'scientificName'], occurrence.loc['c', 'scientificNameAuthorship']), ('Cus dus', 'Jones'))
        self.assertEqual(occurrence.loc['c', 'taxonRank'], 'species')
        self.assertEqual(occurrence.loc['d', 'scientificName'], 'Eus sp.')
        self.assertEqual(occurrence['verbatimIdentification'].to_dict(),
                         {'a': 'Aus bus L.', 'b': 'Aus bus L.', 'c': 'Cus dus (Smith) Jones 1900', 'd': 'Eus sp.'})
        identification = Table.objects.get(dataset_id=self.dataset_id, title='identification').df
        # Rows without a kingdom emit no identification record, so "Eus sp." has none.
        self.assertEqual(sorted(identification['scientificName']), ['Aus bus', 'Aus bus', 'Cus dus'])
        self.assertEqual(sorted(identification['verbatimIdentification']), ['Aus bus L.', 'Aus bus L.', 'Cus dus (Smith) Jones 1900'])
        section = conversion.report['name_review']
        self.assertTrue(conversion.report['validation']['valid'])
        self.assertEqual((section['reviewed'], section['unreviewed'], section['checked'], section['checklist']['alias']), (3, 0, 3, 'COL26.6 XR'))
        entries = {entry['label']: entry for entry in section['entries']}
        self.assertEqual((entries['Aus bus L.']['source'], entries['Aus bus L.']['colUsageId'], entries['Aus bus L.']['checklist']['alias']),
                         ('col', 'COL-AUS', 'COL26.6 XR'))
        self.assertEqual(entries['Aus bus L.']['rows'], {'occurrence': 2, 'identification': 2})
        # The supplied authorship of the source row is kept on its occurrence and on the identification made from it.
        self.assertEqual((entries['Cus dus (Smith) Jones 1900']['source'], entries['Cus dus (Smith) Jones 1900']['authorshipKept']), ('parser', 2))
        cus = identification[identification['verbatimIdentification'] == 'Cus dus (Smith) Jones 1900'].iloc[0]
        self.assertEqual((cus['scientificNameAuthorship'], cus['taxonRank']), ('Jones', 'species'))
        self.assertEqual(entries['Eus sp.']['decision'], 'keep')
        self.assertNotIn('taxonID', str(section['entries']))
        # The downloadable report carries the entries; the state shows only the summary.
        state = self.state()
        self.assertNotIn('entries', state['report']['name_review'])
        self.assertEqual(state['report']['name_review']['reviewed'], 3)

    def test_unreviewed_labels_convert_exactly_as_the_converter_produced_them(self):
        self.reviewed()
        self.post('names', undo_auto='uncertain')
        self.post('names', name_decisions={'Aus bus L.': {'decision': 'col'}})
        self.assertEqual(self.post('convert', decisions=decisions_for(self.conversion.plan)).status_code, 202)
        process_next_conversion()
        occurrence = Table.objects.get(dataset_id=self.dataset_id, title='occurrence').df.set_index('occurrenceID')
        self.assertEqual(occurrence.loc['c', 'scientificName'], 'Cus dus (Smith) Jones 1900')
        self.assertEqual(occurrence.loc['d', 'scientificName'], 'Eus sp.')
        self.assertEqual(self.conversion.report['name_review']['unreviewed'], 2)

    def test_a_new_inspection_rechecks_names_and_carries_the_users_own_decisions(self):
        """A re-inspection of the same source (a new rule version) keeps the user's name decisions (568 had 989)."""
        self.reviewed()
        self.post('names', name_decisions={'Cus dus (Smith) Jones 1900': {'decision': 'keep'}, 'Eus sp.': {'decision': 'empty'}})
        self.post('names', bulk={'group': 'auto', 'decision': 'col'})
        self.assertEqual(sorted(self.conversion.name_review['decisions']), ['Aus bus L.', 'Cus dus (Smith) Jones 1900', 'Eus sp.'])
        old_plan = self.conversion.plan['id']
        self.assertEqual(self.client.post(self.url, {'action': 'inspect'}, format='json').status_code, 202)
        process_next_conversion()
        state = self.conversion.name_review
        self.assertEqual((state['status'], [('match' in record) for record in state['labels']]), ('pending', [False, False, False]))
        # Bulk decisions are offered again under the current rules rather than carried.
        self.assertEqual(sorted(state['decisions']), ['Cus dus (Smith) Jones 1900', 'Eus sp.'])
        self.assertEqual(state['carried'], {'decisions': 2, 'bulk_not_carried': 1, 'dropped': 0})
        self.assertEqual(state['decisions']['Eus sp.']['carriedFrom'], old_plan)
        self.run_names()
        review = self.state()['name_review']
        self.assertEqual((next(group for group in review['summary']['groups'] if group['id'] == 'auto')['options'][0]['eligible'], review['carried']),
                         (1, {'decisions': 2, 'bulk_not_carried': 1, 'dropped': 0}))

    def test_names_fill_a_scientific_name_column_the_converter_left_empty(self):
        self.reviewed()
        self.post('names', name_decisions={'Aus bus L.': {'decision': 'parsed'}, 'Eus sp.': {'decision': 'keep'}})
        decisions = {**decisions_for(self.conversion.plan), 'column:0:1': 'preserve'}
        self.assertEqual(self.post('convert', decisions=decisions).status_code, 202)
        process_next_conversion()
        self.assertEqual(self.conversion.status, 'complete', self.conversion.error)
        occurrence = Table.objects.get(dataset_id=self.dataset_id, title='occurrence').df.set_index('occurrenceID')
        # Only reviewed labels get a name; the rest stay empty, and the source text is untouched.
        self.assertEqual(occurrence['scientificName'].to_dict(), {'a': 'Aus bus', 'b': 'Aus bus', 'c': '', 'd': 'Eus sp.'})
        self.assertEqual(occurrence.loc['a', 'verbatimIdentification'], 'Aus bus L.')
        identification = Table.objects.get(dataset_id=self.dataset_id, title='identification').df
        self.assertEqual(sorted(identification['scientificName']), ['', 'Aus bus', 'Aus bus'])
        self.assertTrue(self.conversion.report['validation']['valid'])


class ExtensionNamesTests(SimpleTestCase):
    def test_a_decision_for_an_occurrence_name_does_not_rewrite_its_identification_extension_row(self):
        extension = ('<extension rowType="' + DWC + 'Identification" fieldsTerminatedBy="," ignoreHeaderLines="1">'
                     '<files><location>identification.csv</location></files><coreid index="0"/>'
                     '<field index="1" term="' + DWC + 'scientificName"/></extension>')
        core_name = '<field index="2" term="' + DWC + 'scientificName"/>'
        archive = read_inputs([('meta.xml', manifest(fields=core_name, extension=extension)),
                               ('occ.csv', b'id,occurrenceID,name\nj1,o1,Species A\n'),
                               ('identification.csv', b'link,name\nj1,Species B\n')])
        state = names.collect_state(archive, {'id': 'plan'})
        self.assertEqual({record['label']: record['tables'] for record in state['labels']},
                         {'Species A': {'occurrence': 1}, 'Species B': {'identification': 1}})
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        sources = names.row_source_names(archive, report['row_crosswalk'], frames)

        def converted(label, name):
            decided = {**state, 'decisions': {label: decision('parsed', name)}}
            result, section = names.apply_name_decisions(frames, decided, sources)
            return result['occurrence']['scientificName'].tolist(), result['identification']['scientificName'].tolist(), section

        only_a = converted('Species A', 'Species Alpha')
        self.assertEqual(only_a[0], ['Species Alpha'])
        self.assertNotIn('Species Alpha', only_a[1])
        only_b = converted('Species B', 'Species Beta')
        self.assertEqual(only_b[0], ['Species A'])
        self.assertEqual(only_b[1].count('Species Beta'), 1)
        self.assertEqual(only_b[2]['entries'][0]['rows'], {'identification': 1})


def texts(values):
    return [value['name'] if value else '' for value in values]


class SourceNameKeyTests(SimpleTestCase):
    def converted(self, content):
        archive = read_inputs([('occurrence.csv', content)])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        return archive, frames, report

    def test_decisions_follow_the_source_scientific_name_not_a_different_verbatim_identification(self):
        archive, frames, report = self.converted(
            b'occurrenceID,scientificName,verbatimIdentification,kingdom\n'
            b'a,Aus bus,A. bus?,Animalia\nb,Cus dus,Aus bus,Animalia\nc,Aus bus,,Animalia\n')
        state = names.collect_state(archive, {'id': 'plan'})
        self.assertEqual({record['label'] for record in state['labels']}, {'Aus bus', 'Cus dus'})
        sources = names.row_source_names(archive, report['row_crosswalk'], frames)
        self.assertEqual(texts(sources['occurrence']), ['Aus bus', 'Cus dus', 'Aus bus'])
        self.assertEqual(sorted(texts(sources['identification'])), ['Aus bus', 'Aus bus', 'Cus dus'])
        result, section = names.apply_name_decisions(frames, {**state, 'decisions': {'Aus bus': decision('parsed', 'Aus busus')}}, sources)
        occurrence = result['occurrence'].set_index('occurrenceID')
        self.assertEqual(occurrence['scientificName'].to_dict(), {'a': 'Aus busus', 'b': 'Cus dus', 'c': 'Aus busus'})
        # verbatimIdentification stays as supplied, and the row whose verbatim text equals the label is not touched.
        self.assertEqual(occurrence['verbatimIdentification'].tolist(), ['A. bus?', 'Aus bus', 'Aus bus'])
        self.assertEqual(section['entries'][0]['rows'], {'occurrence': 2, 'identification': 2})

    def test_crosswalk_positions_offsets_and_disagreeing_sources(self):
        def table(row_type, rows):
            return SimpleNamespace(row_type=row_type, terms=[DWC + 'occurrenceID', names.NAME], rows=rows)
        archive = SimpleNamespace(tables=[
            table(DWC + 'Occurrence', [['a', 'Aus bus'], ['b', ''], ['c', 'Eus eus']]),
            table(DWC + 'Event', [['e', 'Not a name']]),
            table(DWC + 'Identification', [['a', 'Fus fus'], ['b', 'Gus gus']])])
        frames = {'occurrence': pd.DataFrame({'x': [1, 2, 3]}), 'identification': pd.DataFrame({'x': [1, 2, 3, 4]}), 'event': pd.DataFrame({'x': [1]})}

        def entry(table_name, index, row, target_row, source_index=0):
            return {'target_table': table_name, 'target_row': target_row, 'source_table_index': index, 'source_row': row}
        crosswalk = [entry('occurrence', 0, 1, 1), entry('occurrence', 0, 2, 2), entry('occurrence', 0, 3, 3),
                     entry('occurrence', 1, 1, 1),  # an event source supplies no name
                     entry('identification', 2, 1, 2), entry('identification', 2, 2, 4),
                     entry('identification', 0, 3, 4),  # a second, different name for the same target row: ambiguous
                     {'target_table': 'identification', 'target_row': None, 'source_table_index': 2, 'source_row': 1},
                     {'target_table': 'identification', 'target_row': 99, 'source_table_index': 2, 'source_row': 1},
                     {'target_table': 'event', 'target_row': 1, 'source_table_index': 1, 'source_row': 1}]
        self.assertEqual({table: texts(values) for table, values in names.row_source_names(archive, crosswalk, frames).items()},
                         {'occurrence': ['Aus bus', '', 'Eus eus'], 'identification': ['', 'Fus fus', '', '']})

    def test_row_source_names_includes_each_identification_qualifier(self):
        table = SimpleNamespace(row_type=DWC + 'Identification',
                                terms=[names.NAME, DWC + 'identificationQualifier'],
                                rows=[['Pseudocalanus', 'sp.']])
        archive = SimpleNamespace(tables=[table])
        frames = {'identification': pd.DataFrame([{'scientificName': ''}])}
        sources = names.row_source_names(archive, [{'target_table': 'identification', 'target_row': 1,
                                                     'source_table_index': 0, 'source_row': 1}], frames)
        self.assertEqual(sources['identification'][0]['qualifier'], 'sp.')

    def test_nested_taxon_plans_keep_source_tables_and_row_offsets(self):
        from api.dwca_import import DWC as IMPORT_DWC
        from api.test_dwca_taxon import decisions as taxon_decisions, manifest_archive
        terms = [IMPORT_DWC + 'occurrenceID', IMPORT_DWC + 'scientificName', IMPORT_DWC + 'occurrenceStatus']
        archive = read_inputs(manifest_archive([
            ('first.csv', IMPORT_DWC + 'Occurrence', terms, [['join-a', 'occ-1', 'Aus bus', 'present'], ['join-a', 'occ-2', 'Cus dus', 'present']]),
            ('second.csv', IMPORT_DWC + 'Occurrence', terms, [['join-b', 'occ-3', 'Eus eus', 'present']])]).items())
        plan = build_plan(archive)
        frames, report = convert(archive, plan, taxon_decisions(plan))
        sources = names.row_source_names(archive, report['row_crosswalk'], frames)
        self.assertEqual(texts(sources['occurrence']), ['Aus bus', 'Cus dus', 'Eus eus'])
        state = names.collect_state(archive, {'id': 'plan'})
        result, _ = names.apply_name_decisions(frames, {**state, 'decisions': {'Eus eus': decision('parsed', 'Eus eus L.')}}, sources)
        self.assertEqual(result['occurrence']['scientificName'].tolist(), ['Aus bus', 'Cus dus', 'Eus eus L.'])


class AlternativeIdTests(SimpleTestCase):
    def test_numeric_and_string_alternative_ids_match(self):
        match = {'matchType': 'NONE', 'usage': None, 'alternatives': [
            {'id': 12345, 'scientificName': 'Aus bus', 'scientificNameAuthorship': 'L.', 'taxonRank': 'species', 'matchType': 'VARIANT'}]}
        record = {'label': 'Aus bsu', 'parsed': {}, 'match': match}
        for given in ('12345', 12345):
            result = names.build_decision(record, {'decision': 'alternative', 'usage_id': given, 'confirm_coarser': True}, {'col_release': RELEASE})
            self.assertEqual((result['scientificName'], result['usageId']), ('Aus bus', '12345'))
        with self.assertRaises(names.NameDecisionError):
            names.build_decision(record, {'decision': 'alternative', 'usage_id': '1234'}, {})


class LabelLengthTests(SimpleTestCase):
    def test_overlong_cells_are_counted_and_never_stored_or_sent(self):
        huge = 'Aus ' + 'b' * 600
        content = (f'occurrenceID,scientificName,kingdom,family\na,Aus bus,{"K" * 300},Fam\nb,{huge},Animalia,\nc,{huge},Animalia,\nd,Cus dus,,\n').encode()
        state = names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': 'plan'})
        self.assertEqual([record['label'] for record in state['labels']], ['Aus bus', 'Cus dus'])
        self.assertEqual(state['skipped_long'], {'labels': 1, 'rows': 2})
        self.assertNotIn('bbbbbbbbbb', str(state))
        # An oversized context cell is not a hint and is not sent to the matcher either.
        self.assertEqual(state['labels'][0]['hints'], {'family': 'Fam'})
        exact = 'A' * names.MAX_LABEL_CHARS
        self.assertEqual(len(names.collect_state(read_inputs([('occurrence.csv', f'occurrenceID,scientificName\na,{exact}\n'.encode())]), {'id': 'p'})['labels']), 1)

    def test_collection_records_mixed_classification_hints_and_ignores_parenthetical_variants(self):
        content = (b'occurrenceID,scientificName,kingdom,phylum,class\n'
                   b'a,Anura,Animalia,Chordata,Amphibia\n'
                   b'b,Anura,Animalia,Arthropoda,Insecta\n'
                   b'c,Squamata,Animalia,Chordata,Squamata (lizards)\n'
                   b'd,Squamata,Animalia,Chordata,Squamata\n')
        state = names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': 'plan'})
        records = {record['label']: record for record in state['labels']}
        self.assertEqual(records['Anura']['mixed_hints'], ['class', 'phylum'])
        self.assertEqual(records['Squamata']['mixed_hints'], [])

    def test_source_qualifier_counts_are_bounded_to_kept_values(self):
        values = [f'qualifier-{index}' for index in range(names.MAX_SOURCE_QUALIFIERS + 4)]
        table = SimpleNamespace(row_type=DWC + 'Occurrence', terms=[DWC + 'occurrenceID', names.NAME,
                                                                    DWC + 'identificationQualifier'],
                                rows=[[f'a{index}', 'Name', value] for index, value in enumerate(values)])
        record = names.collect_state(SimpleNamespace(tables=[table]), {'id': 'plan'})['labels'][0]
        self.assertEqual(len(record['source_qualifiers']), names.MAX_SOURCE_QUALIFIERS)
        self.assertLessEqual(len(record['source_qualifier_counts']), names.MAX_SOURCE_QUALIFIERS)
        self.assertTrue(record['qualifiers_truncated'])


def respond(payload, max_retries=None):
    return reply([answer(item_id, 'confirm', refs=['table:0']) for item_id in requested(payload)])


def answer_chat(conversion_id, job_id, claim):
    conversion_chat.acknowledge_unanswered(DwcConversion.objects.get(pk=conversion_id), 'Answered.')


def budget_after_first_chunk(on_first=None):
    calls = []

    def limited(queries, deadline=None, **kwargs):
        calls.append(len(queries))
        if len(calls) == 1 and on_first:
            on_first()
        if len(calls) == 2:
            raise TaxonServiceError('the time budget for this matching run was used up')
        return fake_match(queries)
    return limited


@override_settings(**AI)
class JobOrderingTests(NamesCase):
    def job(self):
        return DwcConversionJob.objects.filter(conversion=self.conversion).first()

    def inspect_without_ai(self):
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False):
            process_next_conversion()
        self.assertEqual(self.job().action, 'names')

    def test_the_automatic_ai_review_runs_before_name_checks(self):
        with patch(QUERY, side_effect=respond):
            process_next_conversion()  # inspect
            self.assertEqual((self.job().action, self.conversion.status), ('review', 'reviewing'))
            process_next_conversion()  # review
        self.assertEqual((self.job().action, self.conversion.status), ('names', 'review'))
        self.assertEqual(self.conversion.name_review['status'], 'pending')
        self.run_names()
        self.assertEqual((self.conversion.name_review['status'], self.conversion.status), ('complete', 'review'))
        self.assertIsNone(self.job())

    def test_a_waiting_chat_message_is_answered_at_the_next_names_batch_and_names_then_resume(self):
        self.inspect_without_ai()
        with patch.object(names, 'CHUNK', 2), patch('api.conversion_review.should_auto_review', return_value=False):
            self.run_names(match=budget_after_first_chunk(lambda: self.assertEqual(
                self.post('chat', message='Which names matter?').status_code, 202)))
            # The message did not get a 409 and did not wait for every label: chat has the next turn.
            self.assertEqual(self.conversion.name_review['status'], 'incomplete')
            self.assertEqual(self.job().action, 'chat')
            with patch('api.conversion_chat.run_chat_turn', side_effect=answer_chat):
                process_next_conversion()
            self.assertEqual(self.job().action, 'names')
            self.run_names()
        self.assertEqual(self.conversion.name_review['status'], 'complete')
        self.assertIsNone(self.job())

    def test_a_manual_review_takes_over_a_queued_name_check_which_resumes_afterwards(self):
        self.inspect_without_ai()
        response = self.post('review')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual((self.job().action, self.conversion.status), ('review', 'reviewing'))
        self.assertEqual(self.conversion.name_review['status'], 'pending')
        with patch(QUERY, side_effect=respond):
            process_next_conversion()
        self.assertEqual((self.job().action, self.conversion.status), ('names', 'review'))
        self.run_names()
        self.assertEqual(self.conversion.name_review['status'], 'complete')

    def test_a_manual_review_during_a_running_name_check_gets_the_next_batch_boundary(self):
        self.inspect_without_ai()
        with patch.object(names, 'CHUNK', 2):
            self.run_names(match=budget_after_first_chunk(lambda: self.assertEqual(self.post('review').status_code, 202)))
        self.assertEqual((self.job().action, self.conversion.status), ('review', 'reviewing'))
        self.assertEqual(self.conversion.name_review['status'], 'incomplete')
        with patch(QUERY, side_effect=respond):
            process_next_conversion()
        self.assertEqual(self.job().action, 'names')  # unfinished names resume once the review is done
        self.run_names()
        self.assertEqual((self.conversion.name_review['status'], self.conversion.status), ('complete', 'review'))

    def test_a_running_review_still_refuses_a_second_manual_review(self):
        self.inspect_without_ai()
        with patch(QUERY, side_effect=respond):
            self.assertEqual(self.post('review').status_code, 202)
            self.assertEqual(self.post('review').status_code, 409)

    def test_names_do_not_resume_after_review_when_they_are_finished_or_over_the_run_limit(self):
        self.inspect_without_ai()
        with patch('api.conversion_review.should_auto_review', return_value=False):
            self.run_names()
        self.assertIsNone(self.job())
        self.assertFalse(names.pending(self.conversion))
        state = {**self.conversion.name_review, 'status': 'incomplete', 'runs': names.MAX_RUNS, 'requested': True}
        self.assertFalse(names.pending(DwcConversion(plan=self.conversion.plan, name_review=state)))
        with override_settings(CONVERSION_NAME_CHECKS_ENABLED=False):
            self.assertFalse(names.pending(DwcConversion(plan=self.conversion.plan, name_review={**state, 'runs': 1, 'requested': False})))
            self.assertTrue(names.pending(DwcConversion(plan=self.conversion.plan, name_review={**state, 'runs': 1})))

    def test_a_waiting_chat_message_stops_the_run_after_the_current_chunk(self):
        self.inspect_without_ai()
        calls = []

        def match(queries, deadline=None, **kwargs):
            calls.append(len(queries))
            if len(calls) == 1:
                self.assertEqual(self.post('chat', message='Wait for me').status_code, 202)
            return fake_match(queries)

        with patch.object(names, 'CHUNK', 1), patch('api.conversion_review.should_auto_review', return_value=False):
            self.run_names(match=match)
        self.assertEqual(calls, [1])  # the remaining labels wait for the next run
        self.assertEqual(sum(names.checked(record) for record in self.conversion.name_review['labels']), 1)
        self.assertEqual((self.conversion.name_review['status'], self.job().action), ('incomplete', 'chat'))

    def test_a_manual_review_request_stops_the_run_after_the_current_chunk(self):
        self.inspect_without_ai()
        calls = []

        def match(queries, deadline=None, **kwargs):
            calls.append(len(queries))
            if len(calls) == 1:
                self.assertEqual(self.post('review').status_code, 202)
            return fake_match(queries)

        with patch.object(names, 'CHUNK', 1):
            self.run_names(match=match)
        self.assertEqual(calls, [1])
        self.assertEqual((self.job().action, self.conversion.status), ('review', 'reviewing'))


class OversizedNameTests(SimpleTestCase):
    def test_huge_whitespace_cells_are_skipped_without_splitting_and_reported(self):
        from api.conversion_names import MAX_LABEL_CHARS, apply_name_decisions, collect_state
        from api.dwca_import import read_inputs as read
        huge = 'A' + ' ' * (MAX_LABEL_CHARS * 5) + 'b'
        archive = read([('occurrence.csv', ('occurrenceID,occurrenceStatus,scientificName\no1,present,"' + huge + '"\n').encode())])
        state = collect_state(archive, {'id': 'plan'})
        self.assertEqual(state['labels'], [])
        self.assertEqual(state['skipped_long'], {'labels': 1, 'rows': 1})
        frames, section = apply_name_decisions({}, state, {})
        self.assertEqual(section['skipped_long'], {'labels': 1, 'rows': 1})


class NameQuestionTests(SimpleTestCase):
    def conversion(self, decisions):
        from types import SimpleNamespace
        plan = {'id': 'plan', 'issues': [{'id': 'column:1:16', 'kind': 'name-semantics', 'options': []},
                                         {'id': 'event-category', 'kind': 'event-category', 'options': []}]}
        state = {'plan_id': 'plan', 'labels': [{'label': 'Aus bus'}, {'label': 'Cus dus'}], 'decisions': decisions,
                 'truncated': 0, 'skipped_long': {'labels': 0, 'rows': 0}}
        return SimpleNamespace(plan=plan, name_review=state, decisions={})

    @override_settings(CONVERSION_NAME_CHECKS_ENABLED=True)
    def test_name_questions_are_answered_inside_the_name_check(self):
        from api.conversion_names import name_question_ids
        self.assertEqual(name_question_ids(self.conversion({})), ['column:1:16'])
        empty = self.conversion({}); empty.name_review['labels'] = []
        self.assertEqual(name_question_ids(empty), [])
        with override_settings(CONVERSION_NAME_CHECKS_ENABLED=False):
            self.assertEqual(name_question_ids(self.conversion({})), [])

    @override_settings(CONVERSION_NAME_CHECKS_ENABLED=True)
    def test_deciding_every_name_settles_the_fallback_question(self):
        from api.conversion_names import settle_name_questions
        partial = self.conversion({'Aus bus': {'decision': 'parsed'}})
        with patch('api.conversion_review.apply_decision_changes') as apply, \
                patch('api.conversion_review.latest_sources', return_value={}):
            self.assertEqual(settle_name_questions(partial), [])
            apply.assert_not_called()
            complete = self.conversion({'Aus bus': {'decision': 'parsed'}, 'Cus dus': {'decision': 'keep'}})
            self.assertEqual(settle_name_questions(complete), ['column:1:16'])
            self.assertEqual(apply.call_args.args[1:3], ({'column:1:16': 'preserve'}, 'system'))


# Real GBIF v2 (COL XR) responses, trimmed, for names that went wrong in production conversions 560-570.
REAL = json.loads((Path(__file__).parent / 'testdata' / 'col_v2_real_matches.json').read_text())
GROUP_MATCHES = json.loads((Path(__file__).parent / 'testdata' / 'col_v2_group_matches.json').read_text())
ID_MATCHES_FIXTURE = json.loads((Path(__file__).parent / 'testdata' / 'col_v2_id_matches.json').read_text())


def fixture_record(case, rows=1):
    label = case['label']
    stem, qualifier = taxon_matching.split_qualifier(label)
    if qualifier:
        parsed = {'type': None, 'usable': False, 'canonical': None, 'authorship': None,
                  'rank': None, 'lossless': False, 'splits': False, 'reason': 'qualifier'}
    else:
        authorship_match = re.search(r'\s+(\([^)]*\d{4}[^)]*\)(?:\s+.*)?)$', stem)
        canonical = stem[:authorship_match.start()].strip() if authorship_match else stem
        authorship = authorship_match.group(1) if authorship_match else None
        parse_item = {'type': 'SCIENTIFIC', 'parsed': True, 'parsedPartially': False,
                      'canonicalName': canonical, 'canonicalNameWithMarker': canonical,
                      'canonicalNameComplete': label, 'authorship': authorship}
        parsed = names.summarize_parse(label, parse_item)
    summary = taxon_matching.summarize_match(case['response'])
    return {'label': label, 'rows': rows, 'tables': {'occurrence': rows}, 'hints': case.get('hints') or {},
            'source_rank': case.get('source_rank'), 'qualifier': qualifier,
            'source_authorships': case.get('source_authorships') or [],
            'source_ids': {key: value for key, value in (case.get('query') or {}).items() if key in {'scientificNameID', 'taxonID'}},
            'parsed': parsed, 'match': names.compact_match(summary)}


FIXTURE_RECORDS = {case['label']: fixture_record(case) for case in GROUP_MATCHES}


def id_fixture_record(label, ids=None):
    """Run the captured ID fixture through the same two-step path used by the names job."""
    name_case = next(case for case in ID_MATCHES_FIXTURE
                     if case['label'] == label and not any(key in case['query'] for key in ('scientificNameID', 'taxonID')))
    query = dict(name_case['query'])
    id_case = next((case for case in ID_MATCHES_FIXTURE
                    if case['label'] == label and all(case['query'].get(key) == value for key, value in (ids or {}).items())
                    and len(case['query']) > len(query)), None)
    source_ids = dict(ids if ids is not None else
                      {key: value for key, value in (id_case['query'] if id_case else {}).items()
                       if key in {'scientificNameID', 'taxonID'}})
    cases_by_query = {json.dumps(case['query'], sort_keys=True): case for case in ID_MATCHES_FIXTURE}

    def match(queries, deadline=None, **kwargs):
        return [taxon_matching.summarize_match(cases_by_query[json.dumps(item, sort_keys=True)]['response'])
                for item in queries]

    item = {'label': label, 'query': query, 'qualifier': None, 'ids': source_ids, 'match': None, 'parse': True}
    with patch.object(names, 'match_col', side_effect=match):
        results, error = names.check_chunk([item], deadline=None)
    if error:
        raise error
    parsed = real_parse(label)
    return {'label': label, 'rows': 1, 'tables': {'occurrence': 1}, 'hints': {}, 'source_rank': None,
            'qualifier': None, 'source_authorships': [], 'source_ids': source_ids,
            'parsed': parsed, 'match': results[label]['match']}


def real_get_json(method, url, deadline=None, params=None, **kwargs):
    def response(query):
        query = {key: value for key, value in query.items() if key not in ('checklistKey', 'verbose')}
        return next(case['response'] for case in REAL.values() if case['query'] == query)
    return [response(query) for query in kwargs['json']] if method == 'POST' else response(params)


def real_parse(canonical=None, rank=None, authorship=None):
    if canonical is None:  # a qualified label is not parsed
        return {'type': None, 'usable': False, 'canonical': None, 'authorship': None, 'rank': None, 'lossless': False, 'splits': False,
                'reason': 'qualifier'}
    return {'type': 'SCIENTIFIC', 'usable': True, 'canonical': canonical, 'authorship': authorship, 'rank': rank, 'lossless': True,
            'splits': bool(authorship)}


# label: (parsed summary, source taxonRank) as the names job stored them in production.
REAL_RECORDS = {
    'Calanus': (real_parse('Calanus'), None),
    'Chaetognatha': (real_parse('Chaetognatha'), None),
    'Oncaea': (real_parse('Oncaea'), None),
    'Polychaeta': (real_parse('Polychaeta'), None),
    'Trientalis europaea': (real_parse('Trientalis europaea', 'species'), None),
    'Mnium cuspidatum': (real_parse('Mnium cuspidatum', 'species'), None),
    'Galium boreale': (real_parse('Galium boreale', 'species'), None),
    'AmphibiaReptilia sp.': (real_parse(), 'genus'),
    'Hieracium sp': (real_parse(), None),
    'Larus sp.': (real_parse(), 'species'),
    'Columba livia var. domestica': (real_parse('Columba livia var. domestica', 'variety'), 'species'),
    'Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti': (
        real_parse('Betula pubescens subsp. czerepanovii', 'subspecies', '(N.I.Orlova) Hämet-Ahti'), 'species'),
}


def real_record(label):
    """The stored record for a real label, matched through match_col against its real v2 response."""
    with patch.object(taxon_matching, '_get_json', side_effect=real_get_json):
        summary = taxon_matching.match_col([REAL[label]['query']])[0]
    parsed, source_rank = REAL_RECORDS[label]
    return {'label': label, 'rows': 1, 'tables': {'occurrence': 1}, 'source_rank': source_rank,
            'hints': {key: value for key, value in REAL[label]['query'].items() if key != 'scientificName'},
            'qualifier': taxon_matching.split_qualifier(label)[1], 'parsed': parsed, 'match': names.compact_match(summary)}


class GroupDecisionTests(SimpleTestCase):
    def conversion(self, records):
        return SimpleNamespace(plan={'id': 'plan'}, name_review={'plan_id': 'plan', 'status': 'complete',
                                 'labels': copy.deepcopy(records), 'decisions': {}, 'col_release': RELEASE},
                               save=lambda **kwargs: None)

    def test_real_uncertain_fixture_stems_are_automatic_and_keep_their_formula(self):
        for label in ('Sterna sp.', 'Anthus spp.', 'Anura indet.', 'Viola sp.'):
            record = copy.deepcopy(FIXTURE_RECORDS[label])
            self.assertEqual(names.classify(record)['kind'], 'uncertain')
            state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
            self.assertEqual(names.auto_accept(state), 1)
            decision = state['decisions'][label]
            self.assertEqual((decision['decision'], decision['by'], decision['taxonFormula']),
                             ('stem', 'auto:uncertain', 'A ' + names.formula_qualifier(record)))

    def test_id_and_exact_match_fixture_groups(self):
        for label in ('Calanus', 'Chaetognatha'):
            without_id = id_fixture_record(label, {})
            classification = names.classify(without_id)
            self.assertEqual(classification['kind'], 'unconfirmed')
            self.assertNotIn('col', classification['eligible'])
            with_id = id_fixture_record(label)
            self.assertEqual(names.classify(with_id)['kind'], 'auto')
        oncaea = id_fixture_record('Oncaea')
        classification = names.classify(oncaea)
        self.assertEqual((classification['kind'], classification['reasons'][0]['code']), ('unconfirmed', 'id_not_found'))
        for label in ('Polychaeta', 'Isopoda'):
            with_id = id_fixture_record(label)
            self.assertEqual(names.classify(with_id)['kind'], 'auto')
        parathemisto = id_fixture_record('Parathemisto libellula')
        self.assertEqual((names.classify(parathemisto)['kind'], parathemisto['match']['usage']['scientificName']),
                         ('auto', 'Parathemisto libellula'))
        metridia = id_fixture_record('Metridia longa')
        # With its taxonID COL would say Metridia longa longa; by name it is exact, so the ID is never asked.
        self.assertEqual((names.classify(metridia)['group'], metridia['match']['usage']['scientificName']),
                         ('auto', 'Metridia longa'))

    def test_question_mark_labels_are_doubtful_even_when_exact_or_variant(self):
        for label in ('Calanus ?', 'Calanus?', '? Calanus'):
            for match_type in ('EXACT', 'VARIANT'):
                record = {'label': label, 'qualifier': None, 'parsed': real_parse('Calanus'),
                          'match': {'matchType': match_type, 'hintOnly': False, 'usage': {
                              'scientificName': 'Calanus', 'taxonRank': 'genus', 'scientificNameAuthorship': None}}}
                with self.subTest(label=label, match_type=match_type):
                    classification = names.classify(record)
                    self.assertEqual((classification['kind'], classification['group']), ('unconfirmed', 'unconfirmed'))
                    self.assertEqual(names.qualifier_kind(record), 'doubt')
                    self.assertIn('“?”', classification['reasons'][0]['text'])
                    self.assertEqual(classification['eligible'], [])
                    state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
                    self.assertEqual(names.auto_accept(state), 0)
                    conversion = self.conversion([record])
                    with self.assertRaises(names.NameDecisionError):
                        names.bulk_decide(conversion, 'unconfirmed', 'mine')

    def test_a_group_signature_is_the_conflict_that_named_the_group(self):
        # AmphibiaReptilia sp. (568) whose source also said Plantae: the name conflict names the group, not the kingdom.
        record = copy.deepcopy(FIXTURE_RECORDS['AmphibiaReptilia sp.'])
        record['hints'] = {**record['hints'], 'kingdom': 'Plantae'}
        classification = names.classify(record)
        self.assertEqual(classification['group'], 'check:name')
        self.assertEqual([reason['code'] for reason in classification['reasons']], ['name', 'kingdom'])
        signature = names.groups({'labels': [record], 'decisions': {}})[0]['signature']
        self.assertEqual((signature['code'], signature['col']), ('name', 'Amphibia'))

    def test_doubt_markers_anywhere_in_the_label_win_over_a_trailing_sp(self):
        for label in ('Calanus cf. sp.', 'Calanus aff. sp.', 'Calanus nr sp.', 'cf. Calanus', 'Calanus cf.'):
            record = {'label': label, 'rows': 1, 'qualifier': taxon_matching.split_qualifier(label)[1], 'parsed': real_parse(),
                      'match': {'matchType': 'EXACT', 'hintOnly': False, 'alternatives': [],
                                'usage': {'scientificName': 'Calanus', 'taxonRank': 'genus', 'scientificNameAuthorship': None}}}
            with self.subTest(label=label):
                self.assertEqual(names.qualifier_kind(record), 'doubt')
                self.assertEqual(names.classify(record)['eligible'], [])
                self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)
        # A genus that merely starts with those letters is not a marker.
        plain = {'label': 'Nrella sp.', 'qualifier': 'sp.', 'parsed': real_parse()}
        self.assertEqual(names.qualifier_kind(plain), 'uncertain')

    def test_an_overlong_supplied_authorship_is_never_replaced_automatically(self):
        content = ('occurrenceID,scientificName,scientificNameAuthorship\n'
                   f'a,Aus bus,{"Smith " * 30}\n').encode()
        record = names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': 'plan'})['labels'][0]
        self.assertTrue(record['authorships_truncated'])
        record.update(parsed=real_parse('Aus bus'), match={'matchType': 'EXACT', 'hintOnly': False, 'usage': {
            'id': 'X', 'scientificName': 'Aus bus', 'scientificNameAuthorship': 'Jones, 1900', 'taxonRank': 'species'}})
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        names.auto_accept(state)
        self.assertEqual(state['decisions']['Aus bus']['decision'], 'parsed')
        self.assertNotIn('col', names.classify(record)['eligible'])

    def test_rows_giving_a_uninomial_two_high_ranks_are_mixed(self):
        content = (b'occurrenceID,scientificName,taxonRank\n'
                   b'a,Anura,order\nb,Anura,genus\nc,Larus sp.,species\nd,Larus sp.,genus\n')
        records = {record['label']: record for record in
                   names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': 'plan'})['labels']}
        self.assertEqual(records['Anura']['mixed_hints'], ['rank'])
        # "species" beside "Larus sp." is below genus and says nothing about the stem.
        self.assertEqual(records['Larus sp.']['mixed_hints'], [])

    def test_an_uncertain_stem_at_another_rank_than_supplied_is_a_check(self):
        record = {'label': 'Anura indet.', 'rows': 1, 'qualifier': 'indet.', 'source_rank': 'order', 'parsed': real_parse(),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'alternatives': [],
                            'usage': {'scientificName': 'Anura', 'taxonRank': 'genus', 'scientificNameAuthorship': None}}}
        classification = names.classify(record)
        self.assertEqual((classification['group'], classification['eligible']), ('check:rank:order:genus', ['mine']))
        self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)
        record['source_rank'] = 'species'
        self.assertEqual(names.classify(record)['kind'], 'uncertain')

    def test_same_name_variants_are_checked_for_rank_and_lineage_like_exact_matches(self):
        def record(**extra):
            return {'label': 'Anura', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Anura'), **extra,
                    'match': {'matchType': 'VARIANT', 'hintOnly': False, 'usage': {
                        'scientificName': 'Anura', 'taxonRank': 'order', 'scientificNameAuthorship': None,
                        'classification': {'kingdom': 'Animalia', 'class': 'Amphibia'}}}}
        ranked = names.classify(record(source_rank='genus'))
        self.assertEqual((ranked['group'], ranked['eligible']), ('check:rank:genus:order', ['mine']))
        with self.assertRaises(names.NameDecisionError):
            names.build_decision(record(source_rank='genus'), {'decision': 'col'}, {'col_release': RELEASE},
                                 by='bulk:spelling', group_kind='spelling')
        self.assertEqual(names.classify(record(hints={'class': 'Insecta'}))['group'], 'check:class:Insecta:Amphibia')
        self.assertEqual(names.classify(record())['kind'], 'spelling')

    def test_a_row_choice_says_when_cols_authorship_is_not_the_users(self):
        record = {'label': 'Aus bus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Aus bus'),
                  'source_authorships': ['Smith, 1900'], 'hints': {'kingdom': 'Plantae'},
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {
                      'id': 'X', 'scientificName': 'Aus bus', 'scientificNameAuthorship': 'Jones, 1900', 'taxonRank': 'species',
                      'classification': {'kingdom': 'Animalia'}}}}
        choice = names.col_choices(record)[0]
        self.assertTrue(choice['authorship_differs'])
        self.assertFalse(names.col_choices({**record, 'source_authorships': ['Jones 1900']})[0]['authorship_differs'])

    def test_an_unparsed_label_with_trailing_author_text_never_takes_cols_authorship_automatically(self):
        record = {'label': 'Aus bus Smith, 1900', 'rows': 1, 'qualifier': None, 'source_authorships': [],
                  'parsed': {'type': 'SCIENTIFIC', 'usable': False, 'canonical': None, 'authorship': None, 'rank': None,
                             'lossless': False, 'splits': False},
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {
                      'id': 'X', 'scientificName': 'Aus bus', 'scientificNameAuthorship': 'Jones, 1900', 'taxonRank': 'species'}}}
        self.assertFalse(names.authorship_agrees(record, record['match']['usage']))
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        names.auto_accept(state)
        self.assertEqual(state['decisions']['Aus bus Smith, 1900']['decision'], 'keep')
        self.assertNotIn('col', names.classify(record)['eligible'])
        # A bare name with nothing after it still agrees.
        self.assertTrue(names.authorship_agrees({**record, 'label': 'Aus bus'}, record['match']['usage']))
        # Nor does a COL usage without any authorship drop the unread one.
        self.assertFalse(names.authorship_agrees(record, {'scientificName': 'Aus bus', 'scientificNameAuthorship': None}))

    def test_doubt_markers_joined_to_the_epithet_are_seen(self):
        for label in ('Larus cf.argentatus', 'Larus aff.argentatus', 'Larus nr.argentatus'):
            with self.subTest(label=label):
                self.assertEqual(names.qualifier_kind({'label': label, 'qualifier': None, 'parsed': real_parse('Larus')}), 'doubt')

    def test_an_exact_name_with_homonyms_is_kept_without_choosing_one(self):
        # The captured verbose "Sterna" response lists other exact Sterna usages (a mollusc, another authorship).
        record = copy.deepcopy(FIXTURE_RECORDS['Sterna sp.'])
        record.update(label='Sterna', qualifier=None, parsed=real_parse('Sterna'))
        classification = names.classify(record)
        self.assertEqual(classification['kind'], 'auto')
        self.assertIn('homonym', [reason['code'] for reason in classification['reasons']])
        self.assertNotIn('col', classification['eligible'])
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        names.auto_accept(state)
        self.assertEqual((state['decisions']['Sterna']['decision'], state['decisions']['Sterna']['scientificNameAuthorship']), ('parsed', None))
        with self.assertRaises(names.NameDecisionError):
            names.build_decision(record, {'decision': 'col'}, state, by='bulk:auto', group_kind='auto')

    def test_sp_after_a_name_cols_has_above_family_is_decided_one_at_a_time(self):
        record = copy.deepcopy(FIXTURE_RECORDS['Anura indet.'])
        self.assertEqual(names.classify(record)['kind'], 'uncertain')  # "indet." may stop at an order
        record.update(label='Anura sp.', qualifier='sp.')
        classification = names.classify(record)
        self.assertEqual((classification['kind'], classification['reasons'][0]['code'], classification['eligible']),
                         ('unconfirmed', 'rank', ['mine']))
        self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)

    def test_any_unread_word_in_an_unparsed_label_keeps_the_users_authorship(self):
        unparsed = {'type': None, 'usable': False, 'canonical': None, 'authorship': None, 'rank': None, 'lossless': False, 'splits': False}
        usage = {'scientificName': 'Larus argentatus', 'scientificNameAuthorship': 'Pontoppidan, 1763'}
        for label, agrees in (('Larus argentatus s.', False), ('Larus argentatus', True), ('Betula pubescens subsp. tortuosa', True)):
            with self.subTest(label=label):
                self.assertEqual(names.authorship_agrees({'label': label, 'parsed': unparsed}, usage), agrees)

    def test_a_new_species_marker_is_never_published_as_its_genus_automatically(self):
        for label in ('Larus sp. nov.', 'Larus sp. n.', 'Larus sp.nov. 2'):
            record = {'label': label, 'rows': 1, 'qualifier': taxon_matching.split_qualifier(label)[1], 'parsed': real_parse('Larus'),
                      'match': {'matchType': 'EXACT', 'hintOnly': False, 'alternatives': [],
                                'usage': {'scientificName': 'Larus', 'taxonRank': 'genus', 'scientificNameAuthorship': 'Linnaeus, 1758'}}}
            with self.subTest(label=label):
                self.assertEqual(names.qualifier_kind(record), 'doubt')
                self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)

    def test_a_same_name_usage_without_authorship_or_in_another_family_is_a_homonym(self):
        def record(other):
            return {'label': 'Aus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Aus'),
                    'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {
                        'id': '1', 'scientificName': 'Aus', 'scientificNameAuthorship': 'Smith, 1900', 'taxonRank': 'genus',
                        'classification': {'kingdom': 'Animalia', 'family': 'Aidae'}},
                        'alternatives': [{'id': '2', 'scientificName': 'Aus', 'taxonRank': 'genus', 'matchType': 'EXACT', **other}]}}
        for other in ({'scientificNameAuthorship': None, 'classification': {'kingdom': 'Animalia', 'family': 'Aidae'}},
                      {'scientificNameAuthorship': 'Smith, 1900', 'classification': {'kingdom': 'Animalia', 'family': 'Bidae'}}):
            with self.subTest(other=other):
                self.assertEqual(names.row_default(record(other), names.classify(record(other))), 'parsed')
        same = {'scientificNameAuthorship': 'Smith 1900', 'classification': {'kingdom': 'Animalia', 'family': 'Aidae'}}
        self.assertEqual(names.row_default(record(same), names.classify(record(same))), 'col')
        stem = record({'scientificNameAuthorship': None})
        stem.update(label='Aus sp.', qualifier='sp.', parsed=real_parse())
        self.assertIsNone(names.stem_usage(stem)['scientificNameAuthorship'])

    def test_a_stem_whose_same_name_usages_differ_in_rank_needs_the_supplied_rank(self):
        anura = copy.deepcopy(FIXTURE_RECORDS['Anura indet.'])  # COL's pick is the order; genus homonyms are alternatives
        self.assertEqual(names.stem_usage({**anura, 'source_rank': 'order'})['taxonRank'], 'order')
        unranked = {**anura, 'source_rank': None}
        self.assertIsNone(names.stem_usage(unranked))
        self.assertEqual((names.classify(unranked)['kind'], names.classify(unranked)['reasons'][0]['code']), ('unconfirmed', 'rank'))
        # A genus and its subgenus of the same name ("Sterna") are one assertion.
        self.assertEqual(names.stem_usage(copy.deepcopy(FIXTURE_RECORDS['Sterna sp.']))['taxonRank'], 'genus')

    def test_a_sp_stem_found_only_among_alternatives_is_checked_against_the_sources_kingdom(self):
        # 558: "Sapotaceae sp" tagged Animalia; the verbose match picks nothing and lists the plant family twice.
        record = {'label': 'Sapotaceae sp', 'rows': 28, 'qualifier': 'sp.', 'hints': {'kingdom': 'Animalia'}, 'parsed': real_parse(),
                  'match': {'matchType': 'HIGHERRANK', 'hintOnly': True, 'usage': None, 'alternatives': [
                      {'id': 'A', 'scientificName': 'Sapotaceae', 'taxonRank': 'family', 'matchType': 'EXACT',
                       'classification': {'kingdom': 'Plantae'}},
                      {'id': 'B', 'scientificName': 'Sapotaceae', 'taxonRank': 'family', 'matchType': 'EXACT',
                       'classification': {'kingdom': 'Plantae'}}]}}
        classification = names.classify(record)
        self.assertEqual((classification['group'], classification['eligible']), ('check:kingdom:Animalia:Plantae', ['col', 'mine']))
        self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)

    def test_conflict_groups_keep_names_with_different_values_apart(self):
        def ranked(source, col):
            return {'label': f'Aus {source}', 'rows': 1, 'qualifier': None, 'source_rank': source, 'parsed': real_parse('Aus'),
                    'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {'scientificName': 'Aus', 'taxonRank': col}}}
        self.assertNotEqual(names.classify(ranked('genus', 'order'))['group'], names.classify(ranked('family', 'genus'))['group'])

    def test_a_stem_without_a_cols_rank_is_not_accepted(self):
        record = {'label': 'Larus sp.', 'rows': 1, 'qualifier': 'sp.', 'source_rank': 'species', 'parsed': real_parse(),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'alternatives': [],
                            'usage': {'scientificName': 'Larus', 'taxonRank': None, 'scientificNameAuthorship': None}}}
        self.assertIsNone(names.stem_usage(record))
        self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)

    def test_hints_differing_only_in_case_or_remarks_are_kept(self):
        content = (b'occurrenceID,scientificName,kingdom,order\n'
                   b'a,Aus bus,Animalia,Squamata (lizards)\nb,Aus bus,animalia,Squamata\n')
        record = names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': 'plan'})['labels'][0]
        self.assertEqual(names._hint_normal(record['hints']['kingdom']), 'animalia')
        self.assertEqual(names._hint_normal(record['hints']['order']), 'squamata')
        self.assertEqual(record['mixed_hints'], [])

    def test_a_spelling_correction_keeps_the_supplied_authorship_when_col_has_none(self):
        state = {'labels': [{'label': 'Circium heterophyllum'}], 'decisions': {'Circium heterophyllum': {
            'decision': 'col', 'source': 'col', 'scientificName': 'Cirsium heterophyllum', 'scientificNameAuthorship': None,
            'taxonRank': 'species', 'changeKind': 'spelling', 'nameRules': names.NAME_RULES_VERSION, 'by': 'bulk:spelling'}}}
        frame = pd.DataFrame([{'occurrence_pk': 'o1', 'scientificName': 'Circium heterophyllum', 'scientificNameAuthorship': '(L.) Hill',
                               'taxonRank': 'species'}])
        result, _ = names.apply_name_decisions({'occurrence': frame}, state, {'occurrence': [
            {'name': 'Circium heterophyllum', 'authorship': '(L.) Hill', 'rank': 'species'}]})
        self.assertEqual(result['occurrence'].loc[0, ['scientificName', 'scientificNameAuthorship']].tolist(),
                         ['Cirsium heterophyllum', '(L.) Hill'])

    def test_replacing_a_different_supplied_authorship_needs_confirmation_through_the_api_too(self):
        record = {'label': 'Aus bus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Aus bus'), 'source_authorships': ['Smith'],
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {
                      'id': 'X', 'scientificName': 'Aus bus', 'scientificNameAuthorship': 'Linnaeus', 'taxonRank': 'species'}}}
        with self.assertRaisesRegex(names.NameDecisionError, 'authorship'):
            names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})
        confirmed = names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})
        self.assertEqual(confirmed['scientificNameAuthorship'], 'Linnaeus')

    def test_a_check_group_never_counts_a_stem_it_cannot_apply(self):
        record = copy.deepcopy(FIXTURE_RECORDS['Anura indet.'])
        record.update(label='Anura sp.', qualifier='sp.', hints={**record['hints'], 'kingdom': 'Plantae'})
        classification = names.classify(record)
        self.assertEqual(classification['kind'], 'check')
        self.assertNotIn('col', classification['eligible'])

    def test_a_supplied_rank_in_two_cases_is_one_rank(self):
        content = b'occurrenceID,scientificName,taxonRank\na,Anura,Genus\nb,Anura,genus\n'
        record = names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': 'plan'})['labels'][0]
        self.assertEqual(record['source_rank'], 'genus')
        record.update(parsed=real_parse('Anura'), match={'matchType': 'EXACT', 'hintOnly': False,
                                                         'usage': {'scientificName': 'Anura', 'taxonRank': 'order'}})
        self.assertEqual(names.classify(record)['group'], 'check:rank:genus:order')

    def test_another_explicit_rank_marker_is_another_name(self):
        record = {'label': 'Carex nigra subsp. juncea', 'rows': 1, 'qualifier': None,
                  'parsed': real_parse('Carex nigra subsp. juncea', 'subspecies'),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {
                      'scientificName': 'Carex nigra var. juncea', 'taxonRank': 'variety', 'scientificNameAuthorship': '(Fr.) Hyl.'}}}
        found = names.change(record, record['match']['usage'], 'EXACT')
        self.assertEqual((found['kind'], found['confirm']), ('marker', True))
        self.assertEqual(names.classify(record)['group'], 'check:name')
        self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)
        # "ssp." and "subsp." are one marker.
        same = {**record['match']['usage'], 'scientificName': 'Carex nigra ssp. juncea', 'taxonRank': 'subspecies'}
        self.assertIsNone(names.change(record, same, 'EXACT'))
        for asserted, theirs in (('Carex nigra nothosubsp. juncea', 'Carex nigra subsp. juncea'),
                                 ('Carex nigra juncea', 'Carex nigra var. juncea'), ('Carex nigra var. juncea', 'Carex nigra juncea')):
            with self.subTest(asserted=asserted):
                found = names.change({**record, 'label': asserted, 'parsed': real_parse(asserted)}, {**record['match']['usage'], 'scientificName': theirs}, 'EXACT')
                self.assertEqual(found['kind'], 'marker')

    def test_another_family_keeps_the_users_authorship_and_mixed_families_are_a_check(self):
        record = {'label': 'Aus bus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Aus bus'), 'hints': {'family': 'Bidae'},
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {
                      'id': '1', 'scientificName': 'Aus bus', 'scientificNameAuthorship': 'Smith', 'taxonRank': 'species',
                      'classification': {'family': 'Aidae'}}}}
        classification = names.classify(record)
        self.assertEqual(classification['kind'], 'auto')
        self.assertEqual(names.row_default(record, classification), 'parsed')
        self.assertNotIn('col', classification['eligible'])
        content = b'occurrenceID,scientificName,family\na,Aus,Aidae\nb,Aus,Bidae\n'
        mixed = names.collect_state(read_inputs([('occurrence.csv', content)]), {'id': 'plan'})['labels'][0]
        self.assertEqual(mixed['mixed_hints'], ['family'])

    def test_an_unmarked_trinomial_supplied_as_a_variety_is_not_cols_subspecies(self):
        record = {'label': 'Aus bus cus', 'rows': 1, 'qualifier': None, 'source_rank': 'variety', 'parsed': real_parse('Aus bus cus'),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {'scientificName': 'Aus bus cus', 'taxonRank': 'subspecies'}}}
        self.assertEqual(names.classify(record)['group'], 'check:rank:variety:subspecies')
        self.assertEqual(names.classify({**record, 'source_rank': 'species'})['kind'], 'auto')

    def test_same_author_homonyms_in_other_lineages_publish_a_stem_without_authorship(self):
        def usage(key, family):
            return {'id': key, 'scientificName': 'Aus', 'scientificNameAuthorship': 'Smith', 'taxonRank': 'genus', 'matchType': 'EXACT',
                    'classification': {'kingdom': 'Animalia', 'family': family}}
        record = {'label': 'Aus sp.', 'rows': 1, 'qualifier': 'sp.', 'parsed': real_parse(),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': usage('1', 'Aidae'), 'alternatives': [usage('2', 'Bidae')]}}
        self.assertIsNone(names.stem_usage(record)['scientificNameAuthorship'])
        record['match']['alternatives'] = [usage('2', 'Aidae')]
        self.assertEqual(names.stem_usage(record)['scientificNameAuthorship'], 'Smith')

    def test_unlisted_exact_alternatives_count_as_homonyms(self):
        record = {'label': 'Aus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Aus'),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'exactAlternativesDropped': True, 'alternatives': [],
                            'usage': {'id': '1', 'scientificName': 'Aus', 'scientificNameAuthorship': 'Smith', 'taxonRank': 'genus'}}}
        self.assertTrue(names._homonyms(record))
        self.assertEqual(names.row_default(record, names.classify(record)), 'parsed')

    def test_a_hybrid_sign_or_agg_is_part_of_the_name(self):
        for label, theirs in (('Rosa × canina', 'Rosa canina'), ('Rosa canina agg.', 'Rosa canina'), ('Rosa x canina', 'Rosa canina'),
                              ('Rosa canina', 'Rosa × canina')):
            record = {'label': label, 'rows': 1, 'qualifier': None, 'parsed': real_parse('Rosa canina'),
                      'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {'scientificName': theirs, 'taxonRank': 'species'}}}
            with self.subTest(label=label, theirs=theirs):
                self.assertEqual(names.change(record, record['match']['usage'], 'EXACT')['kind'], 'marker')
                self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)

    def test_a_stem_with_unlisted_exact_alternatives_is_not_resolved(self):
        record = copy.deepcopy(FIXTURE_RECORDS['Larus sp.'])
        record['match']['exactAlternativesDropped'] = True
        self.assertIsNone(names.stem_usage(record))
        self.assertEqual(names.classify(record)['kind'], 'unconfirmed')

    def test_a_rank_the_source_does_not_give_needs_a_second_click_per_name(self):
        record = {'label': 'Anura', 'rows': 1, 'qualifier': None, 'source_rank': 'genus', 'parsed': real_parse('Anura'),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {'id': 'O', 'scientificName': 'Anura', 'taxonRank': 'order'}}}
        self.assertEqual(names.col_choices(record)[0]['rank_change'], 'makes your genus an order')
        with self.assertRaisesRegex(names.NameDecisionError, 'makes your genus an order'):
            names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})
        self.assertEqual(names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})['taxonRank'], 'order')
        stem = copy.deepcopy(FIXTURE_RECORDS['Anura indet.'])
        stem.update(label='Anura sp.', qualifier='sp.')
        with self.assertRaisesRegex(names.NameDecisionError, 'follows a genus or family'):
            names.build_decision(stem, {'decision': 'stem'}, {'col_release': RELEASE})
        self.assertEqual(names.build_decision(stem, {'decision': 'stem', 'confirm_coarser': True}, {'col_release': RELEASE})['taxonRank'], 'order')

    def test_a_spelling_correction_that_drops_a_hybrid_sign_is_a_marker_change(self):
        record = {'label': 'Rosa × caninus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Rosa caninus'), 'hints': {'kingdom': 'Plantae', 'family': 'Rosaceae'},
                  'match': {'matchType': 'VARIANT', 'hintOnly': False, 'usage': {
                      'scientificName': 'Rosa canina', 'taxonRank': 'species', 'classification': {'kingdom': 'Plantae', 'family': 'Rosaceae'}}}}
        self.assertEqual(names.change(record, record['match']['usage'], 'VARIANT')['kind'], 'marker')
        self.assertNotIn('col', names.classify(record)['eligible'])

    def test_variant_and_other_rank_same_name_usages_are_homonyms(self):
        def record(other):
            return {'label': 'Aus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Aus'),
                    'match': {'matchType': 'EXACT', 'hintOnly': False, 'alternatives': [{'id': '2', 'scientificName': 'Aus', **other}],
                              'usage': {'id': '1', 'scientificName': 'Aus', 'scientificNameAuthorship': 'Smith', 'taxonRank': 'genus'}}}
        self.assertTrue(names._homonyms(record({'matchType': 'VARIANT', 'scientificNameAuthorship': 'Jones', 'taxonRank': 'genus'})))
        self.assertTrue(names._homonyms(record({'matchType': 'EXACT', 'scientificNameAuthorship': 'Smith', 'taxonRank': 'order'})))
        self.assertFalse(names._homonyms(record({'matchType': 'EXACT', 'scientificNameAuthorship': 'Smith', 'taxonRank': 'subgenus'})))

    def test_a_confirmation_is_bound_to_the_usage_it_showed(self):
        record = {'label': 'Calanus', 'rows': 1, 'qualifier': None, 'parsed': real_parse('Calanus'),
                  'match': {'matchType': 'HIGHERRANK', 'hintOnly': False, 'usage': {'id': 'P', 'scientificName': 'Plantae', 'taxonRank': 'kingdom'}}}
        with self.assertRaisesRegex(names.NameDecisionError, 'now suggests another name'):
            names.build_decision(record, {'decision': 'col', 'usage_id': 'RT', 'confirm_coarser': True}, {'col_release': RELEASE})

    def test_the_labels_own_authorship_survives_a_col_name_without_one(self):
        state = {'labels': [{'label': 'Aus bus Smith, 1900', 'parsed': real_parse('Aus bus', authorship='Smith, 1900')}],
                 'decisions': {'Aus bus Smith, 1900': {'decision': 'col', 'source': 'col', 'scientificName': 'Aus bus',
                                                       'scientificNameAuthorship': None, 'taxonRank': 'species', 'changeKind': None,
                                                       'nameRules': names.NAME_RULES_VERSION, 'by': 'auto:exact'}}}
        frame = pd.DataFrame([{'occurrence_pk': 'o1', 'scientificName': 'Aus bus Smith, 1900', 'scientificNameAuthorship': '', 'taxonRank': ''}])
        result, _ = names.apply_name_decisions({'occurrence': frame}, state, {'occurrence': [{'name': 'Aus bus Smith, 1900', 'authorship': None, 'rank': None}]})
        self.assertEqual(result['occurrence'].loc[0, ['scientificName', 'scientificNameAuthorship']].tolist(), ['Aus bus', 'Smith, 1900'])

    def test_a_variant_same_name_usage_at_another_rank_makes_a_stem_ambiguous(self):
        record = {'label': 'Anura indet.', 'rows': 1, 'qualifier': 'indet.', 'parsed': real_parse(),
                  'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {'id': '1', 'scientificName': 'Anura', 'taxonRank': 'genus'},
                            'alternatives': [{'id': '2', 'scientificName': 'Anura', 'taxonRank': 'order', 'matchType': 'VARIANT'}]}}
        self.assertIsNone(names.stem_usage(record))
        self.assertEqual(names.stem_usage({**record, 'source_rank': 'genus'})['taxonRank'], 'genus')

    def test_a_confirmed_marker_or_rank_change_does_not_carry_the_supplied_authorship(self):
        frame = pd.DataFrame([{'occurrence_pk': 'o1', 'scientificName': 'x', 'scientificNameAuthorship': 'Smith', 'taxonRank': 'variety'}])
        for label, decision in (('Aus bus var. cus', {'scientificName': 'Aus bus subsp. cus', 'taxonRank': 'subspecies', 'changeKind': 'marker'}),
                                ('Anura', {'scientificName': 'Anura', 'taxonRank': 'order', 'changeKind': None, 'rankChange': 'makes your genus an order'})):
            state = {'labels': [{'label': label, 'parsed': real_parse(label)}],
                     'decisions': {label: {'decision': 'col', 'source': 'col', 'scientificNameAuthorship': None, 'confirmedCoarser': True,
                                           'nameRules': names.NAME_RULES_VERSION, 'by': 'user', **decision}}}
            with self.subTest(label=label):
                result, _ = names.apply_name_decisions({'occurrence': frame.assign(scientificName=label)}, state,
                                                       {'occurrence': [{'name': label, 'authorship': 'Smith', 'rank': 'variety'}]})
                self.assertEqual(result['occurrence'].loc[0, 'scientificNameAuthorship'], '')

    def test_exact_uninomial_source_rank_mismatch_is_a_check_conflict(self):
        record = {'label': 'Anura', 'rows': 1, 'source_rank': 'genus', 'qualifier': None,
                  'parsed': real_parse('Anura', 'genus'), 'match': {'matchType': 'EXACT', 'hintOnly': False,
                  'usage': {'scientificName': 'Anura', 'taxonRank': 'order', 'scientificNameAuthorship': None}}}
        classification = names.classify(record)
        self.assertEqual((classification['group'], classification['kind'], classification['reasons'][0]['code']),
                         ('check:rank:genus:order', 'check', 'rank'))
        self.assertEqual(classification['reasons'][0]['text'], 'Your rank is genus; COL has this name as order')
        # COL's order is never applied to the group; it stays a per-name choice.
        self.assertEqual(classification['eligible'], ['mine'])
        with self.assertRaises(names.NameDecisionError):
            names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE}, by='bulk:check', group_kind='check')
        self.assertEqual(names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})['taxonRank'], 'order')
        self.assertEqual(names.auto_accept({'labels': [record], 'decisions': {}, 'col_release': RELEASE}), 0)
        self.assertEqual(names.groups({'labels': [record], 'decisions': {}})[0]['signature'],
                         {'code': 'rank', 'yours': 'genus', 'col': 'order'})

    def test_source_rank_does_not_conflict_with_inferred_trinomial_or_uncertain_stem(self):
        trinomial = {'label': 'Motacilla flava thunbergi', 'rows': 1, 'source_rank': 'species', 'qualifier': None,
                     'parsed': real_parse('Motacilla flava thunbergi', 'species'),
                     'match': {'matchType': 'EXACT', 'hintOnly': False, 'usage': {
                         'scientificName': 'Motacilla flava thunbergi', 'taxonRank': 'subspecies',
                         'scientificNameAuthorship': None}}}
        self.assertEqual(names.classify(trinomial)['kind'], 'auto')
        uncertain = {'label': 'Larus sp.', 'rows': 1, 'source_rank': 'species', 'qualifier': 'sp.',
                     'parsed': real_parse(), 'match': {'matchType': 'EXACT', 'hintOnly': False, 'alternatives': [],
                     'usage': {'scientificName': 'Larus', 'taxonRank': 'genus', 'scientificNameAuthorship': None}}}
        self.assertEqual(names.classify(uncertain)['kind'], 'uncertain')

    def test_variant_fuzzy_and_canonical_usage_names_share_the_spelling_group(self):
        labels = ('Trema orientalis', 'Erigeron acre', 'Circium heterophyllum', 'Albizzia zygia')
        for label in labels:
            with self.subTest(label=label):
                record = copy.deepcopy(FIXTURE_RECORDS[label])
                classification = names.classify(record)
                self.assertEqual(set(classification), {'group', 'kind', 'reasons', 'default', 'eligible'})
                self.assertEqual(classification['group'], 'spelling')
                self.assertEqual(classification['kind'], 'spelling')
                expected_col = names.change(record, record['match'].get('usage') or {},
                                            record['match'].get('matchType'))
                may_take_col = (expected_col is None or expected_col.get('kind') == 'spelling')
                may_take_col = may_take_col and names.authorship_agrees(record, record['match'].get('usage') or {})
                self.assertEqual('col' in classification['eligible'],
                                 may_take_col)
                state = {'labels': [record], 'decisions': {label: {'by': 'user'}}}
                names.groups(state, {label: classification})
                self.assertNotIn('decisions', classification)

    def test_stem_authorship_disagreement_is_not_written_over_the_supplied_authorship(self):
        record = copy.deepcopy(FIXTURE_RECORDS['Sterna sp.'])
        record['source_authorships'] = ['Other author']
        record['parsed'] = {**record['parsed'], 'authorship': 'Other author'}
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        self.assertEqual(names.auto_accept(state), 1)
        self.assertIsNone(state['decisions'][record['label']]['scientificNameAuthorship'])
        frames = {'occurrence': pd.DataFrame([{'scientificName': '', 'scientificNameAuthorship': ''}])}
        result, _ = names.apply_name_decisions(frames, state, {'occurrence': [{'name': record['label'], 'authorship': 'Other author'}]})
        self.assertEqual(result['occurrence'].loc[0, 'scientificNameAuthorship'], 'Other author')

    def test_stem_formula_fills_only_blank_identification_cells(self):
        record = copy.deepcopy(FIXTURE_RECORDS['Sterna sp.'])
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        names.auto_accept(state)
        source = {'identification': pd.DataFrame([
            {'scientificName': '', 'scientificNameAuthorship': '', 'taxonRank': '', 'taxonFormula': '',
             'verbatimIdentification': 'Sterna sp.'},
            {'scientificName': '', 'scientificNameAuthorship': '', 'taxonRank': '', 'taxonFormula': 'existing',
             'verbatimIdentification': 'Sterna sp.'}])}
        result, section = names.apply_name_decisions(source, state, {'identification': ['Sterna sp.', 'Sterna sp.']})
        self.assertEqual(result['identification']['scientificName'].tolist(), ['Sterna', 'Sterna'])
        self.assertEqual(result['identification']['taxonFormula'].tolist(), ['A sp.', 'existing'])
        self.assertEqual(section['entries'][0]['taxonFormulaWritten'], 1)
        self.assertEqual(result['identification']['verbatimIdentification'].tolist(), ['Sterna sp.', 'Sterna sp.'])

    def test_stem_formula_adds_a_missing_identification_column(self):
        record = copy.deepcopy(FIXTURE_RECORDS['Sterna sp.'])
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        names.auto_accept(state)
        source = {'identification': pd.DataFrame([{'scientificName': '', 'verbatimIdentification': 'Sterna sp.'}])}
        result, _ = names.apply_name_decisions(source, state, {'identification': ['Sterna sp.']})
        self.assertEqual(result['identification']['taxonFormula'].tolist(), ['A sp.'])

    def test_column_qualifiers_auto_publish_stem_with_each_rows_formula(self):
        label = 'Pseudocalanus'
        record = {'label': label, 'rows': 3, 'qualifier': None, 'source_qualifiers': ['sp.', 'spp.'],
                  'source_qualifier_rows': 2, 'source_qualifier_counts': {'sp.': 1, 'spp.': 1},
                  'parsed': real_parse(), 'match': {'matchType': 'EXACT', 'hintOnly': False, 'alternatives': [],
                                                    'usage': {'id': 'GENUS', 'scientificName': label,
                                                              'taxonRank': 'genus', 'scientificNameAuthorship': None}}}
        self.assertEqual(names.qualifier_kind(record), 'uncertain')
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        self.assertEqual(names.auto_accept(state), 1)
        snapshot = state['decisions'][label]
        self.assertEqual((snapshot['decision'], snapshot['taxonFormula'], snapshot['stemFormula']),
                         ('stem', 'A sp.', True))
        source = {'identification': pd.DataFrame([
            {'scientificName': '', 'taxonFormula': ''},
            {'scientificName': '', 'taxonFormula': ''},
            {'scientificName': '', 'taxonFormula': ''}])}
        source_names = {'identification': [
            {'name': label, 'qualifier': 'sp.'}, {'name': label, 'qualifier': 'spp.'}, {'name': label, 'qualifier': None}]}
        result, section = names.apply_name_decisions(source, state, source_names)
        self.assertEqual(result['identification']['scientificName'].tolist(), [label] * 3)
        self.assertEqual(result['identification']['taxonFormula'].tolist(), ['A sp.', 'A spp.', ''])
        self.assertEqual(section['entries'][0]['taxonFormulaWritten'], 2)

    def test_column_cf_rows_are_doubtful_and_not_auto_or_bulk(self):
        record = {'label': 'Eutropis multifasciata (Kuhl, 1820)', 'rows': 10, 'qualifier': None,
                  'source_qualifiers': ['cf.'], 'source_qualifier_rows': 2,
                  'source_qualifier_counts': {'cf.': 2}, 'parsed': real_parse(), 'match': {}}
        self.assertEqual(names.qualifier_kind(record), 'doubt')
        reason = names._reason_for_unconfirmed(record)[0]['text']
        self.assertEqual(reason, '2 of 10 rows say “cf.”; decide this one yourself')
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        self.assertEqual(names.auto_accept(state), 0)
        self.assertFalse(names.eligible(record, 'keep', names.classify(record)))
        conversion = self.conversion([record])
        with self.assertRaises(names.NameDecisionError):
            names.bulk_decide(conversion, 'unconfirmed', 'mine')

    def test_one_question_mark_source_row_makes_draco_sp_doubtful(self):
        record = {'label': 'Draco sp.', 'rows': 10, 'qualifier': 'sp.', 'source_qualifiers': ['?'],
                  'source_qualifier_rows': 1, 'source_qualifier_counts': {'?.': 1}, 'parsed': real_parse(), 'match': {}}
        self.assertEqual(names.qualifier_kind(record), 'doubt')

    def test_558_kingdom_conflict_groups_and_stem_option_are_server_computed(self):
        records = [copy.deepcopy(FIXTURE_RECORDS[label]) for label in
                   ('Bosqueia phoberos', 'Bosqueia phoberos', 'Sapotaceae sp')]
        records[1]['label'] = 'Bosqueia phoberos clone'
        records[1]['hints'] = {'kingdom': 'Animalia'}
        records[2]['hints'] = {'kingdom': 'Animalia'}
        records[2]['source_rank'] = 'family'
        conversion = self.conversion(records)
        state = conversion.name_review
        matching = [group for group in names.groups(state) if group['id'] == 'check:kingdom:Animalia:Plantae']
        self.assertEqual(len(matching), 1)
        result = names.bulk_decide(conversion, matching[0]['id'], 'col')
        self.assertEqual(result['count'], 3)
        self.assertEqual(conversion.name_review['decisions']['Sapotaceae sp']['decision'], 'stem')
        batch_id = result['batch']
        conversion.name_review['decisions']['Bosqueia phoberos'] = names.build_decision(
            records[0], {'decision': 'keep'}, state)
        self.assertEqual(names.undo_batch(conversion, batch_id), 2)
        self.assertNotIn('Bosqueia phoberos clone', conversion.name_review['decisions'])
        self.assertEqual(conversion.name_review['decisions']['Bosqueia phoberos']['decision'], 'keep')

    def test_mixed_and_class_conflicts_are_checked_before_auto_acceptance(self):
        mixed = names.collect_state(read_inputs([('occurrence.csv',
            b'occurrenceID,scientificName,kingdom,phylum,class\n'
            b'a,Anura,Animalia,Chordata,Amphibia\n'
            b'b,Anura,Animalia,Arthropoda,Insecta\n')]), {'id': 'plan'})['labels'][0]
        mixed.update(parsed=real_parse('Anura', 'order'), match={
            'matchType': 'EXACT', 'hintOnly': False, 'usage': {'scientificName': 'Anura', 'taxonRank': 'order',
                'scientificNameAuthorship': None, 'classification': {'kingdom': 'Animalia', 'phylum': 'Chordata', 'class': 'Amphibia'}}})
        classified = names.classify(mixed)
        self.assertEqual((classified['group'], classified['reasons'][0]['code']), ('check:mixed:class,phylum', 'mixed'))
        self.assertNotIn('col', classified['eligible'])
        self.assertIn('mine', classified['eligible'])
        self.assertEqual(names.groups({'labels': [mixed], 'decisions': {}})[0]['signature'],
                         {'code': 'mixed', 'yours': 'class, phylum', 'col': None})

        copepod = {'label': 'Calanus', 'parsed': real_parse('Calanus', 'genus'), 'hints': {'class': 'Copepoda'},
                   'mixed_hints': [], 'qualifier': None, 'match': {'matchType': 'EXACT', 'hintOnly': False,
                   'usage': {'scientificName': 'Calanus', 'taxonRank': 'genus', 'scientificNameAuthorship': None,
                             'classification': {'class': 'Insecta'}}}}
        classified = names.classify(copepod)
        self.assertEqual((classified['group'], classified['reasons'][0]['code']), ('check:class:Copepoda:Insecta', 'class'))
        # Never accepted automatically; COL's name is only the user's explicit group choice, as for 558's kingdom.
        state = {'labels': [copepod], 'decisions': {}, 'col_release': RELEASE}
        self.assertEqual(names.auto_accept(state), 0)
        self.assertEqual(classified['eligible'], ['col', 'mine'])

    def test_spelling_group_reasons_explain_trema_and_albizzia(self):
        for label in ('Trema orientalis', 'Albizzia zygia'):
            record = copy.deepcopy(FIXTURE_RECORDS[label])
            classification = names.classify(record)
            found = names.change(record, record['match']['usage'], record['match']['matchType'])
            self.assertEqual(classification['reasons'][0]['text'],
                             f"COL suggests {record['match']['usage']['scientificName']}; it {found['text']}")

    def test_fixture_auto_and_bulk_writes_obey_name_and_authorship_safety(self):
        fixtures = {case['label']: fixture_record(case) for case in GROUP_MATCHES}
        id_labels = {case['label'] for case in ID_MATCHES_FIXTURE}
        fixtures.update({label: id_fixture_record(label) for label in id_labels})
        for record in fixtures.values():
            for variant, qualifier in (('', record.get('qualifier')), (' cf.', 'cf.')):
                candidate = copy.deepcopy(record)
                if variant and not candidate.get('qualifier'):
                    candidate['label'] += variant
                    candidate['qualifier'] = qualifier
                classification = names.classify(candidate)
                state = {'labels': [candidate], 'decisions': {}, 'col_release': RELEASE}
                if classification['kind'] in {'auto', 'uncertain'}:
                    names.auto_accept(state)
                    snapshot = state['decisions'].get(candidate['label'])
                    if snapshot:
                        self.assertNotEqual(names.qualifier_kind(candidate), 'doubt')
                        if snapshot['decision'] in {'col', 'stem', 'alternative'}:
                            self.assertEqual(taxon_matching.name_parts(snapshot['scientificName']),
                                             taxon_matching.name_parts(names.asserted_name(candidate)))
                            if snapshot.get('scientificNameAuthorship'):
                                self.assertTrue(names.authorship_agrees(candidate, snapshot))
                for option in names.GROUP_OPTIONS.get(classification['kind'], []):
                    if not names.eligible(candidate, option, classification, {}):
                        continue
                    conversion = self.conversion([candidate])
                    try:
                        names.bulk_decide(conversion, classification['group'], option)
                    except names.NameDecisionError:
                        continue
                    snapshot = conversion.name_review['decisions'][candidate['label']]
                    self.assertNotEqual(names.qualifier_kind(candidate), 'doubt')
                    if snapshot['decision'] in {'col', 'stem', 'alternative'}:
                        asserted = names.asserted_name(candidate)
                        # Only a spelling group may write COL's spelling, and only one checked as a spelling change.
                        if not (classification['kind'] == 'spelling' and snapshot.get('changeKind') == 'spelling'):
                            self.assertEqual(taxon_matching.name_parts(snapshot['scientificName']), taxon_matching.name_parts(asserted))
                        if snapshot.get('scientificNameAuthorship'):
                            self.assertTrue(names.authorship_agrees(candidate, snapshot))

    def test_auto_undo_declines_and_group_mine_resolves_lossless_parse(self):
        record = copy.deepcopy(FIXTURE_RECORDS['Sterna sp.'])
        state = {'labels': [record], 'decisions': {}, 'col_release': RELEASE}
        names.auto_accept(state)
        conversion = self.conversion([record]); conversion.name_review['decisions'] = state['decisions']
        self.assertEqual(names.undo_auto(conversion, 'auto'), 0)
        self.assertEqual(names.undo_auto(conversion, 'uncertain'), 1)
        self.assertEqual(conversion.name_review['auto_declined'], [record['label']])
        mine_record = copy.deepcopy(FIXTURE_RECORDS['Trientalis europaea'])
        decision = names.build_decision(mine_record, {'decision': 'mine'}, {'col_release': RELEASE})
        self.assertEqual(decision['decision'], 'parsed')


class RealCoarserMatchTests(SimpleTestCase):
    """COL names coarser than, or in another genus from, the user's name were accepted with one click in production."""
    COARSER = {
        'Calanus': ('Arthropoda', 'replaces your genus with a phylum'),  # 566, 1,092 rows
        'Chaetognatha': ('Animalia', 'replaces your name with a kingdom'),  # 566, 366 rows
        'Oncaea': ('Animalia', 'replaces your genus with a kingdom'),  # 567
        'Polychaeta': ('Animalia', 'replaces your name with a kingdom'),  # 567
        'Trientalis europaea': ('Lysimachia', 'replaces your species with a genus'),  # 569, 679 rows
        'Mnium cuspidatum': ('Plagiomnium', 'replaces your species with a genus'),  # 565
        'Galium boreale': ('Trichogalium', 'replaces your species with a genus'),  # 565
        'AmphibiaReptilia sp.': ('Amphibia', 'replaces your genus with a class'),  # 568: an EXACT match steered by the class hint
    }

    def test_coarser_col_names_are_never_bulk_or_default_and_need_explicit_confirmation(self):
        for label, (col_name, text) in self.COARSER.items():
            with self.subTest(label):
                record = real_record(label)
                self.assertEqual(record['match']['usage']['scientificName'], col_name)
                entry = names._entry(record, {})
                main = entry['col_choices'][0]
                self.assertEqual((main['decision'], main['same_name'], main['replaces']['text']), ('col', False, text))
                self.assertEqual(entry['suggested'], 'keep')
                self.assertFalse(names.eligible(record, 'col', names.classify(record)))
                with self.assertRaisesRegex(names.NameDecisionError, 'Confirm'):
                    names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})
                with self.assertRaisesRegex(names.NameDecisionError, 'never accepted in bulk'):
                    names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE}, by='bulk:auto')
                confirmed = names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})
                self.assertEqual((confirmed['scientificName'], confirmed['confirmedCoarser'], confirmed['replaces']), (col_name, True, text))

    def test_bulk_acceptance_skips_a_coarser_exact_match_even_without_a_qualifier(self):
        record = {**real_record('AmphibiaReptilia sp.'), 'label': 'AmphibiaReptilia', 'qualifier': None}
        self.assertEqual(record['match']['matchType'], 'EXACT')
        self.assertFalse(names.eligible(record, 'col', names.classify(record)))

    def test_same_name_alternatives_are_offered_with_context_and_need_no_confirmation(self):
        calanus = real_record('Calanus')
        same = [choice for choice in names.col_choices(calanus) if choice['same_name']]
        self.assertEqual([(choice['usage']['id'], choice['usage']['scientificNameAuthorship'], choice['usage']['taxonRank'],
                           choice['usage']['classification'].get('phylum'), choice['usage']['classification'].get('class'), choice['replaces'])
                          for choice in same],
                         [('7NRJ6', 'Leach, 1816', 'genus', 'Arthropoda', 'Copepoda', None),
                          ('8NMRP', 'Saussure, 1862', 'genus', 'Arthropoda', 'Insecta', None)])
        chosen = names.build_decision(calanus, {'decision': 'alternative', 'usage_id': '7NRJ6'}, {'col_release': RELEASE})
        self.assertEqual((chosen['scientificName'], chosen['scientificNameAuthorship'], chosen['taxonRank'], chosen.get('replaces')),
                         ('Calanus', 'Leach, 1816', 'genus', None))
        # A spelling-variant alternative in another genus (a plant) still needs confirmation.
        cajanus = next(choice for choice in names.col_choices(calanus) if choice['usage']['id'] == '9CK8F')
        self.assertEqual(cajanus['replaces']['text'], 'replaces your name with Cajanus')
        with self.assertRaises(names.NameDecisionError):
            names.build_decision(calanus, {'decision': 'alternative', 'usage_id': '9CK8F'}, {'col_release': RELEASE})
        for label, usage_id, name in (('Trientalis europaea', '7CSCQ', 'Trientalis europaea'), ('Oncaea', '7PB88', 'Oncaea'),
                                      ('Mnium cuspidatum', '9LWSN', 'Mnium cuspidatum'), ('Chaetognatha', None, 'Chaetognatha')):
            with self.subTest(label):
                choices = [choice for choice in names.col_choices(real_record(label)) if choice['same_name'] and not choice['replaces']]
                self.assertTrue(choices)
                self.assertEqual(choices[0]['usage']['scientificName'], name)
                if usage_id:
                    self.assertEqual(choices[0]['usage']['id'], usage_id)

    def test_the_same_name_is_never_a_replacement_whatever_the_ranks_say(self):
        for label, name, rank in (('Hieracium sp', 'Hieracium', 'genus'), ('Larus sp.', 'Larus', 'genus'),
                                  ('Columba livia var. domestica', 'Columba livia var. domestica', 'variety'),
                                  ('Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti', 'Betula pubescens subsp. czerepanovii', 'subspecies')):
            with self.subTest(label):
                record = real_record(label)
                entry = names._entry(record, {})
                self.assertEqual((entry['col_choices'][0]['same_name'], entry['col_choices'][0]['replaces'], entry['suggested']), (True, None, None))
                decided = names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})
                self.assertEqual((decided['scientificName'], decided['taxonRank']), (name, rank))
        # Unqualified exact matches of the same name stay bulk-acceptable, rank marker and all.
        self.assertTrue(names.eligible(real_record('Columba livia var. domestica'), 'col', names.classify(real_record('Columba livia var. domestica'))) )
        self.assertTrue(names.eligible(real_record('Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti'), 'col', names.classify(real_record('Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti'))) )


class RealOverlayConsistencyTests(SimpleTestCase):
    """Name, rank and authorship are written together, identically on occurrence and identification rows (560, 570, 572)."""
    CONTENT = ('occurrenceID,scientificName,kingdom,taxonRank,scientificNameAuthorship\n'
               'o1,Larus sp.,Animalia,Species,\n'
               'o2,Larus sp.,Animalia,Species,\n'
               'o3,Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti,Plantae,species,\n'
               'o4,Larus canus,Animalia,species,Linnaeus 1758\n'
               'o5,Calanus,Animalia,,Leach\n'
               'o6,Bolephtyphantes index,Animalia,species,Thorell 1856\n').encode()
    LARUS_CANUS = {'label': 'Larus canus', 'qualifier': None, 'source_rank': 'species', 'parsed': real_parse('Larus canus', 'species'),
                   'match': {'matchType': 'EXACT', 'status': 'exact', 'hintOnly': False, 'alternatives': [], 'acceptedUsage': None,
                             'usage': {'id': '3SBPZ', 'scientificName': 'Larus canus', 'scientificNameAuthorship': 'Linnaeus, 1758',
                                       'taxonRank': 'species', 'status': 'accepted'}}}
    BOLEPHTYPHANTES = {'label': 'Bolephtyphantes index', 'qualifier': None, 'parsed': real_parse('Bolephtyphantes index', 'species'), 'match': {}}

    def converted(self, specs):
        archive = read_inputs([('occurrence.csv', self.CONTENT)])
        plan = build_plan(archive)
        frames, report = convert(archive, plan, decisions_for(plan))
        state = names.collect_state(archive, {'id': 'plan'})
        records = {'Larus canus': self.LARUS_CANUS, 'Bolephtyphantes index': self.BOLEPHTYPHANTES}
        records.update({label: real_record(label) for label in ('Larus sp.', 'Calanus',
                                                                 'Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti')})
        for record in state['labels']:
            record.update(records[record['label']])
        state['decisions'] = {label: names.build_decision(records[label], spec, {'col_release': RELEASE}) for label, spec in specs.items()}
        result, section = names.apply_name_decisions(frames, state, names.row_source_names(archive, report['row_crosswalk'], frames))
        occurrence = result['occurrence'].set_index('occurrence_pk')
        pairs = {}
        for _, row in result['identification'].iterrows():
            source = occurrence.loc[row['occurrence_fk']]
            pairs[source['occurrenceID']] = tuple((source[field], row[field]) for field in ('scientificName', 'taxonRank', 'scientificNameAuthorship'))
        return pairs, {entry['label']: entry for entry in section['entries']}, section

    def test_a_decided_name_carries_its_own_rank_and_authorship_to_occurrence_and_identification(self):
        pairs, entries, section = self.converted({
            'Larus sp.': {'decision': 'col'},
            'Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti': {'decision': 'col'},
            'Larus canus': {'decision': 'col'},
            'Bolephtyphantes index': {'decision': 'parsed'}})
        expected = {
            # 570: "Larus sp." accepted as the genus no longer keeps the supplied taxonRank "Species".
            'o1': ('Larus', 'genus', 'Linnaeus, 1758'), 'o2': ('Larus', 'genus', 'Linnaeus, 1758'),
            # 560: the subspecies keeps its marker and its rank on both rows.
            'o3': ('Betula pubescens subsp. czerepanovii', 'subspecies', '(N.I.Orlova) Hämet-Ahti'),
            # 572: "use COL name" writes COL's authorship on both rows, not only where the cell was blank.
            'o4': ('Larus canus', 'species', 'Linnaeus, 1758'),
            # A parsed split keeps the user's own authorship on both rows.
            'o6': ('Bolephtyphantes index', 'species', 'Thorell 1856'),
        }
        for occurrence_id, values in expected.items():
            with self.subTest(occurrence_id):
                self.assertEqual(pairs[occurrence_id], tuple((value, value) for value in values))
        self.assertEqual(entries['Larus sp.']['ranksReplaced'], {'Species': 2})  # the occurrence rows' supplied rank
        self.assertEqual(entries['Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti']['ranksReplaced'], {'species': 1})
        self.assertEqual((entries['Larus canus']['authorshipReplaced'], entries['Larus canus']['ranksReplaced']), (2, {}))
        self.assertEqual(entries['Bolephtyphantes index']['authorshipKept'], 0)
        self.assertEqual(section['ranks_replaced'], 3)

    def test_a_confirmed_coarser_name_drops_the_authorship_of_the_replaced_name(self):
        pairs, entries, _ = self.converted({'Calanus': {'decision': 'col', 'confirm_coarser': True}})
        self.assertEqual(pairs['o5'], (('Arthropoda', 'Arthropoda'), ('phylum', 'phylum'), ('', '')))
        self.assertEqual((entries['Calanus']['replaces'], entries['Calanus']['authorshipReplaced']), ('replaces your genus with a phylum', 2))

    def test_an_exact_same_name_alternative_keeps_the_assertion(self):
        pairs, entries, _ = self.converted({'Calanus': {'decision': 'alternative', 'usage_id': '7NRJ6'}})
        self.assertEqual(pairs['o5'], (('Calanus', 'Calanus'), ('genus', 'genus'), ('Leach, 1816', 'Leach, 1816')))
        self.assertIsNone(entries['Calanus']['replaces'])

    def test_an_earlier_unconfirmed_coarser_decision_keeps_the_users_name(self):
        """566 before this check: "Use COL name" on Calanus saved Arthropoda. The fallback had settled to an empty
        scientificName, so skipping the decision would publish no name; it is applied as "keep" instead."""
        frame = pd.DataFrame([{'occurrence_pk': 'o1', 'scientificName': '', 'verbatimIdentification': 'Calanus',
                               'taxonRank': '', 'scientificNameAuthorship': ''}])
        state = {'plan_id': 'plan', 'status': 'complete', 'labels': [real_record('Calanus')], 'decisions': {'Calanus': OLD_CALANUS}}
        result, section = apply({'occurrence': frame}, state)
        self.assertEqual(result['occurrence']['scientificName'].tolist(), ['Calanus'])
        entry = section['entries'][0]
        self.assertEqual((section['unconfirmed_kept'], entry['rows'], entry['appliedAs'], entry['replaces']),
                         (1, {'occurrence': 1}, 'keep', 'replaces your genus with a phylum'))
        self.assertTrue(entry['notApplied'])
        self.assertTrue(names._entry(state['labels'][0], state['decisions'], names.unconfirmed(state))['decision_unconfirmed'])
        # The same decision, confirmed, is applied.
        result, section = apply({'occurrence': frame}, {**state, 'decisions': {'Calanus': {**OLD_CALANUS, 'confirmedCoarser': True}}})
        self.assertEqual((result['occurrence']['scientificName'].tolist(), section['unconfirmed_kept']), (['Arthropoda'], 0))


# The decision saved for "Calanus" in conversion 566, before a coarser COL name needed confirmation.
OLD_CALANUS = decision('col', 'Arthropoda', None, 'phylum', source='col', usageId='RT', matchType='HIGHERRANK')


class HeldDecisionTests(NamesCase):
    """An unconfirmed coarser decision counts as undecided: it never settles the fallback and stays in the pending view."""

    def test_a_held_decision_does_not_settle_the_fallback_and_is_listed_as_pending(self):
        self.inspected()
        self.run_names(match=coarse_match)
        self.post('names', name_decisions={'Cus dus (Smith) Jones 1900': {'decision': 'keep'}, 'Eus sp.': {'decision': 'keep'}})
        old = decision('col', 'Aus', 'L.', 'genus', source='col', usageId='COL-AUS-GENUS', matchType='HIGHERRANK')
        state = self.conversion.name_review
        state['decisions']['Aus bus L.'] = old
        DwcConversion.objects.filter(pk=self.conversion.pk).update(name_review=state)
        conversion = self.conversion
        self.assertFalse(names.every_name_decided(conversion))
        self.assertEqual(names.settle_name_questions(conversion), [])
        self.assertNotIn('column:0:1', conversion.decisions)
        review = self.state('?names_view=pending')['name_review']
        self.assertEqual((review['summary']['decided'], review['summary']['unconfirmed']), (2, 1))
        self.assertEqual([(item['label'], item['decision_unconfirmed']) for item in review['labels']], [('Aus bus L.', True)])
        # Converting with the fallback set to an empty scientificName still publishes the user's own name for it.
        self.assertEqual(self.post('convert', decisions={**decisions_for(conversion.plan), 'column:0:1': 'preserve'}).status_code, 202)
        process_next_conversion()
        self.assertEqual(self.conversion.status, 'complete', self.conversion.error)
        occurrence = Table.objects.get(dataset_id=self.dataset_id, title='occurrence').df.set_index('occurrenceID')
        self.assertEqual(occurrence.loc[['a', 'b'], 'scientificName'].tolist(), ['Aus bus L.', 'Aus bus L.'])
        self.assertEqual(self.conversion.report['name_review']['unconfirmed_kept'], 1)

    def test_stale_bulk_authorship_is_held_and_explained_as_authorship(self):
        self.reviewed()
        state = self.conversion.name_review
        held = decision('col', 'Aus bus', 'Jones', 'species', source='col', usageId='COL-AUS', matchType='EXACT', by='bulk:auto')
        state['decisions']['Aus bus L.'] = held
        DwcConversion.objects.filter(pk=self.conversion.pk).update(name_review=state)
        self.assertEqual(names.unconfirmed(self.conversion.name_review)['Aus bus L.'],
                         {'kind': 'authorship', 'confirm': True, 'text': 'has a different authorship from yours'})
        entry = next(item for item in self.state()['name_review']['labels'] if item['label'] == 'Aus bus L.')
        self.assertEqual(entry['decision_held'], {'kind': 'authorship', 'text': 'has a different authorship from yours'})
        result, section = names.apply_name_decisions(
            frames(), {**state, 'labels': state['labels'], 'decisions': state['decisions']},
            {'occurrence': ['Aus bus L.', 'Aus bus L.', 'Eus sp.', 'Unreviewed'], 'identification': ['', '']})
        report_entry = next(item for item in section['entries'] if item['label'] == 'Aus bus L.')
        self.assertIn('earlier bulk choice would have replaced your authorship', report_entry['notApplied'])
        self.assertIsNone(report_entry['replaces'])
        self.assertEqual(result['occurrence']['scientificName'].tolist()[:2], ['Aus bus L.', 'Aus bus L.'])


class CarryDecisionTests(SimpleTestCase):
    def conversion(self, decisions, source='sha-1', labels=('Calanus', 'Aus bus')):
        return SimpleNamespace(plan={'id': 'old', 'source_sha256': source},
                               name_review={'plan_id': 'old', 'labels': [{'label': label} for label in labels], 'decisions': decisions})

    def fresh(self, labels=('Calanus', 'Aus bus')):
        return {'plan_id': 'new', 'labels': [{'label': label} for label in labels], 'decisions': {}}

    def test_decisions_carry_only_for_the_same_source_or_the_same_labels(self):
        keep = {'Aus bus': decision('keep', None, source='verbatim')}
        self.assertEqual(list(names.carry_decisions(self.conversion(keep), self.fresh(), {'source_sha256': 'sha-1'})['decisions']), ['Aus bus'])
        # Another source with the same labels still carries; another source with other labels does not.
        self.assertEqual(list(names.carry_decisions(self.conversion(keep), self.fresh(), {'source_sha256': 'sha-2'})['decisions']), ['Aus bus'])
        self.assertEqual(names.carry_decisions(self.conversion(keep), self.fresh(('Aus bus', 'Cus dus')), {'source_sha256': 'sha-2'})['decisions'], {})
        # The same source keeps the labels that are still there.
        carried = names.carry_decisions(self.conversion({**keep, 'Gone': decision('keep', None)}, labels=('Calanus', 'Aus bus', 'Gone')),
                                        self.fresh(), {'source_sha256': 'sha-1'})
        self.assertEqual(list(carried['decisions']), ['Aus bus'])

    def test_carried_col_decisions_are_checked_again_under_the_current_rules(self):
        unconfirmed = names.carry_decisions(self.conversion({'Calanus': OLD_CALANUS}), self.fresh(), {'source_sha256': 'sha-1'})
        unconfirmed['labels'] = [real_record('Calanus'), {'label': 'Aus bus'}]
        self.assertEqual(list(names.unconfirmed(unconfirmed)), ['Calanus'])
        confirmed = names.build_decision(real_record('Calanus'), {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})
        carried = names.carry_decisions(self.conversion({'Calanus': confirmed}), self.fresh(), {'source_sha256': 'sha-1'})
        self.assertNotIn('changeKind', carried['decisions']['Calanus'])
        carried['labels'] = [real_record('Calanus'), {'label': 'Aus bus'}]
        self.assertEqual(names.unconfirmed(carried), {})  # the user's explicit confirmation stands
        # A carried COL decision is checked against fresh matches, so the names are checked even with checks off.
        self.assertTrue(carried['requested'])
        self.assertEqual(carried['carried'], {'decisions': 1, 'bulk_not_carried': 0, 'dropped': 0})

    def test_with_another_source_a_decision_carries_only_where_the_names_context_is_unchanged(self):
        keep = {'Aus bus': decision('keep', None, source='verbatim'), 'Calanus': decision('keep', None, source='verbatim')}
        previous = self.conversion(keep)
        previous.name_review['labels'] = [{'label': 'Calanus', 'hints': {'kingdom': 'Animalia'}}, {'label': 'Aus bus', 'source_rank': 'species'}]
        fresh = self.fresh()
        fresh['labels'] = [{'label': 'Calanus', 'hints': {'kingdom': 'Plantae'}}, {'label': 'Aus bus', 'source_rank': 'species'}]
        self.assertEqual(list(names.carry_decisions(previous, fresh, {'source_sha256': 'sha-2'})['decisions']), ['Aus bus'])

    def test_source_qualifier_change_drops_a_carried_decision(self):
        previous = self.conversion({'Aus bus': decision('keep', None, source='verbatim')})
        previous.name_review['labels'] = [{'label': 'Calanus'}, {'label': 'Aus bus', 'source_qualifiers': [],
                                                                    'source_ids': {}, 'mixed_hints': []}]
        fresh = self.fresh()
        fresh['labels'] = [{'label': 'Calanus'}, {'label': 'Aus bus', 'source_qualifiers': ['cf.'],
                                                  'source_ids': {}, 'mixed_hints': []}]
        carried = names.carry_decisions(previous, fresh, {'source_sha256': 'sha-2'})
        self.assertEqual(carried['decisions'], {})
        self.assertEqual(carried['carried']['dropped'], 1)

    def test_a_newly_overlong_authorship_drops_a_carried_decision(self):
        previous = self.conversion({'Aus bus': decision('col', 'Aus bus')})
        previous.name_review['labels'] = [{'label': 'Calanus'}, {'label': 'Aus bus', 'source_authorships': [], 'authorships_truncated': False}]
        fresh = self.fresh()
        fresh['labels'] = [{'label': 'Calanus'}, {'label': 'Aus bus', 'source_authorships': [], 'authorships_truncated': True}]
        carried = names.carry_decisions(previous, fresh, {'source_sha256': 'sha-2'})
        self.assertEqual((carried['decisions'], carried['carried']['dropped']), ({}, 1))

    def test_auto_decisions_are_not_carried_but_declines_and_dropped_users_are_counted(self):
        user = decision('keep', None, source='verbatim')
        automatic = decision('col', 'Aus bus', source='col', by='auto:exact')
        bulk = decision('keep', None, by='bulk:unconfirmed')
        previous = self.conversion({'Aus bus': user, 'Calanus': automatic, 'Gone': bulk}, labels=('Calanus', 'Aus bus', 'Gone'))
        previous.name_review['auto_declined'] = ['Aus bus', 'Calanus']
        fresh = self.fresh(('Aus bus', 'Changed'))
        carried = names.carry_decisions(previous, fresh, {'source_sha256': 'sha-2'})
        self.assertEqual(carried['decisions'], {})
        self.assertEqual(carried['auto_declined'], [])
        self.assertEqual(carried['carried'], {'decisions': 0, 'bulk_not_carried': 1, 'dropped': 1})

        declined = self.conversion({}, labels=('Calanus', 'Aus bus'))
        declined.name_review['auto_declined'] = ['Aus bus', 'Gone']
        carried = names.carry_decisions(declined, self.fresh(), {'source_sha256': 'sha-1'})
        self.assertEqual(carried['auto_declined'], ['Aus bus'])
        self.assertEqual(carried['carried'], {'decisions': 0, 'bulk_not_carried': 0, 'dropped': 0})

    def test_decisions_checked_under_other_name_rules_are_checked_again(self):
        record = SpellingCorrectionTests().record('Circium heterophyllum', 'Cirsium heterophyllum', hints={'kingdom': 'Plantae'},
                                                  classification={'kingdom': 'Plantae'})
        coarse = names.build_decision(real_record('Calanus'), {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})
        self.assertEqual((coarse['changeKind'], coarse['nameRules']), ('coarser', taxon_matching.NAME_RULES_VERSION))
        # A stamp from other rules that claims "no change" does not hide the replacement.
        stale = {**coarse, 'changeKind': None, 'nameRules': taxon_matching.NAME_RULES_VERSION - 1}
        stale.pop('confirmedCoarser')
        state = {'labels': [real_record('Calanus'), record], 'decisions': {'Calanus': stale}}
        self.assertEqual(list(names.unconfirmed(state)), ['Calanus'])
        current = {**stale, 'nameRules': taxon_matching.NAME_RULES_VERSION}
        self.assertEqual(names.unconfirmed({**state, 'decisions': {'Calanus': current}}), {})


class NamePartTests(SimpleTestCase):
    """Infrageneric names, subgenera and formae speciales keep the parts that make them a different name."""

    def test_infrageneric_epithets_and_terminal_subgenera_are_parts(self):
        cases = {
            'Taraxacum sect. Ruderalia': ['taraxacum', 'sect.ruderalia'],
            'Hieracium subg. Pilosella': ['hieracium', 'subg.pilosella'],
            'Calanus (Calanus)': ['calanus', 'subg.calanus'],
            'Acartia (Acartiura) longiremis (Lilljeborg, 1853)': ['acartia', 'longiremis'],
            'Puccinia graminis f. sp. tritici': ['puccinia', 'graminis', 'tritici'],
            'Puccinia graminis f.sp. tritici Anon.ined.': ['puccinia', 'graminis', 'tritici'],
        }
        for name, parts in cases.items():
            with self.subTest(name):
                self.assertEqual(taxon_matching.name_parts(name), parts)

    def test_a_genus_match_for_an_infrageneric_label_is_coarser_and_a_subgenus_is_not_the_genus(self):
        genus = {'scientificName': 'Taraxacum', 'taxonRank': 'genus'}
        # Live v2: "Taraxacum sect. Ruderalia" -> HIGHERRANK Taraxacum; "Hieracium subg. Pilosella" -> EXACT genus Pilosella.
        for label, usage, match_type in (('Taraxacum sect. Ruderalia', genus, 'HIGHERRANK'),
                                         ('Hieracium subg. Pilosella', {'scientificName': 'Pilosella', 'taxonRank': 'genus'}, 'EXACT'),
                                         ('Calanus (Calanus)', {'scientificName': 'Calanus', 'taxonRank': 'genus'}, 'EXACT'),
                                         ('Puccinia graminis f. sp. tritici', {'scientificName': 'Puccinia graminis', 'taxonRank': 'species'},
                                          'HIGHERRANK')):
            with self.subTest(label):
                record = {'label': label, 'parsed': {}, 'match': {'matchType': match_type, 'usage': usage}}
                self.assertEqual(names.replacement(record, usage, match_type)['kind'], 'coarser')
                self.assertFalse(names.eligible(record, 'col', names.classify(record)))
        calanus = {'label': 'Calanus', 'parsed': real_parse('Calanus'), 'match': {}}
        self.assertFalse(names.same_name(calanus, {'scientificName': 'Calanus (Carinocalanus)', 'taxonRank': 'subgenus'}))


class HomonymRankTests(SimpleTestCase):
    def test_a_same_name_usage_at_another_rank_says_so(self):
        # 568: "Anura indet." with taxonRank order; COL has the order Anura and genus homonyms.
        usage = lambda key, rank, authorship='': {'id': key, 'scientificName': 'Anura', 'scientificNameAuthorship': authorship,  # noqa: E731
                                                  'taxonRank': rank, 'matchType': 'EXACT'}
        record = {'label': 'Anura indet.', 'qualifier': 'indet.', 'source_rank': 'order', 'parsed': real_parse(),
                  'match': {'matchType': 'EXACT', 'usage': usage('ORD', 'order'),
                            'alternatives': [usage('G1', 'genus', 'Agassiz, 1846'), usage('G2', 'genus', 'Nicolet, 1847')]}}
        notes = {choice['usage']['id']: choice['rank_note'] for choice in names.col_choices(record)}
        self.assertEqual(notes, {'ORD': None, 'G1': 'a genus; your name is an order', 'G2': 'a genus; your name is an order'})
        # Without a supplied rank, each homonym says that COL has the name at more than one rank.
        notes = {choice['usage']['id']: choice['rank_note'] for choice in names.col_choices({**record, 'source_rank': None})}
        self.assertEqual(notes, {'ORD': 'an order; COL has this name at more than one rank',
                                 'G1': 'a genus; COL has this name at more than one rank',
                                 'G2': 'a genus; COL has this name at more than one rank'})


class SupplyAuthorshipTests(SimpleTestCase):
    def record(self, authorships, label='Larus canus'):
        match = {'matchType': 'EXACT', 'usage': {'id': '3SBPZ', 'scientificName': 'Larus canus', 'scientificNameAuthorship': 'Linnaeus, 1758',
                                                 'taxonRank': 'species'}}
        return {'label': label, 'parsed': real_parse('Larus canus', 'species'), 'qualifier': None, 'match': match,
                'source_authorships': authorships}

    def test_bulk_skips_an_exact_match_whose_authorship_differs_from_the_supplied_one(self):
        # 572: the source column said "Linnaeus 1758"; punctuation, spacing, parentheses and a missing year are not differences.
        for supplied in ([], ['Linnaeus 1758'], ['(Linnaeus, 1758)'], ['Linnaeus'], ['L.'], ['L., 1758']):
            with self.subTest(supplied):
                self.assertTrue(names.eligible(self.record(supplied), 'col', names.classify(self.record(supplied))))
        for supplied in (['Smith'], ['Linnaeus, 1766'], ['Linnaeus 1758', 'Smith']):
            with self.subTest(supplied):
                self.assertFalse(names.eligible(self.record(supplied), 'col', names.classify(self.record(supplied))))
        # The label's own authorship counts too.
        labelled = {**self.record([]), 'label': 'Larus canus Smith', 'parsed': real_parse('Larus canus', 'species', 'Smith')}
        self.assertFalse(names.eligible(labelled, 'col', names.classify(labelled)))

    def test_collect_records_the_distinct_supplied_authorships(self):
        state = names.collect_state(read_inputs([('occurrence.csv', b'occurrenceID,scientificName,scientificNameAuthorship\n'
                                                                    b'a,Larus canus,Linnaeus 1758\nb,Larus canus,Smith\nc,Larus canus,\n')]),
                                    {'id': 'plan'})
        self.assertEqual(state['labels'][0]['source_authorships'], ['Linnaeus 1758', 'Smith'])


class SpellingCorrectionTests(SimpleTestCase):
    """A VARIANT match that only corrects spelling in the same place is offered in bulk; any other change needs confirming."""

    def record(self, label, name, rank='species', hints=None, classification=None, match_type='VARIANT', authorship=''):
        usage = {'id': 'U', 'scientificName': name, 'scientificNameAuthorship': authorship, 'taxonRank': rank,
                 'classification': classification or {}}
        parsed = real_parse(label, rank if len(label.split()) > 1 else None)
        return {'label': label, 'parsed': parsed, 'qualifier': None, 'hints': hints or {}, 'match': {'matchType': match_type, 'usage': usage}}

    def test_spelling_corrections_qualify(self):
        plants = {'kingdom': 'Plantae'}
        for label, name, hints, classification in (
                ('Circium heterophyllum', 'Cirsium heterophyllum', plants, {'kingdom': 'Plantae', 'family': 'Asteraceae'}),  # 569
                ('Albizzia ferruginea', 'Albizia ferruginea', plants, {'kingdom': 'Plantae'}),  # 558, with the right kingdom
                ('Trema orientalis', 'Trema orientale', plants, {'kingdom': 'Plantae'}),  # 558: a gender ending
                ('Bolephtyphantes index', 'Bolephthyphantes index', {'kingdom': 'Animalia', 'class': 'Arachnida'},
                 {'kingdom': 'Animalia', 'class': 'Arachnida'})):
            with self.subTest(label):
                record = self.record(label, name, hints=hints, classification=classification)
                found = names.change(record, record['match']['usage'], 'VARIANT')
                self.assertEqual((found['kind'], found['confirm']), ('spelling', False))
                self.assertTrue(names.eligible(record, 'col', names.classify(record)))
                self.assertTrue(names.eligible(record, 'col', names.classify(record)))  # spelling-group COL choice
                decided = names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE}, by='bulk:spelling', group_kind='spelling')
                self.assertEqual((decided['scientificName'], decided['corrects']), (name, f'corrects the spelling to {name}'))

    def test_other_changes_need_confirmation(self):
        animals = {'kingdom': 'Animalia'}
        cases = (
            # A plant genus one letter from a copepod genus, with no class or family to place it.
            (self.record('Calanus', 'Cajanus', rank='genus', hints=animals, classification={'kingdom': 'Plantae'}), 'genus'),
            (self.record('Calanus', 'Cajanus', rank='genus', hints=animals, classification={'kingdom': 'Animalia'}), 'genus'),
            # Another epithet in the same genus is another species.
            (self.record('Parus major', 'Parus minor', hints={'kingdom': 'Animalia'}, classification={'kingdom': 'Animalia'},
                         match_type='FUZZY'), 'epithet'),
            # A close spelling without the source's kingdom to agree with (558 tagged its plants Animalia).
            (self.record('Albizzia ferruginea', 'Albizia ferruginea', hints=animals, classification={'kingdom': 'Plantae'}), 'genus'),
            (self.record('Albizzia ferruginea', 'Albizia ferruginea', classification={'kingdom': 'Plantae'}), 'genus'),
        )
        for record, kind in cases:
            with self.subTest(record['label'], kind=kind):
                found = names.change(record, record['match']['usage'], record['match']['matchType'])
                self.assertEqual((found['kind'], found['confirm']), (kind, True))
                self.assertFalse(names.eligible(record, 'col', names.classify(record)))
                with self.assertRaisesRegex(names.NameDecisionError, 'Confirm'):
                    names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})

    def test_bulk_spelling_action_lists_from_and_to(self):
        conversion = SimpleNamespace(plan={'id': 'plan'}, name_review={
            'plan_id': 'plan', 'decisions': {},
            'labels': [self.record('Circium heterophyllum', 'Cirsium heterophyllum', hints={'kingdom': 'Plantae'},
                                   classification={'kingdom': 'Plantae'}),
                       self.record('Parus major', 'Parus minor', hints={'kingdom': 'Animalia'}, classification={'kingdom': 'Animalia'})]},
            save=lambda **kwargs: None)
        self.assertEqual(names.bulk_decide(conversion, 'spelling', 'col')['count'], 1)
        self.assertEqual(list(conversion.name_review['decisions']), ['Circium heterophyllum'])
        self.assertEqual(conversion.name_review['decisions']['Circium heterophyllum']['by'], 'bulk:spelling')
        # The accepted correction is applied, not held as an unconfirmed change (it was checked with its classification).
        state = conversion.name_review
        self.assertEqual(names.unconfirmed(state), {})
        self.assertEqual(self.corrected(state), (['Cirsium heterophyllum'], 0))

    def corrected(self, state):
        frame = pd.DataFrame([{'occurrence_pk': 'o1', 'scientificName': '', 'verbatimIdentification': 'Circium heterophyllum'}])
        result, section = apply({'occurrence': frame}, state)
        return result['occurrence']['scientificName'].tolist(), section['unconfirmed_kept']

    def test_a_single_spelling_accept_and_a_legacy_snapshot_are_applied(self):
        record = self.record('Circium heterophyllum', 'Cirsium heterophyllum', hints={'kingdom': 'Plantae'}, classification={'kingdom': 'Plantae'})
        single = names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})
        self.assertEqual(single['changeKind'], 'spelling')
        state = {'plan_id': 'plan', 'labels': [record], 'decisions': {'Circium heterophyllum': single}}
        self.assertEqual((names.unconfirmed(state), self.corrected(state)), ({}, (['Cirsium heterophyllum'], 0)))
        # A snapshot saved without the stamp is checked against the stored usage, classification included.
        legacy = {key: value for key, value in single.items() if key != 'changeKind'}
        state['decisions'] = {'Circium heterophyllum': legacy}
        self.assertEqual(names.unconfirmed(state), {})
        # A confirming request for it is a plain decision too and is never held.
        confirmed = names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})
        state['decisions'] = {'Circium heterophyllum': confirmed}
        self.assertEqual(names.unconfirmed(state), {})


def coarse_match(queries, deadline=None, **kwargs):
    """"Aus bus L." matched only to its genus, as GBIF does for a species missing from COL."""
    return [col_summary('HIGHERRANK', 'COL-AUS-GENUS', 'Aus', 'L.', rank='GENUS') if query['scientificName'] == 'Aus bus L.'
            else fake_match([query])[0] for query in queries]


class CoarserDecisionAPITests(NamesCase):
    def test_a_coarser_col_name_needs_confirm_coarser_and_keep_is_suggested(self):
        self.inspected()
        self.run_names(match=coarse_match)
        aus = self.state()['name_review']['labels'][0]
        self.assertEqual((aus['label'], aus['suggested'], aus['col_choices'][0]['replaces']['text']),
                         ('Aus bus L.', 'keep', 'replaces your species with a genus'))
        refused = self.post('names', name_decisions={'Aus bus L.': {'decision': 'col'}})
        self.assertEqual(refused.status_code, 400)
        self.assertIn('replaces your species with a genus', str(refused.data))
        self.assertNotIn('Aus bus L.', self.conversion.name_review['decisions'])
        self.post('names', bulk={'group': 'auto', 'decision': 'col'})
        self.assertNotIn('Aus bus L.', self.conversion.name_review['decisions'])
        confirmed = self.post('names', name_decisions={'Aus bus L.': {'decision': 'col', 'confirm_coarser': True}})
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        decided = self.conversion.name_review['decisions']['Aus bus L.']
        self.assertEqual((decided['scientificName'], decided['confirmedCoarser']), ('Aus', True))
