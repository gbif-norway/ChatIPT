import json
import re
from pathlib import Path

from django.test import SimpleTestCase

from api.dwca_germplasm import (ACCESSION_ROW_TYPE, ACCESSION_STATEMENT_TERMS, G, GEO, GERMPLASM_DERIVED_TERMS,
                                GERMPLASM_FAMILIES, GTYPE, SCORE_ROW_TYPE, TRAIT_ROW_TYPE, TRIAL_ROW_TYPE,
                                emit_germplasm_records, germplasm_targets)
from api.dwca_import import DWC, REGISTRY, ImportFailure

SCHEMAS = Path(__file__).parent / "templates/dwc-dp/table-schemas"
NORDGEN = "http://rs.nordgen.org/dwc/germplasm/0.1/terms/"
TRAIL = [G + "measurementTrail" + suffix for suffix in ("ID", "Identifier", "Year", "Report", "Remarks")]


def schema(table):
    return json.loads((SCHEMAS / f"{table}.json").read_text())


def assert_schema_valid(case, rows):
    keys = {}
    for table, row in rows:
        spec = schema(table)
        fields = {field["name"]: field for field in spec["fields"]}
        for name, value in row.items():
            case.assertIn(name, fields, f"{table}.{name}")
            case.assertIsInstance(value, str)
            case.assertTrue(value.strip(), f"empty {table}.{name}")
            kind = fields[name].get("type")
            if kind == "integer":
                case.assertRegex(value, r"^-?\d+$")
            if kind == "number":
                bounds = fields[name].get("constraints", {})
                case.assertTrue(bounds.get("minimum", -1e9) <= float(value) <= bounds.get("maximum", 1e9))
        for name, field in fields.items():
            if field.get("constraints", {}).get("required") and table != "event":
                case.assertTrue(row.get(name), f"required {table}.{name}")
        pk = spec.get("primaryKey")
        if pk and table != "event":
            key = row[pk if isinstance(pk, str) else pk[0]]
            case.assertNotIn(key, keys.setdefault(table, set()), f"duplicate {table} key")
            keys[table].add(key)


class GermplasmTargetTests(SimpleTestCase):
    def test_families_use_exact_row_types(self):
        self.assertEqual(GERMPLASM_FAMILIES, {
            "http://purl.org/germplasm/germplasmTerm#GermplasmAccession": "germplasm-accession",
            "http://purl.org/germplasm/germplasmTerm#MeasurementScore": "germplasm-score",
            "http://purl.org/germplasm/germplasmTerm#MeasurementTrait": "germplasm-trait",
            "http://purl.org/germplasm/germplasmTerm#MeasurementTrial": "germplasm-trial",
        })

    def test_registered_terms_targets_exist_and_aliases_carry_reasons(self):
        exact = []
        for row_type in GERMPLASM_FAMILIES:
            for iris in REGISTRY["row_types"][row_type].values():
                for term in iris:
                    targets, reason = germplasm_targets(row_type, term, ["x"])
                    if (row_type, term) in GERMPLASM_DERIVED_TERMS:
                        self.assertEqual(targets, [])
                        self.assertTrue(reason)
                    for target in targets:
                        table, field = target.split(".", 1)
                        spec = next(item for item in schema(table)["fields"] if item["name"] == field)
                        if spec.get("dcterms:isVersionOf") == term:
                            exact.append((row_type, term))
                        self.assertTrue(reason, target)
        self.assertEqual(exact, [(TRIAL_ROW_TYPE, DWC + "locationID")])

    def test_accession_statement_terms_are_registered(self):
        registered = {iri for iris in REGISTRY["row_types"][ACCESSION_ROW_TYPE].values() for iri in iris}
        self.assertEqual(len(ACCESSION_STATEMENT_TERMS), 31)
        self.assertLessEqual(set(ACCESSION_STATEMENT_TERMS), registered)
        self.assertIn(GTYPE + "storageCondition", ACCESSION_STATEMENT_TERMS)

    def test_misspelled_and_basename_iris_never_match(self):
        for term in TRAIL:
            self.assertEqual(germplasm_targets(SCORE_ROW_TYPE, term, ["t1"])[0], [])
        for term in (G + "measurementTrialID", G + "measurementTrialIdentifier"):
            self.assertEqual(germplasm_targets(TRIAL_ROW_TYPE, term, ["t1"])[0], [])
            self.assertEqual(germplasm_targets(SCORE_ROW_TYPE, term, ["t1"]), ([], None))
        self.assertEqual(germplasm_targets(TRIAL_ROW_TYPE, G + "measurementTrailID", ["t1"])[0], ["event-identifier.identifier"])
        for term in (NORDGEN + "GermplasmID", "http://example.org/germplasmID", G + "storageCondition", DWC + "germplasmID"):
            self.assertEqual(germplasm_targets(ACCESSION_ROW_TYPE, term, ["A1"]), ([], None))
            self.assertNotIn((ACCESSION_ROW_TYPE, term), GERMPLASM_DERIVED_TERMS)
        self.assertEqual(germplasm_targets(NORDGEN + "GermplasmSample", G + "germplasmID", ["A1"]), ([], None))
        self.assertEqual(germplasm_targets(DWC + "MeasurementOrFact", DWC + "measurementValue", ["1"]), ([], None))

    def test_preserved_terms(self):
        for term in (GEO + "lat", GEO + "lon", GEO + "alt", DWC + "locationID", G + "collectingInstituteID"):
            self.assertEqual(germplasm_targets(ACCESSION_ROW_TYPE, term, ["1"])[0], [])
        for term in (G + "germplasmID", G + "germplasmIdentifier", G + "measurementTraitID", G + "measurementTraitIdentifier",
                     G + "measurementByInstituteID", G + "measurementGrowthStage"):
            self.assertEqual(germplasm_targets(SCORE_ROW_TYPE, term, ["v"])[0], [])
        for term in (DWC + "measurementType", G + "measurementTraitCategory", G + "measurementTraitScale", G + "measurementTraitIdentifier"):
            self.assertEqual(germplasm_targets(TRAIT_ROW_TYPE, term, ["v"])[0], [])

    def test_germplasm_id_alias_and_gbif_occurrence_urls(self):
        targets, reason = germplasm_targets(ACCESSION_ROW_TYPE, G + "germplasmID", ["NGB1234"])
        self.assertEqual(targets, ["material-identifier.identifier"]); self.assertTrue(reason)
        targets, reason = germplasm_targets(ACCESSION_ROW_TYPE, G + "germplasmID", ["NGB1", "http://data.gbif.org/occurrences/7"])
        self.assertEqual(targets, []); self.assertIn("GBIF", reason)

    def test_score_type_fields_do_not_share_one_target(self):
        self.assertEqual(germplasm_targets(SCORE_ROW_TYPE, DWC + "measurementType", ["Plant height"])[0], ["occurrence-assertion.verbatimAssertionType"])
        self.assertEqual(germplasm_targets(SCORE_ROW_TYPE, G + "measurementTraitName", ["Plant height"])[0], [])


class AccessionEmitTests(SimpleTestCase):
    source = {G + "biologicalStatus": "Landrace", G + "purdyPedigree": "A/B//C", G + "safetyDuplicationID": "SD-1",
              G + "acquisitionID": "ACQ-9", GTYPE + "storageCondition": "13", G + "breedingRemarks": "  ",
              GEO + "lat": "60.1", G + "collectingInstituteID": "NOR051"}

    def test_identifier_and_one_statement_per_nonempty_term(self):
        rows = emit_germplasm_records("germplasm-accession", self.source, {"material-identifier": {"identifier": "NGB1234"}},
                                      "material", "mat-key", "row-key")
        self.assertEqual(rows[0], ("material-identifier", {"materialEntity_fk": "mat-key", "identifier": "NGB1234"}))
        statements = {row["assertionTypeIRI"]: row["assertionValue"] for table, row in rows[1:]}
        self.assertEqual(statements, {G + "biologicalStatus": "Landrace", G + "purdyPedigree": "A/B//C",
                                      G + "safetyDuplicationID": "SD-1", G + "acquisitionID": "ACQ-9", GTYPE + "storageCondition": "13"})
        self.assertEqual({table for table, row in rows}, {"material-identifier", "material-assertion"})
        self.assertTrue(all(row["materialEntity_fk"] == "mat-key" for table, row in rows))
        assert_schema_valid(self, rows)

    def test_requires_explicit_material_subject(self):
        for subject in ("occurrence", "event", "protocol"):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-accession", self.source, {}, subject, "key", "row-key")
        for key in ("", "  ", None):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-accession", self.source, {}, "material", key, "row-key")

    def test_nothing_approved_is_rejected_and_unaudited_targets_fail(self):
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-accession", {GEO + "lat": "1"}, {"material-identifier": {"identifier": ""}}, "material", "m", "r")
        for mapped in ({"material": {"materialEntityID": "x"}}, {"event": {"locationID": "x"}},
                       {"material-identifier": {"identifier": "a", "identifierType": "accession"}}):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-accession", {}, mapped, "material", "m", "r")


class ScoreEmitTests(SimpleTestCase):
    mapped = {"occurrence-assertion": {"assertionID": "s1", "verbatimAssertionType": "", "assertionValue": "3",
                                       "assertionUnit": "cm", "assertionMadeDate": "2013-06-01", "assertionBy": "Kari"}}
    source = {G + "measurementTraitName": "Plant height", G + "germplasmID": "NGB1234"}

    def test_retargets_to_each_approved_subject(self):
        for subject, fk in (("material", "materialEntity_fk"), ("occurrence", "occurrence_fk"), ("event", "event_fk")):
            rows = emit_germplasm_records("germplasm-score", self.source, self.mapped, subject, "s-key", "r")
            self.assertEqual(rows, [(f"{subject}-assertion", {fk: "s-key", "assertionID": "s1", "verbatimAssertionType": "Plant height",
                                                              "assertionValue": "3", "assertionUnit": "cm",
                                                              "assertionMadeDate": "2013-06-01", "assertionBy": "Kari"})])
            assert_schema_valid(self, rows)
        rows = emit_germplasm_records("germplasm-score", {}, {"material-assertion": {"assertionValue": "3", "verbatimAssertionType": "h"}},
                                      "material", "m", "r")
        self.assertEqual(rows[0][0], "material-assertion")

    def test_conflicting_type_aliases_are_rejected(self):
        mapped = {"occurrence-assertion": {"verbatimAssertionType": "Height", "assertionValue": "3"}}
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-score", self.source, mapped, "occurrence", "o", "r")
        mapped = {"occurrence-assertion": {"verbatimAssertionType": "Plant height", "assertionValue": "3"}}
        self.assertEqual(emit_germplasm_records("germplasm-score", self.source, mapped, "occurrence", "o", "r")[0][1]["verbatimAssertionType"], "Plant height")

    def test_subject_mismatch_and_required_values(self):
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-score", self.source, self.mapped, "protocol", "p", "r")
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-score", self.source, {"event-assertion": {"assertionValue": "3"}}, "material", "m", "r")
        for mapped in ({"occurrence-assertion": {"assertionValue": "", "verbatimAssertionType": "h"}}, {"occurrence-assertion": {"assertionValue": "3"}}):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-score", {}, mapped, "occurrence", "o", "r")

    def test_no_protocol_or_agent_rows_from_score(self):
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-score", {}, {"occurrence-assertion": {"assertionValue": "3", "verbatimAssertionType": "h",
                                                                                    "assertionProtocol_fk": "t1"}}, "occurrence", "o", "r")
        rows = emit_germplasm_records("germplasm-score", {G + "measurementTraitID": "CO_321:0000020", G + "measurementByInstituteID": "NOR051"},
                                      {"occurrence-assertion": {"assertionValue": "3", "verbatimAssertionType": "h"}}, "occurrence", "o", "r")
        self.assertEqual(rows, [("occurrence-assertion", {"occurrence_fk": "o", "assertionValue": "3", "verbatimAssertionType": "h"})])


class TraitEmitTests(SimpleTestCase):
    def test_protocol_description_without_core_link(self):
        mapped = {"protocol": {"protocolID": "CO_321:0000020", "protocolName": "Plant height", "protocolDescription": "Measured at maturity",
                               "protocolReferences": "", "protocolRemarks": "cm"}}
        rows = emit_germplasm_records("germplasm-trait", {DWC + "measurementType": "height"}, mapped, "protocol", "p-key", "r")
        self.assertEqual(rows, [("protocol", {"protocol_pk": "p-key", "protocolID": "CO_321:0000020", "protocolName": "Plant height",
                                              "protocolDescription": "Measured at maturity", "protocolRemarks": "cm"})])
        assert_schema_valid(self, rows)

    def test_subject_and_empty_descriptions(self):
        for subject in ("occurrence", "event", "material"):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-trait", {}, {"protocol": {"protocolName": "x"}}, subject, "k", "r")
        for mapped in ({}, {"protocol": {"protocolID": "CO_321:0000020"}}, {"protocol": {"protocolName": " "}},
                       {"occurrence-assertion": {"assertionValue": "3"}}):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-trait", {}, mapped, "protocol", "p", "r")


class TrialEmitTests(SimpleTestCase):
    mapped = {"event": {"fieldNumber": "T-2013", "year": "2013", "eventRemarks": "Field trial", "locationID": "loc:7", "locality": "Ås"},
              "event-identifier": {"identifier": "trial-42"},
              "bibliographic-resource": {"bibliographicCitation": "Trial report 2013"}}
    source = {GEO + "lat": "59.66", GEO + "lon": "10.78", GEO + "alt": "90"}

    def test_event_patch_identifier_and_report(self):
        rows = emit_germplasm_records("germplasm-trial", self.source, self.mapped, "event", "ev-key", "row-key")
        self.assertEqual(rows, [
            ("event", {"event_pk": "ev-key", "fieldNumber": "T-2013", "year": "2013", "eventRemarks": "Field trial", "locationID": "loc:7",
                       "locality": "Ås", "decimalLatitude": "59.66", "decimalLongitude": "10.78",
                       "minimumElevationInMeters": "90", "maximumElevationInMeters": "90"}),
            ("event-identifier", {"event_fk": "ev-key", "identifier": "trial-42"}),
            ("bibliographic-resource", {"reference_pk": "row-key", "bibliographicCitation": "Trial report 2013"}),
            ("event-reference", {"reference_fk": "row-key", "event_fk": "ev-key"}),
        ])
        assert_schema_valid(self, rows)
        self.assertNotIn("relationshipType", rows[3][1])

    def test_event_only_and_no_new_events(self):
        for subject in ("occurrence", "material", "protocol"):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-trial", self.source, self.mapped, subject, "k", "r")
        rows = emit_germplasm_records("germplasm-trial", {}, {"event-identifier": {"identifier": "trial-42"}}, "event", "ev", "r")
        self.assertEqual(rows, [("event-identifier", {"event_fk": "ev", "identifier": "trial-42"})])

    def test_invalid_or_partial_values_are_rejected(self):
        for source in ({GEO + "lat": "59.6"}, {GEO + "lat": "95", GEO + "lon": "10"}, {GEO + "lat": "N59", GEO + "lon": "10"},
                       {GEO + "alt": "high"}):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-trial", source, {}, "event", "ev", "r")
        for mapped in ({"event": {"year": "2013/14"}}, {"event": {"eventDate": "2013"}}, {"event-assertion": {"assertionValue": "1"}}):
            with self.assertRaises(ImportFailure):
                emit_germplasm_records("germplasm-trial", {}, mapped, "event", "ev", "r")
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-trial", {}, {"bibliographic-resource": {"bibliographicCitation": "R"}}, "event", "ev", "")
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-trial", {G + "measurementTrialID": "x"}, {}, "event", "ev", "r")

    def test_unknown_family(self):
        with self.assertRaises(ImportFailure):
            emit_germplasm_records("germplasm-sample", {}, {}, "material", "m", "r")
        self.assertTrue(all(re.match(r"germplasm-", family) for family in GERMPLASM_FAMILIES.values()))
