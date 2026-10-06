"""Scientific-name checks for archive conversion: parsing, COL matching, review decisions and the convert-time overlay.

All HTTP is mocked; nothing here reaches GBIF or ChecklistBank.
"""
import copy
import json
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


def fake_match(queries, deadline=None):
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
        col = names.build_decision(record, {'decision': 'col'}, self.state(), by='bulk:exact_col')
        self.assertEqual((col['usageId'], col['checklist'], col['by'], col['matchType']), ('COL-CUS', {'checklistKey': 'col-key', 'alias': 'COL26.6 XR'},
                                                                                         'bulk:exact_col', 'VARIANT'))
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
        # One batched parse for the plain labels and one batched match for all of them, with the shared kingdom as hint.
        self.assertEqual(mocks.parse.call_args.args[0], ['Aus bus L.', 'Cus dus (Smith) Jones 1900'])
        self.assertEqual(mocks.match.call_args.args[0][0], {'kingdom': 'Animalia', 'scientificName': 'Aus bus L.'})
        self.assertEqual(mocks.match.call_args.args[0][2], {'scientificName': 'Eus'})
        public = self.state()['name_review']
        self.assertEqual((public['summary']['checked'], public['checking']), (3, False))
        aus, cus, eus = public['labels']
        self.assertEqual((aus['match']['status'], aus['parsed']['canonical'], aus['bulk']), ('exact', 'Aus bus', {'col': True, 'parsed': True, 'spelling': False}))
        # A variant match and a parse that rewrites the supplied text are offered, but never in bulk.
        self.assertEqual((cus['match']['status'], cus['bulk'], cus['offers']), ('variant', {'col': False, 'parsed': False, 'spelling': False}, {'parsed': True, 'col': True}))
        self.assertEqual((eus['qualifier'], eus['parsed']['usable'], eus['parsed']['reason'], eus['bulk']['col']), ('sp.', False, 'qualifier', False))
        self.assertEqual(public['summary']['bulk_col'], 1)
        self.assertEqual(public['summary']['bulk_parsed'], 1)
        self.assertEqual(public['checklist'], 'COL26.6 XR')

    def test_unfinished_labels_continue_in_a_later_run(self):
        self.inspected()
        calls = []

        def limited(queries, deadline=None):
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
        self.assertEqual([query['scientificName'] for query in mocks.match.call_args.args[0]], ['Cus dus (Smith) Jones 1900', 'Eus'])
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
                         ({'offset': 0, 'limit': 2, 'total': 3, 'view': 'all'}, ['Aus bus L.', 'Cus dus (Smith) Jones 1900']))
        second = self.state('?names_limit=2&names_offset=2')['name_review']
        self.assertEqual([item['label'] for item in second['labels']], ['Eus sp.'])
        pending = self.state('?names_view=pending')['name_review']
        self.assertEqual((pending['page']['total'], pending['summary']['decided']), (2, 1))
        self.assertEqual([item['label'] for item in pending['labels']], ['Cus dus (Smith) Jones 1900', 'Eus sp.'])
        self.assertEqual(self.state('?names_limit=99999&names_offset=junk')['name_review']['page'],
                         {'offset': 0, 'limit': names.MAX_PAGE_SIZE, 'total': 3, 'view': 'all'})


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
        # Deciding every name settled the scientificName fallback, which now applies to no row; withdrawing
        # one decision later keeps that safe fallback (the text stays in verbatimIdentification).
        self.assertEqual(self.conversion.decisions, {'column:0:1': 'preserve'})
        self.assertEqual(self.state()['decision_sources']['column:0:1']['source'], 'system')

    def test_bulk_actions_decide_only_pending_labels_they_may(self):
        self.reviewed()
        self.post('names', name_decisions={'Cus dus (Smith) Jones 1900': {'decision': 'keep'}})
        response = self.post('names', bulk='exact_col')
        self.assertEqual(response.status_code, 200)
        decisions = self.conversion.name_review['decisions']
        # Only the exact, unqualified match is accepted; the qualified "Eus sp." and the variant match are not.
        self.assertEqual(sorted(decisions), ['Aus bus L.', 'Cus dus (Smith) Jones 1900'])
        self.assertEqual((decisions['Aus bus L.']['decision'], decisions['Aus bus L.']['by']), ('col', 'bulk:exact_col'))
        self.assertEqual(decisions['Cus dus (Smith) Jones 1900']['decision'], 'keep')  # an earlier decision is never replaced
        self.assertEqual(self.post('names', bulk='parsed').data['name_review']['summary']['decided'], 2)
        self.post('names', name_decisions={'Aus bus L.': None, 'Cus dus (Smith) Jones 1900': None})
        # Only a split that rebuilds the supplied text exactly is accepted in bulk.
        self.post('names', bulk='parsed')
        decisions = self.conversion.name_review['decisions']
        self.assertEqual(sorted(decisions), ['Aus bus L.'])
        self.assertEqual((decisions['Aus bus L.']['decision'], decisions['Aus bus L.']['scientificNameAuthorship']), ('parsed', 'L.'))

    def test_invalid_decisions_are_rejected_without_saving_anything(self):
        self.reviewed()
        for body in ({'name_decisions': {'Unknown name': {'decision': 'keep'}}},
                     {'name_decisions': {'Aus bus L.': {'decision': 'keep'}, 'Eus sp.': {'decision': 'parsed'}}},
                     {'name_decisions': {'Aus bus L.': {'decision': 'alternative', 'usage_id': 'nope'}}},
                     {'name_decisions': ['Aus bus L.']}, {'bulk': 'everything'}):
            response = self.post('names', **body)
            self.assertEqual(response.status_code, 400, body)
        self.assertEqual(self.conversion.name_review['decisions'], {})

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
        self.assertEqual((state['status'], list(state['decisions'])), ('complete', ['Eus sp.']))

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
        self.post('names', name_decisions={'Aus bus L.': {'decision': 'col'}})
        self.assertEqual(self.post('convert', decisions=decisions_for(self.conversion.plan)).status_code, 202)
        process_next_conversion()
        occurrence = Table.objects.get(dataset_id=self.dataset_id, title='occurrence').df.set_index('occurrenceID')
        self.assertEqual(occurrence.loc['c', 'scientificName'], 'Cus dus (Smith) Jones 1900')
        self.assertEqual(occurrence.loc['d', 'scientificName'], 'Eus sp.')
        self.assertEqual(self.conversion.report['name_review']['unreviewed'], 2)

    def test_a_new_inspection_discards_name_results_and_decisions(self):
        self.reviewed()
        self.post('names', name_decisions={'Aus bus L.': {'decision': 'keep'}})
        self.assertEqual(self.client.post(self.url, {'action': 'inspect'}, format='json').status_code, 202)
        process_next_conversion()
        state = self.conversion.name_review
        self.assertEqual((state['status'], state['decisions'], [('match' in record) for record in state['labels']]),
                         ('pending', {}, [False, False, False]))

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


def respond(payload, max_retries=None):
    return reply([answer(item_id, 'confirm', refs=['table:0']) for item_id in requested(payload)])


def answer_chat(conversion_id, job_id, claim):
    conversion_chat.acknowledge_unanswered(DwcConversion.objects.get(pk=conversion_id), 'Answered.')


def budget_after_first_chunk(on_first=None):
    calls = []

    def limited(queries, deadline=None):
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

        def match(queries, deadline=None):
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

        def match(queries, deadline=None):
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
        with patch('api.conversion_review.apply_decision_changes') as apply:
            self.assertEqual(settle_name_questions(partial), [])
            apply.assert_not_called()
            complete = self.conversion({'Aus bus': {'decision': 'parsed'}, 'Cus dus': {'decision': 'keep'}})
            self.assertEqual(settle_name_questions(complete), ['column:1:16'])
            self.assertEqual(apply.call_args.args[1:3], ({'column:1:16': 'preserve'}, 'system'))


# Real GBIF v2 (COL XR) responses, trimmed, for names that went wrong in production conversions 560-570.
REAL = json.loads((Path(__file__).parent / 'testdata' / 'col_v2_real_matches.json').read_text())


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
                self.assertFalse(names.bulk_acceptable(record, {}))
                with self.assertRaisesRegex(names.NameDecisionError, 'Confirm'):
                    names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})
                with self.assertRaisesRegex(names.NameDecisionError, 'never accepted in bulk'):
                    names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE}, by='bulk:exact_col')
                confirmed = names.build_decision(record, {'decision': 'col', 'confirm_coarser': True}, {'col_release': RELEASE})
                self.assertEqual((confirmed['scientificName'], confirmed['confirmedCoarser'], confirmed['replaces']), (col_name, True, text))

    def test_bulk_acceptance_skips_a_coarser_exact_match_even_without_a_qualifier(self):
        record = {**real_record('AmphibiaReptilia sp.'), 'label': 'AmphibiaReptilia', 'qualifier': None}
        self.assertEqual(record['match']['matchType'], 'EXACT')
        self.assertFalse(names.bulk_acceptable(record, {}))

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
        self.assertTrue(names.bulk_acceptable(real_record('Columba livia var. domestica'), {}))
        self.assertTrue(names.bulk_acceptable(real_record('Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti'), {}))


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
                self.assertFalse(names.bulk_acceptable(record, {}))
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
        for supplied in ([], ['Linnaeus 1758'], ['(Linnaeus, 1758)'], ['Linnaeus']):
            with self.subTest(supplied):
                self.assertTrue(names.bulk_acceptable(self.record(supplied), {}))
        for supplied in (['Smith'], ['Linnaeus, 1766'], ['Linnaeus 1758', 'Smith']):
            with self.subTest(supplied):
                self.assertFalse(names.bulk_acceptable(self.record(supplied), {}))
        # The label's own authorship counts too.
        labelled = {**self.record([]), 'label': 'Larus canus Smith', 'parsed': real_parse('Larus canus', 'species', 'Smith')}
        self.assertFalse(names.bulk_acceptable(labelled, {}))

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
                self.assertTrue(names.spelling_acceptable(record, {}))
                self.assertFalse(names.bulk_acceptable(record, {}))  # not an exact match
                decided = names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE}, by='bulk:spelling')
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
                self.assertFalse(names.spelling_acceptable(record, {}))
                with self.assertRaisesRegex(names.NameDecisionError, 'Confirm'):
                    names.build_decision(record, {'decision': 'col'}, {'col_release': RELEASE})

    def test_bulk_spelling_action_lists_from_and_to(self):
        conversion = SimpleNamespace(plan={'id': 'plan'}, name_review={
            'plan_id': 'plan', 'decisions': {},
            'labels': [self.record('Circium heterophyllum', 'Cirsium heterophyllum', hints={'kingdom': 'Plantae'},
                                   classification={'kingdom': 'Plantae'}),
                       self.record('Parus major', 'Parus minor', hints={'kingdom': 'Animalia'}, classification={'kingdom': 'Animalia'})]},
            save=lambda **kwargs: None)
        self.assertEqual(names.bulk_decide(conversion, 'spelling'), 1)
        self.assertEqual(list(conversion.name_review['decisions']), ['Circium heterophyllum'])
        self.assertEqual(conversion.name_review['decisions']['Circium heterophyllum']['by'], 'bulk:spelling')


def coarse_match(queries, deadline=None):
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
        self.assertEqual(self.conversion.name_review['decisions'], {})
        self.post('names', bulk='exact_col')
        self.assertNotIn('Aus bus L.', self.conversion.name_review['decisions'])
        confirmed = self.post('names', name_decisions={'Aus bus L.': {'decision': 'col', 'confirm_coarser': True}})
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        decided = self.conversion.name_review['decisions']['Aus bus L.']
        self.assertEqual((decided['scientificName'], decided['confirmedCoarser']), ('Aus', True))
