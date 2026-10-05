from django.test import SimpleTestCase

from api.dwca_value_ledger import build_value_disposition_ledger


class ValueDispositionLedgerTests(SimpleTestCase):
    def setUp(self):
        self.plan = {
            'tables': [{'name': 'occurrences'}],
            'columns': [
                {'id': 'column:0:0', 'table': 0, 'column': 0, 'term': 'dwc:individualCount', 'default': 'preserve', 'nonempty': 4},
                {'id': 'column:0:1', 'table': 0, 'column': 1, 'term': 'dwc:locality', 'default': 'preserve', 'nonempty': 2, 'review': True},
                {'id': 'column:0:2', 'table': 0, 'column': 2, 'term': 'dwc:measure', 'default': 'derive', 'nonempty': 3},
                {'id': 'column:0:3', 'table': 0, 'column': 3, 'term': 'dwc:elevation', 'default': 'event.minimumElevationInMeters', 'nonempty': 3},
                {'id': 'column:0:4', 'table': 0, 'column': 4, 'term': 'dwc:eventID', 'default': 'join', 'nonempty': 5},
            ],
        }
        self.report = {
            'effective_decisions': {},
            'columns': [
                {'source_table': 'occurrences', 'term': 'dwc:individualCount', 'target': 'derived quantity → occurrence.organismQuantity', 'disposition': 'derived', 'mapped_rows': 3, 'retained_only_rows': 1},
                {'source_table': 'occurrences', 'term': 'dwc:locality', 'target': 'preserve', 'disposition': 'retained-unmapped', 'nonempty': 2},
                {'source_table': 'occurrences', 'term': 'dwc:measure', 'target': 'derived extension records', 'disposition': 'derived', 'mapped_rows': 2, 'retained_only_rows': 1},
                {'source_table': 'occurrences', 'term': 'dwc:elevation', 'target': 'event.minimumElevationInMeters', 'disposition': 'mapped+retained', 'mapped_rows': 2, 'retained_only_rows': 1},
                {'source_table': 'occurrences', 'term': 'dwc:eventID', 'target': 'join', 'disposition': 'retained-unmapped', 'nonempty': 5},
            ],
            'withheld_values': [{'source_table_index': 0, 'term': 'dwc:elevation', 'value': '999999'}],
            'warnings': [{'id': 'column:0:3'}],
        }

    def test_reports_dispositions_and_output_provenance(self):
        ledger = build_value_disposition_ledger(self.plan, self.report, {
            'occurrence': [{'organismQuantity': '3', 'generated_pk': 'a'}], 'event': []})
        rows = {row['source_term']: row for row in ledger['source_terms']}
        self.assertEqual((rows['dwc:individualCount']['derived_values'], rows['dwc:individualCount']['originals_only_values']), (3, 1))
        self.assertEqual(rows['dwc:locality']['status'], 'needs-review')
        self.assertEqual(rows['dwc:measure']['derived_values'], 2)
        self.assertEqual(rows['dwc:elevation']['withheld_invalid_values'], 1)
        self.assertEqual(rows['dwc:elevation']['emitted_but_flagged_values'], 2)
        self.assertEqual(rows['dwc:elevation']['status'], 'mapped')
        self.assertEqual(rows['dwc:eventID']['status'], 'unverified')
        self.assertEqual(rows['dwc:eventID']['unverified_values'], 5)
        self.assertEqual(ledger['output_fields'], [
            {'field': 'event.minimumElevationInMeters', 'source_term_count': 2},
            {'field': 'occurrence.organismQuantity', 'source_term_count': 3},
        ])
        self.assertEqual(ledger['emitted_resources'], {'event': 0, 'occurrence': 1})
        self.assertIn({'field': 'occurrence.generated_pk', 'nonempty_output_values': 1},
                      ledger['untraced_or_generated_output_fields'])
        for row in rows.values():
            classified = sum(row[key] for key in ('mapped_values', 'derived_values', 'withheld_invalid_values', 'originals_only_values', 'unverified_values'))
            self.assertLessEqual(classified, row['nonempty_values'])
            self.assertLessEqual(row['emitted_but_flagged_values'], row['nonempty_values'])

    def test_is_deterministic_and_does_not_mutate_inputs(self):
        import copy
        plan, report = copy.deepcopy(self.plan), copy.deepcopy(self.report)
        one = build_value_disposition_ledger(plan, report)
        two = build_value_disposition_ledger(plan, report)
        self.assertEqual(one, two)
        self.assertEqual(plan, self.plan)
        self.assertEqual(report, self.report)
