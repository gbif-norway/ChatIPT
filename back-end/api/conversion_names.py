"""Scientific-name checks for archive conversion.

After inspection a `names` job collects the distinct source name labels of the Occurrence and
Identification tables, parses each with GBIF's name parser and matches it against Catalogue of Life XR
through the publication workflow's matcher (api.taxon_matching). Results and the user's decisions live in
`DwcConversion.name_review`. Nothing here is required for conversion: network failures only record a status,
and `convert()` itself never calls a service.

Policy: exact same-name matches and resolvable uncertain stems may be accepted automatically. Bulk and automatic decisions
never write a coarser or different taxon. `verbatimIdentification` is unchanged; COL usage ids are report provenance, not taxonID.
Reviewed decisions are applied to the converted frames by `apply_name_decisions`, a pure function.
"""
import copy
import logging
import re
import secrets
import time
from collections import Counter, defaultdict

from django.conf import settings
from django.utils import timezone

from api import taxon_matching
from api.taxon_matching import (HINT_RANKS, MAX_ALTERNATIVES, NAME_RULES_VERSION, RANK_ORDER, TaxonServiceError, authorships_agree, col_release,
                                implied_rank, match_col, name_change, name_parts, split_qualifier)

logger = logging.getLogger(__name__)

DWC = 'http://rs.tdwg.org/dwc/terms/'
NAME = DWC + 'scientificName'
GBIF_PARSER_URL = 'https://api.gbif.org/v1/parser/name'
# Source tables whose names are reviewed, and the converted tables the decisions are applied to.
SOURCE_TABLES = {DWC + 'Occurrence': 'occurrence', DWC + 'Identification': 'identification'}
OUTPUT_TABLES = ('occurrence', 'identification')
MAX_LABEL_CHARS = 500  # longer cells are not names; they never reach the state or a service
MAX_CONTEXT_CHARS = 100
MAX_LABELS = 5000  # most frequent first; the rest keep the converter's output
CHUNK = 100  # labels matched, parsed and saved together
MAX_RUNS = 30
LEASE_SECONDS = 900  # a names run is bounded by its budget, so a silent worker is reclaimed sooner than a review
PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
DECISIONS = ('parsed', 'col', 'alternative', 'keep', 'empty', 'stem')
STATUSES = ('none', 'pending', 'running', 'incomplete', 'complete', 'error')
REVIEW_STATUSES = {'review', 'reviewing'}
# GBIF's parser reports a rank marker rather than a rank; only these are mapped to a DwC taxonRank.
RANK_MARKERS = {'sp.': 'species', 'subsp.': 'subspecies', 'var.': 'variety', 'f.': 'form',
                'subvar.': 'subvariety', 'subf.': 'subform', 'agg.': 'species aggregate'}
CONTEXT_RANKS = ('kingdom', 'phylum', 'class', 'family')
MAX_AUTHORSHIPS = 5  # distinct supplied authorships kept per label; more is "too many to agree"
MAX_SOURCE_QUALIFIERS = 5
_OVERLONG_SOURCE_ID = '\0overlong'
UNCERTAIN_QUALIFIERS = {'sp.', 'spp.', 'indet.'}
GROUP_OPTIONS = {
    'auto': ['col', 'parsed', 'keep'],
    'uncertain': ['stem', 'keep'],
    'spelling': ['col', 'mine'],
    'unconfirmed': ['mine'],
    'check': ['col', 'mine'],
}


class NameDecisionError(ValueError):
    pass


def enabled():
    return bool(getattr(settings, 'CONVERSION_NAME_CHECKS_ENABLED', False))


def budget_seconds():
    return float(getattr(settings, 'CONVERSION_NAME_BUDGET_SECONDS', taxon_matching.RUN_BUDGET_SECONDS))


def normal(value, limit=None):
    """Whitespace-normalised text, or None for a cell far too long to be a name (never split or copied)."""
    text = str(value if value is not None else '')
    if limit is not None and len(text) > limit * 4:
        return None
    return ' '.join(text.split())


# Collection ------------------------------------------------------------------------------------

def collect_state(archive, plan):
    """Distinct labels with row counts and consistent classification context, most frequent first."""
    found = {}
    too_long = {'rows': 0, 'hashes': set()}
    for table in archive.tables:
        target = SOURCE_TABLES.get(table.row_type)
        if target is None or NAME not in table.terms:
            continue
        name_at = table.terms.index(NAME)
        context_at = {rank: table.terms.index(DWC + rank) for rank in (*HINT_RANKS, 'taxonRank', 'scientificNameAuthorship')
                      if DWC + rank in table.terms}
        for row in table.rows:
            label = normal(row[name_at], MAX_LABEL_CHARS)
            if label is None or len(label) > MAX_LABEL_CHARS:
                too_long['rows'] += 1
                too_long['hashes'].add(hash(row[name_at]))
                continue
            if not label:
                continue
            record = found.setdefault(label, {'label': label, 'rows': 0, 'tables': {}, 'context': defaultdict(set),
                                              'source_ids': defaultdict(set), 'source_qualifiers': set(),
                                              'source_qualifier_rows': 0, 'source_qualifier_counts': Counter(),
                                              'source_qualifiers_truncated': False,
                                              'has_qualifier_column': False})
            record['rows'] += 1
            record['tables'][target] = record['tables'].get(target, 0) + 1
            for rank, index in context_at.items():
                hint = normal(row[index], MAX_CONTEXT_CHARS)
                if hint and len(hint) <= MAX_CONTEXT_CHARS:
                    record['context'][rank].add(hint)
            for field in ('scientificNameID', 'taxonID'):
                term = DWC + field
                if term in table.terms:
                    raw = row[table.terms.index(term)]
                    value = normal(raw, 300)
                    if len(record['source_ids'][field]) < 2:
                        if value and len(value) <= 300:
                            record['source_ids'][field].add(value)
                        elif (value and len(value) > 300) or (value is None and str(raw or '').strip()):
                            record['source_ids'][field].add(_OVERLONG_SOURCE_ID)
            qualifier_term = DWC + 'identificationQualifier'
            if qualifier_term in table.terms:
                record['has_qualifier_column'] = True
                raw = row[table.terms.index(qualifier_term)]
                value = normal(raw, MAX_CONTEXT_CHARS)
                if value and len(value) <= MAX_CONTEXT_CHARS:
                    record['source_qualifier_rows'] += 1
                    qualifier = _normal_qualifier(value)
                    if qualifier in record['source_qualifiers']:
                        record['source_qualifier_counts'][qualifier] += 1
                    elif len(record['source_qualifiers'] - {'\0overlong'}) < MAX_SOURCE_QUALIFIERS:
                        record['source_qualifiers'].add(qualifier)
                        record['source_qualifier_counts'][qualifier] += 1
                    else:
                        record['source_qualifiers_truncated'] = True
                elif str(raw or '').strip():
                    record['source_qualifier_rows'] += 1
                    record['source_qualifiers'].add('\0overlong')
                    record['source_qualifier_counts']['\0overlong'] += 1
    ordered = sorted(found.values(), key=lambda record: (-record['rows'], record['label']))
    labels = []
    for record in ordered[:MAX_LABELS]:
        # Context is a matching hint only when the label's rows agree on it.
        context = {rank: next(iter(values)) for rank, values in record['context'].items() if len(values) == 1}
        qualifier = split_qualifier(record['label'])[1]
        # The supplied authorships, so that accepting COL's in bulk never silently rewrites a different one.
        authorships = sorted(record['context'].get('scientificNameAuthorship', ()))
        mixed_hints = sorted(rank for rank in ('kingdom', 'phylum', 'class')
                             if len({_hint_normal(value) for value in record['context'].get(rank, ()) if _hint_normal(value)}) > 1)
        source_ids = {}
        for field, values in record['source_ids'].items():
            if len(values) == 1:
                value = next(iter(values))
                if re.match(r'^(urn:lsid:|https?://)', value, re.IGNORECASE):
                    source_ids[field] = value
        item = {'label': record['label'], 'rows': record['rows'], 'tables': record['tables'],
                       'hints': {rank: value for rank, value in context.items() if rank in HINT_RANKS},
                       'mixed_hints': mixed_hints,
                       'source_rank': context.get('taxonRank', '').lower() or None, 'qualifier': qualifier,
                       'source_authorships': authorships[:MAX_AUTHORSHIPS], 'authorships_truncated': len(authorships) > MAX_AUTHORSHIPS,
                       'source_ids': source_ids}
        if record['has_qualifier_column'] and record['source_qualifiers']:
            qualifiers = sorted(record['source_qualifiers'] - {'\0overlong'})
            kept_qualifiers = qualifiers[:MAX_SOURCE_QUALIFIERS]
            if '\0overlong' in record['source_qualifiers']:
                kept_qualifiers.append('\0overlong')
            kept_counts = {_normal_qualifier(value) for value in kept_qualifiers if value != '\0overlong'}
            if '\0overlong' in kept_qualifiers:
                kept_counts.add('\0overlong')
            item.update(source_qualifiers=sorted(kept_qualifiers),
                        source_qualifier_rows=record['source_qualifier_rows'],
                        source_qualifier_counts={key: count for key, count in record['source_qualifier_counts'].items()
                                                 if key in kept_counts},
                        qualifiers_truncated=record['source_qualifiers_truncated'])
        labels.append(item)
    return {'plan_id': plan['id'], 'status': 'pending' if labels else 'none', 'error': '', 'runs': 0,
            'truncated': max(len(ordered) - MAX_LABELS, 0),
            'skipped_long': {'labels': len(too_long['hashes']), 'rows': too_long['rows']}, 'col_release': {}, 'labels': labels, 'decisions': {}}


def carry_decisions(conversion, fresh, plan):
    """A new inspection's name state with the user's own name decisions carried over from the previous plan.

    A re-inspection (a new rule version, or the user asking) would otherwise drop every name decision. They are
    carried by label when the source is unchanged (same fingerprint) or yields exactly the same labels, and are checked
    again under the current rules: the stamp of the check they passed is dropped, so a COL name that now counts as a
    coarser or different taxon is held until confirmed (an explicit earlier confirmation stands). Bulk decisions are
    not carried; the bulk actions are offered again under the current rules. Name results are not carried either:
    the names are checked again.
    """
    previous = conversion.name_review or {}
    old_plan = conversion.plan or {}
    decisions = previous.get('decisions') or {}
    if not fresh or not (decisions or previous.get('auto_declined')) or previous.get('plan_id') != old_plan.get('id'):
        return fresh
    records = {record['label']: record for record in fresh['labels']}
    before = {record['label']: record for record in previous.get('labels', [])}
    same_source = bool(old_plan.get('source_sha256')) and old_plan.get('source_sha256') == plan.get('source_sha256')
    same_labels = set(records) == set(before)

    def same_context(label):
        # Another source with the same labels: a decision carries only where the name's supplied context is unchanged.
        return same_source or (same_labels and label in before and all(records[label].get(key) == before[label].get(key)
                                                   for key in ('hints', 'source_rank', 'source_authorships', 'qualifier',
                                                               'source_qualifiers', 'source_ids', 'mixed_hints')))
    carried, bulk, dropped = {}, 0, 0
    auto_declined = [label for label in (previous.get('auto_declined') or [])
                     if label in records and same_context(label)]
    for label, decision in decisions.items():
        by = str(decision.get('by') or 'user')
        if by.startswith('auto:'):
            continue
        if by.startswith('bulk:'):
            bulk += 1
            continue
        if label not in records or not same_context(label):
            if by == 'user':
                dropped += 1
            continue
        if by != 'user':
            continue
        carried[label] = {**{key: value for key, value in decision.items() if key not in {'changeKind', 'nameRules'}},
                          'carriedFrom': previous['plan_id']}
    if not carried and not bulk and not dropped and not auto_declined:
        return fresh
    # Carried COL decisions are checked against fresh matches, so the names are checked even when checks are off.
    requested = bool(previous.get('requested') or any(decision['decision'] in {'col', 'alternative'} for decision in carried.values()))
    return {**fresh, 'decisions': carried, 'auto_declined': auto_declined,
            'requested': requested or fresh.get('requested', False),
            'carried': {'decisions': len(carried), 'bulk_not_carried': bulk, 'dropped': dropped}}


def current(conversion):
    """The stored name review when it belongs to the conversion's current plan."""
    state = conversion.name_review or {}
    plan_id = (conversion.plan or {}).get('id')
    return state if plan_id and state.get('plan_id') == plan_id else {}


def name_question_ids(conversion):
    """The plan's scientificName questions; the name check answers them per name when it has names to check."""
    state = current(conversion)
    # Only a working name check takes over the question: checks enabled, or requested by the user.
    if not state.get('labels') or not (enabled() or state.get('requested')):
        return []
    return [issue['id'] for issue in (conversion.plan or {}).get('issues', []) if issue.get('kind') == 'name-semantics']


def every_name_decided(conversion):
    state = current(conversion)
    decisions = state.get('decisions') or {}
    held = unconfirmed(state)
    return (bool(state.get('labels')) and not state.get('truncated') and not (state.get('skipped_long') or {}).get('labels')
            and all(record['label'] in decisions and record['label'] not in held for record in state['labels']))


def settle_name_questions(conversion):
    """Once every name has a decision, the scientificName fallback applies to no row; record that instead of asking.

    The caller holds the conversion lock. Withdrawing a name decision later leaves this safe fallback in place:
    an undecided name keeps its text in verbatimIdentification.
    """
    if not every_name_decided(conversion):
        return []
    from api.conversion_review import apply_decision_changes
    open_ids = [identifier for identifier in name_question_ids(conversion) if identifier not in conversion.decisions]
    if open_ids:
        apply_decision_changes(conversion, dict.fromkeys(open_ids, 'preserve'), 'system',
                               rationale='Every scientific name has a decision in the name check, so this fallback applies to no row. '
                                         'The supplied text stays in verbatimIdentification.')
    return open_ids


def pending(conversion):
    """True when name checks should (still) run: unchecked names, under the run limit, and wanted."""
    state = current(conversion)
    return (bool(state.get('labels')) and state.get('status') in {'pending', 'incomplete'} and state.get('runs', 0) < MAX_RUNS
            and (enabled() or bool(state.get('requested'))))


def checked(record):
    return 'match' in record and 'parsed' in record


# Parsing and matching ----------------------------------------------------------------------------

def summarize_parse(label, item):
    """What review needs from one GBIF name-parser result."""
    item = item or {}
    with_marker = item.get('canonicalNameWithMarker') or item.get('canonicalName') or ''
    complete = item.get('canonicalNameComplete') or ''
    usable = bool(item.get('parsed')) and not item.get('parsedPartially') and item.get('type') == 'SCIENTIFIC' and bool(with_marker)
    authorship = None
    if with_marker and complete.startswith(with_marker + ' '):
        authorship = complete[len(with_marker):].strip() or None  # the full authorship, year and brackets included
    elif item.get('authorship') and complete != with_marker:
        authorship = item['authorship']
    rank = str(item.get('rank') or '').lower().replace('_', ' ') or RANK_MARKERS.get(item.get('rankMarker'))
    if rank in {'unranked', 'infraspecific name', 'infrasubspecific name', 'informal'}:
        rank = None
    return {'type': item.get('type'), 'usable': usable, 'canonical': with_marker or None, 'authorship': authorship,
            'rank': rank or None,
            # The parts rebuild the supplied text exactly, so splitting loses and renames nothing.
            'lossless': usable and complete == normal(label), 'splits': usable and bool(authorship)}


def parse_names(labels, deadline=None):
    """One summary per label from GBIF's name parser (batch POST)."""
    if not labels:
        return []
    payload = taxon_matching._get_json('POST', GBIF_PARSER_URL, deadline=deadline, json=list(labels)) or []
    if not isinstance(payload, list) or len(payload) != len(labels):
        raise TaxonServiceError(f'GBIF name parser returned {len(payload) if isinstance(payload, list) else "an invalid response"} '
                                f'results for {len(labels)} names.')
    return [summarize_parse(label, item) for label, item in zip(labels, payload)]


def compact_usage(usage):
    if not usage:
        return None
    compact = {key: usage.get(key) for key in ('id', 'scientificName', 'scientificNameAuthorship', 'taxonRank', 'status')}
    # Enough classification to tell same-name homonyms apart ("Calanus" the copepod or the grasshopper).
    context = {rank: (usage.get('classification') or {}).get(rank) for rank in CONTEXT_RANKS}
    if any(context.values()):
        compact['classification'] = {rank: value for rank, value in context.items() if value}
    return compact


def compact_match(summary):
    return {'matchType': summary.get('matchType'), 'status': summary.get('status'), 'confidence': summary.get('confidence'),
            'usage': compact_usage(summary.get('usage')), 'acceptedUsage': compact_usage(summary.get('acceptedUsage')),
            'alternatives': [{**compact_usage(alternative), 'matchType': alternative.get('matchType')}
                             for alternative in (summary.get('alternatives') or [])[:MAX_ALTERNATIVES]],
            'hintOnly': bool(summary.get('hintOnly')), 'issues': list(summary.get('issues') or []),
            'matchedId': summary.get('matchedId'),
            **{key: summary[key] for key in ('idCheck', 'disambiguatedBy', 'nameMatch') if key in summary}}


def _exact_same_name(summary, stem):
    usage = summary.get('usage') or {}
    return (summary.get('matchType') == 'EXACT' and bool(usage) and not summary.get('hintOnly')
            and name_parts(usage.get('scientificName')) == name_parts(stem))


def check_chunk(items, deadline):
    """Run matching, ID disambiguation and parsing for one names-job chunk without database access."""
    results = defaultdict(dict)
    error = None
    match_items = [item for item in items if not item.get('match')]
    step1 = {}
    try:
        summaries = match_col([item['query'] for item in match_items], deadline=deadline) if match_items else []
        step1 = {item['label']: compact_match(summary) for item, summary in zip(match_items, summaries)}
        id_items = [item for item in match_items if item.get('ids')
                    and not _exact_same_name(step1[item['label']], split_qualifier(item['label'])[0])]
        step2_queries = [{**item['query'], **item['ids']} for item in id_items]
        step2 = match_col(step2_queries, deadline=deadline) if step2_queries else []
        for item, summary in zip(id_items, step2):
            first = step1[item['label']]
            second = compact_match(summary)
            usage = second.get('usage')
            stem = split_qualifier(item['label'])[0]
            same_name = bool(usage and name_parts(usage.get('scientificName')) == name_parts(stem))
            safe_promotion = (summary.get('matchType') == 'EXACT' and usage and same_name
                              and name_change(stem, usage, 'EXACT') is None
                              and not {'TAXON_ID_NOT_FOUND', 'SCIENTIFIC_NAME_AND_ID_INCONSISTENT'}.intersection(summary.get('issues') or []))
            if safe_promotion:
                promoted = dict(second)
                promoted['disambiguatedBy'] = sorted(item['ids'])
                promoted['nameMatch'] = {key: first.get(key) for key in ('matchType', 'status', 'usage')}
                step1[item['label']] = promoted
            else:
                issues = list(summary.get('issues') or [])
                outcome = ('not_found' if 'TAXON_ID_NOT_FOUND' in issues else
                           'elsewhere' if ('SCIENTIFIC_NAME_AND_ID_INCONSISTENT' in issues
                                           or (summary.get('matchType') == 'EXACT' and usage and not same_name)) else 'no_help')
                first['idCheck'] = {'fields': item['ids'], 'outcome': outcome, 'matchType': summary.get('matchType'),
                                    'usage': compact_usage(usage), 'issues': issues, 'matchedId': second.get('matchedId')}
        for label, summary in step1.items():
            results[label]['match'] = summary
    except TaxonServiceError as exc:
        # The first and ID matches are a unit: the next run retries them together.
        error = exc
    need_parse = [item for item in items if not item.get('parse')]
    plain = [item for item in need_parse if not item.get('qualifier')]
    for item in need_parse:
        if item.get('qualifier'):
            results[item['label']]['parsed'] = {'type': None, 'usable': False, 'canonical': None, 'authorship': None,
                                                'rank': None, 'lossless': False, 'splits': False, 'reason': 'qualifier'}
    try:
        for item, parsed in zip(plain, parse_names([item['label'] for item in plain], deadline=deadline)):
            results[item['label']]['parsed'] = parsed
    except TaxonServiceError as exc:
        error = error or exc
    return dict(results), error


def asserted_name(record):
    """The name the user asserted: the parsed name when the parser could read the label, else the label without its qualifier."""
    parsed = record.get('parsed') or {}
    return parsed['canonical'] if parsed.get('usable') and parsed.get('canonical') else split_qualifier(record['label'])[0]


def asserted_rank(record):
    """The rank of the asserted name, when the name, the parser or same-name COL usages say what it is."""
    parsed = record.get('parsed') or {}
    if parsed.get('usable') and parsed.get('rank'):
        return parsed['rank']
    parts = name_parts(asserted_name(record))
    if len(parts) != 1:
        return implied_rank(parts)
    match = record.get('match') or {}
    same = {usage.get('taxonRank') for usage in [match.get('usage') or {}, *(match.get('alternatives') or [])]
            if usage.get('scientificName') and name_parts(usage['scientificName']) == parts}
    if len(same) == 1 and None not in same:
        return same.pop()
    # A uninomial is at genus rank or above, so a supplied "species" (as for "Larus sp.") says nothing about it.
    source = record.get('source_rank')
    return source if source in RANK_ORDER and RANK_ORDER.index(source) <= RANK_ORDER.index('genus') else None


def change(record, usage, match_type):
    """How accepting a COL usage would change the asserted name (see taxon_matching.name_change); None for the same name."""
    return name_change(asserted_name(record), usage, match_type, asserted_rank(record), record.get('hints'))


def replacement(record, usage, match_type):
    """The change accepting a COL usage would make when it needs explicit confirmation (coarser or different); else None."""
    found = change(record, usage, match_type)
    return found if found and found['confirm'] else None


def same_name(record, usage):
    parts = name_parts(asserted_name(record))
    return bool(parts) and name_parts((usage or {}).get('scientificName')) == parts


def authorship_agrees(record, usage):
    """Every supplied authorship of the label (its column, or the label's own) names COL's authors, or none is supplied.

    When COL has no authorship nothing is overwritten (a same-name decision keeps the supplied one), so it agrees.
    """
    parsed = record.get('parsed') or {}
    supplied = [*record.get('source_authorships', ()), *([parsed['authorship']] if parsed.get('usable') and parsed.get('authorship') else [])]
    if record.get('authorships_truncated'):
        return False
    theirs = (usage or {}).get('scientificNameAuthorship')
    return not normal(theirs) or all(authorships_agree(value, theirs) for value in supplied)


def unconfirmed(state):
    """Labels whose saved COL decision would replace the name but was never confirmed (saved before that was required).

    Such a decision is applied as "keep" and the label counts as undecided until it is made again.
    """
    records = {record['label']: record for record in state.get('labels', [])}
    held = {}
    for label, decision in (state.get('decisions') or {}).items():
        found = _unconfirmed_replacement(records.get(label) or {'label': label}, decision)
        if found:
            held[label] = found
    return held


def _query(record):
    return {**record.get('hints', {}), 'scientificName': split_qualifier(record['label'])[0]}


def _is_budget(exc):
    return 'time budget' in str(exc)


# Decisions ---------------------------------------------------------------------------------------

def _checklist(state):
    release = state.get('col_release') or {}
    return {'checklistKey': release.get('checklistKey') or taxon_matching.col_checklist_key(), 'alias': release.get('alias')}


def _normal_qualifier(value):
    """Return a lower-case qualifier with a trailing period."""
    value = normal(value) or ''
    value = value.casefold()
    return value if value.endswith('.') else value + '.'


def qualifier_kind(record):
    """Classify the label and source-column qualifier as uncertain, doubtful, or absent."""
    qualifier = normal(record.get('qualifier'))
    label = str(record.get('label') or '')
    label_kind = ('doubt' if '?' in label else
                  None if not qualifier else ('uncertain' if _normal_qualifier(qualifier) in UNCERTAIN_QUALIFIERS else 'doubt'))
    values = record.get('source_qualifiers') or []
    if values:
        source_kind = ('uncertain' if not record.get('qualifiers_truncated')
                       and all(_normal_qualifier(value) in UNCERTAIN_QUALIFIERS for value in values)
                       and label_kind in {None, 'uncertain'} else 'doubt')
        label_kind = source_kind
    if label_kind == 'uncertain' and len(name_parts(asserted_name(record))) != 1:
        return 'doubt'
    return label_kind


def formula_qualifier(record):
    """Return the normalised formula qualifier for an uncertain stem label."""
    if qualifier_kind(record) != 'uncertain':
        return None
    if record.get('qualifier'):
        return _normal_qualifier(record['qualifier'])
    values = record.get('source_qualifiers') or []
    for qualifier in ('sp.', 'spp.', 'indet.'):
        if any(_normal_qualifier(value) == qualifier for value in values):
            return qualifier
    return None


def _stem_candidates(record):
    """Return exact COL candidates whose names equal the asserted stem."""
    match = record.get('match') or {}
    stem_parts = name_parts(asserted_name(record))
    candidates = []
    pick = match.get('usage') or {}
    if match.get('matchType') == 'EXACT' and not match.get('hintOnly') and name_parts(pick.get('scientificName')) == stem_parts:
        candidates.append((pick, True))
    candidates.extend((usage, False) for usage in match.get('alternatives') or []
                      if usage.get('matchType') == 'EXACT' and name_parts(usage.get('scientificName')) == stem_parts)
    return candidates


def stem_usage(record):
    """Resolve the exact COL usage for an uncertain label's stem, if unambiguous."""
    candidates = _stem_candidates(record)
    if not candidates:
        return None
    if candidates[0][1]:
        usage = candidates[0][0]
        authorship = usage.get('scientificNameAuthorship')
        if (not authorship_agrees(record, usage)
                or any(not authorships_agree(authorship, other.get('scientificNameAuthorship'))
                       for other, _ in candidates[1:] if other.get('scientificNameAuthorship'))):
            authorship = None
        return {'scientificName': usage.get('scientificName'), 'scientificNameAuthorship': authorship,
                'taxonRank': usage.get('taxonRank'), 'usageId': str(usage['id']) if usage.get('id') is not None else None,
                'candidates': len(candidates)}
    ranks = {usage.get('taxonRank') for usage, _ in candidates}
    if len(ranks) != 1 or None in ranks:
        return None
    usage = candidates[0][0]
    return {'scientificName': usage.get('scientificName'), 'scientificNameAuthorship': None,
            'taxonRank': usage.get('taxonRank'), 'usageId': str(usage['id']) if len(candidates) == 1 and usage.get('id') is not None else None,
            'candidates': len(candidates)}


def _hint_normal(value):
    value = re.sub(r'\s*\([^)]*\)', '', normal(value) or '').strip().casefold()
    return {'metazoa': 'animalia', 'viridiplantae': 'plantae'}.get(value, value)


def _conflicts(record):
    match = record.get('match') or {}
    usage = match.get('usage') or {}
    out = []
    if match.get('matchType') == 'EXACT' and usage and not match.get('hintOnly'):
        # The matched usage is compared, not its accepted name: a synonym the user wrote is still the user's name.
        found = change(record, usage, 'EXACT')
        if found:
            out.append(('name', 'check:name', f"COL returned a different name: {usage.get('scientificName')} ({usage.get('taxonRank') or 'unknown rank'})"))
        source_rank = record.get('source_rank')
        col_rank = usage.get('taxonRank')
        source_parts = name_parts(asserted_name(record))
        if (qualifier_kind(record) != 'uncertain' and len(source_parts) == 1 and source_rank in RANK_ORDER
                and RANK_ORDER.index(source_rank) <= RANK_ORDER.index('genus')
                and col_rank in RANK_ORDER and source_rank != col_rank):
            out.append(('rank', 'check:rank', f'Your rank is {source_rank}; COL has this name as {col_rank}'))
    idcheck = match.get('idCheck') or {}
    if idcheck.get('outcome') == 'elsewhere' or 'SCIENTIFIC_NAME_AND_ID_INCONSISTENT' in (match.get('issues') or []):
        fields = ', '.join((idcheck.get('fields') or record.get('source_ids') or {}).keys()) or 'identifier'
        target = (match.get('matchedId') or {}).get('scientificName') or (idcheck.get('usage') or {}).get('scientificName') or 'another name'
        out.append(('id', 'check:id', f'Your {fields} points to {target}'))
    mixed = record.get('mixed_hints') or []
    if mixed:
        out.append(('mixed', 'check:mixed', f"Your rows give this name different {', '.join(mixed)}"))
    for rank in ('kingdom', 'phylum', 'class') if match.get('matchType') == 'EXACT' else ():
        hint = (record.get('hints') or {}).get(rank)
        theirs = ((usage.get('classification') or {}).get(rank))
        if hint and theirs and _hint_normal(hint) != _hint_normal(theirs):
            out.append((rank, f'check:{rank}:{hint}:{theirs}', f'Your {rank} says {hint}; COL places this name in {theirs}'))
    return out


def _reason_for_unconfirmed(record):
    """Explain why the current COL result cannot be confirmed automatically."""
    match = record.get('match') or {}
    if qualifier_kind(record) == 'doubt':
        values = record.get('source_qualifiers') or []
        doubtful = next((value for value in values if _normal_qualifier(value) not in UNCERTAIN_QUALIFIERS), None)
        if doubtful is not None:
            if doubtful == '\0overlong':
                count = record.get('source_qualifier_rows') or 1
                return [{'code': 'doubt', 'text': f'{count} of {record.get("rows", count)} rows have an overlong qualifier; decide this one yourself'}]
            qualifier = _normal_qualifier(doubtful)
            count = (record.get('source_qualifier_counts') or {}).get(qualifier)
            if count is None:
                count = record.get('source_qualifier_rows') or 1
            return [{'code': 'doubt', 'text': f'{count} of {record.get("rows", count)} rows say “{qualifier}”; decide this one yourself'}]
        qualifier = '?' if '?' in str(record.get('label') or '') else record.get('qualifier') or (record.get('source_qualifiers') or ['qualifier'])[0]
        return [{'code': 'doubt', 'text': f'“{qualifier}” marks an uncertain identification; decide this one yourself'}]
    if qualifier_kind(record) == 'uncertain':
        candidates = [usage for usage, _ in _stem_candidates(record)]
        ranks = {usage.get('taxonRank') for usage in candidates}
        if candidates and (len(ranks) != 1 or None in ranks):
            return [{'code': 'rank', 'text': 'COL has this name at more than one rank'}]
        return [{'code': 'none', 'text': 'No exact stem name found in COL'}]
    idcheck = match.get('idCheck') or {}
    if idcheck.get('outcome') == 'not_found' or 'TAXON_ID_NOT_FOUND' in (match.get('issues') or []):
        field = ', '.join((idcheck.get('fields') or record.get('source_ids') or {}).keys()) or 'identifier'
        return [{'code': 'id_not_found', 'text': f"Your {field} wasn't found in COL"}]
    usage = match.get('usage') or {}
    same = [item for item in [usage, *(match.get('alternatives') or [])]
            if item.get('scientificName') and name_parts(item['scientificName']) == name_parts(asserted_name(record))
            and item.get('matchType', match.get('matchType')) == 'EXACT']
    if len(same) > 1:
        ranks = {item.get('taxonRank') for item in same}
        if len(ranks) > 1:
            return [{'code': 'rank', 'text': 'COL has this name at more than one rank'}]
        return [{'code': 'homonym', 'text': f"COL can't pick between {len(same)} names"}]
    if match.get('status') == 'higher_rank' and usage:
        return [{'code': 'higher', 'text': f"COL only knows {usage.get('scientificName')} ({usage.get('taxonRank')})"}]
    ranks = {x.get('taxonRank') for x in same}
    if len(ranks) > 1:
        return [{'code': 'rank', 'text': 'COL has this name at more than one rank'}]
    if not usage:
        return [{'code': 'none', 'text': 'Not found in COL'}]
    return []


def row_default(record, classification):
    """Return the safe per-label choice shown as the default for its group."""
    kind = classification.get('kind')
    if kind == 'auto':
        usage = (record.get('match') or {}).get('usage') or {}
        parsed = record.get('parsed') or {}
        return 'col' if authorship_agrees(record, usage) else ('parsed' if parsed.get('usable') and parsed.get('lossless') else 'keep')
    if kind == 'uncertain':
        return 'stem'
    if kind == 'spelling':
        return 'col' if eligible(record, 'col', classification) else 'mine'
    if kind == 'unconfirmed':
        return 'mine'
    return None


def eligible(record, option, classification, decisions=None):
    """Whether this record may take an option, considering optional existing decisions."""
    decisions = decisions or {}
    if record['label'] in decisions and (decisions[record['label']].get('by') or 'user') == 'user':
        return False
    kind = classification.get('kind')
    qkind = qualifier_kind(record)
    match = record.get('match') or {}
    usage = match.get('usage') or {}
    if qkind == 'doubt':
        return False
    if option == 'mine' or option == 'keep':
        return True
    if option == 'parsed':
        parsed = record.get('parsed') or {}
        return qkind is None and parsed.get('usable') and parsed.get('lossless')
    if option == 'stem':
        return kind == 'uncertain' and stem_usage(record) is not None
    if option == 'col':
        if kind == 'check' and record.get('mixed_hints'):
            return False
        if kind == 'check' and qkind == 'uncertain':
            stem = stem_usage(record)
            return bool(stem and change(record, stem, 'EXACT') is None)
        found = change(record, usage, match.get('matchType')) if usage else None
        return (kind in {'auto', 'spelling', 'check'} and qkind is None and bool(usage) and not match.get('hintOnly')
                and (found is None or (kind == 'spelling' and found.get('kind') == 'spelling'))
                and authorship_agrees(record, usage))
    return False


def classify(record):
    """Classify one checked name and calculate its intrinsic safe options."""
    if not checked(record):
        return {'group': 'unchecked', 'kind': None, 'reasons': [], 'default': None, 'eligible': []}
    conflicts = _conflicts(record)
    if conflicts:
        group = conflicts[0][1]
        kind = 'check'
        reasons = [{'code': code, 'text': text} for code, _, text in conflicts]
    elif qualifier_kind(record) == 'doubt':
        group, kind, reasons = 'unconfirmed', 'unconfirmed', _reason_for_unconfirmed(record)
    elif qualifier_kind(record) == 'uncertain':
        if stem_usage(record):
            group, kind, reasons = 'uncertain', 'uncertain', []
        else:
            group, kind, reasons = 'unconfirmed', 'unconfirmed', _reason_for_unconfirmed(record)
    else:
        match = record.get('match') or {}
        usage = match.get('usage') or {}
        found = change(record, usage, match.get('matchType')) if usage else None
        if match.get('matchType') == 'EXACT' and usage and not match.get('hintOnly') and found is None:
            group, kind, reasons = 'auto', 'auto', []
            if match.get('disambiguatedBy'):
                reasons.append({'code': 'disambiguated', 'text': f"Matched with your {', '.join(match['disambiguatedBy'])}"})
            if not authorship_agrees(record, usage):
                reasons.append({'code': 'authorship', 'text': f"COL's authorship {usage.get('scientificNameAuthorship') or '(none)'} differs; yours is kept"})
        elif (match.get('matchType') in {'VARIANT', 'FUZZY', 'CANONICAL'} and usage
              and not match.get('hintOnly')):
            group, kind = 'spelling', 'spelling'
            if found and found.get('kind') == 'spelling':
                reasons = [{'code': 'spelling', 'text': f"COL spells it {usage.get('scientificName')}"}]
            elif found:
                reasons = [{'code': 'suggestion', 'text': f"COL suggests {usage.get('scientificName')}; it {found.get('text')}"}]
            else:
                authorship = usage.get('scientificNameAuthorship')
                suffix = f' {authorship}' if authorship else ''
                reasons = [{'code': 'variant', 'text': f"COL writes it {usage.get('scientificName')}{suffix}"}]
        else:
            group, kind, reasons = 'unconfirmed', 'unconfirmed', _reason_for_unconfirmed(record)
    result = {'group': group, 'kind': kind, 'reasons': reasons, 'default': None, 'eligible': []}
    result['eligible'] = [option for option in GROUP_OPTIONS.get(kind, []) if eligible(record, option, result)]
    if kind == 'spelling':
        result['default'] = row_default(record, result)
    elif kind == 'unconfirmed':
        result['default'] = 'mine'
    elif kind == 'uncertain':
        result['default'] = 'stem'
    elif kind == 'auto':
        result['default'] = row_default(record, result)
    return result


def groups(state, classified=None):
    """Summarize the checked labels into groups and count current bulk eligibility."""
    decisions = state.get('decisions') or {}
    held_labels = set(unconfirmed(state))
    buckets = {}
    for record in state.get('labels', []):
        classification = (classified or {}).get(record['label']) or classify(record)
        group = classification['group']
        if group == 'unchecked':
            continue
        bucket = buckets.setdefault(group, {'id': group, 'kind': classification['kind'], 'members': [], 'reasons': classification['reasons']})
        bucket['members'].append((record, classification))
    def order(item):
        group_id = item['id']
        if group_id.startswith('check:'):
            return (0, -len(item['members']), group_id)
        return (1, {'unconfirmed': 0, 'spelling': 1, 'uncertain': 2, 'auto': 3}.get(group_id, 9), group_id)
    output = []
    for bucket in sorted(buckets.values(), key=order):
        members = bucket['members']
        kind = bucket['kind']
        opts = GROUP_OPTIONS[kind]
        counts = Counter()
        auto = by_user = bulk = held = authorship_kept = undecided = 0
        rows = 0
        for record, classification in members:
            rows += int(record.get('rows', 0))
            decision = decisions.get(record['label'])
            if not decision or record['label'] in held_labels:
                undecided += 1
            elif str(decision.get('by') or 'user').startswith('auto:'):
                auto += 1
                authorship_kept += int(any(reason['code'] == 'authorship' for reason in classification['reasons']))
            elif str(decision.get('by') or 'user').startswith('bulk:'):
                bulk += 1
            elif decision.get('by', 'user') == 'user':
                by_user += 1
            held += int(record['label'] in held_labels)
            for option in opts:
                if eligible(record, option, classification, decisions):
                    counts[option] += 1
        signature = None
        if kind == 'check':
            reason = next((r for r in bucket['reasons'] if r['code'] in {'mixed', 'kingdom', 'phylum', 'class', 'id', 'rank'}), None)
            if reason is None:
                reason = next((r for r in bucket['reasons'] if r['code'] == 'name'), None)
            if reason:
                record = members[0][0]
                code = reason['code']
                if code in {'kingdom', 'phylum', 'class'}:
                    signature = {'code': code, 'yours': (record.get('hints') or {}).get(code),
                                 'col': (((record.get('match') or {}).get('usage') or {}).get('classification') or {}).get(code)}
                elif code == 'mixed':
                    signature = {'code': code, 'yours': ', '.join(record.get('mixed_hints') or []), 'col': None}
                elif code == 'id':
                    idcheck = (record.get('match') or {}).get('idCheck') or {}
                    signature = {'code': code, 'yours': ', '.join((idcheck.get('fields') or {}).keys()) or None,
                                 'col': ((record.get('match') or {}).get('matchedId') or {}).get('scientificName')
                                 or (idcheck.get('usage') or {}).get('scientificName')}
                elif code == 'rank':
                    signature = {'code': code, 'yours': record.get('source_rank'),
                                 'col': ((record.get('match') or {}).get('usage') or {}).get('taxonRank')}
                else:
                    signature = {'code': code, 'yours': asserted_name(record),
                                 'col': ((record.get('match') or {}).get('usage') or {}).get('scientificName')}
        default = ('col' if kind == 'auto' else 'stem' if kind == 'uncertain' else
                   ('col' if counts.get('col') else 'mine') if kind == 'spelling' else
                   'mine' if kind == 'unconfirmed' else None)
        output.append({'id': bucket['id'], 'kind': kind, 'labels': len(members), 'rows': rows, 'undecided': undecided,
                       'auto': auto, 'by_user': by_user, 'bulk': bulk, 'default': default,
                       'options': [{'decision': option, 'eligible': counts.get(option, 0)} for option in opts],
                       'signature': signature, 'authorship_kept': authorship_kept, 'held': held})
    return output


def build_decision(record, spec, state, by='user', group_kind=None):
    """Build a validated, self-contained snapshot for a user's or system's name choice."""
    if not isinstance(spec, dict):
        raise NameDecisionError('Choose a name decision.')
    requested = spec.get('decision')
    if requested == 'mine':
        parsed = record.get('parsed') or {}
        requested = 'parsed' if qualifier_kind(record) is None and parsed.get('usable') and parsed.get('lossless') else 'keep'
        spec = {**spec, 'decision': requested}
    if requested not in DECISIONS:
        raise NameDecisionError(f'Choose one of: {", ".join(DECISIONS)}.')
    kind = requested
    if by != 'user' and qualifier_kind(record) == 'doubt':
        raise NameDecisionError('An uncertain identification can only be decided one at a time.')
    snapshot = {'decision': kind, 'by': by, 'at': timezone.now().isoformat(), 'scientificName': None,
                'scientificNameAuthorship': None, 'taxonRank': None, 'source': 'verbatim' if kind == 'keep' else 'none'}
    parsed, match = record.get('parsed') or {}, record.get('match') or {}
    if kind == 'empty':
        snapshot['scientificName'] = ''
    elif kind in {'parsed', 'stem'}:
        if kind == 'stem':
            usage = stem_usage(record)
            if not usage:
                raise NameDecisionError(f'There is no exact stem name in Catalogue of Life for "{record["label"]}".')
            if by != 'user' and change(record, usage, 'EXACT') is not None:
                raise NameDecisionError('A non-user decision cannot change the asserted name or decide a doubtful identification.')
            snapshot.update(source='col', scientificName=usage['scientificName'], scientificNameAuthorship=usage.get('scientificNameAuthorship'),
                            taxonRank=usage.get('taxonRank'), usageId=usage.get('usageId'), candidates=usage['candidates'],
                            checklist=_checklist(state), changeKind=None, nameRules=NAME_RULES_VERSION)
        else:
            if not parsed.get('usable'):
                raise NameDecisionError(f'The name parser could not split "{record["label"]}".')
            snapshot.update(source='parser', scientificName=parsed['canonical'], scientificNameAuthorship=parsed.get('authorship'), taxonRank=parsed.get('rank'))
    elif kind in {'col', 'alternative'}:
        if kind == 'col':
            usage = match.get('usage')
        else:
            wanted = str(spec.get('usage_id')) if spec.get('usage_id') is not None else None
            usage = next((item for item in match.get('alternatives') or [] if wanted and str(item['id']) == wanted), None)
        if not usage or not usage.get('scientificName'):
            raise NameDecisionError(f'There is no such Catalogue of Life name to accept for "{record["label"]}".')
        match_type = match.get('matchType') if kind == 'col' else usage.get('matchType')
        found = change(record, usage, match_type)
        replaces = found if found and found['confirm'] else None
        if by != 'user' and replaces:
            raise NameDecisionError(f'"{usage["scientificName"]}" {replaces["text"]} for "{record["label"]}"; it is never accepted in bulk.')
        if by != 'user':
            allowed_spelling = group_kind == 'spelling' and str(by).startswith('bulk:') and found and found.get('kind') == 'spelling'
            if found and not allowed_spelling:
                raise NameDecisionError('A non-user decision cannot change the asserted name.')
            if not authorship_agrees(record, usage):
                raise NameDecisionError('A non-user decision cannot replace a different authorship.')
        if replaces and spec.get('confirm_coarser') is not True:
            raise NameDecisionError(f'"{usage["scientificName"]}" {replaces["text"]} for "{record["label"]}". Confirm that replacement explicitly, or keep your name.')
        snapshot.update(source='col', scientificName=usage['scientificName'], scientificNameAuthorship=usage.get('scientificNameAuthorship'),
                        taxonRank=usage.get('taxonRank'), usageId=str(usage['id']) if usage.get('id') is not None else None,
                        taxonomicStatus=usage.get('status'), matchType=match_type, checklist=_checklist(state),
                        changeKind=found['kind'] if found else None, nameRules=NAME_RULES_VERSION)
        if replaces:
            snapshot.update(replaces=replaces['text'], confirmedCoarser=True)
        elif found:
            snapshot['corrects'] = found['text']
    if kind in {'stem', 'col', 'alternative'} and qualifier_kind(record) == 'uncertain' and same_name(record, {'scientificName': snapshot.get('scientificName')}):
        # A valid row qualifier wins, then the label qualifier; a blank column-only row stays blank.
        snapshot['stemFormula'] = True
        formula = formula_qualifier(record)
        if formula:
            snapshot['taxonFormula'] = 'A ' + formula
    if str(by).startswith('bulk:'):
        snapshot['batch'] = spec.get('batch')
    return snapshot


def set_decisions(conversion, changes):
    """Validate and save per-label decisions, recording withdrawn automatic choices."""
    state = current(conversion)
    if not state.get('labels'):
        raise NameDecisionError('There are no names to review for this plan.')
    if not isinstance(changes, dict) or len(changes) > MAX_LABELS:
        raise NameDecisionError('Name decisions must map labels to decisions.')
    state.setdefault('decisions', {})
    index = {record['label']: record for record in state['labels']}
    declined = set(state.get('auto_declined') or [])
    for label, spec in changes.items():
        if label not in index:
            raise NameDecisionError(f'"{str(label)[:80]}" is not a name in this archive.')
        if spec is None:
            state['decisions'].pop(label, None)
            declined.add(label)
        else:
            state['decisions'][label] = build_decision(index[label], spec, state)
            declined.discard(label)
    state['auto_declined'] = sorted(declined)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])


def auto_accept(state):
    """Accept safe exact matches and resolvable uncertain stems in the in-memory state."""
    decisions = state.setdefault('decisions', {})
    declined = set(state.get('auto_declined') or [])
    count = 0
    for record in state.get('labels', []):
        label = record['label']
        if not checked(record) or label in decisions or label in declined:
            continue
        classification = classify(record)
        if classification['kind'] == 'auto':
            spec = {'decision': row_default(record, classification)}
            by = 'auto:exact'
        elif classification['kind'] == 'uncertain':
            spec = {'decision': 'stem'}
            by = 'auto:uncertain'
        else:
            continue
        try:
            decisions[label] = build_decision(record, spec, state, by=by, group_kind=classification['kind'])
            count += 1
        except NameDecisionError:
            continue
    return count


def bulk_decide(conversion, group, decision):
    """Apply one eligible group decision and save an undoable batch."""
    state = current(conversion)
    if not state.get('labels'):
        raise NameDecisionError('There are no names to review for this plan.')
    if not isinstance(group, str) or not isinstance(decision, str):
        raise NameDecisionError('Bulk decisions need a group and decision.')
    state.setdefault('decisions', {})
    classifications = {record['label']: classify(record) for record in state.get('labels', [])}
    current_groups = {item['id']: item for item in groups(state, classifications)}
    summary = current_groups.get(group)
    if not summary:
        raise NameDecisionError('That name group is no longer available.')
    if decision not in {option['decision'] for option in summary['options']}:
        raise NameDecisionError('That decision is not an option for this group.')
    batch_id = secrets.token_hex(6)
    kind = summary['kind']
    changes = {}
    count = 0
    for record in state.get('labels', []):
        classification = classifications[record['label']]
        if classification['group'] != group:
            continue
        previous = state['decisions'].get(record['label'])
        if previous and previous.get('by', 'user') == 'user':
            continue
        if not eligible(record, decision, classification, state['decisions']):
            continue
        resolved = decision
        if decision == 'mine':
            parsed = record.get('parsed') or {}
            resolved = 'parsed' if qualifier_kind(record) is None and parsed.get('usable') and parsed.get('lossless') else 'keep'
        if decision == 'col' and qualifier_kind(record) == 'uncertain':
            resolved = 'stem'
        changes[record['label']] = copy.deepcopy(previous)
        state['decisions'][record['label']] = build_decision(record, {'decision': resolved, 'batch': batch_id}, state,
                                                              by=f'bulk:{kind}', group_kind=kind)
        count += 1
    if not count:
        raise NameDecisionError('No name in this group can take that decision; decide them one at a time.')
    batch = {'id': batch_id, 'group': group, 'decision': decision, 'count': count,
             'at': timezone.now().isoformat(), 'changes': changes}
    state.setdefault('batches', []).append(batch)
    state['batches'] = state['batches'][-10:]
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return {'batch': batch_id, 'count': count}


def undo_batch(conversion, batch_id):
    """Restore the previous decision for members that still carry the target batch."""
    state = current(conversion)
    batches = state.get('batches') or []
    batch = next((item for item in batches if item.get('id') == batch_id), None)
    if not batch:
        raise NameDecisionError('That bulk decision can no longer be undone.')
    count = 0
    for label, previous in batch.get('changes', {}).items():
        current_decision = state.get('decisions', {}).get(label)
        if current_decision and current_decision.get('batch') == batch_id:
            if previous is None:
                state['decisions'].pop(label, None)
            else:
                state['decisions'][label] = previous
            count += 1
    state['batches'] = [item for item in batches if item.get('id') != batch_id]
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return count


def undo_auto(conversion, kind):
    """Remove automatic decisions of one group kind and decline their re-acceptance."""
    if not isinstance(kind, str) or kind not in {'auto', 'uncertain'}:
        raise NameDecisionError('Undo kind must be auto or uncertain.')
    state = current(conversion)
    by = 'auto:exact' if kind == 'auto' else 'auto:uncertain'
    declined = set(state.get('auto_declined') or [])
    count = 0
    for label, decision in list((state.get('decisions') or {}).items()):
        if decision.get('by') == by:
            state['decisions'].pop(label)
            declined.add(label)
            count += 1
    state['auto_declined'] = sorted(declined)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return count


def request_check(conversion, refresh=False):
    """Prepare a stored review for another names job; refresh also drops results of undecided labels."""
    state = current(conversion)
    if not state or 'labels' not in state:
        state = {'plan_id': conversion.plan['id'], 'status': 'pending', 'error': '', 'runs': 0, 'decisions': {}}
    state.update(status='pending', error='', runs=0, requested=True)
    if refresh:
        for record in state.get('labels', []):
            if record['label'] not in state['decisions']:
                for part in ('parsed', 'match'):
                    record.pop(part, None)
        state['col_release'] = {}
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])


# Applying decisions ------------------------------------------------------------------------------

def row_source_names(archive, row_crosswalk, frames):
    """Per output table, the source name behind each frame row ('' when it has none or the sources disagree).

    Each found name is {'name': source scientificName text, 'authorship': ..., 'rank': ..., 'qualifier': ...}, read from
    the same source row (None when the source table has no such column). An occurrence row and the identification row made from
    it therefore see the same supplied authorship and rank, whichever output table the converter copied them to.
    A crosswalk entry's `target_row` is the 1-based position in the converted frame (offset-corrected for nested Taxon plans),
    and `source_table_index` indexes the archive's tables.
    """
    found = {table: [''] * len(frames[table]) for table in OUTPUT_TABLES if table in frames}
    for entry in row_crosswalk or []:
        names, position = found.get(entry.get('target_table')), (entry.get('target_row') or 0) - 1
        if names is None or not 0 <= position < len(names):
            continue
        if entry.get('source_table_index') is None:
            continue
        table = archive.tables[entry['source_table_index']]
        if SOURCE_TABLES.get(table.row_type) is None or NAME not in table.terms:
            continue
        row = table.rows[entry['source_row'] - 1]
        text = row[table.terms.index(NAME)]
        if not normal(text, MAX_LABEL_CHARS):
            continue
        if not names[position]:
            names[position] = {'name': text, **{key: row[table.terms.index(DWC + term)] if DWC + term in table.terms else None
                                                for key, term in (('authorship', 'scientificNameAuthorship'), ('rank', 'taxonRank'),
                                                                  ('qualifier', 'identificationQualifier'))}}
        elif names[position] is not CONFLICT and normal(names[position]['name']) != normal(text):
            names[position] = CONFLICT
    return {table: ['' if value is CONFLICT else value for value in values] for table, values in found.items()}


CONFLICT = object()


def _source(value):
    """(name text, supplied authorship, supplied rank, qualifier) of one row source value."""
    if isinstance(value, dict):
        return value.get('name') or '', value.get('authorship'), value.get('rank'), value.get('qualifier')
    return value or '', None, None, None


def _same_text(left, right):
    return normal(left).casefold() == normal(right).casefold()


def _blank_cell(value):
    try:
        if value != value:
            return True
    except (TypeError, ValueError):
        pass
    return not normal(value)


def apply_name_decisions(frames, name_review, source_names):
    """Overlay reviewed names on converted frames; returns (new frames, report section or None).

    Rows are found by their own source scientificName text (`source_names`: per output table, one value per frame row,
    from `row_source_names`; never borrowed from a linked row). verbatimIdentification is never read or changed.
    Whenever a decision writes scientificName it also writes a taxonRank and authorship that belong to that name, from the
    source row, so an occurrence and the identification made from it agree:
    - a parsed split writes its rank; the supplied authorship is kept (a differing one is counted), the parsed one fills a blank;
    - a COL name writes COL's rank and authorship; the supplied ones survive only where COL has none for the same name;
    - "don't publish a name" clears all three; "keep" only fills a blank scientificName with the supplied text.
    Replaced ranks are counted per entry. A COL decision that replaces the asserted name with a coarser or different taxon
    and was never explicitly confirmed (decided before that check existed) is applied as "keep": the user's own name is
    published, never an empty one. Unreviewed labels leave the converter's output as it is.
    """
    from api.dwc_dp_specs import get_table_spec
    state = name_review or {}
    if not state.get('labels'):
        skipped = state.get('skipped_long') or {}
        # The report still says when every name was too long to check.
        return frames, ({'status': state.get('status'), 'labels': 0, 'checked': 0, 'reviewed': 0, 'unreviewed': 0,
                         'skipped_long': skipped, 'decision_counts': {}, 'names': []} if skipped.get('labels') else None)
    decisions = state.get('decisions') or {}
    records = {record['label']: record for record in state['labels']}
    held = unconfirmed(state)
    entries = {label: {'label': label, 'decision': decision['decision'], 'source': decision.get('source'),
                       'scientificName': decision.get('scientificName'), 'scientificNameAuthorship': decision.get('scientificNameAuthorship'),
                       'taxonRank': decision.get('taxonRank'), 'colUsageId': decision.get('usageId'), 'checklist': decision.get('checklist'),
                       'matchType': decision.get('matchType'), 'decidedBy': decision.get('by'), 'decidedAt': decision.get('at'),
                       'replaces': decision.get('replaces') or ((held.get(label) or {}).get('text')
                                                                if (held.get(label) or {}).get('kind') != 'authorship' else None),
                       'corrects': decision.get('corrects'),
                       'appliedAs': 'keep' if label in held else decision['decision'],
                       'notApplied': (("the earlier bulk choice would have replaced your authorship, so your authorship was kept"
                                       if held[label].get('kind') == 'authorship' else
                                       'the COL name replaces the supplied name and was never confirmed, so the supplied name was kept')
                                      if label in held else None),
                       'rows': {}, 'authorshipKept': 0, 'authorshipReplaced': 0, 'ranksReplaced': {}, 'taxonFormulaWritten': 0}
               for label, decision in decisions.items()}
    applied = {label: {'decision': 'keep'} if label in held else decision for label, decision in decisions.items()}
    result = dict(frames)
    for table in OUTPUT_TABLES:
        frame = frames.get(table)
        # A row without a source name of its own is left untouched.
        verbatim = (source_names or {}).get(table) or []
        if frame is None or not applied or not len(frame) or len(verbatim) != len(frame):
            continue
        fields = set(get_table_spec(table).fields)
        positions = defaultdict(list)
        for position, value in enumerate(verbatim):
            if normal(_source(value)[0]) in applied:
                positions[normal(_source(value)[0])].append(position)
        if not positions:
            continue
        columns = {}
        taxon_formula = None

        def column(field):
            if field not in fields:
                return None
            if field not in columns:
                columns[field] = frame[field].tolist() if field in frame.columns else [''] * len(frame)
            return columns[field]

        for label, rows in positions.items():
            decision, entry = applied[label], entries[label]
            kind = decision['decision']
            entry['rows'][table] = len(rows)
            names, authorship, rank = column('scientificName'), column('scientificNameAuthorship'), column('taxonRank')
            decided_name, decided_authorship = decision.get('scientificName') or '', decision.get('scientificNameAuthorship') or ''
            # A COL name that is the asserted name may keep supplied parts COL lacks; a different name may not.
            keeps_name = kind in {'col', 'alternative', 'stem'} and same_name(records.get(label) or {'label': label}, {'scientificName': decided_name})
            formula = decision.get('taxonFormula')
            if taxon_formula is None and table == 'identification' and formula:
                taxon_formula = column('taxonFormula')
            formula_written = 0
            for position in rows:
                text, supplied_authorship, supplied_rank, row_qualifier = _source(verbatim[position])
                if supplied_authorship is None and authorship is not None:
                    supplied_authorship = authorship[position]
                if supplied_rank is None and rank is not None:
                    supplied_rank = rank[position]
                supplied_authorship, supplied_rank = normal(supplied_authorship), normal(supplied_rank)
                if kind == 'keep':
                    # The supplied text fills a blank scientificName; a name the converter already wrote stands.
                    if names is not None and not normal(names[position]):
                        names[position] = str(text)
                    continue
                if kind == 'empty':
                    new_authorship, new_rank = '', ''
                elif kind == 'parsed':
                    new_authorship = supplied_authorship or decided_authorship
                    if supplied_authorship and decided_authorship and supplied_authorship != normal(decided_authorship):
                        entry['authorshipKept'] += 1
                    new_rank = decision.get('taxonRank') or supplied_rank
                else:
                    new_authorship = decided_authorship or (supplied_authorship if keeps_name else '')
                    if supplied_authorship and supplied_authorship != normal(new_authorship):
                        entry['authorshipReplaced'] += 1
                    new_rank = decision.get('taxonRank') or (supplied_rank if keeps_name else '')
                if names is not None:
                    names[position] = decided_name
                row_formula = formula
                if decision.get('stemFormula'):
                    qualifier = normal(row_qualifier)
                    qualifier = _normal_qualifier(qualifier) if qualifier else None
                    if qualifier not in UNCERTAIN_QUALIFIERS:
                        qualifier = normal(records.get(label, {}).get('qualifier'))
                        qualifier = _normal_qualifier(qualifier) if qualifier else None
                    row_formula = 'A ' + qualifier if qualifier else None
                if row_formula and taxon_formula is not None and _blank_cell(taxon_formula[position]):
                    taxon_formula[position] = row_formula
                    formula_written += 1
                if authorship is not None:
                    authorship[position] = new_authorship
                if rank is not None:
                    previous = normal(rank[position])
                    if previous and not _same_text(previous, new_rank):
                        entry['ranksReplaced'][previous] = entry['ranksReplaced'].get(previous, 0) + 1
                    rank[position] = new_rank
            entry['taxonFormulaWritten'] = entry.get('taxonFormulaWritten', 0) + formula_written
        changed = frame.copy()
        for field, values in columns.items():
            changed[field] = values
        if table == 'identification' and taxon_formula is not None:
            changed['taxonFormula'] = taxon_formula
        result[table] = changed
    section = {
        'status': state.get('status'), 'error': state.get('error') or None, 'checklist': state.get('col_release') or None,
        'labels': len(state['labels']), 'checked': sum(1 for record in state['labels'] if checked(record)),
        'not_reviewed_limit': state.get('truncated', 0), 'skipped_long': state.get('skipped_long'), 'reviewed': len(decisions), 'unreviewed': len(state['labels']) - len(decisions),
        'decision_counts': dict(Counter(decision['decision'] for decision in decisions.values())),
        'ranks_replaced': sum(sum(entry['ranksReplaced'].values()) for entry in entries.values()),
        'unconfirmed_kept': len(held),
        'policy': 'Decisions apply to rows by their own source scientificName text; verbatimIdentification is never changed. Names only are published; COL usage ids are provenance, not taxonID. '
                  'A decided name is written with its own taxonRank and authorship on occurrence and identification rows alike; replaced ranks are counted. '
                  'A COL name coarser than or different from the supplied name is applied only when the user confirmed it; '
                  'an unconfirmed one keeps the supplied name. '
                  'Unreviewed labels are exactly as the converter produced them.',
        'entries': [entries[label] for label in sorted(entries)],
    }
    return result, section


def _unconfirmed_replacement(record, decision):
    """The replacement an older COL decision would make without the explicit confirmation it now needs; None otherwise.

    A decision saved by `build_decision` carries the change kind it was checked against (`changeKind`, None for the
    same name) and the name rules it was checked under (`nameRules`), and is trusted: it was confirmed when it needed
    to be. A snapshot without that stamp, or from other name rules, is checked again, against the record's stored usage
    (with its classification); stale automatic and bulk choices also need authorship agreement.
    """
    if decision.get('decision') not in {'col', 'alternative', 'stem'} or decision.get('confirmedCoarser'):
        return None
    if 'changeKind' in decision and decision.get('nameRules') == NAME_RULES_VERSION:
        return None
    match = record.get('match') or {}
    stored = next((usage for usage in [match.get('usage') or {}, *(match.get('alternatives') or [])]
                   if decision.get('usageId') is not None and str(usage.get('id')) == str(decision['usageId'])), None)
    usage = {**(stored or {}), 'scientificName': decision.get('scientificName'), 'taxonRank': decision.get('taxonRank')}
    if str(decision.get('by') or '').startswith(('bulk:', 'auto:')) and not authorship_agrees(
            record, {'scientificNameAuthorship': decision.get('scientificNameAuthorship')}):
        return {'kind': 'authorship', 'confirm': True, 'text': 'has a different authorship from yours'}
    return replacement(record, usage, decision.get('matchType'))


def public_report(report):
    """The report as shown in state: the per-label entries stay in the downloadable report only."""
    section = report.get('name_review')
    if not isinstance(section, dict) or 'entries' not in section:
        return report
    return {**report, 'name_review': {key: value for key, value in section.items() if key != 'entries'}}


# State -------------------------------------------------------------------------------------------

def col_choices(record):
    """Every COL usage the user may pick, with whether it is the asserted name and what it would replace."""
    match = record.get('match') or {}
    usages = [('col', match.get('usage'), match.get('matchType'))]
    usages += [('alternative', alternative, alternative.get('matchType')) for alternative in match.get('alternatives') or []]
    choices = []
    for kind, usage, match_type in usages:
        if not usage or not usage.get('scientificName'):
            continue
        found = change(record, usage, match_type)
        choices.append({'decision': kind, 'usage': usage, 'matchType': match_type, 'same_name': same_name(record, usage),
                        'replaces': found if found and found['confirm'] else None,
                        'corrects': found['text'] if found and found['kind'] == 'spelling' else None, 'rank_note': None})
    # Homonyms of the user's name at another rank ("Anura" the order and the genus) say so.
    mine = asserted_rank(record)
    ranks = {choice['usage'].get('taxonRank') for choice in choices if choice['same_name']}
    for choice in choices:
        rank = choice['usage'].get('taxonRank')
        if choice['same_name'] and rank and mine and rank != mine:
            choice['rank_note'] = f'{_article(rank)} {rank}; your name is {_article(mine)} {mine}'
        elif choice['same_name'] and rank and not mine and len(ranks) > 1:
            choice['rank_note'] = f'{_article(rank)} {rank}; COL has this name at more than one rank'
    hints = record.get('hints') or {}
    for choice in choices:
        classification = choice['usage'].get('classification') or {}
        for rank in ('kingdom', 'phylum', 'class'):
            yours, theirs = hints.get(rank), classification.get(rank)
            if yours and theirs and _hint_normal(yours) != _hint_normal(theirs):
                choice['lineage_note'] = f'different {rank} than your data: {theirs}'
                break
    choices.sort(key=lambda choice: bool(choice.get('lineage_note')))
    return choices


def _article(word):
    return 'an' if str(word)[:1] in 'aeiou' else 'a'


def _entry(record, decisions, held=(), classification=None):
    """Build one public state entry, including its classification and safe options."""
    parsed, match = record.get('parsed'), record.get('match')
    usage = (match or {}).get('usage') or {}
    choices = col_choices(record)
    main = next((choice for choice in choices if choice['decision'] == 'col'), None)
    decision = decisions.get(record['label'])
    classification = classification or classify(record)
    return {**record, 'checked': checked(record), 'decision': decision,
            # An older decision for a coarser or different COL name: the user's name is kept until it is made again.
            'decision_unconfirmed': record['label'] in held,
            'decision_held': ({'kind': held[record['label']]['kind'], 'text': held[record['label']]['text']}
                              if record['label'] in held else None),
            'rank_mismatch': bool(record.get('source_rank') and usage.get('taxonRank') and record['source_rank'] != usage['taxonRank']),
            'offers': {'parsed': bool(parsed and parsed.get('usable')), 'col': bool(usage.get('scientificName'))},
            'col_choices': choices,
            # COL's own pick replaces the user's name with a coarser or different taxon: keeping the name is the safe default.
            'suggested': 'keep' if main and main['replaces'] else None,
            'group': classification['group'], 'kind': classification['kind'],
            'reasons': classification['reasons'],
            'eligible': [option for option in GROUP_OPTIONS.get(classification['kind'], [])
                         if eligible(record, option, classification, decisions)],
            'row_default': row_default(record, classification),
            'stem': stem_usage(record) if qualifier_kind(record) == 'uncertain' else None}


def state_section(conversion, offset=0, limit=PAGE_SIZE, view='all', group=None, q=''):
    """Bounded name-review state: summary counts and one page of labels.

    A label whose saved decision is unconfirmed (see `unconfirmed`) counts as undecided and is listed as pending.
    """
    state = current(conversion)
    labels = state.get('labels', [])
    decisions = state.get('decisions', {})
    held = unconfirmed(state)
    statuses = Counter((record.get('match') or {}).get('status') for record in labels if checked(record))
    classified = {record['label']: classify(record) for record in labels}
    group_summaries = groups(state, classified)
    summary = {'labels': len(labels), 'rows': sum(record['rows'] for record in labels), 'checked': sum(1 for record in labels if checked(record)),
               'decided': len(decisions) - len(held), 'unconfirmed': len(held), 'truncated': state.get('truncated', 0),
               'skipped_long': state.get('skipped_long') or {'labels': 0, 'rows': 0}, 'max_label_chars': MAX_LABEL_CHARS, 'match_status': dict(statuses),
               'groups': group_summaries, 'unchecked': sum(1 for record in labels if not checked(record)),
               'auto_declined': len(state.get('auto_declined') or []),
               'last_batch': ({key: value for key, value in state['batches'][-1].items() if key != 'changes'} if state.get('batches') else None)}
    shown = [record for record in labels if (view != 'pending' or record['label'] not in decisions or record['label'] in held)
             and (view != 'group' or classified[record['label']]['group'] == group)
             and (not q or q.casefold() in record['label'].casefold())]
    offset = max(int(offset), 0)
    limit = min(max(int(limit), 1), MAX_PAGE_SIZE)
    from api.models import DwcConversionJob
    checking = DwcConversionJob.objects.filter(conversion=conversion, action='names').exists()
    return {'status': state.get('status', 'none'), 'checking': checking, 'error': state.get('error', ''), 'runs': state.get('runs', 0),
            'plan_id': state.get('plan_id'), 'checklist': (state.get('col_release') or {}).get('alias'), 'summary': summary,
            # Name decisions kept from the previous check when the archive was inspected again.
            'carried': state.get('carried'),
            # scientificName questions shown inside the name check instead of as separate choices.
            'question_ids': name_question_ids(conversion),
            'page': {'offset': offset, 'limit': limit, 'total': len(shown), 'view': view, 'group': group},
            'labels': [_entry(record, decisions, held, classified[record['label']]) for record in shown[offset:offset + limit]]}


# Job ---------------------------------------------------------------------------------------------

def process_job(conversion_id, job_id, claim):
    """Run a claimed names job, then finish (continue, chain or delete) it under the fence."""
    from api.conversion_review import Fenced, fence
    try:
        _run(conversion_id, job_id, claim)
    except Fenced:
        return
    except Exception:
        logger.exception('Conversion %s name check failed', conversion_id)
        try:
            with fence(conversion_id, job_id, claim, 'names', None) as (conversion, _):
                state = current(conversion)
                if state:
                    state.update(status='error', error='Name checks stopped because of a server error. Conversion is not affected; you can check again.')
                    conversion.save(update_fields=['name_review', 'updated_at'])
        except Fenced:
            return
    finish_job(conversion_id, job_id, claim)


def _run(conversion_id, job_id, claim):
    from api import conversion_chat
    from api.conversion_review import Fenced, fence, review_state
    from api.models import DwcConversion
    snapshot = DwcConversion.objects.select_related('dataset').get(pk=conversion_id)
    plan_id = (snapshot.plan or {}).get('id')
    fresh = None
    if not current(snapshot) or 'labels' not in current(snapshot):
        from api.conversion_jobs import load_sources
        # A conversion inspected before name checks existed collects its labels now, outside any lock.
        fresh = collect_state(load_sources(snapshot), snapshot.plan)
    with fence(conversion_id, job_id, claim, 'names', REVIEW_STATUSES) as (conversion, _):
        if fresh is not None and (not current(conversion) or 'labels' not in current(conversion)):
            fresh.update(runs=current(conversion).get('runs', 0), requested=current(conversion).get('requested', False))
            conversion.name_review = fresh
        state = current(conversion)
        if not state:
            raise Fenced()
        state.update(status='running', error='', runs=state.get('runs', 0) + 1)
        conversion.save(update_fields=['name_review', 'updated_at'])
        pending = [{'label': record['label'], 'query': _query(record), 'qualifier': record.get('qualifier'),
                    'ids': record.get('source_ids') or {}, 'match': 'match' in record, 'parse': 'parsed' in record}
                   for record in state['labels'] if not checked(record)]
        release = state.get('col_release') or {}
    deadline = time.monotonic() + budget_seconds()
    error = None
    try:
        if pending and not release:
            release = col_release(deadline)
    except TaxonServiceError as exc:
        error = exc
    for start in range(0, len(pending), CHUNK):
        if error is not None:
            break
        chunk = pending[start:start + CHUNK]
        results, chunk_error = check_chunk(chunk, deadline)
        error = error or chunk_error
        with fence(conversion_id, job_id, claim, 'names', REVIEW_STATUSES) as (conversion, _):
            state = current(conversion)
            if not state or state['plan_id'] != plan_id:
                raise Fenced()
            index = {record['label']: record for record in state['labels']}
            for label, parts in results.items():
                index[label].update(parts)
            if release and not state.get('col_release'):
                state['col_release'] = release
            conversion.save(update_fields=['name_review', 'updated_at'])
            auto_accept(state)
            conversion.save(update_fields=['name_review', 'updated_at'])
            try:
                settle_name_questions(conversion)
            except Exception:
                logger.exception('Could not settle scientific-name questions during conversion %s name check', conversion_id)
            # A waiting chat message or manual review gets its turn at this batch boundary.
            waiting = bool(conversion_chat.unanswered(conversion) or review_state(conversion).get('manual'))
        if waiting:
            break
    with fence(conversion_id, job_id, claim, 'names', REVIEW_STATUSES) as (conversion, _):
        state = current(conversion)
        if not state or state['plan_id'] != plan_id:
            raise Fenced()
        if release and not state.get('col_release'):
            state['col_release'] = release
        auto_accept(state)
        if all(checked(record) for record in state['labels']):
            state.update(status='complete', error='')
        elif error is not None and not _is_budget(error):
            state.update(status='error', error=f'Name checks stopped: {error}'[:500])
        else:
            state.update(status='incomplete', error='')
        conversion.save(update_fields=['name_review', 'updated_at'])
        try:
            settle_name_questions(conversion)
        except Exception:
            logger.exception('Could not settle scientific-name questions during conversion %s name check', conversion_id)


def finish_job(conversion_id, job_id, claim):
    """At each batch boundary yield to a waiting chat message or an AI review; otherwise continue or end."""
    from api import conversion_chat
    from api.conversion_review import Fenced, fence, review_state, should_auto_review
    try:
        with fence(conversion_id, job_id, claim, 'names', None) as (conversion, job):
            state = current(conversion)
            job.claimed_at = None
            job.heartbeat_at = None
            in_review = conversion.status in REVIEW_STATUSES
            if conversion_chat.unanswered(conversion) and conversion.status in REVIEW_STATUSES | {'blocked'}:
                job.action = 'chat'
                job.save(update_fields=['action', 'claimed_at', 'heartbeat_at'])
            elif in_review and (review_state(conversion).get('manual') or should_auto_review(conversion)):
                job.action = 'review'
                job.save(update_fields=['action', 'claimed_at', 'heartbeat_at'])
                conversion.status = 'reviewing'
            elif in_review and state.get('status') == 'incomplete' and state.get('runs', 0) < MAX_RUNS:
                job.save(update_fields=['claimed_at', 'heartbeat_at'])
                return
            else:
                if state.get('status') == 'incomplete':
                    state['error'] = 'Name checks did not finish. Check again to continue.'
                job.delete()
            conversion.save(update_fields=['status', 'name_review', 'updated_at'])
    except Fenced:
        return
