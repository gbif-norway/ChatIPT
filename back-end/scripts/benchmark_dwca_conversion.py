#!/usr/bin/env python
"""Offline local archive audit; run only through the backend Compose image.

No database writes, API calls, model calls, or source mutations are performed.
Converted files and full conversion reports contain source data and belong in /tmp.
Only aggregate summaries are suitable for the checked-in audit directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
import django

django.setup()

from api.dwca_conversion import RULE_VERSION, build_plan, convert
from api.dwca_import import DWC, REGISTRY, ImportFailure, read_inputs, source_zip
from api.dwc_dp_specs import TABLE_SPECS, create_dwc_dp_archive, validate_dwc_dp_archive, validate_eml

BACKEND = Path(__file__).resolve().parents[1]


def digest(content):
    return hashlib.sha256(content).hexdigest()


def folder_inputs(name):
    return [(path.name, path.read_bytes()) for path in sorted(
        (BACKEND / "api/templates/examples" / name).iterdir())
        if path.is_file() and not path.name.startswith(".")]


def simulated_decisions(plan, material=False):
    """Explicit simulation, never evidence of publisher/scientific approval.

    Keep row context; confirm supplied extension subjects; approve direct/default
    term mappings; preserve unsupported fields; assert missing presence statuses.
    Physical material is approved only in the Akagera benchmark scenario.
    """
    decisions = {issue["id"]: issue["options"][0]["value"] for issue in plan["issues"]}
    for issue in plan["issues"]:
        if issue["id"].startswith("material:"):
            decisions[issue["id"]] = "per_row" if material else "preserve"
    return decisions


def source_profile(archive, plan):
    core = next(table for table in archive.tables if table.is_core)
    join_ids = set(core.ids)
    return {
        "fingerprint": archive.fingerprint,
        "source_bytes": sum(len(content) for content in archive.files.values()),
        "has_meta": archive.has_meta,
        "files": len(archive.files),
        "tables": [{
            "name": table.name, "rows": len(table.rows), "columns": len(table.terms),
            "row_type": table.row_type, "join_basis": table.join_basis,
            "unique_join_ids": len(set(table.ids)),
            "orphans": sum(value not in join_ids for value in table.ids) if not table.is_core else 0,
        } for table in archive.tables],
        "nonempty_cells": sum(column["nonempty"] for column in plan["columns"]),
        "null_tokens": [{"table": archive.tables[column["table"]].name,
                         "term": column["term"], "rows": sum(row[column["column"]] == "NA"
                         for row in archive.tables[column["table"]].rows),
                         "review": column["review"], "default": column["default"]}
                        for column in plan["columns"] if any(row[column["column"]] == "NA"
                        for row in archive.tables[column["table"]].rows)],
        "review": {
            "total": len(plan["issues"]),
            "by_kind": dict(Counter(issue["id"].split(":")[0] for issue in plan["issues"])),
            "by_title": dict(Counter(issue["title"] for issue in plan["issues"])),
            "single_option": sum(len(issue["options"]) == 1 for issue in plan["issues"]),
            "column_mapping": sum(column["review"] for column in plan["columns"]),
            "automatic_nonempty_columns": sum(not column["review"] and column["nonempty"] > 0
                                               for column in plan["columns"]),
            "beyond_ai_first_40": max(0, len(plan["issues"]) - 40),
        },
        "event_grouping_evidence": plan.get("event_grouping_evidence"),
    }


def table_sources(archive):
    return {table.name: [dict(zip(table.terms, row)) for row in table.rows]
            for table in archive.tables}


def akagera_evidence(archive):
    sources = table_sources(archive)
    occurrences = sources["377__occurrence.csv"]
    dna = sources["377__dnaderiveddata.csv"]
    relationships = sources["377__resourcerelationship.csv"]
    facts = sources["377__extendedmeasurementorfact.csv"]
    history = sources["377__identificationhistory.csv"]
    occurrence_map = {row[DWC + "occurrenceID"]: row for row in occurrences}
    dna_table = next(table for table in archive.tables if table.name == "377__dnaderiveddata.csv")
    source_by_join = {identifier: row for identifier, row in zip(
        dna_table.ids, dna)}
    markers = next(term for term in dna_table.terms if term in REGISTRY["terms"]["target_gene"])
    read_groups = defaultdict(lambda: {"total": 0, "denominators": set()})
    for occurrence in occurrences:
        identifier = occurrence[DWC + "occurrenceID"]
        marker = source_by_join[identifier][markers]
        group = read_groups[(occurrence[DWC + "materialSampleID"], marker)]
        group["total"] += int(occurrence[DWC + "organismQuantity"])
        group["denominators"].add(int(occurrence[DWC + "sampleSizeValue"]))
    subject_samples = defaultdict(set)
    same_event = same_material = 0
    kingdom_counts = Counter()
    for relationship in relationships:
        subject = occurrence_map[relationship[DWC + "resourceID"]]
        related = occurrence_map[relationship[DWC + "relatedResourceID"]]
        same_event += subject[DWC + "eventID"] == related[DWC + "eventID"]
        same_material += subject[DWC + "materialSampleID"] == related[DWC + "materialSampleID"]
        subject_samples[subject[DWC + "materialSampleID"]].add(subject[DWC + "occurrenceID"])
        kingdom_counts[related[DWC + "kingdom"]] += 1
    fact_groups = defaultdict(set)
    for fact in facts:
        sample = occurrence_map[fact[DWC + "occurrenceID"]][DWC + "materialSampleID"]
        fact_groups[(sample, fact[DWC + "measurementType"])].add(fact[DWC + "measurementValue"])
    event_samples = defaultdict(set)
    for occurrence in occurrences:
        event_samples[occurrence[DWC + "eventID"]].add(occurrence[DWC + "materialSampleID"])
    lines = archive.files["377__occurrence.csv"].decode("utf-8-sig").splitlines()
    return {
        "occurrence_records": len(occurrences), "occurrence_physical_lines": len(lines),
        "distinct_events": len(event_samples),
        "distinct_material_identifiers": len({row[DWC + "materialSampleID"] for row in occurrences}),
        "event_to_material_not_one_to_one": sum(len(value) != 1 for value in event_samples.values()),
        "dna_join_set_equals_core": set(source_by_join) == set(occurrence_map),
        "relationship_rows_same_event": same_event, "relationship_rows_same_material": same_material,
        "samples_with_multiple_relationship_subjects": sum(len(value) > 1 for value in subject_samples.values()),
        "samples_with_no_relationship_rows": len({row[DWC + "materialSampleID"] for row in occurrences} - set(subject_samples)),
        "relationship_related_kingdom": dict(kingdom_counts),
        "fact_type_counts": dict(Counter(row[DWC + "measurementType"] for row in facts)),
        "sample_fact_groups": len(fact_groups),
        "sample_fact_groups_with_conflicting_values": sum(len(value) != 1 for value in fact_groups.values()),
        "read_groups": len(read_groups),
        "read_groups_denominator_inconsistent": sum(len(group["denominators"]) != 1
                                                      for group in read_groups.values()),
        "read_groups_sum_mismatch": sum(group["denominators"] != {group["total"]}
                                          for group in read_groups.values()),
        "history_rows": len(history),
        "history_id_unique": len({row[DWC + "identificationID"] for row in history}) == len(history),
        "history_rows_dna_suffix": sum(row[DWC + "identificationID"].endswith("_dna") for row in history),
        "history_rows_field_suffix": sum(row[DWC + "identificationID"].endswith("_field") for row in history),
    }


def crosswalk_audit(archive, frames, report):
    names = {table.name: table for table in archive.tables}
    bounds_failures = 0
    traced_rows = defaultdict(set)
    for row in report["row_crosswalk"]:
        table = names[row["source_table"]]
        if not 1 <= row["source_row"] <= len(table.rows):
            bounds_failures += 1
        if row.get("target_row") is not None:
            traced_rows[row["target_table"]].add(row["target_row"])
    copied_cells, unmatched_cells = Counter(), Counter()
    source_targets = defaultdict(list)
    records = {name: frame.to_dict("records") for name, frame in frames.items()}
    keyed = {}
    for name, rows in records.items():
        fields = TABLE_SPECS[name].primary_key
        if fields:
            keyed[name] = {tuple((field, record.get(field)) for field in sorted(fields)): record for record in rows}
    for trace in report["row_crosswalk"]:
        if trace.get("target_row") is not None:
            record = records[trace["target_table"]][trace["target_row"] - 1]
        else:
            record = keyed.get(trace["target_table"], {}).get(tuple(sorted(trace["key"].items())))
        if record is not None:
            source_targets[(trace["source_table"], trace["source_row"], trace["target_table"])].append(record)
    withheld = {(item["source_table"], item["source_row"], item["term"])
                for item in report.get("withheld_values", [])}
    for column in report["columns"]:
        target = column["target"]
        if column["disposition"] != "mapped+retained" or "." not in target:
            continue
        name, field = target.split(".", 1)
        table = names[column["source_table"]]
        index = table.terms.index(column["term"])
        for n, row in enumerate(table.rows, 1):
            value = row[index]
            if not value:
                continue
            matches = source_targets[(table.name, n, name)]
            if any(record.get(field) == value for record in matches):
                copied_cells[(table.name, column["term"])] += 1
            elif (table.name, n, column["term"]) in withheld:
                pass
            else:
                unmatched_cells[(table.name, column["term"])] += 1
    count_disagreements = []
    for column in report["columns"]:
        if "mapped_rows" in column and "." in column["target"]:
            actual = copied_cells[(column["source_table"], column["term"])]
            if actual != column["mapped_rows"]:
                count_disagreements.append({"table": column["source_table"], "term": column["term"],
                                            "reported": column["mapped_rows"], "verified": actual})
    core = next(table for table in archive.tables if table.is_core)
    core_occurrences, core_events = {}, {}
    for n, identifier in enumerate(core.ids, 1):
        for record in source_targets[(core.name, n, "occurrence")]:
            core_occurrences[identifier] = record["occurrence_pk"]
        for record in source_targets[(core.name, n, "event")]:
            core_events[identifier] = record["event_pk"]
    join_checks, join_failures = Counter(), Counter()
    for table in archive.tables:
        if table.is_core:
            continue
        for n, identifier in enumerate(table.ids, 1):
            for name, foreign, expected in [("identification", "occurrence_fk", core_occurrences),
                                            ("occurrence-assertion", "occurrence_fk", core_occurrences),
                                            ("nucleotide-analysis", "event_fk", core_events)]:
                for record in source_targets[(table.name, n, name)]:
                    join_checks[name] += 1
                    join_failures[name] += record.get(foreign) != expected.get(identifier)
    return {
        "entries": len(report["row_crosswalk"]), "source_row_out_of_bounds": bounds_failures,
        "untraced_target_rows": {name: len(frame) - len(traced_rows[name])
                                 for name, frame in frames.items()},
        "columns_reported": len(report["columns"]),
        "columns_expected": sum(len(table.terms) for table in archive.tables),
        "columns_with_explicit_copied_counts": sum("mapped_rows" in column for column in report["columns"]),
        "column_copied_counts": [{"table": column["source_table"], "term": column["term"],
                                  "mapped_rows": column["mapped_rows"], "retained_only_rows": column["retained_only_rows"]}
                                 for column in report["columns"] if "mapped_rows" in column],
        "column_dispositions": dict(Counter(column["disposition"] for column in report["columns"])),
        "retained_only_nonempty_cells": sum(column["nonempty"] for column in report["columns"]
                                             if column["disposition"] == "retained-unmapped"),
        "mapped_nonempty_cells_verified_verbatim": sum(copied_cells.values()),
        "mapped_nonempty_cells_unmatched": [{"table": table, "term": term, "rows": count}
                                            for (table, term), count in unmatched_cells.items()],
        "copied_count_disagreements": count_disagreements,
        "extension_join_checks": dict(join_checks),
        "extension_join_failures": dict(join_failures),
        "reported_file_checksums_match_sources": all(digest(archive.files[item["name"]]) == item["sha256"]
                                                      for item in report["files"]),
    }


def serialize(archive, frames, report, output):
    original_eml = [content for name, content in archive.files.items() if Path(name).name == "eml.xml"]
    eml = original_eml[0] if len(original_eml) == 1 and not validate_eml(original_eml[0]) else None
    originals = [("source-originals.zip", source_zip(archive.files)),
                 ("conversion-report.json", json.dumps(report, ensure_ascii=False).encode())]
    if len(archive.uploaded_files) == 1 and next(iter(archive.uploaded_files)).endswith(".zip"):
        originals.append(("uploaded-archive.zip", next(iter(archive.uploaded_files.values()))))
    descriptor = create_dwc_dp_archive(output, frames, "Local benchmark simulation", "",
                                      include_eml=eml is not None, eml_content=eml,
                                      additional_files=originals, declare_additional_resources=True)
    validation = validate_dwc_dp_archive(output, require_eml=eml is not None)
    with tarfile.open(output, "r:gz") as package:
        original_zip = package.extractfile("source-originals.zip").read()
        with zipfile.ZipFile(io.BytesIO(original_zip)) as preserved:
            originals_equal = set(preserved.namelist()) == set(archive.files) and all(
                preserved.read(name) == content for name, content in archive.files.items())
        uploaded = [(name, content) for name, content in originals if name == "uploaded-archive.zip"]
        uploaded_equal = all(package.extractfile(name).read() == content for name, content in uploaded) if uploaded else "not applicable"
        eml_equal = package.extractfile("eml.xml").read() == eml if eml else "original-only"
    return {
        "valid": validation["valid"], "errors": len(validation.get("errors", [])),
        "warnings": len(validation.get("warnings", [])), "archive_bytes": output.stat().st_size,
        "original_file_bytes_equal": originals_equal, "uploaded_zip_bytes_equal": uploaded_equal,
        "eml": "original validated EML 2.2.0" if eml else "metadata retained only in originals",
        "eml_bytes_equal": eml_equal,
        "descriptor_resources": len(descriptor["resources"]),
    }


def run_case(name, inputs, output, evidence, material=False, preserve_targets=(), grouped=False):
    started = time.monotonic()
    archive = read_inputs(inputs)
    plan = build_plan(archive)
    result = {"case": name, "evidence": evidence, "schema": plan["schema"], **source_profile(archive, plan)}
    core = next(table for table in archive.tables if table.is_core)
    terms = {term: index for index, term in enumerate(core.terms)}
    names = [row[terms[DWC + "scientificName"]].split() for row in core.rows] if DWC + "scientificName" in terms else []
    result["semantic_probes"] = {
        "float_shaped_dates": sum(bool(re.fullmatch(r"\d{4}\.\d+", row[terms[DWC + "eventDate"]]))
                                  for row in core.rows) if DWC + "eventDate" in terms else 0,
        "repeated_genus_names": sum(len(parts) > 2 and parts[0] == parts[1] for parts in names),
    }
    decisions = simulated_decisions(plan, material)
    if grouped:
        decisions["event-grain"] = "by_id"
        for issue in plan["issues"]:
            if issue["id"].startswith("material:"):
                decisions[issue["id"]] = "by_id"
    for column in plan["columns"]:
        if column["default"] in preserve_targets:
            decisions[column["id"]] = "preserve"
    result["simulated_review"] = {
        "physical_material": "combine supplied identifiers" if grouped else "per source row" if material else "preserved only",
        "event_grain": "combine supplied eventID" if grouped else "per occurrence source row",
        "missing_occurrence_status": "explicit simulated presence assertion",
        "extensions": "first declared meaning; supplied joins retained",
        "columns": "approved default mappings; unsupported terms preserved",
        "additional_preserved_targets": list(preserve_targets),
        "publisher_approval": False,
    }
    frames, report = convert(archive, plan, decisions)
    result.update(resources=report["resources"], valid=report["validation"]["valid"],
                  validation_error_count=len(report["validation"].get("errors", [])),
                  validation_warning_count=len(report["validation"].get("warnings", [])),
                  withheld_value_count=len(report.get("withheld_values", [])),
                  preserved_extension_row_count=len(report.get("preserved_extension_rows", [])),
                  plan_rebuild_identical=build_plan(archive) == plan,
                  crosswalk=crosswalk_audit(archive, frames, report))
    result["deterministic_resources"] = {}
    second, _ = convert(archive, plan, decisions)
    for resource, frame in frames.items():
        result["deterministic_resources"][resource] = digest(frame.to_csv(index=False).encode()) == digest(
            second[resource].to_csv(index=False).encode())
    if name.startswith("akagera"):
        result["source_evidence"] = akagera_evidence(archive)
        result["identification_accepted_flags_nonempty"] = sum(
            str(value) != "" for value in frames["identification"].get("isAcceptedIdentification", []))
        result["invented_organism_or_interaction_resources"] = any(
            resource.startswith("organism") for resource in frames)
        result["analysis_links"] = {
            "rows": len(frames["nucleotide-analysis"]),
            "collection_event_links": int((frames["nucleotide-analysis"]["event_fk"] != "").sum()),
            "material_links": int((frames["nucleotide-analysis"].get("materialEntity_fk", []) != "").sum())
                if "materialEntity_fk" in frames["nucleotide-analysis"] else 0,
            "readCount_present": "readCount" in frames["nucleotide-analysis"],
            "processedTotalReadCount_present": "processedTotalReadCount" in frames["nucleotide-analysis"],
        }
        by_id = dict(decisions, **{"event-grain": "by_id"})
        try:
            convert(archive, plan, by_id)
            result["event_merge_probe_rejected"] = False
        except ImportFailure:
            result["event_merge_probe_rejected"] = True
    if result["valid"]:
        result["serialized"] = serialize(archive, frames, report, output / (name + ".tar.gz"))
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def synthetic_repros():
    coordinates = read_inputs([("occurrence.csv", b"occurrenceID,decimalLatitude,decimalLongitude,occurrenceStatus\nexample,NA,NA,present\n")])
    plan = build_plan(coordinates)
    _, report = convert(coordinates, plan, simulated_decisions(plan))
    empty_event = read_inputs([("occurrence.csv", b"occurrenceID,scientificName,occurrenceStatus\nexample,Apus apus,present\n"),
                             ("dnaderiveddata.csv", b"occurrenceID,DNA_sequence,target_gene\nexample,ACGT,12S\n")])
    event_plan = build_plan(empty_event)
    event_frames, _ = convert(empty_event, event_plan, simulated_decisions(event_plan))
    nbn = read_inputs([("event.csv", b"eventID,eventCategory\nevent,survey\n"),
                       ("nbn.csv", ("eventID,sensitiveOccurrence\n" + "event,true\n" * 50).encode())])
    nbn_plan = build_plan(nbn)
    order_inputs = [("occurrence.csv", b"occurrenceID,occurrenceStatus\nexample,present\n"),
                    ("identificationhistory.csv", b"occurrenceID,scientificName\nexample,Apus apus\n")]
    first, reverse = read_inputs(order_inputs), read_inputs(list(reversed(order_inputs)))
    first_plan, reverse_plan = build_plan(first), build_plan(reverse)
    first_frames, _ = convert(first, first_plan, simulated_decisions(first_plan))
    reverse_frames, _ = convert(reverse, reverse_plan, simulated_decisions(reverse_plan))
    return {
        "numeric_na_review_gap": {
            "input": "occurrenceID,decimalLatitude,decimalLongitude,occurrenceStatus\\nexample,NA,NA,present\\n",
            "review_ids": [issue["id"] for issue in plan["issues"]],
            "coordinate_columns_reviewed": [column["review"] for column in plan["columns"]
                                            if column["default"] in {"event.decimalLatitude", "event.decimalLongitude"}],
            "conversion_valid": report["validation"]["valid"],
            "expected": "invalid numeric values need a preservation decision before export; never assume NA is null",
        },
        "molecular_event_context_without_analysis_event": {
            "analysis_event_fk_populated": bool(event_frames["nucleotide-analysis"].iloc[0]["event_fk"]),
            "table_choice": next(issue["options"][0]["label"] for issue in event_plan["issues"] if issue["id"].startswith("table:")),
            "classification": "declared contextual-link behavior; source supplies no distinct analysis event",
        },
        "nbn_row_review_beyond_ai_window": {
            "synthetic_rows": 50, "issues": len(nbn_plan["issues"]),
            "row_issues": sum(issue["id"].startswith("row:") for issue in nbn_plan["issues"]),
            "beyond_first_40": max(0, len(nbn_plan["issues"]) - 40),
        },
        "loose_input_order_reproducibility": {
            "source_fingerprint_equal": first.fingerprint == reverse.fingerprint,
            "plan_id_equal": first_plan["id"] == reverse_plan["id"],
            "occurrence_uuid_equal": first_frames["occurrence"].iloc[0]["occurrence_pk"] == reverse_frames["occurrence"].iloc[0]["occurrence_pk"],
            "expected": "report upload order if ordering is part of identity; otherwise canonicalize loose table order",
        },
    }


def prior_comparison():
    result = {}
    for name in ["event.csv", "material.csv", "identification.csv", "material-assertion.csv",
                 "molecular-protocol.csv", "nucleotide-analysis.csv", "nucleotide-sequence.csv",
                 "occurrence_VTCMS0k.csv", "resource-relationship.csv"]:
        path = BACKEND / "user_files" / name
        if path.exists():
            rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))
            result[name] = {"rows": len(rows) - 1, "columns": len(rows[0])}
    return {"gold_standard": False, "scope": "aggregate structural comparison only", "tables": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/dwca-real-archive-benchmark"))
    parser.add_argument("--cases", nargs="*", help="Optional subset of case names")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    names = ["occurrence", "extendedmeasurementorfact", "resourcerelationship", "identificationhistory", "dnaderiveddata"]
    akagera = [(f"377__{name}.csv", (BACKEND / "user_files" / f"377__{name}.csv").read_bytes()) for name in names]
    gaynor = folder_inputs("gaynor_et_al_v3")
    cases = [
        ("akagera", akagera, "local real source tables; no meta.xml", True, ()),
        ("akagera-environment-preserved", akagera,
         "same real tables; sample environment values explicitly preserved outside protocol records", True,
         ("molecular-protocol.env_broad_scale", "molecular-protocol.env_local_scale", "molecular-protocol.env_medium")),
        ("akagera-grouped-reviewed", akagera,
         "same real tables; explicit event/material grouping after coordinate-uncertainty and environment preservation", True,
         ("event.coordinateUncertaintyInMeters", "molecular-protocol.env_broad_scale", "molecular-protocol.env_local_scale", "molecular-protocol.env_medium")),
        ("gaynor", gaynor, "local real meta.xml archive", False, ()),
        ("gaynor-preserved-coordinates", gaynor, "same real archive; explicit coordinate-column preservation workaround", False,
         ("event.decimalLatitude", "event.decimalLongitude")),
        ("gaynor-zip", [("gaynor.zip", source_zip(dict(gaynor)))], "real local records wrapped as ZIP for upload-byte retention", False,
         ("event.decimalLatitude", "event.decimalLongitude")),
        ("bigtree", folder_inputs("bigtree"), "additional local meta.xml archive; source provenance not independently verified", False, ()),
        ("newick", folder_inputs("newick"), "local synthetic/anomaly-handling fixture", False, ()),
    ]
    summary = {"rule_version": RULE_VERSION, "model_calls": 0, "database_writes": 0, "cases": [],
               "synthetic_repros": synthetic_repros(), "prior_comparison": prior_comparison()}
    for name, inputs, evidence, material, preserve in cases:
        if args.cases and name not in args.cases:
            continue
        print(json.dumps({"started": name}), flush=True)
        result = run_case(name, inputs, args.output, evidence, material, preserve,
                          grouped=name == "akagera-grouped-reviewed")
        summary["cases"].append(result)
        (args.output / "aggregate.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"finished": name, "valid": result["valid"], "resources": result["resources"],
                          "issues": result["review"]["total"], "seconds": result["seconds"]}), flush=True)
    print(json.dumps({"aggregate": str(args.output / "aggregate.json")}), flush=True)


if __name__ == "__main__":
    main()
