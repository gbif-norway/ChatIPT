import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api import taxon_matching
from api.agent_tools import ApplyTaxonDecisions, MatchTaxonNames, RequestTaxonReview
from api.models import Agent, CustomUser, Dataset, Message, Table, Task, TaxonNameMatch


RELEASE = {"checklistKey": "col", "alias": "COL26.6 XR", "checklistBankDatasetKey": "315557"}


def usage(key, name, authorship, rank, status="ACCEPTED", classification=None):
    return {
        "usage": {
            "key": key, "name": f"{name} {authorship}".strip(), "canonicalName": name,
            "authorship": authorship, "rank": rank, "status": status,
        },
        "classification": classification or [
            {"key": "N", "name": "Animalia", "rank": "KINGDOM"},
            {"key": "RT", "name": "Arthropoda", "rank": "PHYLUM"},
            {"key": "CCQKT", "name": "Arachnida", "rank": "CLASS"},
        ],
    }


def summary(match_type, key=None, name=None, rank="SPECIES", authorship="", alternatives=()):
    payload = usage(key, name, authorship, rank) if key else {}
    payload["diagnostics"] = {"matchType": match_type, "confidence": 97, "alternatives": list(alternatives)}
    return taxon_matching.summarize_match(payload)


class SplitQualifierTests(SimpleTestCase):
    def test_uncertain_and_new_species_markers(self):
        self.assertEqual(taxon_matching.split_qualifier("Dinychus sp."), ("Dinychus", "sp."))
        self.assertEqual(taxon_matching.split_qualifier("Camisia  spp"), ("Camisia", "spp."))
        self.assertEqual(taxon_matching.split_qualifier("Cilliba rafalskii sp.n"), ("Cilliba rafalskii", None))
        self.assertEqual(taxon_matching.split_qualifier("Cilliba rafalskii sp. nov."), ("Cilliba rafalskii", None))
        self.assertEqual(
            taxon_matching.split_qualifier("Zercon cf. triangularis"), ("Zercon triangularis", "cf. triangularis"),
        )
        self.assertEqual(
            taxon_matching.split_qualifier("Trachytes aegrota (C. L. Koch, 1841)"),
            ("Trachytes aegrota (C. L. Koch, 1841)", None),
        )


class SummarizeMatchTests(SimpleTestCase):
    def test_synonym_keeps_identified_name_and_reports_accepted(self):
        payload = usage("RQTLF", "Phthiracarus nitens", "(Nicolet, 1855)", "SPECIES", status="SYNONYM")
        payload.update({
            "synonym": True,
            "acceptedUsage": {"key": "7WF9L", "canonicalName": "Phthiracarus laevigatus",
                              "authorship": "(Koch, 1844)", "rank": "SPECIES"},
            "diagnostics": {"matchType": "EXACT", "confidence": 98},
        })
        result = taxon_matching.summarize_match(payload)
        self.assertEqual(result["status"], "exact")
        self.assertEqual(result["usage"]["scientificName"], "Phthiracarus nitens")
        self.assertEqual(result["usage"]["scientificNameAuthorship"], "(Nicolet, 1855)")
        self.assertEqual(result["usage"]["taxonRank"], "species")
        self.assertEqual(result["usage"]["classification"]["class"], "Arachnida")
        self.assertEqual(result["acceptedUsage"]["scientificName"], "Phthiracarus laevigatus")

    def test_alternatives_are_compacted(self):
        alternative = usage("C3DM4", "Cryptognathidae", "Oudemans, 1902", "FAMILY")
        alternative["diagnostics"] = {"matchType": "VARIANT", "confidence": 79}
        result = taxon_matching.summarize_match(
            {"diagnostics": {"matchType": "NONE", "alternatives": [alternative]}}
        )
        self.assertEqual(result["status"], "none")
        self.assertIsNone(result["usage"])
        self.assertEqual(result["alternatives"][0]["id"], "C3DM4")
        self.assertEqual(result["alternatives"][0]["matchType"], "VARIANT")


REAL = json.loads((Path(__file__).parent / "testdata" / "col_v2_real_matches.json").read_text())


class RealNameTests(SimpleTestCase):
    """Real GBIF v2 responses (trimmed) for names published wrongly from conversions 560 and 570."""

    def test_rank_markers_survive_from_the_v2_name(self):
        betula = taxon_matching.summarize_match(
            REAL["Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti"]["response"])["usage"]
        # canonicalName is "Betula pubescens czerepanovii", a different combination in botany.
        self.assertEqual((betula["scientificName"], betula["scientificNameAuthorship"], betula["taxonRank"]),
                         ("Betula pubescens subsp. czerepanovii", "(N.I.Orlova) Hämet-Ahti", "subspecies"))
        columba = taxon_matching.summarize_match(REAL["Columba livia var. domestica"]["response"])["usage"]
        self.assertEqual((columba["scientificName"], columba["scientificNameAuthorship"]),
                         ("Columba livia var. domestica", "J.F.Gmelin, 1789"))
        larus = taxon_matching.summarize_match(REAL["Larus sp."]["response"])["usage"]
        self.assertEqual(larus["scientificName"], "Larus")
        # A higher taxon without authorship, and an alternative, keep their plain names.
        calanus = taxon_matching.summarize_match(REAL["Calanus"]["response"])
        self.assertEqual(calanus["usage"]["scientificName"], "Arthropoda")
        self.assertEqual(calanus["alternatives"][0]["scientificName"], "Calanus")

    def test_names_whose_authorship_is_not_a_suffix_fall_back_to_the_canonical_name(self):
        self.assertEqual(taxon_matching._name_without_authorship(
            {"name": "Aus bus L. subsp. bus", "canonicalName": "Aus bus bus", "authorship": "L."}), "Aus bus bus")
        self.assertEqual(taxon_matching._name_without_authorship(
            {"name": "Mentha × piperita L.", "canonicalName": "Mentha piperita", "authorship": "L."}), "Mentha × piperita")
        self.assertEqual(taxon_matching._name_without_authorship({"canonicalName": "Aus"}), "Aus")

    def test_name_parts_ignore_markers_subgenus_and_authorship(self):
        self.assertEqual(taxon_matching.name_parts("Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti"),
                         ["betula", "pubescens", "czerepanovii"])
        self.assertEqual(taxon_matching.name_parts("Carabus (Morphocarabus) kruberi Fischer, 1823"), ["carabus", "kruberi"])
        self.assertEqual(taxon_matching.name_parts("Mentha x piperita"), taxon_matching.name_parts("Mentha × piperita L."))
        self.assertEqual(taxon_matching.name_parts("Trachytes aegrota (C. L. Koch, 1841)"), ["trachytes", "aegrota"])

    def test_publication_bulk_accept_skips_an_exact_match_on_another_name(self):
        def row(label):
            summary = taxon_matching.summarize_match(REAL[label]["response"])
            query = REAL[label]["query"]
            return SimpleNamespace(decision=TaxonNameMatch.Decision.PENDING, record_count=1, matched_at=timezone.now(), match=summary,
                                   identification_qualifier="", query=query, verbatim_label=query["scientificName"])
        # 568: the class hint steered "AmphibiaReptilia" to an EXACT match on the class Amphibia.
        self.assertFalse(taxon_matching.bulk_acceptable(row("AmphibiaReptilia sp.")))
        self.assertTrue(taxon_matching.bulk_acceptable(row("Columba livia var. domestica")))


class MatchColTests(SimpleTestCase):
    @patch("api.taxon_matching._get_json")
    def test_batch_then_verbose_retry_for_unmatched(self, get_json):
        typo_retry = {"diagnostics": {"matchType": "NONE", "alternatives": [
            {**usage("C3DM4", "Cryptognathidae", "Oudemans, 1902", "FAMILY"),
             "diagnostics": {"matchType": "VARIANT", "confidence": 79}},
        ]}}
        get_json.side_effect = [
            [
                {**usage("74BRN", "Nothrus silvestris", "Nicolet, 1855", "SPECIES"),
                 "diagnostics": {"matchType": "EXACT"}},
                {"diagnostics": {"matchType": "NONE"}},
            ],
            typo_retry,
        ]
        results = taxon_matching.match_col([
            {"scientificName": "Nothrus silvestris"},
            {"scientificName": "Crypthognathidae", "kingdom": "Animalia"},
            {"scientificName": "Nothrus silvestris"},
        ])
        self.assertEqual([result["status"] for result in results], ["exact", "none", "exact"])
        self.assertEqual(results[1]["alternatives"][0]["scientificName"], "Cryptognathidae")
        batch_call, retry_call = get_json.call_args_list
        self.assertEqual(batch_call.args[0], "POST")
        self.assertEqual(len(batch_call.kwargs["json"]), 2)  # duplicate query sent once
        self.assertEqual(retry_call.kwargs["params"]["verbose"], "true")
        self.assertEqual(retry_call.kwargs["params"]["kingdom"], "Animalia")

    @patch("api.taxon_matching._get_json")
    def test_higher_rank_match_that_echoes_the_hint_is_no_match(self, get_json):
        animalia = {**usage("N", "Animalia", "", "KINGDOM"), "diagnostics": {"matchType": "HIGHERRANK"}}
        get_json.side_effect = [[animalia, animalia], animalia, animalia]
        hinted, unhinted = taxon_matching.match_col([
            {"scientificName": "Pająki", "kingdom": "Animalia"},
            {"scientificName": "Animalcule"},
        ])
        self.assertEqual(hinted["status"], "none")
        self.assertIsNone(hinted["usage"])
        self.assertTrue(hinted["hintOnly"])
        self.assertEqual(unhinted["status"], "higher_rank")


def fake_match(queries, deadline=None):
    results = {
        "Nothrus silvestris": summary("EXACT", "74BRN", "Nothrus silvestris", authorship="Nicolet, 1855"),
        "Neodiscopoma splendida (Kramer, 1882)": summary("HIGHERRANK", "VNH95", "Neodiscopoma", rank="GENUS"),
        "Araneae": summary("EXACT", "5W", "Araneae", rank="ORDER"),
        "Dinychus": summary("EXACT", "DNY", "Dinychus", rank="GENUS", authorship="Kramer, 1886"),
    }
    return [results.get(query["scientificName"], summary("NONE")) for query in queries]


def fake_aids(query, deadline=None):
    return {"gbifBackbone": {"status": "exact", "scientificName": query["scientificName"], "taxonRank": "species"}}


@patch("api.taxon_matching.col_release", return_value=RELEASE)
@patch("api.taxon_matching.review_aids", side_effect=fake_aids)
@patch("api.taxon_matching.match_col", side_effect=fake_match)
class RecordAndApplyTests(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(title="Soil fauna")
        self.table = Table.objects.create(dataset=self.dataset, title="occurrence", df=pd.DataFrame({
            "occurrenceID": ["o1", "o2", "o3", "o4", "o5", "o6"],
            "verbatimIdentification": [
                "N_silvestris", "N_silvestris", "Neodiscopoma splendida (Kramer, 1882)",
                "Pająki", "Dinychus sp.", None,
            ],
            "scientificName": [None, None, "Neodiscopoma splendida", None, "Dinychus", None],
        }))

    def record(self, overrides=None):
        rows, finished = taxon_matching.record_matches(
            self.dataset, "occurrence", self.table.df, "verbatimIdentification", overrides=overrides,
            hints={"kingdom": "Animalia"},
        )
        self.assertTrue(finished)
        return rows

    def test_records_distinct_labels_with_counts_and_qualifiers(self, match_col, aids, release):
        rows = {row.verbatim_label: row for row in self.record()}
        self.assertEqual(set(rows), {"N_silvestris", "Neodiscopoma splendida (Kramer, 1882)", "Pająki", "Dinychus sp."})
        self.assertEqual(rows["N_silvestris"].record_count, 2)
        self.assertEqual(rows["N_silvestris"].verbatim_column, "verbatimIdentification")
        self.assertEqual(rows["Dinychus sp."].query, {"kingdom": "Animalia", "scientificName": "Dinychus"})
        self.assertEqual(rows["Dinychus sp."].identification_qualifier, "sp.")
        self.assertEqual(rows["N_silvestris"].suggestion_status, "none")
        self.assertEqual(rows["Neodiscopoma splendida (Kramer, 1882)"].suggestion_status, "higher_rank")
        self.assertEqual(rows["Pająki"].col_release["alias"], "COL26.6 XR")
        # Review aids only for names COL could not place exactly.
        self.assertEqual(rows["Dinychus sp."].review_aids, {})
        self.assertIn("gbifBackbone", rows["N_silvestris"].review_aids)

    def test_interpretations_rematch_only_changed_pending_labels(self, match_col, aids, release):
        self.record()
        match_col.reset_mock()
        rows = {row.verbatim_label: row for row in self.record(overrides={
            ("N_silvestris", ""): {"query": {"scientificName": "Nothrus silvestris"}, "note": "Nothridae block"},
            ("Pająki", ""): {"query": {"scientificName": "Araneae"}, "note": "Polish for spiders"},
        })}
        self.assertEqual(
            sorted(query["scientificName"] for query in match_col.call_args.args[0]),
            ["Araneae", "Nothrus silvestris"],
        )
        self.assertEqual(rows["N_silvestris"].suggestion_status, "exact")
        self.assertTrue(taxon_matching.is_preprocessed(rows["N_silvestris"]))
        self.assertEqual(rows["N_silvestris"].preprocessing_note, "Nothridae block")

        # Re-running without a new interpretation keeps a decision; a new interpretation reopens it.
        taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.ACCEPTED)
        match_col.reset_mock()
        self.record()
        match_col.assert_not_called()
        self.assertEqual(TaxonNameMatch.objects.get(verbatim_label="Pająki").decision, "accepted")
        self.record(overrides={("Pająki", ""): {"query": {"scientificName": "Opiliones"}, "note": "x"}})
        reopened = TaxonNameMatch.objects.get(verbatim_label="Pająki")
        self.assertEqual(reopened.decision, "pending")
        self.assertEqual(reopened.decided_usage, {})
        self.assertEqual(reopened.query["scientificName"], "Opiliones")
        self.assertEqual(match_col.call_args.args[0], [{"kingdom": "Animalia", "scientificName": "Opiliones"}])

    def test_matching_the_same_labels_from_another_column_reopens_decisions(self, match_col, aids, release):
        rows = {row.verbatim_label: row for row in self.record()}
        taxon_matching.decide(rows["Dinychus sp."], TaxonNameMatch.Decision.ACCEPTED)
        self.table.df["originalName"] = self.table.df["verbatimIdentification"]
        self.table.save()
        taxon_matching.record_matches(self.dataset, "occurrence", self.table.df, "originalName")
        row = TaxonNameMatch.objects.get(verbatim_label="Dinychus sp.")
        self.assertEqual(row.verbatim_column, "originalName")
        self.assertEqual(row.decision, "pending")

    def test_labels_no_longer_in_the_table_drop_to_zero_records(self, match_col, aids, release):
        self.record()
        self.table.df = self.table.df[self.table.df.verbatimIdentification != "Pająki"]
        self.table.save()
        self.record()
        self.assertEqual(TaxonNameMatch.objects.get(verbatim_label="Pająki").record_count, 0)

    def test_bulk_accept_takes_only_exact_uninterpreted_unqualified_matches(self, match_col, aids, release):
        self.table.df = pd.concat([self.table.df, pd.DataFrame({
            "occurrenceID": ["o7"], "verbatimIdentification": ["Dinychus"], "scientificName": ["Dinychus"],
        })], ignore_index=True)
        self.table.save()
        self.record(overrides={
            ("N_silvestris", ""): {"query": {"scientificName": "Nothrus silvestris"}, "note": "Nothridae block"},
        })
        accepted = taxon_matching.accept_exact_matches(self.dataset)
        # Not the interpreted N_silvestris, not the qualified "Dinychus sp.".
        self.assertEqual([row.verbatim_label for row in accepted], ["Dinychus"])

    def test_bulk_accept_skips_a_match_made_obsolete_by_a_new_interpretation(self, match_col, aids, release):
        self.table.df = pd.DataFrame({"verbatimIdentification": ["Araneae"]})
        self.table.save()
        self.record()
        # The new interpretation's match is cut short by the time budget: the old EXACT match remains.
        match_col.side_effect = taxon_matching.TaxonServiceError("budget")
        with self.assertRaises(taxon_matching.TaxonServiceError):
            self.record(overrides={("Araneae", ""): {"query": {"scientificName": "Opiliones"}, "note": "x"}})
        row = TaxonNameMatch.objects.get(verbatim_label="Araneae")
        self.assertEqual(row.match["matchType"], "EXACT")
        self.assertIsNone(row.matched_at)
        self.assertEqual(taxon_matching.accept_exact_matches(self.dataset), [])
        with self.assertRaises(ValueError):
            taxon_matching.decide(row, TaxonNameMatch.Decision.ACCEPTED)

    def test_decisions(self, match_col, aids, release):
        rows = {row.verbatim_label: row for row in self.record()}
        neodiscopoma = taxon_matching.decide(
            rows["Neodiscopoma splendida (Kramer, 1882)"], TaxonNameMatch.Decision.NOT_IN_COL,
            name={"scientificName": "Neodiscopoma splendida", "scientificNameAuthorship": "(Kramer, 1882)",
                  "taxonRank": "Species"},
        )
        self.assertEqual(neodiscopoma.decided_usage["scientificName"], "Neodiscopoma splendida")
        self.assertEqual(neodiscopoma.decided_usage["taxonRank"], "species")
        self.assertEqual(neodiscopoma.decided_usage["classification"]["class"], "Arachnida")
        with self.assertRaises(ValueError):
            taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.ACCEPTED)  # nothing suggested

        with patch("api.taxon_matching.resolve_col_usage", return_value={
            "id": "5W", "scientificName": "Araneae", "scientificNameAuthorship": None,
            "taxonRank": "order", "status": "accepted", "classification": {"order": "Araneae"},
        }) as resolve:
            spiders = taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.ACCEPTED, usage_id="5W")
        resolve.assert_called_once_with("5W")
        self.assertEqual(spiders.decided_usage["source"], "checklistbank_xr")

        reset = taxon_matching.decide(spiders, TaxonNameMatch.Decision.PENDING)
        self.assertEqual(reset.decided_usage, {})
        self.assertIsNone(reset.decided_at)

    def test_apply_never_blanks_or_overwrites_supplied_taxonomy(self, match_col, aids, release):
        self.table.df = pd.DataFrame({
            "verbatimIdentification": ["Dinychus sp.", "Dinychus sp."],
            "scientificNameAuthorship": ["Kramer", None],
            "kingdom": ["Metazoa", None],
        })
        self.table.title = "species labels"
        self.table.save()
        taxon_matching.record_matches(self.dataset, "species labels", self.table.df, "verbatimIdentification")
        row = TaxonNameMatch.objects.get(verbatim_label="Dinychus sp.")
        taxon_matching.decide(row, TaxonNameMatch.Decision.NOT_IN_COL, name={"scientificName": "Dinychus"})
        taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification")
        self.table.refresh_from_db()
        df = self.table.df
        self.assertEqual(list(df.scientificName), ["Dinychus", "Dinychus"])
        self.assertEqual(df.scientificNameAuthorship[0], "Kramer")  # no authorship decided: kept
        self.assertEqual(df.kingdom[0], "Metazoa")  # supplied classification is never overwritten

    def test_duplicate_index_and_context_are_read_positionally(self, match_col, aids, release):
        df = pd.DataFrame(
            {"verbatimIdentification": ["P. nitens", "P. nitens", "P. nitens"], "block": ["A", "B", "A"]},
            index=[0, 0, 1],
        )
        self.assertEqual(
            taxon_matching.distinct_labels(df, "verbatimIdentification", "block"),
            {("P. nitens", "A"): 2, ("P. nitens", "B"): 1},
        )

    def test_decisions_are_scoped_to_table_and_context_configuration(self, match_col, aids, release):
        rows = {row.verbatim_label: row for row in self.record()}
        taxon_matching.decide(rows["Dinychus sp."], TaxonNameMatch.Decision.ACCEPTED)
        # Matching another table leaves this table's labels alone.
        other = pd.DataFrame({"verbatimIdentification": ["Araneae"]})
        taxon_matching.record_matches(self.dataset, "identification", other, "verbatimIdentification")
        self.assertEqual(TaxonNameMatch.objects.get(source_table="occurrence", verbatim_label="Dinychus sp.").record_count, 1)
        # A decision made without a context column cannot be applied with one, or vice versa.
        self.table.df["block"] = "Mesostigmata"
        self.table.save()
        with self.assertRaises(taxon_matching.MatchScopeError):
            taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification", "block")
        with self.assertRaises(taxon_matching.MatchScopeError):
            taxon_matching.record_matches(self.dataset, "occurrence", self.table.df, "verbatimIdentification", "block")
        # A working table that was never matched gets nothing applied.
        working = Table.objects.create(dataset=self.dataset, title="species labels", df=self.table.df.copy())
        result = taxon_matching.apply_decisions(self.dataset, working, "verbatimIdentification")
        self.assertEqual(result["labels_applied"], 0)
        self.assertEqual(result["unmatched_labels"], 4)

    def test_time_budget_saves_progress_for_the_next_call(self, match_col, aids, release):
        big = pd.DataFrame({"verbatimIdentification": [f"Label {i}" for i in range(60)]})
        calls = []

        def budget_after_two_chunks(queries, deadline=None):
            calls.append(len(queries))
            if len(calls) > 2:
                raise taxon_matching.TaxonServiceError("budget")
            return fake_match(queries)

        match_col.side_effect = budget_after_two_chunks
        rows, finished = taxon_matching.record_matches(self.dataset, "big", big, "verbatimIdentification")
        self.assertFalse(finished)
        self.assertEqual(calls, [25, 25, 10])
        self.assertEqual(sum(1 for row in rows if row.matched_at), 50)
        self.assertFalse(taxon_matching.matching_complete(rows))

        match_col.side_effect = fake_match
        rows, finished = taxon_matching.record_matches(self.dataset, "big", big, "verbatimIdentification")
        self.assertTrue(finished)
        self.assertEqual(len(match_col.call_args.args[0]), 10)
        self.assertTrue(taxon_matching.matching_complete(rows))

    def test_unfinished_review_aids_resume_on_the_next_call(self, match_col, aids, release):
        aids.side_effect = taxon_matching.TaxonServiceError("budget")
        rows, finished = taxon_matching.record_matches(self.dataset, "occurrence", self.table.df, "verbatimIdentification")
        self.assertFalse(finished)  # primary matches were saved, aids were not
        self.assertTrue(all(row.matched_at for row in rows))
        self.assertFalse(taxon_matching.matching_complete(rows))
        aids.side_effect = fake_aids
        match_col.reset_mock()
        rows, finished = taxon_matching.record_matches(self.dataset, "occurrence", self.table.df, "verbatimIdentification")
        self.assertTrue(finished)
        match_col.assert_not_called()
        self.assertTrue(taxon_matching.matching_complete(rows))
        self.assertIn("gbifBackbone", TaxonNameMatch.objects.get(verbatim_label="Pająki").review_aids)

    def test_service_down_before_any_progress_raises(self, match_col, aids, release):
        match_col.side_effect = taxon_matching.TaxonServiceError("down")
        with self.assertRaises(taxon_matching.TaxonServiceError):
            taxon_matching.record_matches(self.dataset, "occurrence", self.table.df, "verbatimIdentification")

    def test_apply_writes_only_reviewed_names_within_the_schema(self, match_col, aids, release):
        rows = {row.verbatim_label: row for row in self.record()}
        taxon_matching.decide(rows["Dinychus sp."], TaxonNameMatch.Decision.ACCEPTED)
        taxon_matching.decide(
            rows["Neodiscopoma splendida (Kramer, 1882)"], TaxonNameMatch.Decision.NOT_IN_COL,
            name={"scientificName": "Neodiscopoma splendida", "scientificNameAuthorship": "(Kramer, 1882)",
                  "taxonRank": "species"},
        )
        taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.KEEP_ORIGINAL)

        result = taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification")
        self.table.refresh_from_db()
        df = self.table.df.set_index("occurrenceID")

        self.assertEqual(result["labels_applied"], 2)
        self.assertEqual(result["rows_updated"], 2)
        self.assertEqual(result["pending_labels"], 1)
        self.assertEqual(result["kept_labels"], 1)
        # The occurrence resource has no classification or qualifier columns.
        self.assertIn("kingdom", result["skipped_columns"])
        self.assertIn("identificationQualifier", result["skipped_columns"])
        self.assertNotIn("kingdom", df.columns)
        self.assertEqual(df.loc["o5", "scientificName"], "Dinychus")
        self.assertEqual(df.loc["o5", "taxonRank"], "genus")
        self.assertEqual(df.loc["o3", "scientificNameAuthorship"], "(Kramer, 1882)")
        self.assertTrue(pd.isna(df.loc["o1", "scientificName"]))  # pending
        self.assertTrue(pd.isna(df.loc["o4", "scientificName"]))  # kept
        self.assertEqual(df.loc["o3", "verbatimIdentification"], "Neodiscopoma splendida (Kramer, 1882)")
        self.assertIsNotNone(TaxonNameMatch.objects.get(verbatim_label="Dinychus sp.").applied_at)

    def test_apply_to_a_working_table_writes_classification(self, match_col, aids, release):
        working = Table.objects.create(dataset=self.dataset, title="species labels", df=self.table.df.copy())
        taxon_matching.record_matches(self.dataset, "species labels", working.df, "verbatimIdentification")
        row = TaxonNameMatch.objects.get(source_table="species labels", verbatim_label="Dinychus sp.")
        taxon_matching.decide(row, TaxonNameMatch.Decision.ACCEPTED)
        result = taxon_matching.apply_decisions(self.dataset, working, "verbatimIdentification")
        working.refresh_from_db()
        row = working.df.set_index("occurrenceID").loc["o5"]
        self.assertEqual(result["skipped_columns"], [])
        self.assertEqual(row["kingdom"], "Animalia")
        self.assertEqual(row["identificationQualifier"], "sp.")

    def test_context_column_separates_identical_labels(self, match_col, aids, release):
        self.table.df = pd.DataFrame({
            "verbatimIdentification": ["P. nitens", "P. nitens"],
            "block": ["Ptyctima", "Other"],
        })
        self.table.save()
        taxon_matching.record_matches(self.dataset, "occurrence", self.table.df, "verbatimIdentification", "block")
        self.assertEqual(
            sorted(TaxonNameMatch.objects.filter(verbatim_label="P. nitens").values_list("context_key", flat=True)),
            ["Other", "Ptyctima"],
        )


@patch("api.taxon_matching.col_release", return_value=RELEASE)
@patch("api.taxon_matching.review_aids", side_effect=fake_aids)
@patch("api.taxon_matching.match_col", side_effect=fake_match)
class TaxonToolTests(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(title="Soil fauna")
        self.agent = Agent.objects.create(
            dataset=self.dataset, task=Task.objects.create(name="Data validation and refinement", text="x"),
        )
        self.table = Table.objects.create(dataset=self.dataset, title="occurrence", df=pd.DataFrame({
            "verbatimIdentification": ["N_silvestris", "Dinychus sp.", "Pająki"],
            "scientificName": [None, "Dinychus", None],
        }))

    def test_match_reports_open_labels_and_ignores_unknown_interpretations(self, match_col, aids, release):
        result = MatchTaxonNames(
            agent_id=self.agent.id, table_id=self.table.id, kingdom="Animalia",
            interpretations=[
                {"verbatim_label": "N_silvestris", "query_name": "Nothrus silvestris", "family": "Nothridae",
                 "note": "Nothridae column block"},
                {"verbatim_label": "Not a label", "query_name": "Araneae", "note": "x"},
            ],
        ).run()
        self.assertIn("COL26.6 XR", result)
        self.assertIn("3 distinct labels, 3 rows", result)
        self.assertIn("ignored because the label is not in the table: 'Not a label'", result)
        self.assertIn('"Pająki", 1 rows: NONE -> no suggestion', result)
        self.assertNotIn('"N_silvestris", 1 rows', result)  # exact after interpretation
        row = TaxonNameMatch.objects.get(verbatim_label="N_silvestris")
        self.assertEqual(row.query, {"kingdom": "Animalia", "scientificName": "Nothrus silvestris", "family": "Nothridae"})

    def test_apply_tool_reports_pending_labels(self, match_col, aids, release):
        MatchTaxonNames(agent_id=self.agent.id, table_id=self.table.id).run()
        taxon_matching.decide(TaxonNameMatch.objects.get(verbatim_label="Dinychus sp."), "accepted")
        result = ApplyTaxonDecisions(agent_id=self.agent.id, table_id=self.table.id).run()
        self.assertIn("Applied reviewed names for 1 labels to 1 rows", result)
        self.assertIn("2 labels (2 rows) are still pending review", result)
        self.assertIn("1 labels have an identification qualifier", result)

    def test_request_review_requires_finished_matching_for_its_scope(self, match_col, aids, release):
        review = RequestTaxonReview(agent_id=self.agent.id, table_id=self.table.id, message="Please review")
        self.assertIn("MatchTaxonNames first", review.run())
        aids.side_effect = taxon_matching.TaxonServiceError("budget")
        self.assertIn("PARTIAL", MatchTaxonNames(agent_id=self.agent.id, table_id=self.table.id).run())
        self.assertIn("matching is not finished", review.run())
        aids.side_effect = fake_aids
        MatchTaxonNames(agent_id=self.agent.id, table_id=self.table.id).run()
        self.assertEqual(json.loads(review.run()), {
            "status": "awaiting_taxon_review", "source_table": "occurrence", "context_column": "",
            "labels": 3, "pending_labels": 3,
        })
        other_context = RequestTaxonReview(
            agent_id=self.agent.id, table_id=self.table.id, context_column="block", message="x",
        )
        self.assertIn("MatchTaxonNames first", other_context.run())

    def test_rejects_tables_from_other_datasets(self, match_col, aids, release):
        other = Table.objects.create(dataset=Dataset.objects.create(title="Other"), title="occurrence",
                                     df=pd.DataFrame({"verbatimIdentification": ["x"]}))
        self.assertIn("does not belong", MatchTaxonNames(agent_id=self.agent.id, table_id=other.id).run())
        self.assertIn("does not belong", ApplyTaxonDecisions(agent_id=self.agent.id, table_id=other.id).run())


class TaxonMatchApiTests(TestCase):
    def setUp(self):
        self.owner = CustomUser.objects.create_user(username="owner", password="x")
        self.other = CustomUser.objects.create_user(username="other", password="x")
        self.dataset = Dataset.objects.create(title="Soil fauna", user=self.owner)
        base = {"dataset": self.dataset, "source_table": "occurrence", "record_count": 3,
                "query": {"scientificName": "Dinychus"}, "matched_at": timezone.now()}
        self.exact = TaxonNameMatch.objects.create(
            verbatim_label="Dinychus", match=summary("EXACT", "DNY", "Dinychus", rank="GENUS"), **base,
        )
        self.higher = TaxonNameMatch.objects.create(
            verbatim_label="Neodiscopoma splendida",
            match=summary("HIGHERRANK", "VNH95", "Neodiscopoma", rank="GENUS"),
            **{**base, "query": {"scientificName": "Neodiscopoma splendida"}},
        )
        TaxonNameMatch.objects.create(verbatim_label="gone", **{**base, "record_count": 0})
        self.agent = Agent.objects.create(
            dataset=self.dataset, task=Task.objects.create(name="Data validation and refinement", text="x"),
        )
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant", "content": "Please review.",
            "taxon_review": {"source_table": "occurrence", "context_column": ""},
        })
        self.client = APIClient()

    def test_owner_lists_current_labels_only(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get("/api/taxon-matches/", {"dataset": self.dataset.id})
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"] if isinstance(response.json(), dict) else response.json()
        self.assertEqual({row["verbatim_label"] for row in results}, {"Dinychus", "Neodiscopoma splendida"})

    def test_other_users_cannot_see_or_decide(self):
        self.client.force_authenticate(self.other)
        response = self.client.get("/api/taxon-matches/", {"dataset": self.dataset.id})
        results = response.json()["results"] if isinstance(response.json(), dict) else response.json()
        self.assertEqual(results, [])
        response = self.client.post(f"/api/taxon-matches/{self.exact.id}/decide/", {"decision": "accepted"})
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/api/taxon-matches/accept-exact/", {"dataset": self.dataset.id})
        self.assertEqual(response.status_code, 404)

    def test_decide_and_bulk_accept(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f"/api/taxon-matches/{self.higher.id}/decide/",
            {"decision": "not_in_col", "scientificName": "Neodiscopoma splendida", "taxonRank": "species"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["decision"], "not_in_col")
        self.assertEqual(response.json()["decided_by"], self.owner.id)

        response = self.client.post(f"/api/taxon-matches/{self.higher.id}/decide/", {"decision": "bogus"})
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/api/taxon-matches/accept-exact/", {"dataset": self.dataset.id})
        self.assertEqual(response.json()["accepted"], 1)
        self.exact.refresh_from_db()
        self.assertEqual(self.exact.decision, "accepted")

    def test_decisions_are_refused_once_the_review_is_closed(self):
        self.client.force_authenticate(self.owner)
        Message.objects.create(agent=self.agent, openai_obj={"role": "user", "content": "Done"})
        response = self.client.post(f"/api/taxon-matches/{self.exact.id}/decide/", {"decision": "accepted"})
        self.assertEqual(response.status_code, 409)
        response = self.client.post("/api/taxon-matches/accept-exact/", {"dataset": self.dataset.id})
        self.assertEqual(response.status_code, 409)
        self.exact.refresh_from_db()
        self.assertEqual(self.exact.decision, "pending")

    def test_decisions_are_refused_for_another_table_than_the_open_review(self):
        self.client.force_authenticate(self.owner)
        other = TaxonNameMatch.objects.create(
            dataset=self.dataset, source_table="identification", verbatim_label="Dinychus", record_count=1,
            query={"scientificName": "Dinychus"}, matched_at=timezone.now(),
            match=summary("EXACT", "DNY", "Dinychus", rank="GENUS"),
        )
        response = self.client.post(f"/api/taxon-matches/{other.id}/decide/", {"decision": "accepted"})
        self.assertEqual(response.status_code, 409)

    @patch("api.taxon_matching.search_col", return_value=[{"id": "C3DM4", "label": "Cryptognathidae"}])
    def test_search_requires_three_characters(self, search):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get("/api/taxon-matches/search/", {"q": "Cr"}).json(), {"results": []})
        response = self.client.get("/api/taxon-matches/search/", {"q": "Cryptognath"})
        self.assertEqual(response.json()["results"][0]["id"], "C3DM4")
        search.assert_called_once_with("Cryptognath")


@patch("api.views.ensure_dataset_work")
class FinishReviewApiTests(TestCase):
    def setUp(self):
        self.owner = CustomUser.objects.create_user(username="owner", password="x")
        self.dataset = Dataset.objects.create(title="Soil fauna", user=self.owner)
        self.agent = Agent.objects.create(
            dataset=self.dataset, task=Task.objects.create(name="Data validation and refinement", text="x"),
        )
        self.table = Table.objects.create(dataset=self.dataset, title="occurrence", df=pd.DataFrame({
            "species": ["Dinychus", "Dinychus", "Pająki", "Zieminek"],
        }))
        base = {"dataset": self.dataset, "source_table": "occurrence", "verbatim_column": "species",
                "matched_at": timezone.now()}
        self.dinychus = TaxonNameMatch.objects.create(
            verbatim_label="Dinychus", record_count=2, query={"scientificName": "Dinychus"},
            match=summary("EXACT", "DNY", "Dinychus", rank="GENUS", authorship="Kramer, 1886"), **base,
        )
        TaxonNameMatch.objects.create(verbatim_label="Pająki", record_count=1, decision="keep_original", **base)
        TaxonNameMatch.objects.create(verbatim_label="Zieminek", record_count=1, **base)
        taxon_matching.decide(self.dinychus, TaxonNameMatch.Decision.ACCEPTED)
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant", "content": "Please review.",
            "taxon_review": {"source_table": "occurrence", "context_column": ""},
        })
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def finish(self):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/taxon-matches/finish-review/", {"agent": self.agent.id}, format="json")

    def test_applies_decisions_and_hands_the_outcome_to_the_agent(self, ensure_work):
        response = self.finish()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["applied"])
        self.table.refresh_from_db()
        self.assertEqual(list(self.table.df.scientificName[:2]), ["Dinychus", "Dinychus"])
        self.assertEqual(list(self.table.df.species), ["Dinychus", "Dinychus", "Pająki", "Zieminek"])

        message = self.agent.message_set.last()
        self.assertEqual(message.role, Message.Role.USER)
        content = message.openai_obj["content"]
        self.assertTrue(content.startswith(
            "I have finished reviewing the taxon names: 1 name accepted, 1 name kept unchanged. "
            "1 name (1 record) is left unreviewed."
        ))
        self.assertIn("[NOTE: ChatIPT applied the reviewed taxon names itself", content)
        self.assertIn("Applied reviewed names for 1 labels to 2 rows", content)
        self.assertEqual(response.json()["message"]["id"], message.id)
        ensure_work.assert_called_once_with(self.dataset.id)

        # A second press does not apply or message again.
        self.assertEqual(self.finish().status_code, 409)
        self.assertEqual(ensure_work.call_count, 1)

    def test_falls_back_to_the_agent_when_the_table_is_ambiguous(self, ensure_work):
        Table.objects.create(dataset=self.dataset, title="occurrence", df=self.table.df.copy())
        response = self.finish()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["applied"])
        content = self.agent.message_set.last().openai_obj["content"]
        self.assertIn("could not be applied automatically (expected one `occurrence` table but found 2)", content)
        self.assertIn("Call ApplyTaxonDecisions for the `occurrence` table", content)
        ensure_work.assert_called_once()

    def test_labels_matched_before_the_column_was_recorded_fall_back_to_the_agent(self, ensure_work):
        TaxonNameMatch.objects.filter(dataset=self.dataset).update(verbatim_column="")
        response = self.finish()
        self.assertFalse(response.json()["applied"])
        self.assertIn("do not record one source column", self.agent.message_set.last().openai_obj["content"])
        self.table.refresh_from_db()
        self.assertNotIn("scientificName", self.table.df.columns)

    def test_reviewed_labels_missing_from_the_table_fall_back_without_writing(self, ensure_work):
        self.table.df = pd.DataFrame({"species": ["Pająki", "Zieminek"], "scientificName": [None, None]})
        self.table.save()
        response = self.finish()
        self.assertFalse(response.json()["applied"])
        content = self.agent.message_set.last().openai_obj["content"]
        self.assertIn("1 reviewed labels, e.g. 'Dinychus', are no longer in column `species`", content)
        self.table.refresh_from_db()
        self.assertTrue(self.table.df.scientificName.isna().all())

    def test_a_decision_after_done_is_refused(self, ensure_work):
        self.finish()
        pending = TaxonNameMatch.objects.get(verbatim_label="Zieminek")
        response = self.client.post(f"/api/taxon-matches/{pending.id}/decide/", {"decision": "keep_original"})
        self.assertEqual(response.status_code, 409)
        pending.refresh_from_db()
        self.assertEqual(pending.decision, "pending")

    def test_refuses_when_the_agent_is_not_waiting_for_this_review(self, ensure_work):
        self.agent.busy_thinking = True
        self.agent.save()
        self.assertEqual(self.finish().status_code, 409)
        self.agent.busy_thinking = False
        self.agent.save()
        Message.objects.create(agent=self.agent, openai_obj={"role": "assistant", "content": "Something else"})
        self.assertEqual(self.finish().status_code, 409)
        ensure_work.assert_not_called()

    def test_other_users_cannot_finish(self, ensure_work):
        self.client.force_authenticate(CustomUser.objects.create_user(username="other", password="x"))
        self.assertEqual(self.finish().status_code, 404)
        ensure_work.assert_not_called()
