import pandas as pd
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from api.models import CustomUser, Dataset, Table


class TableApiTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username='owner@example.org',
            email='owner@example.org',
            password='password',
        )
        self.dataset = Dataset.objects.create(user=self.user, title='Large dataset')
        self.table = Table.objects.create(
            dataset=self.dataset,
            title='occurrence',
            description='Occurrence records',
            df=pd.DataFrame(
                [
                    {'occurrenceID': f'occ-{index}', 'scientificName': f'Taxon {index}'}
                    for index in range(250)
                ]
            ),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_table_save_keeps_shape_metadata_current(self):
        self.assertEqual(self.table.row_count, 250)
        self.assertEqual(self.table.columns, ['occurrenceID', 'scientificName'])

        self.table.df = pd.DataFrame([[1, 2, 3]], columns=['id', 'id', None])
        self.table.save()
        self.table.refresh_from_db()

        self.assertEqual(self.table.row_count, 1)
        self.assertEqual(self.table.columns, ['id (1)', 'id (2)', 'Unnamed column'])

    def test_invalid_unicode_is_replaced_in_columns_and_rows(self):
        table = Table.objects.create(
            dataset=self.dataset,
            title='unicode',
            df=pd.DataFrame([['bad\udcf3value']], columns=['bad\udcf3column']),
        )

        self.assertEqual(table.columns, ['bad?column'])
        self.assertEqual(table.row_page(0, 1), [{'bad?column': 'bad?value'}])

    def test_list_returns_metadata_without_loading_dataframe(self):
        Table.objects.create(
            dataset=self.dataset,
            title='dwca_occurrence_ext_tmp',
            df=pd.DataFrame({'hidden': [1]}),
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('table-list'), {'dataset': self.dataset.id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], self.table.id)
        self.assertEqual(response.data[0]['row_count'], 250)
        self.assertEqual(response.data[0]['columns'], ['occurrenceID', 'scientificName'])
        self.assertNotIn('df', response.data[0])
        table_queries = [query['sql'] for query in queries if 'api_table' in query['sql'].lower()]
        self.assertTrue(table_queries)
        self.assertTrue(all('"api_table"."df"' not in sql for sql in table_queries))

    def test_rows_returns_only_the_requested_page(self):
        response = self.client.get(
            reverse('table-rows', args=[self.table.id]),
            {'offset': 100, 'limit': 25},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 250)
        self.assertEqual(response.data['offset'], 100)
        self.assertEqual(response.data['limit'], 25)
        self.assertEqual(len(response.data['results']), 25)
        self.assertEqual(response.data['results'][0]['occurrenceID'], 'occ-100')
        self.assertEqual(response.data['results'][-1]['occurrenceID'], 'occ-124')

    def test_rows_rejects_unbounded_page_sizes(self):
        response = self.client.get(
            reverse('table-rows', args=[self.table.id]),
            {'limit': 201},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('limit', response.data)

    def test_other_users_cannot_list_or_read_tables(self):
        other = CustomUser.objects.create_user(
            username='other@example.org',
            email='other@example.org',
            password='password',
        )
        self.client.force_authenticate(other)

        list_response = self.client.get(reverse('table-list'), {'dataset': self.dataset.id})
        rows_response = self.client.get(reverse('table-rows', args=[self.table.id]))

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data, [])
        self.assertEqual(rows_response.status_code, 404)
