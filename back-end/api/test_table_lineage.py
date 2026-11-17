import io
import shutil
import tempfile
from unittest import mock

import pandas as pd
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from api import table_lineage as tl
from api.models import CustomUser, Dataset, DwcConversion, Table, UserFile

def names(n=8):
    return [f'Vespula {i}' for i in range(n)]


class MatchedLineageTests(SimpleTestCase):
    def source(self, df, label='wasps.xlsx - Sheet1', table_id=7):
        return tl.source_from_df(table_id, label, df)

    def match(self, published, *sources):
        return tl.matched_lineage(published, tl._display_columns(published.columns), list(sources))

    def test_positive_match_with_partial_reverse_coverage(self):
        source = pd.DataFrame({'Species': names(10) * 2, 'Notes': [f'n{i}' for i in range(20)]})
        published = pd.DataFrame({'scientificName': names(8), 'occurrenceID': [f'occ:{i}' for i in range(8)]})
        result = self.match(published, self.source(source))
        self.assertEqual(result, {'scientificName': [
            {'table_id': 7, 'table': 'wasps.xlsx - Sheet1', 'column': 'Species', 'basis': 'matched', 'note': ''},
        ]})

    def test_constant_and_low_cardinality_columns_are_not_attributed(self):
        source = pd.DataFrame({'Sex': ['male', 'female'] * 10, 'Status': ['present'] * 20, 'Kind': list('abcd') * 5})
        published = pd.DataFrame({'sex': ['male', 'female'] * 4, 'occurrenceStatus': ['present'] * 8, 'kind': list('abcd') * 2})
        self.assertEqual(self.match(published, self.source(source)), {})

    def test_generated_keys_are_not_attributed(self):
        source = pd.DataFrame({'Species': names(8), 'Site': [f'site {i}' for i in range(8)]})
        published = pd.DataFrame({'occurrence_pk': [f'{i:08x}-uuid' for i in range(8)]})
        self.assertEqual(self.match(published, self.source(source)), {})

    def test_small_whole_numbers_need_most_of_the_source_values(self):
        # event_pk 1..12 is inside "Plot number" 1..200 by chance only.
        source = pd.DataFrame({'Plot number': [str(i) for i in range(1, 201)]})
        published = pd.DataFrame({'event_pk': [str(i) for i in range(1, 13)]})
        self.assertEqual(self.match(published, self.source(source)), {})
        # Using all of a whole-number column is evidence enough.
        source = pd.DataFrame({'Count': [str(i) for i in range(1, 13)] * 3})
        self.assertEqual(self.match(published, self.source(source))['event_pk'][0]['column'], 'Count')

    def test_normalised_only_match_says_so(self):
        weights = [str(i) for i in range(5, 15)]
        source = pd.DataFrame({'Species': [n.upper() + '  ' for n in names(10)], 'Weight': ['7.50' if w == '7' else w for w in weights]})
        published = pd.DataFrame({'scientificName': names(10), 'mass': [('7.5' if w == '7' else w + '.0') for w in weights]})
        result = self.match(published, self.source(source))
        self.assertEqual(result['scientificName'][0]['column'], 'Species')
        self.assertEqual(result['scientificName'][0]['note'], tl.NOTE_NORMALISED)
        # mass is whole-number-like and uses every source value.
        self.assertEqual(result['mass'][0]['column'], 'Weight')
        self.assertIn('upper/lower case', result['mass'][0]['note'])

    def test_exact_match_beats_normalised_match(self):
        exact = pd.DataFrame({'Species': names()})
        loose = pd.DataFrame({'Taxon': [n.upper() for n in names()]})
        result = self.match(pd.DataFrame({'scientificName': names()}),
                            self.source(loose, 'b.csv', 2), self.source(exact, 'a.csv', 1))
        self.assertEqual([e['table'] for e in result['scientificName']], ['a.csv'])

    def test_genuine_ties_are_all_returned(self):
        a = pd.DataFrame({'Species': names()})
        b = pd.DataFrame({'Taxon': names()})
        result = self.match(pd.DataFrame({'scientificName': names()}), self.source(a, 'a.csv', 1), self.source(b, 'b.csv', 2))
        self.assertEqual([(e['table'], e['column']) for e in result['scientificName']], [('a.csv', 'Species'), ('b.csv', 'Taxon')])

    def test_better_reverse_coverage_wins_over_a_wider_column(self):
        exact = pd.DataFrame({'Species': names(8)})
        wide = pd.DataFrame({'All names': names(40)})
        result = self.match(pd.DataFrame({'scientificName': names(8)}), self.source(wide, 'w.csv', 2), self.source(exact, 'e.csv', 1))
        self.assertEqual([e['column'] for e in result['scientificName']], ['Species'])

    def test_too_many_equal_sources_means_no_attribution(self):
        sources = [self.source(pd.DataFrame({f'c{i}': names()}), f's{i}.csv', i) for i in range(4)]
        self.assertEqual(self.match(pd.DataFrame({'scientificName': names()}), *sources), {})

    def test_nearly_all_values_is_enough_and_noted(self):
        published = pd.DataFrame({'scientificName': names(40)})
        source = pd.DataFrame({'Species': names(39)})
        result = self.match(published, self.source(source))
        self.assertEqual(result['scientificName'][0]['note'], '39 of 40 distinct values found')
        self.assertEqual(self.match(published, self.source(pd.DataFrame({'Species': names(30)}))), {})

    def test_midnight_times_and_iso_t_match_dates(self):
        days = pd.date_range('2020-01-01', periods=8).to_series().reset_index(drop=True)
        source = pd.DataFrame({'Date': days})  # Excel dates arrive as timestamps
        published = pd.DataFrame({'eventDate': days.dt.strftime('%Y-%m-%d')})
        self.assertEqual(self.match(published, self.source(source))['eventDate'][0]['column'], 'Date')


class RecordedLineageTests(SimpleTestCase):
    REPORT = {'columns': [
        {'source_table': 'occurrence.txt', 'term': 'http://rs.tdwg.org/dwc/terms/sex', 'target': 'occurrence.sex', 'disposition': 'mapped+retained'},
        {'source_table': 'occurrence.txt', 'term': 'http://rs.tdwg.org/dwc/terms/scientificName',
         'target': 'derived verbatim copy → occurrence.verbatimIdentification', 'disposition': 'mapped+retained'},
        {'source_table': 'occurrence.txt', 'term': 'http://rs.tdwg.org/dwc/terms/habitat', 'target': 'preserve', 'disposition': 'retained-unmapped'},
        {'source_table': 'event.txt', 'term': 'http://rs.tdwg.org/dwc/terms/samplingProtocol', 'target': 'derived sampling records',
         'target_counts': {'event-protocol.protocol_fk': 3, 'survey.sampleSizeValue': 0}, 'disposition': 'derived'},
        {'source_table': 'taxon.txt', 'term': 'http://rs.tdwg.org/dwc/terms/taxonID',
         'target': 'taxonomy-taxon.http://rs.tdwg.org/dwc/terms/taxonID', 'disposition': 'additional-taxonomy-table+retained'},
    ]}

    def test_targets_are_read_from_the_report(self):
        result = tl.recorded_lineage(self.REPORT, 'occurrence', ['sex', 'verbatimIdentification', 'habitat'])
        self.assertEqual(result, {
            'sex': [{'table_id': None, 'table': 'occurrence.txt', 'column': 'sex', 'basis': 'recorded', 'note': ''}],
            'verbatimIdentification': [{'table_id': None, 'table': 'occurrence.txt', 'column': 'scientificName',
                                        'basis': 'recorded', 'note': 'derived from this column'}],
        })
        self.assertEqual(
            tl.recorded_lineage(self.REPORT, 'event-protocol', ['protocol_fk'])['protocol_fk'][0]['column'], 'samplingProtocol')
        self.assertEqual(tl.recorded_lineage(self.REPORT, 'survey', ['sampleSizeValue']), {})
        taxon = tl.recorded_lineage(self.REPORT, 'taxonomy-taxon', ['http://rs.tdwg.org/dwc/terms/taxonID'])
        self.assertEqual(taxon['http://rs.tdwg.org/dwc/terms/taxonID'][0]['column'], 'taxonID')


class LineageApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(username='owner@example.org', email='owner@example.org', password='pw')
        self.other = CustomUser.objects.create_user(username='other@example.org', email='other@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Wasps')
        self.sheet = pd.DataFrame({'Species': names(), 'Sex': ['male', 'female'] * 4})
        self.source = Table.objects.create(dataset=self.dataset, title='Sheet1', df=self.sheet)
        self.upload = UserFile.objects.create(dataset=self.dataset, file='user_files/wasps.xlsx')
        self.published = Table.objects.create(dataset=self.dataset, title='occurrence', df=pd.DataFrame({
            'occurrenceID': [f'occ:{i}' for i in range(8)], 'scientificName': names(), 'sex': ['male', 'female'] * 4,
        }))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        patcher = mock.patch.object(UserFile, 'extract_data', autospec=True,
                                    side_effect=lambda uf: ('tabular', {'Sheet1': self.sheet.copy()}))
        self.extract = patcher.start()
        self.addCleanup(patcher.stop)

    def lineage(self, table=None):
        return self.client.get(reverse('table-lineage', args=[(table or self.published).id]))

    def test_matched_lineage_names_the_upload_and_its_live_table(self):
        response = self.lineage()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'available': True, 'columns': {'scientificName': [
            {'table_id': self.source.id, 'table': 'wasps.xlsx - Sheet1', 'column': 'Species', 'basis': 'matched', 'note': ''},
        ]}})

    def test_uploads_still_count_after_the_working_tables_are_deleted(self):
        self.source.delete()
        entry = self.lineage().json()['columns']['scientificName'][0]
        self.assertEqual((entry['table_id'], entry['table']), (None, 'wasps.xlsx - Sheet1'))

    def test_source_table_is_unavailable(self):
        self.assertEqual(self.lineage(self.source).json(), {'available': False, 'columns': {}})

    def test_nothing_known_is_unavailable(self):
        self.sheet = pd.DataFrame({'Other': [f'x{i}' for i in range(8)]})
        self.assertEqual(self.lineage().json(), {'available': False, 'columns': {}})

    def test_cached_until_a_source_changes(self):
        with mock.patch('api.table_lineage.compute_lineage', wraps=tl.compute_lineage) as compute:
            first = self.lineage().json()
            self.assertEqual(self.lineage().json(), first)
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(self.extract.call_count, 1)
            self.source.df = pd.DataFrame({'Species': names(), 'Sex': ['m', 'f'] * 4})
            self.source.save()
            self.lineage()
            self.assertEqual(compute.call_count, 2)
            # The upload's values are cached separately: a table change does not re-read it.
            self.assertEqual(self.extract.call_count, 1)
            UserFile.objects.create(dataset=self.dataset, file='user_files/more.csv')
            self.lineage()
            self.assertEqual(compute.call_count, 3)
            self.assertEqual(self.extract.call_count, 2)
            self.published.save()
            self.lineage()
            self.assertEqual(compute.call_count, 4)
            self.assertEqual(self.extract.call_count, 2)
            # Saving another package table changes neither this table nor its sources.
            Table.objects.create(dataset=self.dataset, title='event', df=pd.DataFrame({'eventID': ['e1']}))
            self.lineage()
            self.assertEqual(compute.call_count, 4)

    def test_other_datasets_of_the_same_user_are_ignored(self):
        other = Dataset.objects.create(user=self.user, title='Bees')
        Table.objects.create(dataset=other, title='Sheet1', df=pd.DataFrame({'Taxon': names()}))
        UserFile.objects.create(dataset=other, file='user_files/bees.csv')
        self.sheet = pd.DataFrame({'Unrelated': [f'u{i}' for i in range(8)]})
        self.assertEqual(self.lineage().json(), {'available': False, 'columns': {}})
        # Only this dataset's own upload was read.
        self.assertEqual([call.args[0].pk for call in self.extract.call_args_list], [self.upload.pk])

    def test_a_working_table_cleaned_in_place_only_lends_its_id(self):
        # Same title, rows and labels as the upload sheet, but the agent rewrote the names.
        self.source.df = pd.DataFrame({'Species': [n.upper() for n in names()], 'Sex': ['m', 'f'] * 4})
        self.source.save()
        entry = self.lineage().json()['columns']['scientificName'][0]
        self.assertEqual((entry['table_id'], entry['column'], entry['note']), (self.source.id, 'Species', ''))
        # Different column labels: no longer the upload sheet, so no id.
        self.source.df = pd.DataFrame({'Species name': names(), 'Sex': ['m', 'f'] * 4})
        self.source.save()
        self.assertIsNone(self.lineage().json()['columns']['scientificName'][0]['table_id'])

    def test_an_oversized_upload_is_not_read_and_live_tables_are_used(self):
        with mock.patch.object(tl, 'MAX_UPLOAD_BYTES', 10), \
                mock.patch('django.db.models.fields.files.FieldFile.size', new_callable=mock.PropertyMock, return_value=11):
            entry = self.lineage().json()['columns']['scientificName'][0]
        self.extract.assert_not_called()
        # Without a readable upload the live non-package table is the source.
        self.assertEqual((entry['table'], entry['column']), ('Sheet1', 'Species'))

    def test_live_tables_are_indexed_without_keeping_their_dataframes(self):
        self.extract.side_effect = FileNotFoundError('gone')
        live = tl._live_tables(self.dataset)
        index = tl.table_index(live[0])
        self.assertEqual(index['columns'][0][0], 'Species')
        self.assertIn('df', live[0].get_deferred_fields())

    def test_oversized_live_tables_are_not_unpickled(self):
        live = tl._live_tables(self.dataset)
        with mock.patch.object(tl, 'MAX_LIVE_TABLE_CELLS', 1), \
                mock.patch.object(Table.objects, 'only', side_effect=AssertionError('unpickled')):
            index = tl.table_index(live[0])
        self.assertEqual(index['columns'], [])

    def test_reading_stops_when_the_total_value_budget_is_used(self):
        UserFile.objects.create(dataset=self.dataset, file='user_files/later.csv')
        # The first upload's columns already use the whole budget: the later file is never read.
        with mock.patch.object(tl, 'MAX_TOTAL_INDEX_VALUES', 1):
            self.lineage()
        self.assertEqual([call.args[0].pk for call in self.extract.call_args_list], [self.upload.pk])

    def test_reading_stops_when_the_column_cap_is_reached(self):
        UserFile.objects.create(dataset=self.dataset, file='user_files/later.csv')
        with mock.patch.object(tl, 'MAX_SOURCE_COLUMNS', 2):
            self.assertTrue(self.lineage().json()['available'])
        self.assertEqual([call.args[0].pk for call in self.extract.call_args_list], [self.upload.pk])

    def test_an_unreadable_upload_is_not_retried_on_every_request(self):
        self.extract.side_effect = FileNotFoundError('gone')
        # Without a readable upload the live non-package table is the source.
        self.assertEqual(self.lineage().json()['columns']['scientificName'][0]['table'], 'Sheet1')
        self.published.save()
        self.lineage()
        self.assertEqual(self.extract.call_count, 1)

    def test_other_users_get_404(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.lineage().status_code, 404)

    def test_conversion_lineage_is_recorded(self):
        dataset = Dataset.objects.create(user=self.user, workflow_type=Dataset.WorkflowType.DWCA_CONVERSION)
        table = Table.objects.create(dataset=dataset, title='occurrence', df=pd.DataFrame({'sex': ['male'], 'occurrenceID': ['a']}))
        conversion = DwcConversion.objects.create(dataset=dataset, status='complete', report=RecordedLineageTests.REPORT)
        self.assertEqual(self.lineage(table).json(), {'available': True, 'columns': {'sex': [
            {'table_id': None, 'table': 'occurrence.txt', 'column': 'sex', 'basis': 'recorded', 'note': ''},
        ]}})
        # A re-plan clears the report and leaves the old output tables until the next conversion.
        conversion.status = 'review'
        conversion.save()
        self.assertEqual(self.lineage(table).json(), {'available': False, 'columns': {}})


class RealUploadLineageTests(TestCase):
    """The real ``UserFile.extract_data`` path, through a temporary file storage."""

    def setUp(self):
        cache.clear()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        storages = {
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': self.media}},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        }
        override = override_settings(STORAGES=storages, MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = CustomUser.objects.create_user(username='owner@example.org', email='owner@example.org', password='pw')
        self.dataset = Dataset.objects.create(user=self.user, title='Wasps')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def published(self):
        return Table.objects.create(dataset=self.dataset, title='occurrence', df=pd.DataFrame({
            'scientificName': names(), 'occurrenceID': [f'occ:{i}' for i in range(8)],
        }))

    def lineage(self, table):
        return self.client.get(reverse('table-lineage', args=[table.id])).json()

    def test_csv_upload(self):
        csv = 'Species,Count\n' + '\n'.join(f'{name},{i}' for i, name in enumerate(names()))
        upload = UserFile.objects.create(dataset=self.dataset, file=SimpleUploadedFile('wasps.csv', csv.encode()))
        entry = self.lineage(self.published())['columns']['scientificName'][0]
        self.assertEqual((entry['table'], entry['column'], entry['table_id']), (upload.filename, 'Species', None))

    def test_xlsx_upload(self):
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            pd.DataFrame({'Notes': [f'n{i}' for i in range(8)]}).to_excel(writer, sheet_name='Notes', index=False)
            pd.DataFrame({'Taxon': names()}).to_excel(writer, sheet_name='Specimens', index=False)
        upload = UserFile.objects.create(dataset=self.dataset, file=SimpleUploadedFile('wasps.xlsx', buffer.getvalue()))
        entry = self.lineage(self.published())['columns']['scientificName'][0]
        self.assertEqual((entry['table'], entry['column']), (f'{upload.filename} - Specimens', 'Taxon'))


class LineageBoundsTests(SimpleTestCase):
    """Memory and sampling bounds (second review)."""

    def test_index_holds_hashes_not_texts(self):
        index = tl.sheet_index(pd.DataFrame({'Species': names(10)}))
        label, exact, keys = index['columns'][0]
        self.assertEqual(label, 'Species')
        self.assertEqual(exact.dtype.name, 'uint64')
        self.assertEqual(len(exact), 10)
        self.assertEqual(keys.dtype.name, 'uint64')

    def test_column_budget_is_shared_across_the_sheets_of_one_upload(self):
        budget = {'columns': 3, 'values': tl.MAX_INDEX_VALUES}
        first = tl.sheet_index(pd.DataFrame({'a': names(), 'b': names()}), budget)
        second = tl.sheet_index(pd.DataFrame({'c': names(), 'd': names()}), budget)
        self.assertEqual([c[0] for c in first['columns']], ['a', 'b'])
        self.assertEqual([c[0] for c in second['columns']], ['c'])
        third = tl.sheet_index(pd.DataFrame({'e': names()}), budget)
        self.assertEqual(third['columns'], [])

    def test_value_budget_stops_indexing(self):
        budget = {'columns': 100, 'values': 30}
        index = tl.sheet_index(pd.DataFrame({'a': names(10), 'b': [f'x{i}' for i in range(10)], 'c': names(10)}), budget)
        # Each column holds 10 exact and 10 normalised hashes: only the first fits in 30.
        self.assertEqual([c[0] for c in index['columns']], ['a'])
        self.assertEqual(budget['values'], 0)

    def test_rows_are_sampled_across_the_whole_table(self):
        with mock.patch.object(tl, 'MAX_ROWS', 10):
            df = pd.DataFrame({'v': ['head'] * 50 + ['tail'] * 50})
            rows, sampled = tl.sample_rows(df)
            self.assertTrue(sampled)
            self.assertEqual(len(rows.index), 10)
            self.assertEqual(set(rows['v']), {'head', 'tail'})

    def test_a_matching_prefix_does_not_hide_the_rest_of_the_table(self):
        # The first rows match the source; most later rows do not. Sampling across the table sees that.
        with mock.patch.object(tl, 'MAX_ROWS', 20):
            source = tl.source_from_df(7, 'wasps.xlsx', pd.DataFrame({'Species': names(10)}))
            published = pd.DataFrame({'scientificName': names(10) + [f'Other {i}' for i in range(90)]})
            self.assertEqual(tl.matched_lineage(published, ['scientificName'], [source]), {})

    def test_sampled_rows_are_mentioned_in_the_note(self):
        with mock.patch.object(tl, 'MAX_ROWS', 20):
            # Each name fills a block of rows, so a spread-out sample still sees every name.
            source = tl.source_from_df(7, 'wasps.xlsx', pd.DataFrame({'Species': [n for n in names(10) for _ in range(5)]}))
            published = pd.DataFrame({'scientificName': [n for n in names(10) for _ in range(4)]})
            entry = tl.matched_lineage(published, ['scientificName'], [source])['scientificName'][0]
        self.assertIn('checked on 20 of the 40 rows', entry['note'])
        self.assertIn('source checked on 20 of its 50 rows', entry['note'])
