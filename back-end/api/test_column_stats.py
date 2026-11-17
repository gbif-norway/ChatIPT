import json
import threading
import time
from unittest import mock

import numpy as np
import pandas as pd
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from api import column_stats as cs
from api.models import CustomUser, Dataset, Table


def summary(values, name='col'):
    return cs.column_summary(pd.Series(values, dtype=object), name, 0)


class KindTests(SimpleTestCase):
    def test_number_kind_needs_every_value_to_be_a_finite_number(self):
        self.assertEqual(summary(['1', '2.5', '-3', '1e3', None])['kind'], 'number')
        self.assertEqual(summary([1.0, 2.0, np.nan])['kind'], 'number')
        self.assertEqual(summary(['1', '1,5'])['kind'], 'text')
        self.assertEqual(summary(['1', '5 cm'])['kind'], 'text')
        self.assertEqual(summary(['1', float('inf')])['kind'], 'text')
        self.assertEqual(summary(['1', 'NaN'])['kind'], 'text')

    def test_date_kind_is_strict_iso_8601(self):
        values = [
            '2004-05', '2004-05-03', '2005-02-28T10:00', '2005-02-28T10:00:00Z',
            '2005-02-28T10:00:00.5+02:00', '2006-01-01 00:00:00', '2004/2005', '2004-05-01/2004-05-09',
        ]
        self.assertEqual(summary(values)['kind'], 'date')
        self.assertEqual(summary([pd.Timestamp('2020-01-01'), pd.Timestamp('2020-03-01 10:00')])['kind'], 'date')
        self.assertEqual(summary(['2004-05', 'May'])['kind'], 'text')
        self.assertEqual(summary(['2004-05', '12'])['kind'], 'text')
        self.assertEqual(summary(['2004-02-30'])['kind'], 'text')
        self.assertEqual(summary(['2004-13'])['kind'], 'text')
        self.assertEqual(summary(['04/05/2004'])['kind'], 'text')
        self.assertEqual(summary(['2004-05-03T25:00'])['kind'], 'text')
        self.assertEqual(summary(['2004-02-29', '2000-02-29'])['kind'], 'date')
        self.assertEqual(summary(['1900-02-29'])['kind'], 'text')

    def test_years_alone_are_numbers(self):
        self.assertEqual(summary(['2004', '2005'])['kind'], 'number')

    def test_empty_kind_and_blank_counting(self):
        result = summary([None, np.nan, '', '   ', '\t\n'])
        self.assertEqual(result['kind'], 'empty')
        self.assertEqual(result['distinctness'], 'empty')
        self.assertEqual((result['populated'], result['blank'], result['unique']), (0, 5, 0))
        self.assertEqual(result['top_values'], [])
        self.assertIsNone(result['numeric'])
        self.assertIsNone(result['date'])

    def test_text_kind(self):
        result = summary(['Pinus', 'Picea', ' Pinus '])
        self.assertEqual(result['kind'], 'text')
        self.assertEqual(result['top_values'][0], {'value': 'Pinus', 'count': 2, 'truncated': False})


class DistinctnessTests(SimpleTestCase):
    def test_each_distinctness(self):
        self.assertEqual(summary([None])['distinctness'], 'empty')
        self.assertEqual(summary(['a', 'a ', None])['distinctness'], 'constant')
        self.assertEqual(summary(['a'])['distinctness'], 'constant')
        self.assertEqual(summary(['a', 'b', None])['distinctness'], 'all_distinct')
        self.assertEqual(summary(['a', 'b', 'b'])['distinctness'], 'repeats')

    def test_duplicated_values_and_rows(self):
        result = summary(['a', 'a', 'b', 'b', 'b', 'c', None])
        self.assertEqual(result['duplicated_values'], 2)
        self.assertEqual(result['duplicated_rows'], 5)


class TopValueTests(SimpleTestCase):
    def test_most_common_first_ties_by_value_and_at_most_ten(self):
        values = ['z'] * 5 + ['b'] * 3 + ['a'] * 3 + [f'v{i:02d}' for i in range(20)]
        top = summary(values)['top_values']
        self.assertEqual(len(top), 10)
        self.assertEqual([item['value'] for item in top[:5]], ['z', 'a', 'b', 'v00', 'v01'])
        self.assertEqual([item['count'] for item in top[:4]], [5, 3, 3, 1])

    def test_long_values_are_cut_and_marked(self):
        long_value = 'x' * 400
        top = summary([long_value, long_value, 'short'])['top_values']
        self.assertEqual(top[0]['value'], 'x' * 300)
        self.assertTrue(top[0]['truncated'])
        self.assertFalse(top[1]['truncated'])

    def test_partial_ordering_matches_full_ordering(self):
        rng = np.random.default_rng(1)
        words = [f'w{i}' for i in rng.integers(0, 60, 500)]
        counts = cs.value_counts(pd.Series(words))
        full = cs.by_count(counts)
        self.assertEqual(list(cs.by_count(counts, 10).items()), list(full.head(10).items()))
        distinct = cs.value_counts(pd.Series([f'id{i}' for i in rng.permutation(1000)]))
        self.assertEqual(list(cs.by_count(distinct, 10).index), sorted(distinct.index)[:10])


class NumericSummaryTests(SimpleTestCase):
    def test_integer_summary_uses_one_bin_per_integer(self):
        numeric = summary(['1', '2', '3', '4', '4'])['numeric']
        self.assertEqual((numeric['min'], numeric['max'], numeric['median']), (1, 4, 3))
        self.assertAlmostEqual(numeric['mean'], 2.8)
        self.assertEqual([b['count'] for b in numeric['histogram']], [1, 1, 1, 2])
        self.assertEqual(numeric['histogram'][0], {'start': 1, 'end': 2, 'count': 1})

    def test_even_count_median_and_bounded_bins(self):
        numeric = summary([float(i) / 3 for i in range(1000)])['numeric']
        self.assertLessEqual(len(numeric['histogram']), 12)
        self.assertEqual(sum(b['count'] for b in numeric['histogram']), 1000)
        self.assertAlmostEqual(numeric['median'], 499.5 / 3)

    def test_constant_number(self):
        numeric = summary([7, 7.0, '7'])['numeric']
        self.assertEqual(numeric['histogram'], [{'start': 7, 'end': 7, 'count': 3}])


class DateSummaryTests(SimpleTestCase):
    def test_years_bucketed_and_interval_bounds(self):
        date = summary(['2004-04-01', '2004-06', '2007/2011-09', '2006-02-01T10:00:00Z'])['date']
        self.assertEqual(date['min'], '2004-04-01')
        self.assertEqual(date['max'], '2011-09-30')
        self.assertEqual(date['histogram'], [
            {'label': '2004', 'count': 2}, {'label': '2005', 'count': 0},
            {'label': '2006', 'count': 1}, {'label': '2007', 'count': 1},
        ])

    def test_months_within_two_years(self):
        date = summary(['2020-01-15', '2020-03', '2020-03-02'])['date']
        self.assertEqual(date['histogram'], [
            {'label': '2020-01', 'count': 1}, {'label': '2020-02', 'count': 0}, {'label': '2020-03', 'count': 2},
        ])

    def test_long_spans_use_at_most_forty_buckets(self):
        date = summary([f'{year}-01-01' for year in range(1800, 2020)])['date']
        self.assertLessEqual(len(date['histogram']), 40)
        self.assertEqual(sum(b['count'] for b in date['histogram']), 220)
        self.assertEqual(date['histogram'][0]['label'], '1800–1805')


class VariantGroupTests(SimpleTestCase):
    def test_groups_by_case_whitespace_and_punctuation(self):
        result = summary(['male'] * 88 + ['Male'] * 2 + ['MALE.'] + ['fe  male'] * 3 + ['Fe male'] + ['female'])
        self.assertEqual(result['variant_group_count'], 2)
        first = result['variant_groups'][0]['values']
        self.assertEqual([(v['value'], v['count']) for v in first], [('male', 88), ('Male', 2), ('MALE.', 1)])
        second = result['variant_groups'][1]['values']
        self.assertEqual([v['value'] for v in second], ['fe  male', 'Fe male'])

    def test_groups_capped_at_twenty_with_untruncated_count(self):
        values = []
        for i in range(25):
            values += [f'name{i:02d}'] * (i + 2) + [f'NAME{i:02d}']
        result = summary(values)
        self.assertEqual(result['variant_group_count'], 25)
        self.assertEqual(len(result['variant_groups']), 20)
        self.assertEqual(result['variant_groups'][0]['values'][0]['value'], 'name24')

    def test_punctuation_only_values_do_not_group(self):
        self.assertEqual(summary(['-', '?', '-', '...'])['variant_group_count'], 0)

    def test_number_columns_and_distinct_date_columns_are_skipped(self):
        self.assertEqual(summary(['1.0', '10', '10'])['variant_groups'], [])
        self.assertEqual(summary(['2004-05-01T10:00', '2004-05-01 10:00'])['variant_groups'], [])

    def test_normalisation_is_unicode_aware(self):
        self.assertEqual(cs.variant_key(' Cafe\u0301,  AU LAIT! '), cs.variant_key('caf\u00e9 au lait'))


class ColumnValuesFunctionTests(SimpleTestCase):
    def test_search_sort_and_paging(self):
        series = pd.Series(['Apple'] * 3 + ['banana'] * 2 + ['apricot', 'cherry', None, ' '])
        page = cs.column_values(series, search='AP', offset=0, limit=1)
        self.assertEqual((page['populated'], page['blank'], page['unique'], page['count']), (7, 2, 4, 2))
        self.assertEqual(page['results'], [{'value': 'Apple', 'count': 3, 'truncated': False}])
        page = cs.column_values(series, offset=1, limit=2, sort='value')
        self.assertEqual([r['value'] for r in page['results']], ['apricot', 'banana'])


class TableExplorerApiTests(TestCase):
    def setUp(self):
        cache.clear()
        cs.row_positions_cache.clear()
        self.user = CustomUser.objects.create_user(username='owner@example.org', email='owner@example.org', password='pw')
        self.other = CustomUser.objects.create_user(username='other@example.org', email='other@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Explorer')
        df = pd.DataFrame(
            [
                ['female', 10.0, 'occ-1', 'Pinus sylvestris', '2004-05-01'],
                ['male', 2.0, 'occ-2', 'pinus  sylvestris', '2004-06'],
                ['Male', None, 'occ-2', 'Picea abies', None],
                [None, 1.0, 'occ-3', 'Pinus sylvestris.', '2005'],
                ['female', 1.0, 'occ-4', None, '2005-01-01'],
                ['  ', 30.5, 'occ-5', 'Picea abies', '2004-05-01'],
            ],
            columns=['sex', 'count', 'id', 'name', 'id'],
        )
        self.table = Table.objects.create(dataset=self.dataset, title='occurrence', df=df)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def stats(self, table=None):
        return self.client.get(reverse('table-column-stats', args=[(table or self.table).id]))

    def rows(self, **params):
        if 'filters' in params and not isinstance(params['filters'], str):
            params['filters'] = json.dumps(params['filters'])
        return self.client.get(reverse('table-rows', args=[self.table.id]), {'limit': 200, **params})

    def test_column_stats_shape_and_duplicate_labels(self):
        response = self.stats()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['table'], self.table.id)
        self.assertEqual(data['row_count'], 6)
        self.assertTrue(data['version'].endswith('Z'))
        self.assertEqual([c['name'] for c in data['columns']], ['sex', 'count', 'id (1)', 'name', 'id (2)'])
        self.assertEqual([c['position'] for c in data['columns']], [0, 1, 2, 3, 4])
        self.assertEqual([c['kind'] for c in data['columns']], ['text', 'number', 'text', 'text', 'date'])
        sex = data['columns'][0]
        self.assertEqual((sex['populated'], sex['blank'], sex['unique']), (4, 2, 3))
        self.assertEqual(set(sex), {
            'name', 'position', 'kind', 'populated', 'blank', 'unique', 'distinctness', 'duplicated_values',
            'duplicated_rows', 'top_values', 'numeric', 'date', 'variant_groups', 'variant_group_count',
        })
        self.assertEqual(data['columns'][2]['duplicated_rows'], 2)
        self.assertEqual(data['columns'][4]['date']['min'], '2004-05-01')

    def test_column_stats_are_cached_until_the_table_is_saved(self):
        with mock.patch('api.column_stats.column_stats', wraps=cs.column_stats) as compute:
            first = self.stats().json()
            with CaptureQueriesContext(connection) as queries:
                second = self.stats().json()
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(first, second)
            self.assertTrue(all('"api_table"."df"' not in q['sql'] for q in queries))

            table = Table.objects.get(pk=self.table.pk)
            table.df = pd.DataFrame({'only': ['x', 'y']})
            table.save()
            third = self.stats().json()
            self.assertEqual(compute.call_count, 2)
            self.assertEqual(third['row_count'], 2)
            self.assertNotEqual(third['version'], first['version'])

    def test_column_values_endpoint(self):
        url = reverse('table-column-values', args=[self.table.id])
        response = self.client.get(url, {'column': 'sex', 'search': 'MAL', 'sort': 'value'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'column': 'sex', 'populated': 4, 'blank': 2, 'unique': 3, 'count': 3, 'offset': 0, 'limit': 50,
            'results': [
                {'value': 'Male', 'count': 1, 'truncated': False},
                {'value': 'female', 'count': 2, 'truncated': False},
                {'value': 'male', 'count': 1, 'truncated': False},
            ],
        })
        page = self.client.get(url, {'column': 'sex', 'offset': 1, 'limit': 1}).json()
        self.assertEqual(page['results'], [{'value': 'Male', 'count': 1, 'truncated': False}])
        self.assertEqual(page['count'], 3)
        self.assertEqual(self.client.get(url, {'column': 'id (2)'}).json()['results'][0]['value'], '2004-05-01')

    def test_column_values_rejects_bad_input(self):
        url = reverse('table-column-values', args=[self.table.id])
        unknown = self.client.get(url, {'column': 'nope'})
        self.assertEqual(unknown.status_code, 400)
        self.assertIn('detail', unknown.json())
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.get(url, {'column': 'sex', 'limit': 201}).status_code, 400)
        self.assertEqual(self.client.get(url, {'column': 'sex', 'sort': 'random'}).status_code, 400)

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return [row['id (1)'] for row in response.json()['results']]

    def test_every_filter_op(self):
        def run(op, column, value=None):
            item = {'column': column, 'op': op}
            if value is not None:
                item['value'] = value
            return self.ids(self.rows(filters=[item]))

        self.assertEqual(run('is', 'sex', 'female'), ['occ-1', 'occ-4'])
        self.assertEqual(run('is', 'sex', ' female '), ['occ-1', 'occ-4'])
        self.assertEqual(run('is_not', 'sex', 'female'), ['occ-2', 'occ-2', 'occ-3', 'occ-5'])
        self.assertEqual(run('contains', 'sex', 'MAL'), ['occ-1', 'occ-2', 'occ-2', 'occ-4'])
        self.assertEqual(run('blank', 'sex'), ['occ-3', 'occ-5'])
        self.assertEqual(run('not_blank', 'sex'), ['occ-1', 'occ-2', 'occ-2', 'occ-4'])
        self.assertEqual(run('duplicated', 'id (1)'), ['occ-2', 'occ-2'])
        self.assertEqual(run('duplicated', 'id (2)'), ['occ-1', 'occ-5'])
        self.assertEqual(run('variant_of', 'name', 'PINUS sylvestris'), ['occ-1', 'occ-2', 'occ-3'])
        # Float cells match their cell text ("1.0") and what the grid shows ("1").
        self.assertEqual(run('is', 'count', '1.0'), ['occ-3', 'occ-4'])
        self.assertEqual(run('is', 'count', 1), ['occ-3', 'occ-4'])

    def test_filters_combine_with_each_other_and_search(self):
        response = self.rows(
            filters=[{'column': 'sex', 'op': 'not_blank'}, {'column': 'name', 'op': 'contains', 'value': 'pinus'}],
            search='occ-2',
        )
        self.assertEqual(self.ids(response), ['occ-2'])
        self.assertEqual(response.json()['count'], 1)
        self.assertEqual(response.json()['total'], 6)
        exact = self.rows(search='occ-2', column='id (1)', exact=True, filters=[{'column': 'sex', 'op': 'is', 'value': 'Male'}])
        self.assertEqual(exact.json()['count'], 1)

    def test_sort_is_numeric_aware_with_blanks_last(self):
        self.assertEqual(self.ids(self.rows(sort='count')), ['occ-3', 'occ-4', 'occ-2', 'occ-1', 'occ-5', 'occ-2'])
        self.assertEqual(self.ids(self.rows(sort='count', order='desc')), ['occ-5', 'occ-1', 'occ-2', 'occ-3', 'occ-4', 'occ-2'])
        self.assertEqual(self.ids(self.rows(sort='sex')), ['occ-1', 'occ-4', 'occ-2', 'occ-2', 'occ-3', 'occ-5'])
        self.assertEqual(self.ids(self.rows(sort='sex', order='desc')), ['occ-2', 'occ-2', 'occ-1', 'occ-4', 'occ-3', 'occ-5'])
        page = self.rows(sort='count', order='desc', offset=1, limit=2, filters=[{'column': 'sex', 'op': 'not_blank'}])
        self.assertEqual(self.ids(page), ['occ-2', 'occ-4'])
        self.assertEqual(page.json()['count'], 4)

    def test_rows_reject_bad_filters_and_sort(self):
        bad = [
            {'filters': 'not json'},
            {'filters': json.dumps({'column': 'sex'})},
            {'filters': json.dumps([{'column': 'sex', 'op': 'like', 'value': 'x'}])},
            {'filters': json.dumps([{'column': 'nope', 'op': 'blank'}])},
            {'filters': json.dumps([{'column': 'sex', 'op': 'is'}])},
            {'filters': json.dumps(['sex'])},
            {'sort': 'nope'},
        ]
        for params in bad:
            response = self.rows(**params)
            self.assertEqual(response.status_code, 400, params)
            self.assertIn('detail', response.json())
        self.assertEqual(self.rows(sort='sex', order='sideways').status_code, 400)

    def test_rows_without_new_params_are_unchanged(self):
        data = self.client.get(reverse('table-rows', args=[self.table.id]), {'offset': 1, 'limit': 2}).json()
        self.assertEqual((data['count'], data['total'], data['offset'], data['limit']), (6, 6, 1, 2))
        self.assertEqual([row['id (1)'] for row in data['results']], ['occ-2', 'occ-2'])

    def test_top_values_round_trip_as_is_filters(self):
        df = pd.DataFrame({
            'float': [1.0, 1.0, 2.5, np.nan, 1e20, 3.0],
            'int': [1, 1, 2, 3, 3, 3],
            'mixed': [1, '1', 1.0, ' 1 ', None, 'é '],
            'when': pd.to_datetime(['2020-01-01', '2020-01-01', '2020-01-02 10:30', None, '2021-05-05', '2020-01-01'], format='ISO8601'),
            'bool': [True, False, True, None, True, False],
            'text': ['ünïcödé', 'tab\tinside', 'ünïcödé', '', 'x' * 400, 'x' * 400],
        })
        table = Table.objects.create(dataset=self.dataset, title='types', df=df)
        stats = self.stats(table).json()
        checked = 0
        for column in stats['columns']:
            for top in column['top_values']:
                if top['truncated']:
                    continue
                response = self.client.get(reverse('table-rows', args=[table.id]), {
                    'filters': json.dumps([{'column': column['name'], 'op': 'is', 'value': top['value']}]),
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['count'], top['count'], (column['name'], top))
                checked += 1
        self.assertGreater(checked, 15)
        float_values = {t['value'] for t in stats['columns'][0]['top_values']}
        self.assertIn('1.0', float_values)
        mixed = {t['value']: t['count'] for t in stats['columns'][2]['top_values']}
        self.assertEqual(mixed['1'], 3)
        self.assertEqual(mixed['1.0'], 1)

    def test_other_users_cannot_read_explorer_endpoints(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.stats().status_code, 404)
        self.assertEqual(
            self.client.get(reverse('table-column-values', args=[self.table.id]), {'column': 'sex'}).status_code, 404,
        )
        self.assertEqual(self.rows(filters=[{'column': 'sex', 'op': 'blank'}]).status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.stats().status_code, (401, 403))


class ReviewFixFunctionTests(SimpleTestCase):
    def test_extreme_floats_summarise_without_overflow(self):
        result = summary([1e308, 1e308, -1e308])
        self.assertEqual(result['kind'], 'number')
        numeric = result['numeric']
        self.assertEqual((numeric['min'], numeric['max'], numeric['median']), (-1e308, 1e308, 1e308))
        self.assertAlmostEqual(numeric['mean'] / 1e308, 1 / 3)
        self.assertEqual(sum(b['count'] for b in numeric['histogram']), 3)
        json.dumps(result, allow_nan=False)
        self.assertEqual(summary([1.7e308, 1.7e308])['numeric']['median'], 1.7e308)

    def test_one_failing_column_degrades_to_a_plain_summary(self):
        df = pd.DataFrame({'a': [1, 2, 2], 'b': ['x', 'y', 'y']})
        with mock.patch('api.column_stats.numeric_summary', side_effect=ValueError('boom')), \
                self.assertLogs('api.column_stats', level='ERROR'):
            result = cs.column_stats(df, ['a', 'b'])
        a, b = result['columns']
        self.assertEqual((a['kind'], a['numeric'], a['unique'], a['populated']), ('text', None, 2, 3))
        self.assertEqual(a['top_values'][0], {'value': '2', 'count': 2, 'truncated': False})
        self.assertEqual(b['kind'], 'text')

    def test_deeply_nested_filters_are_rejected(self):
        with self.assertRaises(ValueError):
            cs.parse_filters('[' * 20000 + ']' * 20000, ['a'])

    def test_variant_group_members_are_capped_with_totals(self):
        values = ['ab'] * 50 + ['a' + '.' * i + 'b' for i in range(1, 12)]
        group = summary(values)['variant_groups'][0]
        self.assertEqual(len(group['values']), 10)
        self.assertEqual(group['value_count'], 12)
        self.assertEqual(group['count'], 61)
        self.assertEqual(group['values'][0], {'value': 'ab', 'count': 50, 'truncated': False})

    def test_numeric_sort_breaks_ties_on_text_and_compares_big_integers_exactly(self):
        df = pd.DataFrame({'n': ['1.0', '1', '2', None, '1']})
        self.assertEqual(list(cs.sort_positions(df, 0)), [1, 4, 0, 2, 3])
        self.assertEqual(list(cs.sort_positions(df, 0, 'desc')), [2, 0, 1, 4, 3])
        big = pd.DataFrame({'n': ['9007199254740993', '9007199254740992', '9007199254740994', '-3']})
        self.assertEqual(list(cs.sort_positions(big, 0)), [3, 1, 0, 2])
        self.assertEqual(list(cs.sort_positions(big, 0, 'desc')), [2, 0, 1, 3])

    def test_variant_of_punctuation_only_value_matches_exactly(self):
        df = pd.DataFrame({'a': ['?', '-', '?', '...', 'x']})
        mask = cs.filter_mask(df, cs.parse_filters(json.dumps([{'column': 'a', 'op': 'variant_of', 'value': '?'}]), ['a']))
        self.assertEqual(list(np.flatnonzero(mask)), [0, 2])

    def test_label_and_value_length_limits(self):
        long_label = 'x' * (cs.MAX_LABEL_CHARS + 1)
        with self.assertRaises(ValueError):
            cs.parse_filters(json.dumps([{'column': long_label, 'op': 'blank'}]), [long_label])
        with self.assertRaises(ValueError):
            cs.parse_filters(json.dumps([{'column': 'a', 'op': 'is', 'value': 'v' * 1001}]), ['a'])
        label = 'y' * cs.MAX_LABEL_CHARS
        self.assertEqual(len(cs.parse_filters(json.dumps([{'column': label, 'op': 'blank'}]), [label])), 1)

    def test_fast_variant_keys_match_the_reference_normalisation(self):
        samples = [
            'Male ', 'MALE.', 'fe  male', 'Fe_male', 'a\tb\nc', '\x1fx\x00y\x7f', 'Caf\u00e9', 'Cafe\u0301',
            '\uff2d\uff41\uff4c\uff45', 'Stra\u00dfe', 'STRASSE', '\u00a0x\u2003y\u3000', 'n\u00ba 5', '\u00bd',
            'O\u2019Brien', '\u0130stanbul', '\u03a3\u03c3\u03c2', '???', '', '\u2028line',
        ] + [''.join(chr(c) for c in range(start, start + 40)) for start in range(0, 0x3000, 40)]
        texts = pd.Series(samples, dtype='string')
        reference = (
            texts.str.normalize('NFKC').str.casefold().str.replace(r'[^\w\s]|_', '', regex=True)
            .str.replace(r'\s+', ' ', regex=True).str.strip().fillna('').to_numpy(dtype=object)
        )
        self.assertEqual(list(cs.variant_keys(samples)), list(reference))

    def test_positions_cache_is_bounded(self):
        cache_ = cs.PositionsCache(max_entries=2, max_bytes=100, ttl=60)
        cache_.set('a', np.arange(10, dtype=np.int32))
        cache_.set('b', np.arange(10, dtype=np.int32))
        cache_.set('c', np.arange(10, dtype=np.int32))
        self.assertIsNone(cache_.get('a'))
        self.assertIsNotNone(cache_.get('c'))
        cache_.set('big', np.arange(100, dtype=np.int32))
        self.assertIsNone(cache_.get('big'))
        expired = cs.PositionsCache(ttl=-1)
        expired.set('a', np.arange(3))
        self.assertIsNone(expired.get('a'))


class ReviewFixApiTests(TestCase):
    def setUp(self):
        cache.clear()
        cs.row_positions_cache.clear()
        self.user = CustomUser.objects.create_user(username='u@example.org', email='u@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Review')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def make(self, df, title='t'):
        return Table.objects.create(dataset=self.dataset, title=title, df=df)

    def rows(self, table, **params):
        if 'filters' in params:
            params['filters'] = json.dumps(params['filters'])
        return self.client.get(reverse('table-rows', args=[table.id]), {'limit': 200, **params})

    def stats(self, table):
        response = self.client.get(reverse('table-column-stats', args=[table.id]))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_surrogate_values_round_trip_through_stats_filters_and_rows(self):
        table = self.make(pd.DataFrame({'name': ['M\udcfcller', 'M\udcfcller ', 'Muller', 'm\udcfcller', None]}))
        column = self.stats(table)['columns'][0]
        self.assertEqual(column['top_values'][0], {'value': 'M?ller', 'count': 2, 'truncated': False})
        self.assertEqual(self.rows(table, filters=[{'column': 'name', 'op': 'is', 'value': 'M?ller'}]).json()['count'], 2)
        self.assertEqual(self.rows(table, filters=[{'column': 'name', 'op': 'is_not', 'value': 'M?ller'}]).json()['count'], 3)
        variant = self.rows(table, filters=[{'column': 'name', 'op': 'variant_of', 'value': 'M?ller'}]).json()
        self.assertEqual(variant['count'], 3)
        self.assertEqual({row['name'] for row in variant['results']}, {'M?ller', 'M?ller ', 'm?ller'})
        values = self.client.get(reverse('table-column-values', args=[table.id]), {'column': 'name', 'search': '?'}).json()
        self.assertEqual(values['count'], 2)

    def test_extreme_floats_do_not_break_the_table(self):
        table = self.make(pd.DataFrame({'big': [1e308, 1e308, -1e308], 'name': ['a', 'b', 'c']}))
        payload = self.stats(table)
        self.assertEqual(payload['columns'][0]['kind'], 'number')
        self.assertEqual(payload['columns'][1]['kind'], 'text')

    def test_nested_filters_return_400(self):
        table = self.make(pd.DataFrame({'a': [1]}))
        response = self.client.get(reverse('table-rows', args=[table.id]), {'filters': '[' * 20000 + ']' * 20000})
        self.assertEqual(response.status_code, 400)
        self.assertIn('detail', response.json())

    def test_variant_of_a_group_value_returns_the_group_total(self):
        table = self.make(pd.DataFrame({'sex': ['male'] * 5 + ['Male'] * 3 + ['MALE.', 'female', 'Female', None]}))
        group = self.stats(table)['columns'][0]['variant_groups'][0]
        expected = sum(item['count'] for item in group['values'])
        self.assertEqual(expected, group['count'])
        for item in group['values']:
            response = self.rows(table, filters=[{'column': 'sex', 'op': 'variant_of', 'value': item['value']}])
            self.assertEqual(response.json()['count'], expected)

    def test_top_values_cut_off_ties_through_the_endpoint(self):
        values = ['k'] * 4 + ['c'] * 2 + ['a'] * 2 + [f't{i:02d}' for i in range(15, -1, -1)]
        table = self.make(pd.DataFrame({'v': values}))
        top = self.stats(table)['columns'][0]['top_values']
        self.assertEqual([item['value'] for item in top], ['k', 'a', 'c'] + [f't{i:02d}' for i in range(7)])
        self.assertEqual([item['count'] for item in top], [4, 2, 2] + [1] * 7)

    def test_concurrent_cold_requests_compute_once(self):
        table = self.make(pd.DataFrame({'a': ['x', 'y']}))
        tables = [Table.objects.get(pk=table.pk) for _ in range(4)]
        for item in tables:
            item.df  # load before the threads so they never touch the database
        original = cs.column_stats

        def slow(df, labels):
            time.sleep(0.2)
            return original(df, labels)

        results = []
        # Threads cannot see this test's transaction, so skip the snapshot re-read (tested separately).
        with mock.patch.object(Table, 'load_snapshot', lambda self: self), \
                mock.patch('api.column_stats.column_stats', side_effect=slow) as compute:
            threads = [threading.Thread(target=lambda t=t: results.append(t.column_stats())) for t in tables]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(cs._flights, {})

    def test_row_positions_are_cached_per_query_and_table_version(self):
        table = self.make(pd.DataFrame({'n': list(range(100)), 'g': ['a', 'b'] * 50}))
        params = {'filters': [{'column': 'g', 'op': 'is', 'value': 'a'}], 'sort': 'n', 'order': 'desc', 'limit': 10}
        with mock.patch('api.column_stats.filter_mask', wraps=cs.filter_mask) as compute:
            first = self.rows(table, **params).json()
            second = self.rows(table, offset=10, **params).json()
            self.assertEqual(compute.call_count, 1)
            self.assertEqual([r['n'] for r in first['results']], list(range(98, 78, -2)))
            self.assertEqual([r['n'] for r in second['results']], list(range(78, 58, -2)))
            self.assertEqual(second['count'], 50)
            self.rows(table, filters=params['filters'], sort='n', order='asc', limit=10)
            self.assertEqual(compute.call_count, 2)

            table.df = pd.DataFrame({'n': [1, 2], 'g': ['a', 'a']})
            table.save()
            third = self.rows(table, **params).json()
            self.assertEqual(compute.call_count, 3)
            self.assertEqual([r['n'] for r in third['results']], [2, 1])

    def test_saving_df_with_update_fields_bumps_updated_at(self):
        table = self.make(pd.DataFrame({'a': [1]}))
        before = Table.objects.get(pk=table.pk).updated_at
        table.df = pd.DataFrame({'a': [1, 2]})
        table.save(update_fields=['df'])
        stored = Table.objects.get(pk=table.pk)
        self.assertGreater(stored.updated_at, before)
        self.assertEqual(stored.row_count, 2)
        self.assertEqual(self.stats(stored)['row_count'], 2)

    def test_long_labels_are_accepted_up_to_the_shared_limit(self):
        label = 'h' * 1500
        table = self.make(pd.DataFrame({label: ['x', None]}))
        self.assertEqual(self.rows(table, sort=label).status_code, 200)
        self.assertEqual(self.rows(table, filters=[{'column': label, 'op': 'blank'}]).json()['count'], 1)
        values = self.client.get(reverse('table-column-values', args=[table.id]), {'column': label})
        self.assertEqual(values.status_code, 200)
        self.assertEqual(self.rows(table, sort='h' * 2001).status_code, 400)



def js_string(value):
    """What the browser shows for a rows API JSON value (String(value) in JavaScript)."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return cs.js_number(value)
    return value


class DisplayedValueFilterTests(TestCase):
    def setUp(self):
        cache.clear()
        cs.row_positions_cache.clear()
        self.user = CustomUser.objects.create_user(username='d@example.org', email='d@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Display')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def make(self, df):
        return Table.objects.create(dataset=self.dataset, title='t', df=df)

    def count(self, table, column, value, op='is'):
        response = self.client.get(reverse('table-rows', args=[table.id]), {
            'filters': json.dumps([{'column': column, 'op': op, 'value': value}]), 'limit': 200,
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['count']

    def test_js_number_matches_javascript_string(self):
        cases = {
            12.0: '12', 1.5: '1.5', -0.5: '-0.5', 0.0: '0', 1e21: '1e+21', 1e20: '100000000000000000000',
            1.5e-7: '1.5e-7', 1e-6: '0.000001', 0.1 + 0.2: '0.30000000000000004', 123456.789: '123456.789',
            7: '7', 2 ** 53 + 1: '9007199254740992', -2.5e-10: '-2.5e-10',
        }
        for value, expected in cases.items():
            self.assertEqual(cs.js_number(value), expected, value)

    def test_float_key_column_matches_what_the_grid_shows(self):
        table = self.make(pd.DataFrame({'eventID': [12.0, 13.0, np.nan, 12.0, 1.5]}))
        self.assertEqual(self.count(table, 'eventID', '12'), 2)
        self.assertEqual(self.count(table, 'eventID', '12.0'), 2)
        self.assertEqual(self.count(table, 'eventID', '12.00'), 2)
        self.assertEqual(self.count(table, 'eventID', 12), 2)
        self.assertEqual(self.count(table, 'eventID', '1.5'), 1)
        self.assertEqual(self.count(table, 'eventID', '12', op='is_not'), 3)
        self.assertEqual(self.count(table, 'eventID', '14'), 0)

    def test_int_as_float_with_nans(self):
        table = self.make(pd.DataFrame({'n': [1.0, np.nan, 2.0, 1.0, None]}))
        self.assertEqual(self.count(table, 'n', '1'), 2)
        self.assertEqual(self.count(table, 'n', '2'), 1)
        self.assertEqual(self.count(table, 'n', '1', op='is_not'), 3)

    def test_exact_text_wins_when_it_exists(self):
        # Kind number, two distinct texts: exact matches keep the breakdown's counts;
        # a spelling present in no row falls back to numeric equality.
        table = self.make(pd.DataFrame({'n': ['12', '12.0', '12', '7']}))
        column = self.client.get(reverse('table-column-stats', args=[table.id])).json()['columns'][0]
        self.assertEqual(column['kind'], 'number')
        counts = {item['value']: item['count'] for item in column['top_values']}
        self.assertEqual(self.count(table, 'n', '12'), counts['12'])
        self.assertEqual(self.count(table, 'n', '12.0'), counts['12.0'])
        self.assertEqual(self.count(table, 'n', '12.00'), 3)
        self.assertEqual(self.count(table, 'n', '12', op='is_not'), 2)

    def test_text_columns_do_not_match_numerically(self):
        table = self.make(pd.DataFrame({'t': ['12', '12.0', 'x']}))
        self.assertEqual(self.count(table, 't', '12.00'), 0)
        self.assertEqual(self.count(table, 't', '12'), 1)

    def test_datetime_column_matches_the_rows_api_iso_string(self):
        table = self.make(pd.DataFrame({
            'when': pd.to_datetime(['2020-01-02', '2020-01-03 10:30', None, '2020-01-02'], format='ISO8601'),
        }))
        shown = self.client.get(reverse('table-rows', args=[table.id])).json()['results'][0]['when']
        self.assertTrue(shown.startswith('2020-01-02T00:00:00'))
        self.assertEqual(self.count(table, 'when', shown), 2)
        self.assertEqual(self.count(table, 'when', '2020-01-02 00:00:00'), 2)  # the stats text
        self.assertEqual(self.count(table, 'when', shown, op='is_not'), 2)

    def test_bool_columns_match_javascript_booleans(self):
        table = self.make(pd.DataFrame({'flag': [True, False, True, None], 'pure': [True, False, False, True]}))
        self.assertEqual(self.count(table, 'flag', 'true'), 2)
        self.assertEqual(self.count(table, 'flag', 'True'), 2)
        self.assertEqual(self.count(table, 'pure', 'false'), 2)
        self.assertEqual(self.count(table, 'pure', 'yes'), 0)

    def test_every_displayed_cell_finds_its_own_row(self):
        df = pd.DataFrame({
            'f': [12.0, 1 / 3, 1e20, np.nan, -0.0, 2.5],
            'i': [1, 2, 3, 4, 5, 2 ** 40],
            'd': pd.to_datetime(['2020-01-02', '2021-06-01 08:00:01.5', None, '1999-12-31', '2020-01-02', '2000-02-29'],
                                format='ISO8601'),
            'o': [1.0, 'abc', True, None, 3, ' pad '],
        })
        table = self.make(df)
        rows = self.client.get(reverse('table-rows', args=[table.id])).json()['results']
        for row in rows:
            for column, value in row.items():
                if value is None:
                    continue
                with self.subTest(column=column, value=value):
                    self.assertGreaterEqual(self.count(table, column, js_string(value)), 1)


class SecondReviewApiTests(TestCase):
    """Fixes from the gpt-6.1-sol review: snapshot reads, exact identifiers, untrimmed headers."""

    def setUp(self):
        cache.clear()
        cs.row_positions_cache.clear()
        self.user = CustomUser.objects.create_user(username='s@example.org', email='s@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Second review')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def make(self, df, title='t'):
        return Table.objects.create(dataset=self.dataset, title=title, df=df)

    def rows(self, table, **params):
        if 'filters' in params:
            params['filters'] = json.dumps(params['filters'])
        return self.client.get(reverse('table-rows', args=[table.id]), {'limit': 200, **params})

    def test_stats_for_a_stale_instance_describe_the_saved_version(self):
        table = self.make(pd.DataFrame({'a': ['x', 'y', 'z']}))
        stale = Table.objects.defer('df').get(pk=table.pk)
        fresh = Table.objects.get(pk=table.pk)
        fresh.df = pd.DataFrame({'a': ['x'], 'b': ['new']})
        fresh.save()
        payload = stale.column_stats()
        self.assertEqual(payload['row_count'], 1)
        self.assertEqual([c['name'] for c in payload['columns']], ['a', 'b'])
        self.assertEqual(payload['version'], cs.version(fresh.updated_at))
        self.assertEqual(stale.row_count, 1)

    def test_load_snapshot_reads_data_and_version_together(self):
        table = self.make(pd.DataFrame({'a': [1, 2, 3]}))
        stale = Table.objects.defer('df').get(pk=table.pk)
        fresh = Table.objects.get(pk=table.pk)
        fresh.df = pd.DataFrame({'a': [1]})
        fresh.save()
        stale.load_snapshot()
        self.assertEqual(len(stale.df.index), 1)
        self.assertEqual(stale.row_count, 1)
        self.assertEqual(stale.updated_at, fresh.updated_at)

    def test_equals_matches_identifiers_exactly(self):
        table = self.make(pd.DataFrame({'id': ['001', '002', '1 ', '10']}))
        self.assertEqual(self.rows(table, filters=[{'column': 'id', 'op': 'is', 'value': '1'}]).json()['count'], 1)
        response = self.rows(table, filters=[{'column': 'id', 'op': 'equals', 'value': '1'}]).json()
        self.assertEqual([row['id'] for row in response['results']], ['1 '])
        self.assertEqual(self.rows(table, filters=[{'column': 'id', 'op': 'equals', 'value': '01'}]).json()['count'], 0)
        # "is" falls back to numeric equality when no cell has the exact text; "equals" never does.
        only_padded = self.make(pd.DataFrame({'id': ['001', '002']}), title='padded')
        self.assertEqual(self.rows(only_padded, filters=[{'column': 'id', 'op': 'is', 'value': '1'}]).json()['count'], 1)
        self.assertEqual(self.rows(only_padded, filters=[{'column': 'id', 'op': 'equals', 'value': '1'}]).json()['count'], 0)

    def test_equals_keeps_large_integer_identifiers_apart(self):
        table = self.make(pd.DataFrame({'id': ['9007199254740993', '9007199254740992']}))
        response = self.rows(table, filters=[{'column': 'id', 'op': 'equals', 'value': '9007199254740993'}]).json()
        self.assertEqual([row['id'] for row in response['results']], ['9007199254740993'])

    def test_headers_with_surrounding_spaces_round_trip(self):
        table = self.make(pd.DataFrame([['a', 'b'], ['c', 'd']], columns=[' Species ', 'Species']))
        filtered = self.rows(table, filters=[{'column': ' Species ', 'op': 'is', 'value': 'c'}]).json()
        self.assertEqual(filtered['count'], 1)
        self.assertEqual(filtered['results'][0][' Species '], 'c')
        sorted_rows = self.rows(table, sort=' Species ', order='desc').json()
        self.assertEqual([row[' Species '] for row in sorted_rows['results']], ['c', 'a'])
        scoped = self.rows(table, search='d', column='Species').json()
        self.assertEqual(scoped['count'], 1)
        values = self.client.get(reverse('table-column-values', args=[table.id]), {'column': ' Species '})
        self.assertEqual(values.status_code, 200, values.content)
        self.assertEqual({item['value'] for item in values.json()['results']}, {'a', 'c'})


class PackageModelCacheTests(TestCase):
    """The dataset page asks for the package model often: it is cached per table version."""

    def setUp(self):
        from api.dwc_dp_specs import join_hash_cache

        cache.clear()
        join_hash_cache.clear()
        self.user = CustomUser.objects.create_user(username='p@example.org', email='p@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Package')
        self.event = Table.objects.create(dataset=self.dataset, title='event', df=pd.DataFrame(
            {'event_pk': ['e1', 'e2'], 'parentEventID': ['e2', None], 'eventCategory': ['occurrence'] * 2}))
        self.occurrence = Table.objects.create(dataset=self.dataset, title='occurrence', df=pd.DataFrame(
            {'occurrence_pk': ['o1', 'o2'], 'event_fk': ['e1', 'missing']}))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def fetch(self):
        response = self.client.get(reverse('dataset-package-explorer', args=[self.dataset.id]))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_model_is_computed_once_per_table_version(self):
        from api import dwc_dp_specs

        original = dwc_dp_specs.build_dwc_dp_explorer_model
        with mock.patch('api.dwc_dp_specs.build_dwc_dp_explorer_model', side_effect=original) as build:
            first = self.fetch()
            self.assertEqual(self.fetch(), first)
            self.assertEqual(build.call_count, 1)
            self.occurrence.df = pd.DataFrame({'occurrence_pk': ['o1'], 'event_fk': ['e1']})
            self.occurrence.save()
            edge = next(e for e in self.fetch()['edges'] if e['source'] == 'occurrence' and e['target'] == 'event')
            self.assertEqual(build.call_count, 2)
            self.assertEqual((edge['linkedRows'], edge['unmatchedRows']), (1, 0))

    def test_nodes_carry_their_table_version(self):
        node = next(n for n in self.fetch()['nodes'] if n['id'] == 'occurrence')
        self.occurrence.refresh_from_db()
        self.assertEqual(node['updatedAt'], cs.version(self.occurrence.updated_at))

    def test_a_table_used_with_different_join_fields_is_loaded_once(self):
        from api.dwc_dp_specs import build_dwc_dp_explorer_model

        # event is joined on event_pk (by occurrence) and on parentEventID/event_pk (by itself).
        with CaptureQueriesContext(connection) as queries:
            first = build_dwc_dp_explorer_model(self.dataset)
        loads = [q['sql'] for q in queries.captured_queries if '"df"' in q['sql']]
        self.assertEqual(len(loads), 2, loads)
        edge = next(e for e in first['edges'] if e['source'] == 'occurrence' and e['target'] == 'event')
        self.assertEqual((edge['populatedRows'], edge['linkedRows'], edge['unmatchedRows'], edge['blankRows']), (2, 1, 1, 0))

    def test_saving_one_table_rereads_only_that_table(self):
        from api.dwc_dp_specs import build_dwc_dp_explorer_model

        build_dwc_dp_explorer_model(self.dataset)
        self.occurrence.df = pd.DataFrame({'occurrence_pk': ['o1'], 'event_fk': ['e2']})
        self.occurrence.save()
        with CaptureQueriesContext(connection) as queries:
            model = build_dwc_dp_explorer_model(self.dataset)
        loads = [q['sql'] for q in queries.captured_queries if '"df"' in q['sql']]
        self.assertEqual(len(loads), 1, loads)
        edge = next(e for e in model['edges'] if e['source'] == 'occurrence' and e['target'] == 'event')
        self.assertEqual((edge['populatedRows'], edge['linkedRows']), (1, 1))

    def test_multi_field_keys_need_every_field_and_match_as_a_pair(self):
        from api.dwc_dp_specs import _explorer_join_hashes, _join_column_hashes

        with mock.patch('api.dwc_dp_specs.explorer_join_field_names', return_value={'a', 'b'}):
            source = _join_column_hashes(pd.DataFrame({'a': ['1', '1', None, '2'], 'b': ['x', 'y', 'x', 'x']}))
            target = _join_column_hashes(pd.DataFrame({'a': ['1', '2'], 'b': ['x', 'y']}))
        source_keys = _explorer_join_hashes(source, ['a', 'b'])
        target_keys = _explorer_join_hashes(target, ['a', 'b'])
        self.assertEqual(len(source_keys), 3)
        # (1, x) links; (1, y) and (2, x) do not, although each part occurs in the target.
        self.assertEqual(int(np.isin(source_keys, target_keys).sum()), 1)
        self.assertIsNone(_explorer_join_hashes(source, ['a', 'missing']))

    def test_a_save_replaces_the_cached_hashes_of_that_table(self):
        from api.dwc_dp_specs import build_dwc_dp_explorer_model, join_hash_cache

        build_dwc_dp_explorer_model(self.dataset)
        self.occurrence.df = pd.DataFrame({'occurrence_pk': ['o1'], 'event_fk': ['e2']})
        self.occurrence.save()
        build_dwc_dp_explorer_model(self.dataset)
        self.assertEqual(sorted(join_hash_cache._entries), sorted([self.event.pk, self.occurrence.pk]))
        entry = join_hash_cache.get(self.occurrence.pk, self.occurrence.updated_at.isoformat())
        self.assertEqual(entry['rows'], 1)

    def test_join_hash_cache_is_bounded_by_bytes(self):
        from api.dwc_dp_specs import JoinHashCache

        def entry(rows):
            return {'rows': rows, 'columns': {'a': (np.zeros(rows, dtype=np.uint64), np.ones(rows, dtype=bool))}}

        small = JoinHashCache(max_bytes=200, max_entry_bytes=150)
        small.set(1, 'v', entry(10))   # 90 bytes
        small.set(2, 'v', entry(10))   # 180 bytes in all
        small.set(3, 'v', entry(10))   # over 200: the oldest goes
        self.assertEqual(list(small._entries), [2, 3])
        small.set(4, 'v', entry(20))   # 180 bytes on its own: over the entry limit, not cached
        self.assertIsNone(small.get(4, 'v'))
        self.assertLessEqual(small._bytes, 200)
        self.assertIsNone(small.get(2, 'other version'))  # a superseded version is dropped
        self.assertEqual(list(small._entries), [3])

    def test_coverage_beyond_the_build_budget_is_skipped_not_zero(self):
        from api import dwc_dp_specs

        with mock.patch.object(dwc_dp_specs, 'JOIN_HASH_BUILD_BYTES', 30):
            model = dwc_dp_specs.build_dwc_dp_explorer_model(self.dataset)
        edge = next(e for e in model['edges'] if e['source'] == 'occurrence' and e['target'] == 'event')
        self.assertEqual(edge['coverage'], 'skipped')
        self.assertIsNone(edge['linkedRows'])
        self.assertIsNone(edge['unmatchedRows'])
        checked = dwc_dp_specs.build_dwc_dp_explorer_model(self.dataset)
        edge = next(e for e in checked['edges'] if e['source'] == 'occurrence' and e['target'] == 'event')
        self.assertEqual((edge['coverage'], edge['linkedRows']), ('checked', 1))

    def test_each_table_is_loaded_once(self):
        from api.dwc_dp_specs import build_dwc_dp_explorer_model

        with CaptureQueriesContext(connection) as queries:
            build_dwc_dp_explorer_model(self.dataset)
        loads = [q['sql'] for q in queries.captured_queries if '"df"' in q['sql']]
        self.assertEqual(len(loads), 2, loads)


class PackageCoverageMemoryTests(TestCase):
    """Second-review follow-ups: real array lifetimes and tables that grow during a build."""

    def setUp(self):
        from api.dwc_dp_specs import join_hash_cache

        join_hash_cache.clear()
        self.user = CustomUser.objects.create_user(username='m@example.org', email='m@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Memory')

    def test_a_released_tables_arrays_are_freed_before_the_next_table_loads(self):
        import gc
        import weakref

        from api import dwc_dp_specs

        def table(table_id):
            return {'id': table_id, 'row_count': 4, 'columns': ['k'], 'updated_at': None}

        a, b, c = table(1), table(2), table(3)
        pending = [({'id': 'a-b', 'sourceRows': 4}, a, ['k'], b, ['k']),
                   ({'id': 'c-b', 'sourceRows': 4}, c, ['k'], b, ['k'])]
        refs, alive_when_c_loads = {}, {}

        def fake_hashes(table_id, updated_at, load_snapshot, fits=None):
            if table_id == 3:
                gc.collect()
                alive_when_c_loads.update({key: ref() is not None for key, ref in refs.items()})
            hashes = np.arange(4, dtype=np.uint64)
            filled = np.ones(4, dtype=bool)
            refs[table_id] = weakref.ref(hashes)
            return {'version': '', 'rows': 4, 'columns': {'k': (hashes, filled)}}

        with mock.patch.object(dwc_dp_specs, 'explorer_join_hashes', side_effect=fake_hashes), \
                mock.patch.object(dwc_dp_specs, 'explorer_join_field_names', return_value={'k'}):
            edges = dwc_dp_specs._edges_with_coverage(self.dataset, pending)
        self.assertEqual([e['linkedRows'] for e in edges], [4, 4])
        # A is no longer needed after the first link: its arrays are gone; B is still needed.
        self.assertEqual(alive_when_c_loads, {1: False, 2: True})

    def test_aggregate_budget_skips_coverage_when_tables_together_are_too_large(self):
        from api import dwc_dp_specs

        def table(table_id):
            return {'id': table_id, 'row_count': 10, 'columns': ['k'], 'updated_at': None}

        a, b = table(1), table(2)
        pending = [({'id': 'a-b', 'sourceRows': 10}, a, ['k'], b, ['k'])]

        def fake_hashes(table_id, updated_at, load_snapshot, fits=None):
            return {'version': '', 'rows': 10, 'columns': {'k': (np.zeros(10, dtype=np.uint64), np.ones(10, dtype=bool))}}

        # Each table needs 90 bytes: either fits a 100-byte budget alone, both together do not.
        with mock.patch.object(dwc_dp_specs, 'JOIN_HASH_BUILD_BYTES', 100), \
                mock.patch.object(dwc_dp_specs, 'explorer_join_hashes', side_effect=fake_hashes), \
                mock.patch.object(dwc_dp_specs, 'explorer_join_field_names', return_value={'k'}):
            edges = dwc_dp_specs._edges_with_coverage(self.dataset, pending)
        self.assertEqual(edges[0]['coverage'], 'skipped')
        self.assertIsNone(edges[0]['linkedRows'])

    def test_a_table_that_grew_after_its_metadata_was_read_is_checked_again(self):
        from api import dwc_dp_specs

        table = Table.objects.create(dataset=self.dataset, title='occurrence', df=pd.DataFrame(
            {'occurrence_pk': [f'o{i}' for i in range(100)], 'event_fk': ['e'] * 100}))
        stale = {'id': table.pk, 'row_count': 1, 'columns': list(table.columns), 'updated_at': None}
        target = {'id': table.pk, 'row_count': 1, 'columns': list(table.columns), 'updated_at': None}
        pending = [({'id': 'self', 'sourceRows': 1}, stale, ['event_fk'], target, ['occurrence_pk'])]
        # The stale metadata (1 row) fits; the 100 rows actually stored do not.
        with mock.patch.object(dwc_dp_specs, 'JOIN_HASH_BUILD_BYTES', 50):
            edges = dwc_dp_specs._edges_with_coverage(self.dataset, pending)
        self.assertEqual(edges[0]['coverage'], 'skipped')
        self.assertEqual(dwc_dp_specs.join_hash_cache._entries, {})

    def test_hashes_are_filed_under_the_version_actually_read(self):
        from api import dwc_dp_specs

        table = Table.objects.create(dataset=self.dataset, title='event', df=pd.DataFrame({'event_pk': ['e1']}))
        stale_version = table.updated_at
        table.df = pd.DataFrame({'event_pk': ['e1', 'e2']})
        table.save()
        entry = dwc_dp_specs.explorer_join_hashes(
            table.pk, stale_version,
            lambda: Table.objects.only('df', 'updated_at', 'row_count', 'columns').get(pk=table.pk),
        )
        self.assertEqual(entry['rows'], 2)
        self.assertEqual(entry['version'], table.updated_at.isoformat())
        self.assertIs(dwc_dp_specs.join_hash_cache.get(table.pk, table.updated_at.isoformat()), entry)

    def test_counts_come_from_one_snapshot_when_a_table_changed_within_budget(self):
        from api import dwc_dp_specs

        event = Table.objects.create(dataset=self.dataset, title='event', df=pd.DataFrame({'event_pk': ['e1', 'e2']}))
        occurrence = Table.objects.create(dataset=self.dataset, title='occurrence', df=pd.DataFrame(
            {'occurrence_pk': ['o1'], 'event_fk': ['e1']}))
        stale = {'id': occurrence.pk, 'row_count': 1, 'columns': list(occurrence.columns), 'updated_at': occurrence.updated_at}
        occurrence.df = pd.DataFrame({'occurrence_pk': ['o1', 'o2', 'o3'], 'event_fk': ['e1', 'e2', None]})
        occurrence.save()
        target = {'id': event.pk, 'row_count': 2, 'columns': list(event.columns), 'updated_at': event.updated_at}
        pending = [({'id': 'o-e', 'sourceRows': 1}, stale, ['event_fk'], target, ['event_pk'])]
        edge = dwc_dp_specs._edges_with_coverage(self.dataset, pending)[0]
        self.assertEqual(
            (edge['sourceRows'], edge['populatedRows'], edge['linkedRows'], edge['unmatchedRows'], edge['blankRows']),
            (3, 2, 2, 0, 1),
        )
