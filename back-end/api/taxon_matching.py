"""Match verbatim taxon labels against Catalogue of Life XR.

GBIF.org interprets occurrences against COL XR by default, so the primary suggestion comes from
GBIF's v2 matcher with the COL checklistKey: the same engine and taxonomy copy GBIF indexes with.
When that finds nothing or only a higher rank, ChecklistBank's newer XR release and the legacy
GBIF Backbone are consulted as review aids only; they may not yet be reflected on GBIF.org.

A match suggests a name. It never asserts that the identification is correct, so suggestions are
stored for review and only reviewer decisions are written back to the data.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from django.conf import settings


GBIF_MATCH_URL = "https://api.gbif.org/v2/species/match"
GBIF_MATCH_METADATA_URL = "https://api.gbif.org/v2/species/match/metadata"
CHECKLISTBANK_API = "https://api.checklistbank.org"
USER_AGENT = "ChatIPT (GBIF Norway; https://chatipt.svc.gbif.no)"
BATCH_SIZE = 100
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 30
# A matching run happens inside one agent turn; what does not finish is matched on the next call.
RUN_BUDGET_SECONDS = 180
# Lookups made while a reviewer waits in the browser.
INTERACTIVE_BUDGET_SECONDS = 10
MAX_ALTERNATIVES = 5
CONCURRENT_REQUESTS = 4
PROGRESS_CHUNK = 25

# Hints the matcher accepts; only pass values taken from the source data or confirmed by a reviewer.
HINT_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")
# Classification ranks written back when the target table's schema has a column for them.
CLASSIFICATION_RANKS = (
    "kingdom", "phylum", "class", "order", "superfamily", "family",
    "subfamily", "tribe", "subtribe", "genus", "subgenus",
)

MATCH_STATUS = {
    "EXACT": "exact",
    "VARIANT": "variant",
    "FUZZY": "variant",
    "CANONICAL": "variant",
    "HIGHERRANK": "higher_rank",
    "AMBIGUOUS": "ambiguous",
    "NONE": "none",
}

_NEW_SPECIES_RE = re.compile(r"\s+sp(?:ec)?\.?\s*n(?:ov)?\.?$", re.IGNORECASE)
_TRAILING_SP_RE = re.compile(r"\s+(sp|spp|indet)\.?(?:\s*\d+)?$", re.IGNORECASE)
_INNER_QUALIFIER_RE = re.compile(r"^(\S+)\s+(cf|aff|nr)\.?\s+(\S.*)$", re.IGNORECASE)


class TaxonServiceError(RuntimeError):
    pass


def col_checklist_key():
    return getattr(settings, "GBIF_COL_CHECKLIST_KEY", "7ddf754f-d193-4cc9-b351-99906754a03b")


def col_checklistbank_dataset():
    return getattr(settings, "CHECKLISTBANK_COL_DATASET", "3LXR")


def split_qualifier(label):
    """Return (name to match, identification qualifier) for one verbatim label.

    "Dinychus sp." is matched as the genus with qualifier "sp."; "Genus cf. species" is matched as
    "Genus species" with qualifier "cf. species". "sp. n." marks a newly described species and is
    dropped without a qualifier, because it does not express uncertainty.
    """
    name = " ".join(str(label or "").split())
    if _NEW_SPECIES_RE.search(name):
        return _NEW_SPECIES_RE.sub("", name).strip(), None
    trailing = _TRAILING_SP_RE.search(name)
    if trailing:
        return name[:trailing.start()].strip(), f"{trailing.group(1).lower()}."
    inner = _INNER_QUALIFIER_RE.match(name)
    if inner:
        genus, qualifier, rest = inner.groups()
        return f"{genus} {rest}".strip(), f"{qualifier.lower()}. {rest.split()[0]}"
    return name, None


def _remaining(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TaxonServiceError("the time budget for this matching run was used up")
    return remaining


def _get_json(method, url, deadline=None, **kwargs):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = _remaining(deadline)
        timeout = REQUEST_TIMEOUT if remaining is None else min(REQUEST_TIMEOUT, remaining)
        try:
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                raise TaxonServiceError(f"{url} unreachable: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == MAX_ATTEMPTS:
                raise TaxonServiceError(f"{url} returned HTTP {response.status_code}")
            retry_after = response.headers.get("Retry-After", "")
            time.sleep(min(int(retry_after), 30) if retry_after.isdigit() else 2 ** attempt)
            continue
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise TaxonServiceError(f"{url} returned HTTP {response.status_code}: {response.text[:300]}")
        return response.json()
    return None


def _rank(value):
    return str(value or "").lower() or None


def _usage(usage, classification=None):
    if not usage:
        return None
    ranks = {}
    for item in classification or []:
        rank = _rank(item.get("rank"))
        if rank in CLASSIFICATION_RANKS and item.get("name"):
            ranks[rank] = item["name"]
    return {
        "id": usage.get("key") or usage.get("id"),
        "scientificName": usage.get("canonicalName") or usage.get("name"),
        "scientificNameAuthorship": usage.get("authorship") or None,
        "label": usage.get("name") or usage.get("label"),
        "taxonRank": _rank(usage.get("rank")),
        "status": _rank(usage.get("status")),
        "classification": ranks,
    }


def summarize_match(payload):
    """Reduce a GBIF v2 match response to what review and write-back need."""
    payload = payload or {}
    diagnostics = payload.get("diagnostics") or {}
    usage = _usage(payload.get("usage"), payload.get("classification"))
    match_type = str(diagnostics.get("matchType") or "NONE").upper()
    accepted = None
    if payload.get("synonym") and payload.get("acceptedUsage"):
        accepted = _usage(payload["acceptedUsage"])
    alternatives = []
    for alternative in diagnostics.get("alternatives") or []:
        alt_usage = _usage(alternative.get("usage"), alternative.get("classification"))
        if not alt_usage:
            continue
        alt_diagnostics = alternative.get("diagnostics") or {}
        alternatives.append({
            **alt_usage,
            "matchType": str(alt_diagnostics.get("matchType") or "").upper() or None,
            "confidence": alt_diagnostics.get("confidence"),
        })
        if len(alternatives) >= MAX_ALTERNATIVES:
            break
    return {
        "matchType": match_type,
        "status": MATCH_STATUS.get(match_type, "ambiguous"),
        "confidence": diagnostics.get("confidence"),
        "note": diagnostics.get("note"),
        "usage": usage,
        "acceptedUsage": accepted,
        "alternatives": alternatives,
    }


def _query_params(query):
    params = {"scientificName": query["scientificName"]}
    for rank in HINT_RANKS:
        if query.get(rank):
            params[rank] = query[rank]
    return params


def match_col(queries, deadline=None):
    """Match many names against COL XR through GBIF; returns one summary per query, in order.

    Names go through the batch endpoint first. The batch response omits alternatives, and it has
    been seen to miss names a single request suggests, so anything that is not an exact or variant
    match is retried individually with verbose output.
    """
    checklist_key = col_checklist_key()
    unique = {}
    for query in queries:
        unique.setdefault(json.dumps(_query_params(query), sort_keys=True), _query_params(query))
    keys = list(unique)
    results = {}
    for start in range(0, len(keys), BATCH_SIZE):
        chunk = keys[start:start + BATCH_SIZE]
        payload = _get_json(
            "POST",
            GBIF_MATCH_URL,
            deadline=deadline,
            params={"checklistKey": checklist_key},
            json=[unique[key] for key in chunk],
        ) or []
        if len(payload) != len(chunk):
            raise TaxonServiceError(
                f"GBIF batch matcher returned {len(payload)} results for {len(chunk)} names."
            )
        for key, item in zip(chunk, payload):
            results[key] = summarize_match(item)
    retry = [key for key, summary in results.items() if summary["status"] not in {"exact", "variant"}]

    def verbose_match(key):
        return _get_json(
            "GET",
            GBIF_MATCH_URL,
            deadline=deadline,
            params={**unique[key], "checklistKey": checklist_key, "verbose": "true"},
        )

    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
        for key, single in zip(retry, pool.map(verbose_match, retry)):
            if single is not None:
                results[key] = summarize_match(single)
    return [
        _without_hint_echo(results[json.dumps(_query_params(query), sort_keys=True)], query)
        for query in queries
    ]


def _without_hint_echo(summary, query):
    """A higher-rank match that is only the classification hint we sent is no match at all."""
    usage = summary.get("usage") or {}
    rank = usage.get("taxonRank")
    if (
        summary.get("status") == "higher_rank"
        and rank in HINT_RANKS
        and str(query.get(rank) or "").lower() == str(usage.get("scientificName") or "").lower()
    ):
        return {**summary, "status": "none", "usage": None, "hintOnly": True}
    return summary


def col_release(deadline=None):
    """Describe the COL release GBIF's matcher currently serves, for provenance."""
    metadata = _get_json(
        "GET", GBIF_MATCH_METADATA_URL, deadline=deadline, params={"checklistKey": col_checklist_key()},
    ) or {}
    index = metadata.get("mainIndex") or {}
    return {
        "checklistKey": index.get("datasetKey") or col_checklist_key(),
        "alias": index.get("datasetAlias"),
        "checklistBankDatasetKey": index.get("clbDatasetKey"),
        "created": metadata.get("created"),
    }


def _compact_aid(summary):
    usage = summary.get("usage") or {}
    return {
        "matchType": summary.get("matchType"),
        "status": summary.get("status"),
        "id": usage.get("id"),
        "scientificName": usage.get("scientificName"),
        "scientificNameAuthorship": usage.get("scientificNameAuthorship"),
        "taxonRank": usage.get("taxonRank"),
        "taxonomicStatus": usage.get("status"),
    }


def review_aids(query, deadline=None):
    """Backbone and ChecklistBank XR results for a name GBIF's COL copy could not place exactly."""
    params = _query_params(query)
    backbone = summarize_match(_get_json("GET", GBIF_MATCH_URL, deadline=deadline, params=params))
    clb = _get_json(
        "GET",
        f"{CHECKLISTBANK_API}/dataset/{col_checklistbank_dataset()}/match/nameusage",
        deadline=deadline,
        params={"q": params["scientificName"], **{k: v for k, v in params.items() if k != "scientificName"}},
    ) or {}
    clb_usage = clb.get("usage") or {}
    clb_type = str(clb.get("type") or "NONE").upper()
    return {
        "gbifBackbone": _compact_aid(backbone),
        "checklistBankXR": {
            "matchType": clb_type,
            "status": MATCH_STATUS.get(clb_type, "ambiguous"),
            "id": clb_usage.get("id"),
            "scientificName": clb_usage.get("name"),
            "scientificNameAuthorship": clb_usage.get("authorship"),
            "taxonRank": _rank(clb_usage.get("rank")),
            "taxonomicStatus": _rank(clb_usage.get("status")),
        },
    }


def search_col(text, limit=10):
    """Name suggestions from COL XR for a reviewer's manual choice; ids are COL usage ids."""
    payload = _get_json(
        "GET",
        f"{CHECKLISTBANK_API}/dataset/{col_checklistbank_dataset()}/nameusage/suggest",
        deadline=time.monotonic() + INTERACTIVE_BUDGET_SECONDS,
        params={"q": text, "limit": limit},
    ) or []
    return [
        {
            "id": item.get("usageId"),
            "label": item.get("match"),
            "taxonRank": _rank(item.get("rank")),
            "status": _rank(item.get("status")),
            "context": item.get("context"),
            "suggestion": item.get("suggestion"),
        }
        for item in payload
        if item.get("usageId")
    ]


def resolve_col_usage(usage_id):
    """Fetch one COL XR usage chosen by a reviewer, with its classification."""
    dataset = col_checklistbank_dataset()
    deadline = time.monotonic() + INTERACTIVE_BUDGET_SECONDS
    usage = _get_json("GET", f"{CHECKLISTBANK_API}/dataset/{dataset}/nameusage/{usage_id}", deadline=deadline)
    if not usage:
        raise TaxonServiceError(f"COL usage {usage_id} was not found in ChecklistBank {dataset}.")
    name = usage.get("name") or {}
    status = _rank(usage.get("status"))
    classification_id = usage_id
    if status and status != "accepted" and (usage.get("accepted") or {}).get("id"):
        classification_id = usage["accepted"]["id"]
    classification = _get_json(
        "GET", f"{CHECKLISTBANK_API}/dataset/{dataset}/taxon/{classification_id}/classification",
        deadline=deadline,
    ) or []
    resolved = _usage(
        {
            "id": usage.get("id"),
            "name": usage.get("label") or name.get("scientificName"),
            "canonicalName": name.get("scientificName"),
            "authorship": name.get("authorship"),
            "rank": name.get("rank"),
            "status": status,
        },
        classification,
    )
    if resolved["taxonRank"] in CLASSIFICATION_RANKS:
        resolved["classification"][resolved["taxonRank"]] = resolved["scientificName"]
    return resolved


# --- Dataset-level workflow -------------------------------------------------------------------

# Qualifiers that express uncertainty rather than rank; "sp." is carried by the genus rank.
UNCERTAIN_QUALIFIERS = ("cf.", "aff.", "nr.")


class MatchScopeError(ValueError):
    pass


def _clean_label(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = " ".join(str(value).split())
    return text or None


def _row_keys(df, verbatim_column, context_column=None):
    """(label, context_key) for every table row, positionally; label is None when blank."""
    if verbatim_column not in df.columns:
        raise KeyError(f"Column '{verbatim_column}' is not in the table.")
    if context_column and context_column not in df.columns:
        raise KeyError(f"Context column '{context_column}' is not in the table.")
    labels = [_clean_label(value) for value in df[verbatim_column].tolist()]
    if context_column:
        contexts = [(_clean_label(value) or "")[:200] for value in df[context_column].tolist()]
    else:
        contexts = [""] * len(labels)
    return list(zip(labels, contexts))


def distinct_labels(df, verbatim_column, context_column=None):
    """Return {(label, context_key): record_count} for the populated labels in a table."""
    counts = {}
    for label, context in _row_keys(df, verbatim_column, context_column):
        if label:
            counts[(label, context)] = counts.get((label, context), 0) + 1
    return counts


def default_query(label):
    name, qualifier = split_qualifier(label)
    return {"scientificName": name}, qualifier


def _scope(dataset, source_table, context_column):
    from api.models import TaxonNameMatch

    return TaxonNameMatch.objects.filter(
        dataset=dataset, source_table=source_table, context_column=context_column or "",
    )


def _check_single_context_configuration(dataset, source_table, context_column):
    from api.models import TaxonNameMatch

    other = (
        TaxonNameMatch.objects.filter(dataset=dataset, source_table=source_table, record_count__gt=0)
        .exclude(context_column=context_column or "")
        .values_list("context_column", flat=True)
        .first()
    )
    if other is not None:
        raise MatchScopeError(
            f"Labels in `{source_table}` were matched "
            + (f"with context column `{other}`" if other else "without a context column")
            + "; use the same configuration."
        )


def record_matches(dataset, source_table, df, verbatim_column, context_column=None, overrides=None,
                   hints=None, budget_seconds=RUN_BUDGET_SECONDS):
    """Create or refresh TaxonNameMatch rows for one table and match what needs matching.

    ``overrides`` maps (label, context_key) to {"query": {...}, "note": str} for labels whose
    matchable name differs from the label (vernacular, abbreviated genus, typo). A new
    interpretation of a reviewed label reopens it for review. Matching stops at the time budget;
    unmatched rows are matched by the next call. Returns (rows for the current labels, finished).
    """
    from django.utils import timezone
    from api.models import TaxonNameMatch

    context_column = context_column or ""
    _check_single_context_configuration(dataset, source_table, context_column)
    overrides = overrides or {}
    hints = {rank: value for rank, value in (hints or {}).items() if rank in HINT_RANKS and value}
    counts = distinct_labels(df, verbatim_column, context_column)
    existing = {(row.verbatim_label, row.context_key): row for row in _scope(dataset, source_table, context_column)}

    to_match = []
    current = []
    for key, count in counts.items():
        label, context_key = key
        row = existing.get(key) or TaxonNameMatch(
            dataset=dataset, source_table=source_table, context_column=context_column,
            verbatim_label=label, context_key=context_key,
        )
        row.record_count = count
        row.identification_qualifier = default_query(label)[1] or ""
        if key in overrides:
            query, note = {**hints, **overrides[key]["query"]}, overrides[key].get("note") or ""
        elif row.query:
            query, note = row.query, row.preprocessing_note
        else:
            query, note = {**hints, **default_query(label)[0]}, ""
        if row.decision != TaxonNameMatch.Decision.PENDING and query != row.decided_query:
            # The reviewer decided on a different interpretation; ask again.
            _reset_decision(row)
        changed = query != row.query or not row.matched_at
        row.query, row.preprocessing_note = query, note
        if changed:
            row.matched_at = None
            to_match.append(row)
        row.save()
        current.append(row)

    stale = [row.id for key, row in existing.items() if key not in counts]
    if stale:
        TaxonNameMatch.objects.filter(id__in=stale).update(record_count=0)

    deadline = time.monotonic() + budget_seconds
    # Rows matched by an earlier call whose review aids did not finish.
    aid_rows = [row for row in current if row.matched_at and _aids_pending(row)]
    saved = 0
    try:
        release = col_release(deadline) if to_match else {}
        # Small chunks are saved as they finish, so a run cut short by the budget still progresses.
        for start in range(0, len(to_match), PROGRESS_CHUNK):
            chunk = to_match[start:start + PROGRESS_CHUNK]
            summaries = match_col([row.query for row in chunk], deadline=deadline)
            now = timezone.now()
            for row, summary in zip(chunk, summaries):
                row.match = summary
                row.col_release = release
                row.matched_at = now
                row.review_aids = {} if summary["status"] in {"exact", "variant"} else {"pending": True}
                row.save(update_fields=["match", "col_release", "matched_at", "review_aids", "updated_at"])
                saved += 1
                if _aids_pending(row):
                    aid_rows.append(row)
        saved += _fill_review_aids(aid_rows, deadline)
    except TaxonServiceError:
        if not saved:
            raise
        return current, False
    return current, True


def _aids_pending(row):
    return bool((row.review_aids or {}).get("pending"))


def _fill_review_aids(rows, deadline):
    """Fetch review aids concurrently, saving each row as soon as its aids arrive."""
    saved = 0
    pool = ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS)
    try:
        futures = {pool.submit(review_aids, row.query, deadline): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            row.review_aids = future.result()
            row.save(update_fields=["review_aids", "updated_at"])
            saved += 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return saved


def matching_complete(rows):
    """True when every current label has a match and its review aids."""
    return all(row.matched_at and not _aids_pending(row) for row in rows if row.record_count)


def is_preprocessed(row):
    """True when the matched name came from an interpretation rather than the label itself."""
    return row.query.get("scientificName") != default_query(row.verbatim_label)[0]["scientificName"]


def usage_for_decision(row, usage_id=None):
    """The usage a reviewer accepts: the suggestion, one of its alternatives, or a searched id."""
    suggestion = (row.match or {}).get("usage")
    if usage_id is None or (suggestion and suggestion.get("id") == usage_id):
        if not suggestion:
            raise ValueError("There is no suggested COL name to accept for this label.")
        return {**suggestion, "source": "gbif_col"}
    for alternative in (row.match or {}).get("alternatives") or []:
        if alternative.get("id") == usage_id:
            return {
                key: alternative.get(key)
                for key in ("id", "scientificName", "scientificNameAuthorship", "label",
                            "taxonRank", "status", "classification")
            } | {"source": "gbif_col"}
    return {**resolve_col_usage(usage_id), "source": "checklistbank_xr"}


def _reset_decision(row):
    from api.models import TaxonNameMatch

    row.decision = TaxonNameMatch.Decision.PENDING
    row.decided_usage = {}
    row.decided_query = {}
    row.decided_by = None
    row.decided_at = None


def decide(row, decision, user=None, usage_id=None, name=None):
    """Record one reviewer decision. ``name`` carries the reviewer's name for NOT_IN_COL."""
    from django.utils import timezone
    from api.models import TaxonNameMatch

    Decision = TaxonNameMatch.Decision
    if decision == Decision.PENDING:
        _reset_decision(row)
        row.save(update_fields=["decision", "decided_usage", "decided_query", "decided_by", "decided_at", "updated_at"])
        return row
    if not row.matched_at:
        raise ValueError("This label has not been matched yet.")
    if decision == Decision.ACCEPTED:
        usage = usage_for_decision(row, usage_id)
    elif decision == Decision.NOT_IN_COL:
        name = name or {}
        scientific_name = " ".join(str(name.get("scientificName") or "").split())
        if not scientific_name:
            scientific_name = row.query.get("scientificName") or ""
        if not scientific_name:
            raise ValueError("A scientific name is required when the name is not in COL.")
        # The higher-rank placement COL did find is still a valid classification for the name.
        suggestion = (row.match or {}).get("usage") or {}
        classification = dict(suggestion.get("classification") or {}) if (
            (row.match or {}).get("status") == "higher_rank"
        ) else {}
        usage = {
            "id": None,
            "scientificName": scientific_name,
            "scientificNameAuthorship": name.get("scientificNameAuthorship") or None,
            "taxonRank": _rank(name.get("taxonRank")),
            "classification": classification,
            "source": "reviewer",
        }
    elif decision == Decision.KEEP_ORIGINAL:
        usage = {}
    else:
        raise ValueError(f"Unknown decision '{decision}'.")
    row.decision = decision
    row.decided_usage = usage
    row.decided_query = row.query
    row.decided_by = user
    row.decided_at = timezone.now()
    row.save(update_fields=["decision", "decided_usage", "decided_query", "decided_by", "decided_at", "updated_at"])
    return row


def bulk_acceptable(row):
    """Exact matches of names written in the label, without any identification qualifier.

    A variant or higher-rank match is a different name; an interpreted name (translation, expanded
    abbreviation, typo fix) and a qualified label ("sp.", "cf.") each need a reviewer's own look.
    """
    from api.models import TaxonNameMatch

    match = row.match or {}
    return (
        row.decision == TaxonNameMatch.Decision.PENDING
        and row.record_count > 0
        and row.matched_at is not None  # a changed interpretation awaiting its new match
        and match.get("matchType") == "EXACT"
        and bool(match.get("usage"))
        and not row.identification_qualifier
        and not is_preprocessed(row)
    )


def accept_exact_matches(dataset, user=None, source_table=None, context_column=None):
    from api.models import TaxonNameMatch

    rows = TaxonNameMatch.objects.filter(dataset=dataset, decision=TaxonNameMatch.Decision.PENDING)
    if source_table is not None:
        rows = rows.filter(source_table=source_table, context_column=context_column or "")
    return [decide(row, TaxonNameMatch.Decision.ACCEPTED, user=user) for row in rows if bulk_acceptable(row)]


def _is_blank(value):
    return _clean_label(value) is None


def apply_decisions(dataset, table, verbatim_column, context_column=None):
    """Write reviewed names onto a table's rows; unreviewed and kept labels are left unchanged.

    The reviewed name, authorship and rank replace the current values (only with values the
    decision has). Higher classification only fills blank cells, so supplied taxonomy is never
    overwritten. verbatim_column is only read. Returns a summary dict.
    """
    from django.utils import timezone
    from api.dwc_dp_specs import get_table_spec, normalize_resource_name
    from api.models import TaxonNameMatch

    source_table = normalize_resource_name(table.title)
    context_column = context_column or ""
    _check_single_context_configuration(dataset, source_table, context_column)
    df = table.df.copy()
    keys = _row_keys(df, verbatim_column, context_column)
    try:
        schema_fields = set(get_table_spec(source_table).fields)
    except KeyError:
        schema_fields = None  # a working table: any Darwin Core term may be written

    def writable(column):
        return column in df.columns or schema_fields is None or column in schema_fields

    positions = {}
    for position, key in enumerate(keys):
        if key[0]:
            positions.setdefault(key, []).append(position)

    written = {"rows_updated": 0, "labels_applied": 0, "pending_labels": 0, "pending_rows": 0,
               "kept_labels": 0, "unmatched_labels": 0, "skipped_columns": set(), "qualifiers_not_written": 0}
    rows = {(row.verbatim_label, row.context_key): row for row in _scope(dataset, source_table, context_column)}
    applied = []
    for key, row_positions in positions.items():
        row = rows.get(key)
        if row is None:
            written["unmatched_labels"] += 1
            continue
        if row.decision == TaxonNameMatch.Decision.PENDING:
            written["pending_labels"] += 1
            written["pending_rows"] += len(row_positions)
            continue
        if row.decision == TaxonNameMatch.Decision.KEEP_ORIGINAL:
            written["kept_labels"] += 1
            continue
        usage = row.decided_usage or {}
        replace = {
            column: usage.get(column)
            for column in ("scientificName", "scientificNameAuthorship", "taxonRank")
            if usage.get(column)
        }
        fill = dict(usage.get("classification") or {})
        if row.identification_qualifier:
            fill["identificationQualifier"] = row.identification_qualifier
            if not writable("identificationQualifier"):
                written["qualifiers_not_written"] += 1
        for column, value in [*replace.items(), *fill.items()]:
            if not writable(column):
                written["skipped_columns"].add(column)
                continue
            if column not in df.columns:
                df[column] = None
            if df[column].dtype != object:
                df[column] = df[column].astype(object)
            column_index = df.columns.get_loc(column)
            for position in row_positions:
                if column in replace or _is_blank(df.iat[position, column_index]):
                    df.iat[position, column_index] = value
        written["rows_updated"] += len(row_positions)
        written["labels_applied"] += 1
        applied.append(row.id)

    if applied:
        table.df = df
        table.save()
        TaxonNameMatch.objects.filter(id__in=applied).update(applied_at=timezone.now())
    written["skipped_columns"] = sorted(written["skipped_columns"])
    return written
