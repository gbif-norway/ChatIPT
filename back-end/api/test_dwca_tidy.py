import copy
import json
import time

from django.test import SimpleTestCase

from api.dwca_import import DWC, read_inputs
from api.dwca_tidy import column_note, summarize, tidy_archive


class DwcaTidyTests(SimpleTestCase):
    def build(self, header, rows, filename='occurrence.csv'):
        data = (header + '\n' + '\n'.join(rows) + '\n').encode()
        return read_inputs([(filename, data)])

    def group(self, result, rule, field=None):
        return next(g for g in result['groups'] if g['rule'] == rule and (field is None or g['field'] == field))

    def test_country_names_codes_water_and_unknown_labels(self):
        names = ['Norway', 'Sweden', 'Svalbard and Jan Mayen',
                 'Saint Helena, Ascension and Tristan da Cunha', 'Great Britain', 'Antarctica',
                 'NO', 'North Atlantic Ocean (other parts)', 'Mediterranean Sea',
                 'Other Oceans place', 'NA', 'se', 'NOR']
        archive = self.build('occurrenceID,countryCode', [f'r{i},"{name}"' for i, name in enumerate(names)])
        before_terms = list(archive.tables[0].terms)
        view, table = tidy_archive(archive)
        cells = {row[0]: row[1:] for row in view.tables[0].rows}
        for i, code in enumerate(['NO', 'SE', 'SJ', 'SH', 'GB', 'AQ', 'NO']):
            self.assertEqual(cells[f'r{i}'][0], code)
        self.assertEqual(cells['r7'][0], '')
        self.assertEqual(cells['r8'][0], '')
        self.assertEqual(cells['r9'][0], 'Other Oceans place')
        self.assertEqual(cells['r10'][0], 'NA')
        self.assertEqual(cells['r11'][0], 'SE')
        self.assertEqual(cells['r12'][0], 'NO')
        self.assertEqual(view.tables[0].terms[:2], before_terms)
        self.assertEqual(view.tables[0].terms[2:], [DWC + 'country', DWC + 'waterBody'])
        self.assertEqual(view.tables[0].rows[0][0], 'r0')
        self.assertEqual(view.tables[0].rows[0][2], 'Norway')
        self.assertEqual(view.tables[0].rows[7][3], 'North Atlantic Ocean (other parts)')
        self.assertEqual(column_note(view, 0, 2)['tidy_added']['term'], DWC + 'country')
        self.assertIsNone(column_note(view, 0, 1))
        self.assertTrue(table['groups'])
        json.dumps(table)

    def test_country_fill_agreement_and_conflict(self):
        archive = self.build('occurrenceID,country,countryCode',
                             ['a,Norway,', 'b,Norway,NO', 'c,Norway,SE'])
        view, table = tidy_archive(archive)
        rows = {row[0]: row for row in view.tables[0].rows}
        self.assertEqual(rows['a'][2], 'NO')
        self.assertEqual(rows['b'][2], 'NO')
        self.assertEqual(rows['c'][1:], ['Norway', 'SE'])
        group = self.group(table, 'country-code-fill')
        self.assertEqual(group['values'][0]['conflict_rows'], 1)
        self.assertEqual(group['values'][0]['agree_rows'], 1)
        # 568: country names beside codes that already agree change nothing, so nothing is listed.
        agreeing = self.build('occurrenceID,country,countryCode', ['a,Norway,NO', 'b,Tanzania,TZ'])
        self.assertEqual(tidy_archive(agreeing)[1]['groups'], [])

    def test_vocabulary_placeholders_and_variants(self):
        archive = self.build('occurrenceID,sex,lifeStage', [
            'a,Unknown,Pullus', 'b,Female,Larvae', 'c,f,Age unknown', 'd,m,Adult + Juvenile',
            'e,?,2 cy', 'f,Female + Male,2 cy.', 'g,Female?,AF', 'h,F,2 cy', 'i,M,CV'])
        view, _ = tidy_archive(archive)
        rows = {row[0]: row[1:] for row in view.tables[0].rows}
        self.assertEqual(rows['a'], ['', 'nestling'])
        self.assertEqual(rows['b'], ['female', 'larva'])
        self.assertEqual(rows['c'], ['female', 'unknown'])
        self.assertEqual(rows['d'], ['male', 'adult | juvenile'])
        self.assertEqual(rows['e'], ['', '2 cy'])
        self.assertEqual(rows['f'], ['female | male', '2 cy'])
        self.assertEqual(rows['g'], ['Female?', 'AF'])
        self.assertEqual(rows['i'], ['male', 'CV'])

    def test_occurrence_remark_moves_only_exact_life_stage_values(self):
        values = ['ad', 'ad.', 'juv', 'juv.', 'Juv', 'Juv.', 'subad', 'subad.',
                  'fad', '1 juv.', 'ad + egg', 'ad.m.egg']
        archive = self.build('occurrenceID,eventRemarks', [f'r{i},{v}' for i, v in enumerate(values)])
        view, result = tidy_archive(archive)
        self.assertEqual(view.tables[0].terms[-1], DWC + 'lifeStage')
        rows = view.tables[0].rows
        self.assertEqual([rows[i][1] for i in range(8)], [''] * 8)
        self.assertEqual([rows[i][2] for i in range(8)], ['adult', 'adult', 'juvenile', 'juvenile', 'juvenile', 'juvenile', 'subadult', 'subadult'])
        self.assertEqual([rows[i][1] for i in range(8, 12)], values[8:])
        # Existing values agreeing are cleared; conflicts preserve the remark.
        with_column = self.build('occurrenceID,eventRemarks,lifeStage', ['a,juv.,juvenile', 'b,ad.,adult', 'c,ad.,egg'])
        out, report = tidy_archive(with_column)
        self.assertEqual([r[1:] for r in out.tables[0].rows], [['', 'juvenile'], ['', 'adult'], ['ad.', 'egg']])
        moved = self.group(report, 'life-stage-remark')
        self.assertEqual(moved['conflict_rows'], 1)
        self.assertEqual(moved['values'][0]['agree_rows'], 1)
        event = self.build('eventID,eventRemarks', ['a,juv.'], filename='event.csv')
        event_view, event_report = tidy_archive(event)
        self.assertEqual(event_view.tables[0].rows[0][1], 'juv.')
        self.assertFalse(any(g['rule'] == 'life-stage-remark' for g in event_report['groups']))

    def test_protected_fields_placeholders_whitespace_and_numbers(self):
        archive = self.build('occurrenceID,occurrenceRemarks,fieldNumber,catalogNumber,verbatimDepth,scientificName,habitat,minimumElevationInMeters', [
            'a,NA,NA,NA,NA,"Dictyna  arundinacea","Fuktig myr med  gress.","1000,0"',
            'b,,,,,,,"1,000"'])
        view, result = tidy_archive(archive)
        row = view.tables[0].rows[0]
        self.assertEqual(row[1:7], ['', 'NA', 'NA', 'NA', 'Dictyna  arundinacea', 'Fuktig myr med gress.'])
        self.assertEqual(row[7], '1000.0')
        suggestion = self.group(result, 'thousands-or-decimal')
        self.assertFalse(suggestion['values'][0]['applied'])
        vid = suggestion['values'][0]['id']
        changed, _ = tidy_archive(archive, overrides={vid: 'on'})
        self.assertEqual(changed.tables[0].rows[1][7], '1.000')
        self.assertEqual(archive.tables[0].rows[0][7], '1000,0')
        quantity = self.build('occurrenceID,organismQuantity,organismQuantityType', ['a,"1,",individuals', 'b,2,individuals'])
        trailing = self.group(tidy_archive(quantity)[1], 'trailing-separator')
        self.assertEqual((trailing['tier'], trailing['values'][0]['fields']), ('suggest', {'organismQuantity': '1'}))

    def test_zero_columns_require_elevation_and_depth(self):
        archive = self.build('occurrenceID,minimumElevationInMeters,maximumElevationInMeters,minimumDepthInMeters,maximumDepthInMeters',
                             ['a,0,0,0,0', 'b,0.0,-0,0,0'])
        view, _ = tidy_archive(archive)
        self.assertEqual(view.tables[0].rows[0][1:], ['', '', '', ''])
        depth_only = self.build('occurrenceID,minimumDepthInMeters,maximumDepthInMeters', ['a,0,0', 'b,0.0,-0'])
        kept, _ = tidy_archive(depth_only)
        self.assertEqual(kept.tables[0].rows[0][1:], ['0', '0'])

    def test_encoding_suggestion_is_reversible_and_hash_stable(self):
        archive = self.build('occurrenceID,locality', ['a,B\x99SINGEN'])
        view, result = tidy_archive(archive)
        group = self.group(result, 'encoding')
        self.assertFalse(group['values'][0]['applied'])
        self.assertEqual(view.tables[0].rows[0][1], 'B\x99SINGEN')
        changed, changed_table = tidy_archive(archive, overrides={group['values'][0]['id']: 'on'})
        self.assertEqual(changed.tables[0].rows[0][1], 'BÖSINGEN')
        self.assertNotEqual(result['sha256'], changed_table['sha256'])
        self.assertEqual(changed_table['sha256'], tidy_archive(archive, overrides={group['values'][0]['id']: 'on'})[1]['sha256'])

    def test_overrides_summarize_model_changes_and_source_immutability(self):
        archive = self.build('occurrenceID,sex,locality', ['a,F, A  place', 'b,M,Other'])
        before_rows = copy.deepcopy(archive.tables[0].rows)
        before_files = copy.deepcopy(archive.files)
        view, result = tidy_archive(archive)
        group = self.group(result, 'vocabulary')
        vid = next(value['id'] for value in group['values'] if value['value'] == 'F')
        undone, table = tidy_archive(archive, overrides={group['id']: 'off', vid: 'on', 'unknown': 'off'})
        self.assertEqual(undone.tables[0].rows[0][1], 'female')
        self.assertEqual(undone.tables[0].rows[1][1], 'M')
        self.assertEqual(tidy_archive(archive, overrides={group['id']: 'off'})[0].tables[0].rows[0][1], 'F')
        self.assertNotEqual(result['sha256'], table['sha256'])
        bounded = summarize(result, value_limit=0)
        self.assertEqual(bounded['groups'][0]['more_values'], len(result['groups'][0]['values']))
        self.assertEqual(len(bounded['groups'][0]['values']), 0)
        model, model_table = tidy_archive(archive, model_changes=[
            {'table': 0, 'column': 2, 'value': 'Other', 'fields': {'locality': 'Other Place'},
             'tier': 'suggest', 'confidence': 'high', 'note': 'case'},
            {'table': 0, 'column': 1, 'value': 'F', 'fields': {'sex': 'woman'}, 'tier': 'auto'},
            {'table': 0, 'column': 2, 'value': ' A  place', 'fields': {'bad/name': 'x'}, 'tier': 'auto'},
        ])
        self.assertEqual(model.tables[0].rows[1][2], 'Other')
        suggestion = self.group(model_table, 'model-suggestion')
        self.assertEqual((suggestion['values'][0]['confidence'], suggestion['values'][0]['note']), ('high', 'case'))
        self.assertEqual(model.tables[0].rows[0][1], 'female')  # the rule wins over the model entry for 'F'
        applied, _ = tidy_archive(archive, overrides={suggestion['values'][0]['id']: 'on'}, model_changes=[
            {'table': 0, 'column': 2, 'value': 'Other', 'fields': {'locality': 'Other Place'}, 'tier': 'suggest'}])
        self.assertEqual(applied.tables[0].rows[1][2], 'Other Place')
        self.assertTrue(any(g['rule'] == 'model-suggestion' for g in model_table['groups']))
        self.assertEqual(archive.tables[0].rows, before_rows)
        self.assertEqual(archive.files, before_files)
        self.assertIsNot(view, archive)

    def test_large_distinct_value_profile(self):
        rows = [f'r{i},Norway,F' for i in range(20000)]
        archive = self.build('occurrenceID,countryCode,sex', rows)
        started = time.monotonic()
        view, _ = tidy_archive(archive)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(view.tables[0].rows[-1][1:], ['NO', 'female', 'Norway'])

    def test_review_round_one_cases(self):
        # A countryCode name beside a different supplied country keeps both source values.
        both = self.build('occurrenceID,countryCode,country', ['a,Norway,Sweden', 'b,Norway,Norway'])
        view, table = tidy_archive(both)
        self.assertEqual([row[1:] for row in view.tables[0].rows], [['Norway', 'Sweden'], ['NO', 'Norway']])
        moved = self.group(table, 'country-name')
        self.assertEqual((moved['changed_rows'], moved['conflict_rows']), (1, 1))
        # With every row in conflict, the notice says the values were left as written.
        only = tidy_archive(self.build('occurrenceID,countryCode,country', ['a,Norway,Sweden']))[1]
        self.assertIn('left as written', self.group(only, 'country-name')['title'])
        # Only curated sea names move automatically; mixed place text is a suggestion.
        mixed = self.build('occurrenceID,country', ['a,"United Kingdom (English Channel)"', 'b,North Atlantic Ocean (other parts)'])
        view, table = tidy_archive(mixed)
        self.assertEqual(view.tables[0].rows[0][1], 'United Kingdom (English Channel)')
        self.assertEqual(view.tables[0].rows[1][1:], ['', 'North Atlantic Ocean (other parts)'])
        self.assertFalse(self.group(table, 'water-body-suggestion')['applied'])
        # One elevation and one depth column of zeros may be real: suggested, not applied.
        shore = self.build('occurrenceID,minimumElevationInMeters,minimumDepthInMeters', ['a,0,0', 'b,0,0'])
        view, table = tidy_archive(shore)
        self.assertEqual(view.tables[0].rows[0][1:], ['0', '0'])
        self.assertTrue(all(group['tier'] == 'suggest' for group in table['groups']))
        # A bare NA in country is ambiguous (Namibia or not available) and stays as written.
        namibia = self.build('occurrenceID,country,countryCode', ['a,NA,', 'b,na,'])
        view, _ = tidy_archive(namibia)
        self.assertEqual([row[1:] for row in view.tables[0].rows], [['NA', ''], ['', '']])

    def test_review_round_two_cases(self):
        # Only upper-case NA is Namibia; lower-case na in countryCode is a placeholder.
        codes = self.build('occurrenceID,countryCode', ['a,NA', 'b,na'])
        self.assertEqual([row[1] for row in tidy_archive(codes)[0].tables[0].rows], ['NA', ''])
        # Three zero columns could be a coastal surface survey: suggested only.
        coast = self.build('occurrenceID,minimumElevationInMeters,maximumElevationInMeters,minimumDepthInMeters',
                           ['a,0,0,0', 'b,0,0,0'])
        view, table = tidy_archive(coast)
        self.assertEqual(view.tables[0].rows[0][1:], ['0', '0', '0'])
        self.assertTrue(all(group['tier'] == 'suggest' for group in table['groups']))
        # Group totals count rewritten and cleared cells for the ledger, beyond any listed values.
        sex = self.build('occurrenceID,sex', ['a,f', 'b,Unknown', 'c,F'])
        table = tidy_archive(sex)[1]
        self.assertEqual((self.group(table, 'vocabulary')['tidied_rows'], self.group(table, 'empty-placeholder')['cleared_rows']), (2, 1))

    def test_review_round_six_cases(self):
        from api.dwca_conversion import build_plan
        # A value some rows keep as written because of a conflict still gets its fallback question.
        both = self.build('occurrenceID,countryCode,country,eventRemarks,lifeStage,occurrenceStatus',
                          ['a,Norway,Sweden,juv.,adult,present', 'b,Norway,Norway,ad,,present'])
        view, _ = tidy_archive(both)
        asked = {issue['source_value'] for issue in build_plan(view)['issues']
                 if issue['id'].startswith(('country-label:', 'age-remark:'))}
        self.assertEqual(asked, {'Norway', 'juv.'})
        # A model answer about a value after the space clean-up applies to the source spelling with spaces too.
        spaced = self.build('occurrenceID,eventRemarks,sex', ['a,"fad ",f'])
        model = [{'table': 0, 'column': 1, 'value': 'fad', 'tier': 'auto', 'move': True,
                  'fields': {'eventRemarks': '', 'lifeStage': 'adult', 'occurrenceRemarks': 'fad'}}]
        view, table = tidy_archive(spaced, model_changes=model)
        row = dict(zip([term.rsplit('/', 1)[-1] for term in view.tables[0].terms], view.tables[0].rows[0]))
        self.assertEqual((row['eventRemarks'], row['lifeStage'], row['occurrenceRemarks']), ('', 'adult', 'fad'))
        self.assertFalse(any(group['rule'] == 'whitespace' for group in table['groups']))

    def test_review_round_seven_cases(self):
        from api.dwca_conversion import build_plan
        from api.dwca_tidy import value_id
        # Two changes in one row: the life-stage reading of AF conflicts with sex male and is kept as written, so the
        # remark 'ad' cannot rely on it and stays too.
        row = self.build('occurrenceID,eventRemarks,lifeStage,sex', ['a,ad,AF,male'])
        model = [{'table': 0, 'column': 2, 'value': 'AF', 'tier': 'auto', 'fields': {'lifeStage': 'adult', 'sex': 'female'}}]
        view, table = tidy_archive(row, model_changes=model)
        self.assertEqual(view.tables[0].rows[0][1:4], ['ad', 'AF', 'male'])
        self.assertTrue(all(group['conflict_rows'] for group in table['groups']))
        # An applied suggestion whose destination is occupied leaves the label unsettled, so it is still asked about.
        label = self.build('occurrenceID,countryCode,waterBody,occurrenceStatus', ['a,"United Kingdom (English Channel)",North Sea,present'])
        group = 'tidy:0:1:water-body-suggestion'
        view, _ = tidy_archive(label, overrides={value_id(group, 'United Kingdom (English Channel)'): 'on'})
        self.assertEqual(view.tables[0].rows[0][1], 'United Kingdom (English Channel)')
        self.assertTrue(any(issue['id'].startswith('country-label:') for issue in build_plan(view)['issues']))

    def test_review_round_eight_cases(self):
        from api.dwca_conversion import build_plan
        from api.dwca_value_ledger import build_value_disposition_ledger
        # Two remarks naming different life stages for one row: both stay, whatever the column order.
        for header, row in (('occurrenceID,eventRemarks,occurrenceRemarks', 'a,ad,juv'),
                            ('occurrenceID,occurrenceRemarks,eventRemarks', 'a,juv,ad')):
            view, _ = tidy_archive(self.build(header, [row]))
            self.assertEqual(view.tables[0].rows[0][1:3], row.split(',')[1:], header)
            self.assertNotIn(DWC + 'lifeStage', view.tables[0].terms)
        # Cells filled from another column were empty in the source: an existing empty column and an added one.
        for header, rows in (('occurrenceID,country,countryCode,occurrenceStatus', ['a,Norway,,present', 'b,Sweden,SE,present']),
                             ('occurrenceID,country,occurrenceStatus', ['a,Norway,present', 'b,Sweden,present'])):
            view, table = tidy_archive(self.build(header, rows))
            plan = build_plan(view)
            ledger = build_value_disposition_ledger(plan, {'columns': [], 'tidy': {'groups': table['groups']}})
            entry = next(item for item in ledger['source_terms'] if item['source_term'] == DWC + 'countryCode')
            self.assertEqual((entry['nonempty_values'], entry['tidy_filled_values'], entry['source_nonempty_values']),
                             (2, 2 if 'countryCode' not in header else 1, 0 if 'countryCode' not in header else 1), header)

    def test_final_review_cases(self):
        from api.dwca_conversion import build_plan
        # Titles show letters such as ü as they are, escape only control characters, and name where moved values went.
        names = self.build('occurrenceID,recordedBy,eventRemarks', ['a,Kr\x81ger,ad'])
        table = tidy_archive(names)[1]
        self.assertIn('‘Kr\\x81ger’ → Krüger', self.group(table, 'encoding')['title'])
        self.assertIn('‘ad’ → lifeStage: adult', self.group(table, 'life-stage-remark')['title'])
        # A stray byte beside a letter that survived is dropped, not decoded into a second letter (568).
        damaged = self.build('occurrenceID,recordedBy,locality', ['a,Kl\x81üver,Brü\x81ssel', 'b,B\x99SINGEN,"Liebenzell, W\x81ürttemberg"'])
        repairs = {value['value']: value['fields'] for group in tidy_archive(damaged)[1]['groups'] for value in group['values']}
        self.assertEqual(repairs['Kl\x81üver'], {'recordedBy': 'Klüver'})
        self.assertEqual(repairs['Brü\x81ssel'], {'locality': 'Brüssel'})
        self.assertEqual(repairs['Liebenzell, W\x81ürttemberg'], {'locality': 'Liebenzell, Württemberg'})
        self.assertEqual(repairs['B\x99SINGEN'], {'recordedBy': 'BÖSINGEN'})
        # 572: a suggestion that would change no row (every row already has a different count) is not offered, and the
        # remark is still asked about; one that would change some rows shows its conflicts and leaves the question too.
        model = [{'table': 0, 'column': 1, 'value': '1 juv.', 'tier': 'suggest', 'move': True,
                  'fields': {'eventRemarks': '', 'lifeStage': 'juvenile', 'individualCount': '1', 'occurrenceRemarks': '1 juv.'}}]
        for rows, offered in ((['a,1 juv.,2,present'], False), (['a,1 juv.,2,present', 'b,1 juv.,,present'], True)):
            source = self.build('occurrenceID,eventRemarks,individualCount,occurrenceStatus', rows)
            view, table = tidy_archive(source, model_changes=model)
            suggestion = [value for group in table['groups'] for value in group['values'] if value['value'] == '1 juv.']
            self.assertEqual(bool(suggestion), offered)
            if offered:
                self.assertEqual((suggestion[0]['conflict_rows'], suggestion[0]['applied']), (1, False))
            self.assertIn('1 juv.', [issue.get('source_value') for issue in build_plan(view)['issues']])

    def test_taxon_core_stand_in_plans_know_what_was_tidied(self):
        from api.dwca_conversion import build_plan
        from api.test_dwca_taxon import manifest_archive
        source = read_inputs(manifest_archive([
            ('records.csv', DWC + 'Occurrence', [DWC + term for term in ('occurrenceID', 'occurrenceStatus', 'countryCode', 'eventRemarks')],
             [['join-a', 'occ-1', 'present', 'Norway', 'juv.'], ['join-b', 'occ-2', 'present', 'United Kingdom (English Channel)', 'ad']]),
        ]).items())
        # 'United Kingdom (English Channel)' is offered as a waterBody suggestion in the tidy panel, so it is not asked again.
        nested = build_plan(tidy_archive(source)[0])['taxonomy']['occurrence_plans']
        asked = [issue['id'] for inner in nested.values() for issue in inner['issues']]
        self.assertFalse([identifier for identifier in asked if identifier.startswith(('country-label:', 'age-remark:'))], asked)
        untidied = build_plan(source)['taxonomy']['occurrence_plans']
        self.assertTrue(any(issue['id'].startswith('country-label:') for inner in untidied.values() for issue in inner['issues']))

