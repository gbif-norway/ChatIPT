from django.test import SimpleTestCase

from api import dwca_glossary
from api.dwc_dp_specs import TABLE_SPECS, dwc_dp_schema_snapshot
from api.dwca_conversion import REGISTERED_TERMS, _candidates


class DwcaGlossaryTests(SimpleTestCase):
    def test_glossary_is_pinned_to_the_schema_snapshot(self):
        self.assertEqual(dwca_glossary.SCHEMA_REVISION, dwc_dp_schema_snapshot()['revision'])

    def test_every_ambiguous_registered_candidate_has_a_gloss(self):
        target_sets = (['occurrence', 'event', 'identification'],
                       ['occurrence', 'event', 'identification', 'material'], ['event'])
        failures = []
        for term in sorted(REGISTERED_TERMS):
            for tables in target_sets:
                candidates = _candidates(term, tables)
                if len(candidates) > 1:
                    failures.extend(f'{term}: {target}' for target in candidates if not dwca_glossary.is_explained(target))
        self.assertEqual(failures, [])

    def test_glossary_keys_refer_to_pinned_tables_and_fields(self):
        fields = {table: set(spec.field_descriptors) for table, spec in TABLE_SPECS.items()}
        self.assertTrue(set(dwca_glossary.TABLES) <= set(TABLE_SPECS))
        for family in dwca_glossary.FAMILIES.values():
            for table in family.get('tables', {}):
                self.assertIn(table, TABLE_SPECS)
            for field in family['fields']:
                self.assertTrue(any(field in table_fields for table_fields in fields.values()), field)
        for field in dwca_glossary.FIELD_LABELS:
            self.assertTrue(any(field in table_fields for table_fields in fields.values()), field)
        for target in dwca_glossary.TARGETS:
            table, field = target.split('.', 1)
            self.assertIn(table, fields)
            self.assertIn(field, fields[table])

    def test_plain_field_label_fallbacks_and_target_explanations(self):
        self.assertEqual(dwca_glossary.field_label('organismQuantityType'), 'organism quantity type')
        self.assertEqual(dwca_glossary.field_label('eventID'), 'event identifier')
        self.assertEqual(dwca_glossary.explain('preserve'), dwca_glossary.PRESERVE)
        self.assertEqual(dwca_glossary.explain('material.collectedBy')['label'], 'Who collected the specimen')
        self.assertEqual(dwca_glossary.explain('identification.dateIdentified')['label'],
                         'Date identified, on a separate identification record')
