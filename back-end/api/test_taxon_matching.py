import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api import schema_ledger, taxon_matching
from api.helpers.openai_helpers import CompatAssistantMessage
from api.agent_tools import ApplyTaxonDecisions, MatchTaxonNames, PublishToGBIF, RequestTaxonReview, SetAgentTaskToComplete
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

    def test_match_diagnostics_keep_issues_and_matched_id(self):
        result = taxon_matching.summarize_match({"diagnostics": {
            "matchType": "EXACT", "issues": ["TAXON_ID_NOT_FOUND"],
            "matchedID": {"id": "urn:lsid:x:1", "scientificName": "Aus bus", "datasetTitle": "Example"}}})
        self.assertEqual(result["issues"], ["TAXON_ID_NOT_FOUND"])
        self.assertEqual(result["matchedId"], {"id": "urn:lsid:x:1", "scientificName": "Aus bus", "datasetTitle": "Example"})
        self.assertEqual(taxon_matching.summarize_match({"diagnostics": {}})["issues"], [])
        self.assertIsNone(taxon_matching.summarize_match({"diagnostics": {}})["matchedId"])


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

    def test_a_subgenus_is_not_inserted_into_a_species_name(self):
        # Live v2 for "Acartia longiremis" (EXACT): name "Acartia (Acartiura) longiremis (Lilljeborg, 1853)".
        acartia = {"usage": {"key": "93B9", "name": "Acartia (Acartiura) longiremis (Lilljeborg, 1853)", "canonicalName": "Acartia longiremis",
                             "authorship": "(Lilljeborg, 1853)", "rank": "SPECIES", "status": "ACCEPTED"},
                   "diagnostics": {"matchType": "EXACT"}}
        usage = taxon_matching.summarize_match(acartia)["usage"]
        self.assertEqual((usage["scientificName"], usage["scientificNameAuthorship"]), ("Acartia longiremis", "(Lilljeborg, 1853)"))
        subgenus = taxon_matching._name_without_authorship(
            {"name": "Acartia (Acartiura) Steuer, 1915", "authorship": "Steuer, 1915", "rank": "SUBGENUS"})
        self.assertEqual(subgenus, "Acartia (Acartiura)")
        row = SimpleNamespace(decision=TaxonNameMatch.Decision.PENDING, record_count=1, matched_at=timezone.now(),
                              match=taxon_matching.summarize_match(acartia), identification_qualifier="",
                              query={"scientificName": "Acartia longiremis"}, verbatim_label="Acartia longiremis",
                              parsed={}, supplied={})
        self.assertTrue(taxon_matching.bulk_acceptable(row))
        self.assertIsNone(taxon_matching.choice_check(row, usage, "EXACT")["replaces"])


    def test_missing_col_rank_marker_is_added_but_explicit_markers_remain_changes(self):
        self.assertEqual(taxon_matching.NAME_RULES_VERSION, 3)
        empetrum = taxon_matching.name_change(
            "Empetrum nigrum hermaphroditum",
            {"scientificName": "Empetrum nigrum subsp. hermaphroditum", "taxonRank": "subspecies"},
            "EXACT",
        )
        self.assertIsNone(empetrum)
        carex = taxon_matching.name_change(
            "Carex nigra subsp. juncea",
            {"scientificName": "Carex nigra var. juncea", "taxonRank": "variety"}, "EXACT")
        self.assertEqual((carex["kind"], carex["confirm"]), ("marker", True))
        hybrid = taxon_matching.name_change(
            "Rosa × canina", {"scientificName": "Rosa canina", "taxonRank": "species"}, "EXACT")
        self.assertEqual((hybrid["kind"], hybrid["confirm"]), ("marker", True))

    def test_a_hybrid_formula_without_authorship_drops_a_stray_author(self):
        self.assertEqual(taxon_matching._name_without_authorship(
            {"name": "Viola adunca × Viola labradorica Linnaeus", "canonicalName": "Viola adunca × Viola labradorica"}),
            "Viola adunca × Viola labradorica")
        self.assertEqual(taxon_matching._name_without_authorship(
            {"name": "Betula pubescens subsp. pubescens", "canonicalName": "Betula pubescens pubescens"}),
            "Betula pubescens subsp. pubescens")

    def test_authorships_agree_across_initials_and_abbreviations_but_not_other_authors_or_years(self):
        # Pairs from conversions 559/572 (spiders) and 568 (reptiles and amphibians): supplied, COL.
        agree = [
            ("(O.P.-Cambridge, 1871)", "(O. Pickard-Cambridge, 1871)"),
            ("(O.P.Cambridge, 1875)", "(O.P.-Cambridge, 1875)"),
            ("(L.Koch, 1879)", "(L. Koch, 1879)"),
            ("(C.L. Koch, 1844)", "(C. L. Koch)"),
            ("Koch, 1838", "C.L. Koch, 1838"),  # initials on one side: the same full surname and year
            ("L.", "Linnaeus, 1758"),
            ("DC.", "de Candolle"),
            ("Lam.", "Lamarck"),
            ("Fabr.", "Fabricius"),
            ("Mill.", "Miller"),
            ("Hook.", "Hooker"),
            ("Willd.", "Willdenow"),
            ("Pers.", "Persoon"),
            ("Lamour.", "Lamouroux"),
            ("Lesson, [1830]", "Lesson, 1830"),
            ("Fitzinger, 1838", "Fitzinger in Bonaparte, 1838"),
            ("(Müller, 1836)", "(Müller in Van Oort & Müller, 1836)"),
            ("Welw. ex Ficalho", "Ficalho"),
            ("Smith et al., 2001", "Smith, Jones & Brown, 2001"),
            ("Hämet-Ahti", "Hamet-Ahti"),
            ("L.f.", "L. f."),
            ("F.O.P-Cambridge, 1894", "F. O. Pickard-Cambridge, 1894"),
            ("(Duméril, Bibron & Duméril, 1854)", "(A. M. C. Duméril, Bibron & A. H. A. Duméril, 1854)"),
            ("(Bocage, 1866)", "(Barboza du Bocage, 1866)"),
        ]
        disagree = [
            ("(Blackwall, 1841)", "Seo, 2017"),
            ("(Hahn, 1832)", "(Hahn, 1831)"),
            ("(O.P.-Cambridge, 1875)", "(Westring, 1861)"),
            ("Lichtenstein & Martens, 1856", "Lichtenstein, 1856"),
            ("Smith", "Jones"),
            ("Smith", ""),
            # Short abbreviations, other initials and filius name other authors (review of the first loosening).
            ("L.", "Lam."), ("L.", "Lamarck"), ("L.", "Lindl."), ("L.", "Ledeb."), ("L.", "Lesson, 1830"),
            ("S.", "Smith"), ("Fr.", "Franch."), ("Sm.", "Smirnov"), ("L.f.", "Fabricius"), ("L.f.", "L."),
            ("A.Gray", "Gray"), ("J.E. Gray, 1831", "G.R. Gray, 1831"), ("N.E.Br.", "R.Br."), ("DC.", "A.DC."),
            ("Rich.", "A.Rich."),
            ("Lam.", "Lamouroux"), ("Fabr.", "Fabre"), ("Lin.", "Lindley"), ("Lin.", "Linnaeus"),
            ("L. Koch, 1843", "C. L. Koch, 1843"),  # Ludwig Koch and Carl Ludwig Koch (559)
        ]
        for supplied, col in agree:
            with self.subTest(supplied=supplied, col=col):
                self.assertTrue(taxon_matching.authorships_agree(supplied, col))
                self.assertTrue(taxon_matching.authorships_agree(col, supplied))
        for supplied, col in disagree:
            with self.subTest(supplied=supplied, col=col):
                self.assertFalse(taxon_matching.authorships_agree(supplied, col))
                self.assertFalse(taxon_matching.authorships_agree(col, supplied))

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
                                   identification_qualifier="", query=query, verbatim_label=query["scientificName"],
                                   parsed={}, supplied={})
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
    def test_exact_names_can_be_fetched_again_for_their_homonyms(self, get_json):
        exact = {"diagnostics": {"matchType": "EXACT"}, "usage": {"key": "1", "name": "Sterna", "rank": "GENUS"}}
        get_json.side_effect = [[exact, {**exact, "usage": {"key": "2", "name": "Sterna hirundo", "rank": "SPECIES"}}], exact]
        get_json.side_effect = [[exact, {**exact, "usage": {"key": "2", "name": "Sterna hirundo", "rank": "SPECIES"}}], exact, exact]
        taxon_matching.match_col([{"scientificName": "Sterna"}, {"scientificName": "Sterna hirundo"}], verbose_exact=True)
        verbose = get_json.call_args_list[1:]
        self.assertEqual(sorted(call.kwargs["params"]["scientificName"] for call in verbose), ["Sterna", "Sterna hirundo"])
        get_json.reset_mock(side_effect=True)
        # The batch's exact pick stands even when the verbose answer differs; only the homonyms are taken from it.
        verbose_answer = {"diagnostics": {"matchType": "HIGHERRANK", "alternatives": [
            {"usage": {"key": "9", "name": "Sterna Albers, 1850", "authorship": "Albers, 1850", "rank": "GENUS"},
             "diagnostics": {"matchType": "EXACT"}}]}, "usage": {"key": "A", "name": "Animalia", "rank": "KINGDOM"}}
        get_json.side_effect = [[exact], verbose_answer]
        summary = taxon_matching.match_col([{"scientificName": "Sterna"}], verbose_exact=True)[0]
        self.assertEqual((summary["matchType"], summary["usage"]["id"]), ("EXACT", "1"))
        self.assertEqual([alternative["id"] for alternative in summary["alternatives"]], ["A", "9"])
        get_json.reset_mock(side_effect=True)
        # A verbose pick of another exact usage of the same name joins the homonyms.
        jones = {"diagnostics": {"matchType": "EXACT"}, "usage": {"key": "J", "name": "Sterna Jones", "authorship": "Jones", "rank": "GENUS"}}
        get_json.side_effect = [[exact], jones]
        summary = taxon_matching.match_col([{"scientificName": "Sterna"}], verbose_exact=True)[0]
        self.assertEqual((summary["usage"]["id"], [(item["id"], item["matchType"]) for item in summary["alternatives"]]), ("1", [("J", "EXACT")]))
        get_json.reset_mock(side_effect=True)
        # A variant pick is fetched again too, keeping the batch's pick.
        variant = {"diagnostics": {"matchType": "VARIANT"}, "usage": {"key": "V", "name": "Aus bus", "rank": "SPECIES"}}
        get_json.side_effect = [[variant], jones]
        summary = taxon_matching.match_col([{"scientificName": "Aus bus"}], verbose_exact=True)[0]
        self.assertEqual((summary["matchType"], summary["usage"]["id"], [item["id"] for item in summary["alternatives"]]), ("VARIANT", "V", ["J"]))
        self.assertEqual(verbose[0].kwargs["params"]["verbose"], "true")
        get_json.reset_mock(side_effect=True)
        get_json.side_effect = [[exact]]
        taxon_matching.match_col([{"scientificName": "Sterna"}])
        self.assertEqual(get_json.call_count, 1)

    def test_same_name_alternatives_of_any_match_type_are_kept(self):
        others = [{"usage": {"key": str(i), "name": f"Bus {i}", "rank": "GENUS"}, "diagnostics": {"matchType": "VARIANT"}} for i in range(8)]
        same = {"usage": {"key": "S", "name": "Aus Jones", "authorship": "Jones", "rank": "GENUS"}, "diagnostics": {"matchType": "VARIANT"}}
        summary = taxon_matching.summarize_match({"diagnostics": {"matchType": "EXACT", "alternatives": others + [same]}}, "Aus")
        self.assertIn("S", [item["id"] for item in summary["alternatives"]])

    def test_exact_alternatives_are_kept_beyond_the_display_limit_and_overflow_is_flagged(self):
        def alternative(key, match_type):
            return {"usage": {"key": key, "name": f"Aus {key}", "rank": "GENUS"}, "diagnostics": {"matchType": match_type}}
        payload = {"diagnostics": {"matchType": "EXACT", "alternatives": [alternative(str(i), "VARIANT") for i in range(8)]
                                   + [alternative(f"e{i}", "EXACT") for i in range(3)]}}
        summary = taxon_matching.summarize_match(payload)
        self.assertEqual(sum(item["matchType"] == "EXACT" for item in summary["alternatives"]), 3)
        self.assertEqual(sum(item["matchType"] == "VARIANT" for item in summary["alternatives"]), taxon_matching.MAX_ALTERNATIVES)
        self.assertFalse(summary["exactAlternativesDropped"])
        many = {"diagnostics": {"matchType": "EXACT", "alternatives": [alternative(f"e{i}", "EXACT") for i in range(25)]}}
        self.assertTrue(taxon_matching.summarize_match(many)["exactAlternativesDropped"])

    @patch("api.taxon_matching._get_json")
    def test_identifiers_are_sent_and_are_part_of_the_dedup_key(self, get_json):
        get_json.side_effect = [[{"diagnostics": {"matchType": "NONE"}}, {"diagnostics": {"matchType": "NONE"}}],
                                {"diagnostics": {"matchType": "NONE"}}, {"diagnostics": {"matchType": "NONE"}}]
        query = {"scientificName": "Aus bus", "scientificNameID": "urn:lsid:x:1", "taxonID": "https://x/2"}
        other = {**query, "taxonID": "https://x/3"}
        taxon_matching.match_col([query, dict(query), other])
        batch, *verbose = get_json.call_args_list
        self.assertEqual(len(batch.kwargs["json"]), 2)
        self.assertEqual({item["taxonID"] for item in batch.kwargs["json"]}, {query["taxonID"], other["taxonID"]})
        self.assertEqual(verbose[0].kwargs["params"]["scientificNameID"], query["scientificNameID"])
        self.assertIn(verbose[0].kwargs["params"]["taxonID"], {query["taxonID"], other["taxonID"]})

    @patch("api.taxon_matching._get_json")
    def test_review_aids_keeps_source_ids_out_of_checklistbank_query(self, get_json):
        get_json.side_effect = [{"diagnostics": {"matchType": "NONE"}}, {}]
        taxon_matching.review_aids({"scientificName": "Aus bus", "kingdom": "Animalia",
                                    "scientificNameID": "urn:lsid:x:1", "taxonID": "https://x/2"})
        clb_params = get_json.call_args_list[1].kwargs["params"]
        self.assertEqual(clb_params, {"q": "Aus bus", "kingdom": "Animalia"})

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


def fake_match(queries, deadline=None, verbose_exact=False):
    results = {
        "Nothrus silvestris": summary("EXACT", "74BRN", "Nothrus silvestris", authorship="Nicolet, 1855"),
        "Neodiscopoma splendida (Kramer, 1882)": summary("HIGHERRANK", "VNH95", "Neodiscopoma", rank="GENUS"),
        "Araneae": summary("EXACT", "5W", "Araneae", rank="ORDER"),
        "Dinychus": summary("EXACT", "DNY", "Dinychus", rank="GENUS", authorship="Kramer, 1886"),
    }
    return [results.get(query["scientificName"], summary("NONE")) for query in queries]


def fake_aids(query, deadline=None):
    return {"gbifBackbone": {"status": "exact", "scientificName": query["scientificName"], "taxonRank": "species"}}


def parser_unavailable(labels, deadline=None):
    raise taxon_matching.TaxonServiceError("parser down")


# Labels are matched without GBIF's parser unless a test gives one; "Keep original" then parses when it is chosen.
@patch("api.taxon_matching.parse_names", new=parser_unavailable)
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
            rows["Neodiscopoma splendida (Kramer, 1882)"], TaxonNameMatch.Decision.CORRECTED,
            scientific_name="  Neodiscopoma   splendida ",
        )
        # A corrected name is the typed name alone: no authorship, rank or classification is guessed for it.
        self.assertEqual(neodiscopoma.decided_usage["scientificName"], "Neodiscopoma splendida")
        self.assertEqual(neodiscopoma.decided_usage["source"], "reviewer")
        for typed in ("", "   ", "x" * (taxon_matching.MAX_CORRECTED_NAME_CHARS + 1)):
            with self.assertRaises(ValueError):
                taxon_matching.decide(rows["N_silvestris"], TaxonNameMatch.Decision.CORRECTED, scientific_name=typed)
        with self.assertRaises(ValueError):
            taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.ACCEPTED)  # nothing suggested

        with patch("api.taxon_matching.resolve_col_usage", return_value={
            "id": "5W", "scientificName": "Araneae", "scientificNameAuthorship": None,
            "taxonRank": "order", "status": "accepted", "classification": {"order": "Araneae"},
        }) as resolve:
            spiders = taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.ACCEPTED, usage_id="5W", confirm_coarser=True)
        resolve.assert_called_once_with("5W")
        self.assertEqual(spiders.decided_usage["source"], "col")

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
        taxon_matching.decide(row, TaxonNameMatch.Decision.CORRECTED, scientific_name="Dinychus")
        taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification")
        self.table.refresh_from_db()
        df = self.table.df
        self.assertEqual(list(df.scientificName), ["Dinychus", "Dinychus"])
        self.assertEqual(df.scientificNameAuthorship[0], "Kramer")  # no authorship decided: kept
        # A corrected name changes scientificName only; the label's "sp." stays in verbatimIdentification.
        self.assertNotIn("identificationQualifier", df.columns)
        self.assertEqual(df.kingdom[0], "Metazoa")  # supplied classification is never overwritten

    def test_keep_original_and_dont_publish_follow_the_conversions_rules(self, match_col, aids, release):
        self.table.df = pd.DataFrame({
            "verbatimIdentification": ["Neodiscopoma splendida (Kramer, 1882)", "Pająki", "Dinychus sp."],
            "scientificName": [None, "Araneae", "Dinychus"],
            "scientificNameAuthorship": [None, None, "Kramer"],
            "taxonRank": [None, "order", "genus"],
        })
        self.table.save()
        rows = {row.verbatim_label: row for row in self.record()}
        split = {"usable": True, "lossless": True, "canonical": "Neodiscopoma splendida", "authorship": "(Kramer, 1882)",
                 "rank": "species"}
        rows["Neodiscopoma splendida (Kramer, 1882)"].parsed = split
        with patch("api.taxon_matching.parse_names") as parse:
            neodiscopoma = taxon_matching.decide(rows["Neodiscopoma splendida (Kramer, 1882)"], TaxonNameMatch.Decision.KEEP_ORIGINAL)
        parse.assert_not_called()
        self.assertEqual(neodiscopoma.decided_usage["decision"], "parsed")
        # Without the parser the label is kept as written; a qualified label is never split.
        with patch("api.taxon_matching.parse_names", side_effect=taxon_matching.TaxonServiceError("down")):
            spiders = taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.KEEP_ORIGINAL)
        self.assertEqual(spiders.decided_usage["decision"], "keep")
        with patch("api.taxon_matching.parse_names") as parse:
            self.assertEqual(taxon_matching.decide(rows["Dinychus sp."], TaxonNameMatch.Decision.KEEP_ORIGINAL).decided_usage["decision"], "keep")
        parse.assert_not_called()
        dinychus = taxon_matching.decide(rows["Dinychus sp."], TaxonNameMatch.Decision.EMPTY)

        self.assertEqual(taxon_matching.decision_preview(neodiscopoma), [
            {"field": "scientificName", "how": "set", "value": "Neodiscopoma splendida"},
            {"field": "scientificNameAuthorship", "how": "yours", "value": "(Kramer, 1882)"},
            {"field": "taxonRank", "how": "set", "value": "species"},
        ])
        self.assertEqual(taxon_matching.decision_preview(spiders)[0], {"field": "scientificName", "how": "yours", "value": "Pająki"})
        self.assertEqual({item["how"] for item in taxon_matching.decision_preview(dinychus)}, {"empty"})

        taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification")
        self.table.refresh_from_db()
        df = self.table.df
        self.assertEqual(list(df.loc[0, ["scientificName", "scientificNameAuthorship", "taxonRank"]]),
                         ["Neodiscopoma splendida", "(Kramer, 1882)", "species"])
        self.assertEqual(df.loc[1, "scientificName"], "Araneae")  # kept: the table's own name stands
        self.assertTrue(all(pd.isna(df.loc[2, field]) for field in ("scientificName", "scientificNameAuthorship", "taxonRank")))

    def test_another_taxon_does_not_inherit_the_tables_authorship(self, match_col, aids, release):
        self.table.df = pd.DataFrame({"verbatimIdentification": ["Pająki"], "scientificName": ["Pająki"],
                                      "scientificNameAuthorship": ["Somebody"], "taxonRank": ["family"]})
        self.table.save()
        row = self.record()[0]
        with patch("api.taxon_matching.resolve_col_usage", return_value={
            "id": "5W", "scientificName": "Araneae", "scientificNameAuthorship": None,
            "taxonRank": "order", "status": "accepted", "classification": {"order": "Araneae"},
        }):
            taxon_matching.decide(row, TaxonNameMatch.Decision.ACCEPTED, usage_id="5W", confirm_coarser=True)
        taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification")
        self.table.refresh_from_db()
        self.assertEqual(list(self.table.df.loc[0, ["scientificName", "taxonRank"]]), ["Araneae", "order"])
        self.assertTrue(pd.isna(self.table.df.loc[0, "scientificNameAuthorship"]))

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
        rematched, finished = taxon_matching.record_matches(
            self.dataset, "occurrence", self.table.df, "verbatimIdentification", "block",
        )
        self.assertTrue(finished)
        self.assertTrue(all(row.decision == TaxonNameMatch.Decision.PENDING for row in rematched))
        with self.assertRaises(taxon_matching.MatchScopeError):
            taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification")
        # A working table that was never matched gets nothing applied.
        working = Table.objects.create(dataset=self.dataset, title="species labels", df=self.table.df.copy())
        result = taxon_matching.apply_decisions(self.dataset, working, "verbatimIdentification")
        self.assertEqual(result["labels_applied"], 0)
        self.assertEqual(result["unmatched_labels"], 4)

    def test_time_budget_saves_progress_for_the_next_call(self, match_col, aids, release):
        big = pd.DataFrame({"verbatimIdentification": [f"Label {i}" for i in range(60)]})
        calls = []

        def budget_after_two_chunks(queries, deadline=None, verbose_exact=False):
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
            rows["Neodiscopoma splendida (Kramer, 1882)"], TaxonNameMatch.Decision.CORRECTED,
            scientific_name="Neodiscopoma splendens",
        )
        with patch("api.taxon_matching.parse_names", return_value=[{"usable": False}]):
            taxon_matching.decide(rows["Pająki"], TaxonNameMatch.Decision.KEEP_ORIGINAL)

        result = taxon_matching.apply_decisions(self.dataset, self.table, "verbatimIdentification")
        self.table.refresh_from_db()
        df = self.table.df.set_index("occurrenceID")

        self.assertEqual(result["labels_applied"], 2)
        self.assertEqual(result["rows_updated"], 3)
        self.assertEqual(result["pending_labels"], 1)
        self.assertEqual(result["kept_labels"], 1)
        # The occurrence resource has no classification or qualifier columns.
        self.assertIn("kingdom", result["skipped_columns"])
        self.assertIn("identificationQualifier", result["skipped_columns"])
        self.assertNotIn("kingdom", df.columns)
        self.assertEqual(df.loc["o5", "scientificName"], "Dinychus")
        self.assertEqual(df.loc["o5", "taxonRank"], "genus")
        self.assertEqual(df.loc["o3", "scientificName"], "Neodiscopoma splendens")
        self.assertTrue(pd.isna(df.loc["o3", "scientificNameAuthorship"]))  # a corrected name writes no authorship
        self.assertTrue(pd.isna(df.loc["o1", "scientificName"]))  # pending
        self.assertEqual(df.loc["o4", "scientificName"], "Pająki")  # kept: the label fills an empty name
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


@patch("api.taxon_matching.parse_names", new=parser_unavailable)
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

    def test_rematch_splits_broad_label_by_source_scientific_name(self, match_col, aids, release):
        self.table.title = "identification"
        self.table.df = pd.DataFrame({
            "verbatimIdentification": ["Phycomycetes"] * 5,
            "scientificName": ["Taxon A", "Taxon B", "Taxon C", "Taxon D", "Taxon E"],
        })
        self.table.save()
        original = self.table.df.copy()
        first = MatchTaxonNames(
            agent_id=self.agent.id, table_id=self.table.id,
            interpretations=[{"verbatim_label": "Phycomycetes", "query_name": "Nothrus silvestris",
                              "note": "Earlier broad interpretation"}],
        ).run()
        self.assertIn("1 distinct labels, 5 rows", first)
        old = TaxonNameMatch.objects.get(dataset=self.dataset)
        taxon_matching.decide(old, TaxonNameMatch.Decision.CORRECTED, scientific_name="Old interpretation")
        result = MatchTaxonNames(
            agent_id=self.agent.id, table_id=self.table.id, context_column="scientificName",
            interpretations=[{"verbatim_label": "Phycomycetes", "context_key": "Taxon A",
                              "query_name": "Araneae", "note": "Source evidence for this group only"}],
        ).run()
        self.assertIn("5 distinct labels, 5 rows", result)
        old.refresh_from_db()
        self.assertEqual(old.record_count, 0)
        self.assertEqual(old.decision, TaxonNameMatch.Decision.CORRECTED)
        rows = list(TaxonNameMatch.objects.filter(dataset=self.dataset, record_count__gt=0))
        self.assertEqual({row.context_key for row in rows}, set(original.scientificName))
        self.assertTrue(all(row.decision == TaxonNameMatch.Decision.PENDING for row in rows))
        self.assertEqual({row.context_key: row.query["scientificName"] for row in rows}, {
            "Taxon A": "Araneae", **{f"Taxon {letter}": "Phycomycetes" for letter in "BCDE"},
        })
        self.table.refresh_from_db()
        pd.testing.assert_frame_equal(self.table.df, original)
        # A later switch back must not revive the old broad interpretation or decision.
        result = MatchTaxonNames(agent_id=self.agent.id, table_id=self.table.id).run()
        self.assertIn("1 distinct labels, 5 rows", result)
        old.refresh_from_db()
        self.assertEqual(old.decision, TaxonNameMatch.Decision.PENDING)
        self.assertEqual(old.query, {"scientificName": "Phycomycetes"})
        self.assertEqual(old.preprocessing_note, "")
        self.assertEqual(TaxonNameMatch.objects.filter(dataset=self.dataset, record_count__gt=0).count(), 1)

    def test_changed_context_resumes_after_a_service_failure(self, match_col, aids, release):
        MatchTaxonNames(agent_id=self.agent.id, table_id=self.table.id).run()
        rematch = MatchTaxonNames(
            agent_id=self.agent.id, table_id=self.table.id, context_column="scientificName",
        )
        match_col.side_effect = taxon_matching.TaxonServiceError("down")
        self.assertIn("service is unavailable", rematch.run())
        active = TaxonNameMatch.objects.filter(dataset=self.dataset, record_count__gt=0)
        self.assertEqual(set(active.values_list("context_column", flat=True)), {"scientificName"})
        self.assertTrue(all(row.matched_at is None for row in active))
        match_col.side_effect = fake_match
        self.assertIn("3 distinct labels, 3 rows", rematch.run())
        self.assertTrue(taxon_matching.matching_complete(list(active.all())))
        # Correcting a named context also retires its grouping.
        self.table.df["block"] = "source block"
        self.table.save()
        result = MatchTaxonNames(
            agent_id=self.agent.id, table_id=self.table.id, context_column="block",
        ).run()
        self.assertIn("3 distinct labels, 3 rows", result)
        self.assertEqual(set(active.values_list("context_column", flat=True)), {"block"})

    def test_context_switch_preserves_an_open_user_review(self, match_col, aids, release):
        MatchTaxonNames(agent_id=self.agent.id, table_id=self.table.id).run()
        self.agent.pause_for_taxon_review("Review names", {"source_table": "occurrence", "context_column": ""})
        result = MatchTaxonNames(
            agent_id=self.agent.id, table_id=self.table.id, context_column="scientificName",
        ).run()
        self.assertIn("Only the user closes it", result)
        self.assertEqual(set(TaxonNameMatch.objects.filter(record_count__gt=0)
                             .values_list("context_column", flat=True)), {""})
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.open_taxon_review, {"source_table": "occurrence", "context_column": ""})

    def test_invalid_context_does_not_retire_existing_matches(self, match_col, aids, release):
        MatchTaxonNames(agent_id=self.agent.id, table_id=self.table.id).run()
        with self.assertRaises(KeyError):
            taxon_matching.record_matches(self.dataset, "occurrence", self.table.df,
                                          "verbatimIdentification", "missing")
        self.assertEqual(TaxonNameMatch.objects.filter(record_count__gt=0).count(), 3)

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
            "labels": 3, "pending_labels": 2,
        })
        other_context = RequestTaxonReview(
            agent_id=self.agent.id, table_id=self.table.id, context_column="block", message="x",
        )
        self.assertIn("MatchTaxonNames first", other_context.run())

    def test_open_review_uses_dataset_active_agent_order(self, match_col, aids, release):
        self.agent.task.order = 1
        self.agent.task.save(update_fields=['order'])
        self.agent.pause_for_taxon_review('Review current names.', {'source_table': 'occurrence', 'context_column': ''})
        later_agent = Agent.objects.create(
            dataset=self.dataset,
            task=Task.objects.create(name='Later task', text='Later', order=2),
        )
        later_agent.pause_for_taxon_review('Later review.', {'source_table': 'later', 'context_column': ''})

        review = taxon_matching.open_review(self.dataset)

        self.assertEqual(review[0].id, self.agent.id)
        self.assertEqual(review[1]['source_table'], 'occurrence')

    def test_rejects_tables_from_other_datasets(self, match_col, aids, release):
        other = Table.objects.create(dataset=Dataset.objects.create(title="Other"), title="occurrence",
                                     df=pd.DataFrame({"verbatimIdentification": ["x"]}))
        self.assertIn("does not belong", MatchTaxonNames(agent_id=self.agent.id, table_id=other.id).run())
        self.assertIn("does not belong", ApplyTaxonDecisions(agent_id=self.agent.id, table_id=other.id).run())


class SharedNameRulesTests(TestCase):
    """The publication review checks and writes a COL name by the conversion review's rules."""

    def setUp(self):
        self.dataset = Dataset.objects.create(title="Shared rules")

    def row(self, label, usage, match_type="EXACT", supplied=None, query=None):
        return TaxonNameMatch.objects.create(
            dataset=self.dataset, source_table="species labels", verbatim_column="verbatimIdentification",
            verbatim_label=label, record_count=1, matched_at=timezone.now(), query=query or {"scientificName": label},
            supplied=supplied or {}, match={"matchType": match_type, "status": "exact", "usage": usage, "alternatives": []})

    def test_a_different_authorship_or_rank_needs_confirmation(self):
        parus = self.row("Parus major", {"id": "PM", "scientificName": "Parus major", "scientificNameAuthorship": "Linnaeus, 1758",
                                         "taxonRank": "species"}, supplied={"authorships": ["Smith, 1900"]})
        self.assertTrue(taxon_matching.choice_checks(parus)["suggestion"]["authorship_differs"])
        self.assertFalse(taxon_matching.bulk_acceptable(parus))
        with self.assertRaisesRegex(ValueError, "authorship of \"Parus major\""):
            taxon_matching.decide(parus, TaxonNameMatch.Decision.ACCEPTED)
        taxon_matching.decide(parus, TaxonNameMatch.Decision.ACCEPTED, confirm_coarser=True)

        larus = self.row("Larus", {"id": "L", "scientificName": "Larus", "taxonRank": "genus"}, supplied={"rank": "family"})
        self.assertEqual(taxon_matching.choice_checks(larus)["suggestion"]["rank_change"], "makes your family a genus")
        with self.assertRaisesRegex(ValueError, "makes your family a genus"):
            taxon_matching.decide(larus, TaxonNameMatch.Decision.ACCEPTED)
        larus = taxon_matching.decide(larus, TaxonNameMatch.Decision.ACCEPTED, confirm_coarser=True)
        self.assertEqual(larus.decided_usage["rankChange"], "makes your family a genus")
        # A confirmed rank change is another taxon: the table's authorship does not carry over to it.
        self.assertEqual(taxon_matching.row_fields(larus)["scientificNameAuthorship"], ("set", ""))

    def test_a_spelling_correction_keeps_the_tables_authorship_and_nullable_cells_apply(self):
        table = Table.objects.create(dataset=self.dataset, title="species labels", df=pd.DataFrame({
            "verbatimIdentification": ["Circium arvense", "Circium arvense"],
            "scientificName": pd.array(["Circium arvense", pd.NA], dtype="string"),
            "scientificNameAuthorship": pd.array(["(L.) Scop.", pd.NA], dtype="string"),
        }))
        row = self.row("Circium arvense", {"id": "CA", "scientificName": "Cirsium arvense", "taxonRank": "species",
                                           "classification": {"kingdom": "Plantae"}},
                       match_type="VARIANT", query={"scientificName": "Circium arvense", "kingdom": "Plantae"})
        check = taxon_matching.choice_checks(row)["suggestion"]
        self.assertEqual((check["changeKind"], check["replaces"]), ("spelling", None))
        taxon_matching.decide(row, TaxonNameMatch.Decision.ACCEPTED)
        taxon_matching.apply_decisions(self.dataset, table, "verbatimIdentification")
        table.refresh_from_db()
        self.assertEqual(list(table.df.scientificName), ["Cirsium arvense", "Cirsium arvense"])
        self.assertEqual(table.df.scientificNameAuthorship[0], "(L.) Scop.")  # COL has none: the table's stands
        self.assertTrue(pd.isna(table.df.scientificNameAuthorship[1]))


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
        self.agent.pause_for_taxon_review("Please review.", {"source_table": "occurrence", "context_column": ""})
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
            {"decision": "corrected", "scientificName": "Neodiscopoma splendida"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["decision"], "corrected")
        self.assertEqual(response.json()["decided_usage"]["scientificName"], "Neodiscopoma splendida")
        self.assertEqual(response.json()["decided_usage"]["source"], "reviewer")
        self.assertEqual(response.json()["decided_by"], self.owner.id)

        response = self.client.post(f"/api/taxon-matches/{self.higher.id}/decide/", {"decision": "bogus"})
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/api/taxon-matches/accept-exact/", {"dataset": self.dataset.id})
        self.assertEqual(response.json()["accepted"], 1)
        self.exact.refresh_from_db()
        self.assertEqual(self.exact.decision, "accepted")

    def test_accepting_a_coarser_suggestion_needs_a_confirming_second_request(self):
        self.client.force_authenticate(self.owner)
        listed = self.client.get("/api/taxon-matches/", {"dataset": self.dataset.id}).json()
        listed = listed["results"] if isinstance(listed, dict) else listed
        higher = next(row for row in listed if row["id"] == self.higher.id)
        self.assertEqual(higher["choice_checks"]["suggestion"]["replaces"]["text"], "replaces your species with a genus")
        self.assertIsNone(next(row for row in listed if row["id"] == self.exact.id)["choice_checks"]["suggestion"]["replaces"])
        url = f"/api/taxon-matches/{self.higher.id}/decide/"
        refused = self.client.post(url, {"decision": "accepted"}, format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("replaces your species with a genus", str(refused.json()))
        self.higher.refresh_from_db()
        self.assertEqual(self.higher.decision, "pending")
        confirmed = self.client.post(url, {"decision": "accepted", "confirm_coarser": True}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertEqual((confirmed.json()["decided_usage"]["scientificName"], confirmed.json()["decided_usage"]["replaces"]),
                         ("Neodiscopoma", "replaces your species with a genus"))
        # The exact same-name suggestion needs no confirmation.
        self.assertEqual(self.client.post(f"/api/taxon-matches/{self.exact.id}/decide/", {"decision": "accepted"}).status_code, 200)

    def test_decisions_are_refused_while_the_agent_answers_and_once_the_review_is_closed(self):
        self.client.force_authenticate(self.owner)

        def refused():
            response = self.client.post(f"/api/taxon-matches/{self.exact.id}/decide/", {"decision": "accepted"})
            self.assertEqual(response.status_code, 409)
            response = self.client.post("/api/taxon-matches/accept-exact/", {"dataset": self.dataset.id})
            self.assertEqual(response.status_code, 409)
            self.exact.refresh_from_db()
            self.assertEqual(self.exact.decision, "pending")

        Message.objects.create(agent=self.agent, openai_obj={"role": "user", "content": "A question"})
        refused()
        Message.objects.create(agent=self.agent, openai_obj={"role": "assistant", "content": "An answer"})
        self.agent.open_taxon_review = {}
        self.agent.save()
        refused()

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
        self.agent.pause_for_taxon_review("Please review.", {"source_table": "occurrence", "context_column": ""})
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
        self.assertEqual(list(self.table.df.scientificName[:3]), ["Dinychus", "Dinychus", "Pająki"])  # kept: the label fills it
        self.assertEqual(list(self.table.df.species), ["Dinychus", "Dinychus", "Pająki", "Zieminek"])

        message = self.agent.message_set.last()
        self.assertEqual(message.role, Message.Role.USER)
        content = message.openai_obj["content"]
        self.assertTrue(content.startswith(
            "I have finished reviewing the taxon names: 1 name accepted, 1 name kept as the original. "
            "1 name (1 record) is left unreviewed."
        ))
        self.assertIn("[NOTE: ChatIPT applied the reviewed taxon names itself", content)
        self.assertIn("Applied reviewed names for 1 labels to 3 rows", content)
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
        self.assertEqual(self.skip().status_code, 409)
        self.agent.busy_thinking = False
        self.agent.save()
        Message.objects.create(agent=self.agent, openai_obj={"role": "user", "content": "Not answered yet"})
        self.assertEqual(self.finish().status_code, 409)
        self.assertEqual(self.skip().status_code, 409)
        ensure_work.assert_not_called()

    def test_other_users_cannot_finish_or_skip(self, ensure_work):
        self.client.force_authenticate(CustomUser.objects.create_user(username="other", password="x"))
        self.assertEqual(self.finish().status_code, 404)
        self.assertEqual(self.skip().status_code, 404)
        ensure_work.assert_not_called()

    def skip(self):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/taxon-matches/skip-review/", {"agent": self.agent.id}, format="json")

    def test_a_chat_reply_keeps_the_review_open_and_tells_the_agent(self, ensure_work):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/messages/", {"agent": self.agent.id, "openai_obj": {
                "role": "user", "content": "Is Zieminek a subclass?",
            }}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.agent.message_set.last().openai_obj["content"], "Is Zieminek a subclass?")
        self.agent.refresh_from_db()
        state = self.agent.current_state_update()
        self.assertIn("Taxon review open: the review you requested for the `occurrence` table is waiting for the user "
                      "(1 of 3 names undecided).", state)
        self.assertIn("Done can overwrite such edits.", state)
        self.assertEqual(self.finish().status_code, 409)  # ChatIPT has not answered yet

        Message.objects.create(agent=self.agent, openai_obj={"role": "assistant", "content": "It is a genus."})
        self.assertEqual(self.client.post(
            f"/api/taxon-matches/{TaxonNameMatch.objects.get(verbatim_label='Zieminek').id}/decide/",
            {"decision": "keep_original"},
        ).status_code, 200)
        response = self.finish()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("[NOTE: ChatIPT applied the reviewed taxon names itself", self.agent.message_set.last().openai_obj["content"])
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.open_taxon_review, {})
        self.assertNotIn("Taxon review open", self.agent.current_state_update())

    def test_skip_closes_the_review_without_writing_names(self, ensure_work):
        response = self.skip()
        self.assertEqual(response.status_code, 200, response.content)
        self.table.refresh_from_db()
        self.assertNotIn("scientificName", self.table.df.columns)
        message = self.agent.message_set.last()
        self.assertEqual(response.json()["message"]["id"], message.id)
        self.assertEqual(message.role, Message.Role.USER)
        content = message.openai_obj["content"]
        self.assertTrue(content.startswith("I skipped the taxon name review: keep the names as they are.\n\n[NOTE: "))
        self.assertIn("(1 of 3 names were undecided)", content)
        self.assertIn("Do not call ApplyTaxonDecisions for this review", content)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.open_taxon_review, {})
        self.assertEqual(TaxonNameMatch.objects.get(verbatim_label="Dinychus").decision, "accepted")  # kept, not applied
        ensure_work.assert_called_once_with(self.dataset.id)

        Message.objects.create(agent=self.agent, openai_obj={"role": "assistant", "content": "Noted."})
        self.assertEqual(self.skip().status_code, 409)
        self.assertEqual(self.finish().status_code, 409)
        self.assertEqual(ensure_work.call_count, 1)

    def test_the_agent_cannot_complete_or_publish_while_the_review_is_open(self, ensure_work):
        result = SetAgentTaskToComplete(agent_id=self.agent.id).run()
        self.assertTrue(result.startswith(
            "Error: The taxon review you requested is still open (1 of 3 names undecided), so you cannot complete "
            "'Data validation and refinement' yet."
        ), result)
        self.assertFalse(schema_ledger.result_is_progress("SetAgentTaskToComplete", result))
        self.agent.task.name = Task.FINAL_PUBLICATION_TASK
        self.agent.task.save()
        self.assertIn("so you cannot publish yet.", PublishToGBIF(agent_id=self.agent.id).run())
        self.agent.refresh_from_db()
        self.assertIsNone(self.agent.completed_at)

        self.skip()
        self.assertNotIn("taxon review", SetAgentTaskToComplete(agent_id=self.agent.id).run())
        self.assertNotIn("taxon review", PublishToGBIF(agent_id=self.agent.id).run())

    def test_another_review_cannot_replace_the_open_one(self, ensure_work):
        result = RequestTaxonReview(
            agent_id=self.agent.id, table_id=self.table.id, context_column="block", message="Review again",
        ).run()
        self.assertTrue(result.startswith(
            "Error: The taxon review you requested is still open (1 of 3 names undecided), so you cannot request a "
            "review of another table or context column yet."
        ), result)
        self.assertFalse(schema_ledger.result_is_progress("RequestTaxonReview", result))
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.open_taxon_review, {"source_table": "occurrence", "context_column": ""})


class OpenReviewTurnTests(TestCase):
    @patch("api.models.create_response_message")
    def test_a_plain_answer_ends_the_turn_while_the_review_is_open(self, model):
        task = Task.objects.create(name="Workflow action test", text="Complete it.", order=1)
        Task.objects.create(name="Data maintenance", text="Stay available.", order=2)
        agent = Agent.create_with_system_message(
            dataset=Dataset.objects.create(title="Open review", description="Test"), task=task, tables=[],
        )
        scope = {"source_table": "occurrence", "context_column": ""}
        agent.pause_for_taxon_review("Please review.", scope)
        Message.objects.create(agent=agent, openai_obj={"role": "user", "content": "Is Oligochaeta a subclass?"})
        model.return_value = CompatAssistantMessage(content="Yes; choose COL's subclass in the review.")

        messages = agent.next_message()

        self.assertEqual(model.call_count, 1)  # no "took no action" retry pushing it to complete
        self.assertEqual(messages[-1].openai_obj["content"], "Yes; choose COL's subclass in the review.")
        agent.refresh_from_db()
        self.assertEqual(agent.open_taxon_review, scope)
        self.assertIsNone(agent.completed_at)


class OpenReviewMigrationTests(TestCase):
    def test_only_reviews_still_waiting_for_the_user_stay_open(self):
        from importlib import import_module
        from django.apps import apps

        migration = import_module("api.migrations.0040_agent_open_taxon_review")
        dataset = Dataset.objects.create(title="Migrated")
        task = Task.objects.create(name="Data validation and refinement", text="x")
        scope = {"source_table": "identification", "context_column": ""}
        waiting, answered, completed = (Agent.objects.create(dataset=dataset, task=task) for _ in range(3))
        for agent in (waiting, answered, completed):
            Message.objects.create(agent=agent, openai_obj={"role": "assistant", "content": "Review", "taxon_review": scope})
        Message.objects.create(agent=answered, openai_obj={"role": "user", "content": "Done"})
        completed.completed_at = timezone.now()
        completed.save()

        migration.open_waiting_reviews(apps, None)

        self.assertEqual([Agent.objects.get(id=agent.id).open_taxon_review for agent in (waiting, answered, completed)],
                         [scope, {}, {}])
