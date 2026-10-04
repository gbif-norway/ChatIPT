from django.test import SimpleTestCase

from api.dwca_import import DWC, REGISTRY, ImportFailure
from api.dwca_references import (DC, IDENTIFIER_ROW_TYPE, NON_EXACT_TARGETS, REFERENCE_FAMILIES, REFERENCE_ROW_TYPE,
                                 emit_reference_records, reference_targets)
from api.dwc_dp_specs import TABLE_SPECS
from api.dwca_import import read_inputs
from api.dwca_conversion import build_plan, convert

IDENTIFIER_TERMS = [DC + "identifier", DC + "title", DC + "subject", DC + "format", DWC + "datasetID"]
REFERENCE_TERMS = [DC + name for name in ("identifier", "bibliographicCitation", "title", "creator", "date", "source",
                                          "description", "subject", "language", "rights", "type")] + [DWC + "taxonRemarks", DWC + "datasetID"]


class ReferenceTargetTests(SimpleTestCase):
    def test_families_use_exact_row_type_iris(self):
        self.assertEqual(REFERENCE_FAMILIES, {"http://rs.gbif.org/terms/1.0/Identifier": "identifier",
                                              "http://rs.gbif.org/terms/1.0/Reference": "reference"})

    def test_registry_terms_match_audited_extension_xmls(self):
        for row_type, terms in ((IDENTIFIER_ROW_TYPE, IDENTIFIER_TERMS), (REFERENCE_ROW_TYPE, REFERENCE_TERMS)):
            registered = {iri for iris in REGISTRY["row_types"][row_type].values() for iri in iris}
            self.assertEqual(registered, set(terms))

    def test_targets_exist_in_pinned_schema_and_non_exact_matches_are_declared(self):
        for row_type, terms in ((IDENTIFIER_ROW_TYPE, IDENTIFIER_TERMS), (REFERENCE_ROW_TYPE, REFERENCE_TERMS)):
            for term in terms:
                for target in reference_targets(row_type, term):
                    table, field = target.split(".", 1)
                    spec = next(item for item in TABLE_SPECS[table].schema["fields"] if item["name"] == field)
                    exact = spec.get("dcterms:isVersionOf") == term
                    self.assertEqual(not exact, (row_type, term) in NON_EXACT_TARGETS, target)

    def test_unaudited_terms_are_preserved(self):
        for term in (DC + "format", DC + "title", DC + "subject", DWC + "datasetID"):
            self.assertEqual(reference_targets(IDENTIFIER_ROW_TYPE, term), [])
        for term in (DC + "source", DC + "description", DC + "type", DC + "language", DC + "rights", DWC + "taxonRemarks"):
            self.assertEqual(reference_targets(REFERENCE_ROW_TYPE, term), [])
        self.assertEqual(reference_targets(DWC + "Occurrence", DC + "identifier"), [])
        self.assertEqual(reference_targets(IDENTIFIER_ROW_TYPE, "http://example.org/identifier"), [])

    def test_link_targets_use_neutral_occurrence_tables(self):
        self.assertEqual(reference_targets(IDENTIFIER_ROW_TYPE, DC + "identifier"), ["occurrence-identifier.identifier"])
        self.assertEqual(reference_targets(REFERENCE_ROW_TYPE, DC + "bibliographicCitation"), ["bibliographic-resource.bibliographicCitation"])


class EmitIdentifierTests(SimpleTestCase):
    def test_occurrence_identifier_keeps_source_value(self):
        rows = emit_reference_records("identifier", {"occurrence-identifier": {"identifier": "urn:catalog:O:V:1234"}},
                                      "occurrence", "occ-key", "row-key")
        self.assertEqual(rows, [("occurrence-identifier", {"occurrence_fk": "occ-key", "identifier": "urn:catalog:O:V:1234"})])

    def test_event_core_accepts_renamed_or_neutral_target(self):
        expected = [("event-identifier", {"event_fk": "event-key", "identifier": "https://example.org/event/7"})]
        for table in ("event-identifier", "occurrence-identifier"):
            self.assertEqual(emit_reference_records("identifier", {table: {"identifier": "https://example.org/event/7"}},
                                                    "event", "event-key", "row-key"), expected)

    def test_missing_required_identifier_fails(self):
        for mapped in ({}, {"occurrence-identifier": {"identifier": "  "}}):
            with self.assertRaises(ImportFailure):
                emit_reference_records("identifier", mapped, "occurrence", "occ-key", "row-key")

    def test_contradictory_or_unaudited_fields_fail(self):
        for mapped, subject in (
            ({"event-identifier": {"identifier": "a"}, "occurrence-identifier": {"identifier": "b"}}, "event"),
            ({"event-identifier": {"identifier": "a"}}, "occurrence"),
            ({"occurrence-identifier": {"identifier": "a", "identifierType": "text/html"}}, "occurrence"),
            ({"occurrence-identifier": {"identifier": "a", "occurrence_fk": "x"}}, "occurrence"),
            ({"occurrence-identifier": {"identifier": "a"}, "bibliographic-resource": {"title": "t"}}, "occurrence"),
        ):
            with self.assertRaises(ImportFailure):
                emit_reference_records("identifier", mapped, subject, "key", "row-key")


class EmitReferenceTests(SimpleTestCase):
    mapped = {"bibliographic-resource": {"referenceID": "doi:10.1038/ng0609-637",
                                         "bibliographicCitation": "Hartge, P., Genetics of reproductive lifespan. Nature Genetics 41, 637 - 638 (2009)",
                                         "title": "Genetics of reproductive lifespan", "author": "Patricia Hartge", "issued": ""}}

    def test_one_record_and_link_without_relationship_type(self):
        rows = emit_reference_records("reference", self.mapped, "occurrence", "occ-key", "ref-key")
        self.assertEqual([name for name, _ in rows], ["bibliographic-resource", "occurrence-reference"])
        record, link = rows[0][1], rows[1][1]
        self.assertEqual(record["reference_pk"], "ref-key")
        self.assertEqual(record["referenceID"], "doi:10.1038/ng0609-637")
        self.assertNotIn("issued", record)
        self.assertEqual(link, {"reference_fk": "ref-key", "occurrence_fk": "occ-key"})

    def test_event_core_link(self):
        rows = emit_reference_records("reference", self.mapped, "event", "event-key", "ref-key")
        self.assertEqual(rows[1], ("event-reference", {"reference_fk": "ref-key", "event_fk": "event-key"}))

    def test_rows_without_bibliographic_values_or_keys_fail(self):
        for args in (({"bibliographic-resource": {"title": ""}}, "occurrence", "occ-key", "ref-key"),
                     (self.mapped, "occurrence", "", "ref-key"),
                     (self.mapped, "occurrence", "occ-key", ""),
                     (self.mapped, "taxon", "taxon-key", "ref-key")):
            with self.assertRaises(ImportFailure):
                emit_reference_records("reference", *args)

    def test_relationship_type_and_internal_keys_are_not_accepted(self):
        for mapped in ({**self.mapped, "occurrence-reference": {"relationshipType": "cited in"}},
                       {"bibliographic-resource": {"title": "t", "reference_pk": "doi:10.1/x"}},
                       {"bibliographic-resource": {"title": "t", "referenceType": "Original publication"}}):
            with self.assertRaises(ImportFailure):
                emit_reference_records("reference", mapped, "occurrence", "occ-key", "ref-key")

    def test_unknown_family_fails(self):
        with self.assertRaises(ImportFailure):
            emit_reference_records(REFERENCE_ROW_TYPE, self.mapped, "occurrence", "occ-key", "ref-key")


class ReferenceConversionTests(SimpleTestCase):
    def test_occurrence_references_keep_row_multiplicity_and_review_aliases(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,eventID,occurrenceStatus\no1,e1,present\no2,e1,present\n'),
                               ('references.csv', b'occurrenceID,identifier,title,creator,date,type\no1,urn:ref:one,Shared work,An author,2025,Original publication\no2,urn:ref:one,Shared work,An author,2025,Original publication\n')])
        plan = build_plan(archive)
        columns = {c['term']: c for c in plan['columns'] if c['table'] == 1}
        self.assertFalse(columns[DC + 'identifier']['review'])
        self.assertTrue(columns[DC + 'creator']['review'])
        self.assertTrue(columns[DC + 'date']['review'])
        self.assertEqual(columns[DC + 'type']['default'], 'preserve')
        decisions = {i['id']: i['options'][0]['value'] for i in plan['issues']}
        decisions['event-grain'] = 'by_id'
        frames, report = convert(archive, plan, decisions)
        self.assertEqual(len(frames['event']), 1)
        self.assertEqual(len(frames['bibliographic-resource']), 2)
        self.assertEqual(frames['bibliographic-resource']['reference_pk'].nunique(), 2)
        self.assertEqual(frames['bibliographic-resource']['referenceID'].tolist(), ['urn:ref:one'] * 2)
        self.assertNotIn('referenceType', frames['bibliographic-resource'])
        self.assertNotIn('relationshipType', frames['occurrence-reference'])
        self.assertEqual(set(frames['occurrence-reference']['occurrence_fk']), set(frames['occurrence']['occurrence_pk']))
        self.assertTrue(report['validation']['valid'])

    def test_event_identifiers_and_references_use_event_keys_and_correct_report_targets(self):
        archive = read_inputs([('event.csv', b'eventID,eventCategory\ne1,survey\n'),
                               ('identifier.csv', b'eventID,identifier,format\ne1,urn:survey:one,text/html\n'),
                               ('references.csv', b'eventID,title,bibliographicCitation\ne1,A study,Author 2025\n')])
        plan = build_plan(archive); decisions = {i['id']: i['options'][0]['value'] for i in plan['issues']}
        frames, report = convert(archive, plan, decisions)
        self.assertNotIn('occurrence', frames)
        self.assertNotIn('identifierType', frames['event-identifier'])
        self.assertEqual(frames['event-identifier'].iloc[0]['event_fk'], frames['event'].iloc[0]['event_pk'])
        self.assertEqual(frames['event-reference'].iloc[0]['reference_fk'], frames['bibliographic-resource'].iloc[0]['reference_pk'])
        self.assertTrue(any(c['target'] == 'event-identifier.identifier' for c in report['columns']))
        self.assertTrue(any(r['target_table'] == 'event-reference' and r['key']['event_fk'] for r in report['row_crosswalk']))
        self.assertTrue(report['validation']['valid'])

    def test_eol_row_type_uses_its_own_reference_rules(self):
        meta = ('<archive xmlns="http://rs.tdwg.org/dwc/text/">'
                '<core rowType="' + DWC + 'Occurrence" fieldsTerminatedBy="," ignoreHeaderLines="1">'
                '<files><location>occurrence.txt</location></files><id index="0"/>'
                '<field index="1" term="' + DWC + 'occurrenceID"/><field term="' + DWC + 'occurrenceStatus" default="present"/></core>'
                '<extension rowType="http://eol.org/schema/reference/Reference" fieldsTerminatedBy="," ignoreHeaderLines="1">'
                '<files><location>references.txt</location></files><coreid index="0"/><field index="1" term="' + DC + 'title"/></extension></archive>').encode()
        archive = read_inputs([('meta.xml', meta), ('occurrence.txt', b'key,id\njoin-1,urn:occ:1\n'),
                               ('references.txt', b'coreid,title\njoin-1,A study\n')])
        plan = build_plan(archive)
        issue = next(i for i in [*plan['issues'], *plan.get('automatic_choices', [])] if i['id'] == 'table:1')
        self.assertEqual([o['value'] for o in issue['options']], ['eol-reference', 'preserve'])
        # Unlike the GBIF path, EOL requires an identifier before emitting its title-only row.
        self.assertEqual(next(c for c in plan['columns'] if c['term'] == DC + 'title')['default'], 'bibliographic-resource.title')
        self.assertEqual(next(i for i in [*plan['issues'], *plan.get('automatic_choices', [])] if i['id'] == 'row:1:0')['options'][0]['value'], 'preserve')
        frames, report = convert(archive, plan, {i['id']: i['options'][0]['value'] for i in plan['issues']})
        self.assertNotIn('bibliographic-resource', frames)
