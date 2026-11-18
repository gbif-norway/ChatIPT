"""Shared scientific-name review policy, independent of either workflow's storage and jobs.

Both workflows adapt their source rows to records and persist the snapshots built here.
"""
import copy
import re
import secrets
from collections import Counter, defaultdict
from django.utils import timezone
from api import taxon_matching
from api.taxon_matching import (HINT_RANKS, INFRASPECIFIC_RANKS, NAME_RULES_VERSION, RANK_ORDER, TaxonServiceError,
    authorships_agree, col_authorship_agrees, corrected_name, implied_rank, name_change, name_parts,
    own_name_kind, preview_items, rank_change, split_qualifier, unread_trailing_text, written_fields)

MAX_LABEL_CHARS = 500
MAX_CONTEXT_CHARS = 100
MAX_LABELS = 5000
PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

def record_key(record):
    """A source-context key, or the label when one label has only one context."""
    return str(record.get('key', record['label']))


def match_release(record, state):
    """Prefer the release of this match over the review's display release."""
    if 'col_release' in record:
        return record['col_release'] or {}
    release = state.get('col_release') or {}
    if state.get('runs', 0) > 1:
        # Older resumed conversions did not record a release for each match.
        return {key: value for key, value in release.items() if key != 'checklistBankDatasetKey'}
    return release


def _checklist(record, state):
    release = match_release(record, state)
    dataset_key = release.get('checklistBankDatasetKey')
    return {
        'checklistKey': release.get('checklistKey') or taxon_matching.col_checklist_key(),
        'alias': release.get('alias'),
        **({'checklistBankDatasetKey': str(dataset_key)} if dataset_key else {}),
    }


def with_checklist_release(decision, release, match):
    """Recover a release key for older decisions from their own saved match."""
    if not decision or not release or not release.get('checklistBankDatasetKey'):
        return decision
    checklist = decision.get('checklist') or {}
    if checklist.get('checklistBankDatasetKey') or not checklist.get('checklistKey'):
        return decision
    if decision.get('decision') != 'col' and not any(
        str(usage.get('id')) == decision.get('usageId') for usage in (match or {}).get('alternatives', [])
    ):
        return decision
    alias = checklist.get('alias')
    if not alias or alias != release.get('alias') or checklist['checklistKey'] != release.get('checklistKey'):
        return decision
    return {**decision, 'checklist': {**checklist, 'checklistBankDatasetKey': str(release['checklistBankDatasetKey'])}}

DECISIONS = ('parsed', 'col', 'alternative', 'keep', 'empty', 'stem', 'corrected')


CONTEXT_RANKS = ('kingdom', 'phylum', 'class', 'order', 'family')


MAX_BATCHES = 5


UNCERTAIN_QUALIFIERS = {'sp.', 'spp.', 'indet.'}


_NEW_SPECIES = re.compile(r'\s+sp(?:ec)?\.?\s*n(?:ov)?\.?(?:\s|$)', re.IGNORECASE)


_DOUBT_MARKER = re.compile(r'\?|(?:^|\s)(?:cf|aff|nr)(?:\.|(?=\s|$))', re.IGNORECASE)


GROUP_OPTIONS = {
    'auto': ['col', 'mine'],
    'uncertain': ['stem', 'mine'],
    'spelling': ['col', 'mine'],
    'unconfirmed': ['col', 'mine'],
    'check': ['col', 'mine'],
}


class NameDecisionError(ValueError):
    pass


def normal(value, limit=None):
    """Whitespace-normalised text, or None for a cell far too long to be a name (never split or copied)."""
    if not isinstance(value, str) and taxon_matching._is_blank(value):
        return ''
    text = str(value if value is not None else '')
    if limit is not None and len(text) > limit * 4:
        return None
    return ' '.join(text.split())


def checked(record):
    return 'match' in record and 'parsed' in record


def asserted_name(record):
    """The name the user asserted: the parsed name when the parser could read the label, else the label without its qualifier."""
    parsed = record.get('parsed') or {}
    if record.get('preprocessed') and (record.get('query') or {}).get('scientificName'):
        return record['query']['scientificName']
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
    """How accepting a COL usage would change the asserted name (see taxon_matching.name_change); None for the same name.

    The markers are also read from the label itself, as the parser's canonical name may drop a hybrid sign or "agg.".
    """
    found = name_change(asserted_name(record), usage, match_type, asserted_rank(record), record.get('hints'))
    if (found is None or found.get('kind') == 'spelling') and usage and usage.get('scientificName') \
            and name_parts(split_qualifier(record.get('label'))[0]):
        label_markers = taxon_matching.explicit_markers(split_qualifier(record.get('label'))[0])
        their_markers = taxon_matching.explicit_markers(usage['scientificName'])
        explicit_ranks = {'subsp.', 'var.', 'subvar.', 'f.', 'subf.', 'nothosubsp.', 'nothovar.'}
        mine_rank = next((marker for marker in label_markers if marker in explicit_ranks), None)
        their_rank = next((marker for marker in their_markers if marker in explicit_ranks), None)
        special_differs = (('×' in label_markers) != ('×' in their_markers)
                           or ('agg.' in label_markers) != ('agg.' in their_markers))
        rank_differs = bool(mine_rank and their_rank and mine_rank != their_rank) or bool(mine_rank and not their_rank)
        if special_differs or rank_differs:
            return {'from': asserted_rank(record), 'to': usage.get('taxonRank'), 'confirm': True, 'kind': 'marker',
                    'text': f"writes your {' '.join(label_markers) or 'name without a marker'} as {' '.join(their_markers) or 'a name without one'}"}
    return found


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
    # Text after a name the parser could not read may be an authorship nobody can compare; not even COL's lack of one
    # may replace it (the user's text is kept instead).
    unreadable = bool(record.get('authorships_truncated')) or (not parsed.get('usable') and unread_trailing_text(record.get('label')))
    return col_authorship_agrees(supplied, (usage or {}).get('scientificNameAuthorship'), unreadable)


def unconfirmed(state):
    """Labels whose saved COL decision would replace the name but was never confirmed (saved before that was required).

    Such a decision is applied as "keep" and the label counts as undecided until it is made again.
    """
    records = {record_key(record): record for record in state.get('labels', [])}
    held = {}
    for label, decision in (state.get('decisions') or {}).items():
        found = _unconfirmed_replacement(records.get(label) or {'label': label}, decision)
        if found:
            held[label] = found
    return held


def _normal_qualifier(value):
    """Return a lower-case qualifier with a trailing period."""
    value = normal(value) or ''
    value = value.casefold()
    return value if value.endswith('.') else value + '.'


def qualifier_kind(record):
    """Classify the label and source-column qualifier as uncertain, doubtful, or absent."""
    qualifier = normal(record.get('qualifier'))
    label = str(record.get('label') or '')
    label_kind = ('doubt' if _DOUBT_MARKER.search(label) or _NEW_SPECIES.search(label) else
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


def doubt_formula(record):
    """Return a publishable cf./aff./nr./? formula: the label's own first (it holds for every row), else the column's
    (apply then writes it only to the rows that say it)."""
    label = str(record.get('label') or '')
    formula_match = re.search(r'(?i)(?:^|\s)(cf|aff|nr)\.?', label)
    if formula_match:
        return formula_match.group(1).lower() + '.'
    if '?' in label:
        return '?'
    for value in record.get('source_qualifiers') or []:
        qualifier = _normal_qualifier(value)
        if qualifier in {'cf.', 'aff.', 'nr.'}:
            return qualifier
        if '?' in str(value):
            return '?'
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
    if (record.get('match') or {}).get('exactAlternativesDropped'):
        return None  # more exact usages than were kept: the stem's homonyms cannot all be checked
    candidates = _stem_candidates(record)
    if not candidates:
        return None
    # A genus and its subgenus of the same name are one assertion; any other rank difference ("Anura" the order and the
    # genus) is resolved only when the source's own rank is the pick's.
    same_name_usages = [usage for usage, _ in candidates] + [usage for usage in _same_name_others(record, candidates[0][0])
                                                             if usage not in [item for item, _ in candidates]]
    ranks = {'genus' if usage.get('taxonRank') == 'subgenus' else usage.get('taxonRank') for usage in same_name_usages}
    if candidates[0][1] and len(ranks) > 1 and record.get('source_rank') != candidates[0][0].get('taxonRank'):
        return None
    # Without a rank COL cannot say what the stem is ("Larus" could be written with the source's "species").
    if candidates[0][1] and candidates[0][0].get('taxonRank') not in RANK_ORDER:
        return None
    if candidates[0][1]:
        usage = candidates[0][0]
        authorship = usage.get('scientificNameAuthorship')
        homonyms = any(not _same_taxon(usage, other) for other in _same_name_others(record, usage))
        if not authorship_agrees(record, usage) or homonyms:
            authorship = None
        # With homonyms only the name is published: no COL usage is named for it ("Sterna" the bird or the mollusc).
        return {'scientificName': usage.get('scientificName'), 'scientificNameAuthorship': authorship,
                'taxonRank': usage.get('taxonRank'),
                'usageId': str(usage['id']) if usage.get('id') is not None and not homonyms else None,
                'candidates': len(candidates)}
    if len(ranks) != 1 or not ranks <= set(RANK_ORDER):
        return None
    usage = candidates[0][0]
    return {'scientificName': usage.get('scientificName'), 'scientificNameAuthorship': None,
            'taxonRank': usage.get('taxonRank'), 'usageId': str(usage['id']) if len(candidates) == 1 and usage.get('id') is not None else None,
            'candidates': len(candidates)}


def _hint_normal(value):
    value = re.sub(r'\s*\([^)]*\)', '', normal(value) or '').strip().casefold()
    return {'metazoa': 'animalia', 'viridiplantae': 'plantae'}.get(value, value)


_PLACEHOLDER_HINTS = {'na', 'n/a', 'none', 'null', 'unknown', '-', '?', 'not assigned',
                      'incertae sedis', 'unassigned'}


def _same_name_match(record):
    """COL's pick when it is the user's own name: an EXACT match, or a variant (authorship, case) of the same name parts."""
    match = record.get('match') or {}
    usage = match.get('usage') or {}
    if not usage or match.get('hintOnly'):
        return None
    if match.get('matchType') == 'EXACT' or (match.get('matchType') in {'VARIANT', 'FUZZY', 'CANONICAL'} and same_name(record, usage)):
        return usage
    return None


def _homonyms(record):
    """Other exact usages of the user's own name that COL could also mean: another authorship or another lineage."""
    pick = _same_name_match(record)
    if not pick:
        return []
    # Any other usage of the same name counts, whatever its match type (a verbose pick can come back as a variant).
    found = [usage for usage in _same_name_others(record, pick) if not _same_taxon(pick, usage)]
    if (record.get('match') or {}).get('exactAlternativesDropped'):
        found.append({'id': None, 'scientificName': pick.get('scientificName'), 'unlisted': True})
    return found


def _same_name_others(record, pick):
    """The match's other usages written with the same name parts as `pick`."""
    return [usage for usage in (record.get('match') or {}).get('alternatives') or []
            if name_parts(usage.get('scientificName')) == name_parts(pick.get('scientificName'))
            and str(usage.get('id')) != str(pick.get('id'))]


def _same_taxon(left, right):
    """Two same-name usages are one taxon only at one rank (a genus and its subgenus count as one), with agreeing
    authorships (both given) and no lineage difference."""
    ranks = {'genus' if usage.get('taxonRank') == 'subgenus' else usage.get('taxonRank') for usage in (left, right)}
    if len(ranks) > 1:
        return False
    lineages = [{rank: _hint_normal((usage.get('classification') or {}).get(rank)) for rank in CONTEXT_RANKS} for usage in (left, right)]
    if any(lineages[0][rank] and lineages[1][rank] and lineages[0][rank] != lineages[1][rank] for rank in CONTEXT_RANKS):
        return False
    # A missing authorship cannot show it is the same taxon.
    return bool(normal(left.get('scientificNameAuthorship')) and normal(right.get('scientificNameAuthorship'))
                and authorships_agree(left.get('scientificNameAuthorship'), right.get('scientificNameAuthorship')))


def _usage_doubtful(record):
    """COL has another usage of this name, so keep the supplied authorship on automatic acceptance."""
    return bool(_homonyms(record))


def _rank_change(record, usage):
    """What a COL usage does to the rank the source supplies for its own name (taxon_matching.rank_change); else None."""
    return rank_change(asserted_name(record), record.get('source_rank'), (usage or {}).get('taxonRank'))


def _stem_confirmation(record, stem):
    """Why publishing this stem needs the user's second click, or None."""
    if not stem:
        return None
    if _stem_rank_too_high(record, stem):
        return f"is {_article(stem['taxonRank'])} {stem['taxonRank']}, while “sp.” follows a genus or family"
    return _rank_change(record, stem)


def _stem_rank_too_high(record, stem):
    """'sp.'/'spp.' follow a genus or family: a stem COL has at a higher rank ("Anura sp.", the order) is not what was meant."""
    qualifiers = {_normal_qualifier(value) for value in [record.get('qualifier'), *(record.get('source_qualifiers') or [])] if value}
    rank = (stem or {}).get('taxonRank')
    return bool(qualifiers & {'sp.', 'spp.'}) and rank in RANK_ORDER and RANK_ORDER.index(rank) < RANK_ORDER.index('family')


def _rank_conflict(record):
    """COL's rank when a uninomial's supplied rank (genus or above) differs from it, else None: maybe another taxon.

    The COL rank is the exact match's, or for "sp."/"indet." names the stem's. A "species" given for "Larus sp." is
    below genus and says nothing about the stem.
    """
    source_rank = record.get('source_rank')
    supplied = {source_rank} if source_rank else set(record.get('row_ranks') or [])
    if supplied & INFRASPECIFIC_RANKS and len(name_parts(asserted_name(record))) > 2:
        # An unmarked trinomial supplied as a variety (on any of its rows) is not COL's subspecies; a broad "species" is
        # tolerated.
        col_rank = (_same_name_match(record) or {}).get('taxonRank')
        return col_rank if col_rank in INFRASPECIFIC_RANKS and supplied & INFRASPECIFIC_RANKS - {col_rank} else None
    if source_rank not in RANK_ORDER or RANK_ORDER.index(source_rank) > RANK_ORDER.index('genus'):
        return None
    if len(name_parts(asserted_name(record))) != 1:
        return None
    if qualifier_kind(record) == 'uncertain':
        col_rank = (stem_usage(record) or {}).get('taxonRank')
    else:
        col_rank = (_same_name_match(record) or {}).get('taxonRank')
    return col_rank if col_rank in RANK_ORDER and col_rank != source_rank else None


def _lineage(record):
    """COL's classification of the user's own name: the same-name pick's, or what every exact stem candidate agrees on."""
    pick = _same_name_match(record)
    if pick:
        return pick.get('classification') or {}
    if qualifier_kind(record) != 'uncertain':
        return {}
    candidates = [usage for usage, _ in _stem_candidates(record)]
    shared = {}
    for rank in ('kingdom', 'phylum', 'class', 'order', 'family'):
        values = {(usage.get('classification') or {}).get(rank) for usage in candidates}
        if len(values) == 1 and None not in values:
            shared[rank] = values.pop()
    return shared


def _conflicts(record):
    match = record.get('match') or {}
    usage = match.get('usage') or {}
    out = []
    if match.get('matchType') == 'EXACT' and usage and not match.get('hintOnly'):
        # The matched usage is compared, not its accepted name: a synonym the user wrote is still the user's name.
        found = change(record, usage, 'EXACT')
        if found:
            name = usage.get('scientificName') or 'another name'
            rank = usage.get('taxonRank') or 'unknown rank'
            suffix = ' — a broader group' if found.get('kind') == 'coarser' else ''
            out.append(('name', 'check:name', f'Catalogue of Life has {name} ({rank}) instead{suffix}'))
    col_rank = _rank_conflict(record)
    if col_rank:
        yours = record.get('source_rank') or next(rank for rank in sorted(record.get('row_ranks') or [])
                                                  if rank in INFRASPECIFIC_RANKS and rank != col_rank)
        out.append(('rank', f"check:rank:{yours}:{col_rank}",
                    f"Your file says {yours}; Catalogue of Life has this name as {_article(col_rank)} {col_rank}"))
    idcheck = match.get('idCheck') or {}
    if idcheck.get('outcome') == 'elsewhere' or 'SCIENTIFIC_NAME_AND_ID_INCONSISTENT' in (match.get('issues') or []):
        fields = ', '.join((idcheck.get('fields') or record.get('source_ids') or {}).keys()) or 'identifier'
        target = (match.get('matchedId') or {}).get('scientificName') or (idcheck.get('usage') or {}).get('scientificName') or 'another name'
        out.append(('id', 'check:id', f'Your {fields} points to {target} in Catalogue of Life'))
    mixed = record.get('mixed_hints') or []
    if mixed:
        if 'kingdom' in mixed:
            values = record.get('hint_values', {}).get('kingdom', [])
            rendered = ' and '.join(f'{value} ({rows} rows)' for value, rows in values[:2])
            text = f'Your file lists this name under two kingdoms: {rendered}.'
            out.append(('mixed', 'check:mixed:kingdom', text))
        if 'rank' in mixed:
            values = record.get('rank_values') or []
            rendered = ' and '.join(f'{value} ({rows} rows)' for value, rows in values[:2])
            out.append(('mixed', 'check:mixed:rank', f'Your file gives this name two ranks: {rendered}.'))
    # The source's lineage is compared with COL's pick whenever that pick is the same name, whatever the match type, or
    # for a "sp." stem with the lineage all its same-name candidates share.
    lineage = _lineage(record)
    for rank in ('kingdom',):
        hint = (record.get('hints') or {}).get(rank)
        theirs = lineage.get(rank)
        if hint and theirs and _hint_normal(hint) != _hint_normal(theirs):
            out.append((rank, f'check:kingdom:{hint}:{theirs}',
                        f'Your file says kingdom {hint}; Catalogue of Life has this name in {theirs}.'))
    if lineage:
        for rank in ('phylum', 'class', 'order', 'family'):
            pick_value = _hint_normal(lineage.get(rank))
            if not pick_value:
                continue
            values = list(record.get('hint_values', {}).get(rank) or [])
            if (record.get('hints') or {}).get(rank):
                values.insert(0, [record['hints'][rank], record.get('rows', 1)])
            pick = _same_name_match(record)
            candidates = [usage for usage in (match.get('alternatives') or []) if pick
                          and name_parts(usage.get('scientificName')) == name_parts(pick.get('scientificName'))]
            for candidate in candidates:
                classification = candidate.get('classification') or {}
                candidate_value = _hint_normal(classification.get(rank))
                if candidate_value and candidate_value != pick_value and any(_hint_normal(value) == candidate_value for value, _ in values):
                    display = next(value for value, _ in values if _hint_normal(value) == candidate_value)
                    name = candidate.get('scientificName') or asserted_name(record)
                    out.append(('lineage', 'check:lineage', f'Catalogue of Life also has a {name} in {display}, the group your file uses.'))
                    break
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
        mine = own_name_kind(record.get('parsed'), qualifier_kind(record) is not None)
        return 'col' if authorship_agrees(record, usage) and not _usage_doubtful(record) else mine
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
    if record_key(record) in decisions and (decisions[record_key(record)].get('by') or 'user') == 'user':
        return False
    kind = classification.get('kind')
    qkind = qualifier_kind(record)
    match = record.get('match') or {}
    usage = match.get('usage') or {}
    if option == 'mine' or option == 'keep':
        return True
    if qkind == 'doubt' and not (option == 'col' and classification.get('kind') in {'check', 'unconfirmed'}):
        return False
    if option == 'parsed':
        parsed = record.get('parsed') or {}
        return qkind is None and parsed.get('usable') and parsed.get('lossless')
    if option == 'stem':
        return kind == 'uncertain' and stem_usage(record) is not None and not _stem_rank_too_high(record, stem_usage(record))
    if option == 'col':
        if kind == 'check' and qkind == 'uncertain':
            stem = stem_usage(record)
            return bool(stem or usage.get('scientificName'))
        return bool(kind in {'auto', 'spelling', 'check', 'unconfirmed'} and usage and usage.get('scientificName'))
    return False


def classify(record):
    """Classify one checked name and calculate its intrinsic safe options."""
    if not checked(record):
        return {'group': 'unchecked', 'kind': None, 'reasons': [], 'default': None, 'eligible': []}
    conflicts = _conflicts(record)
    if record.get('preprocessed'):
        conflicts.insert(0, ('interpretation', 'check:interpretation', 'The name was interpreted from your label; check it before publishing.'))
    if conflicts:
        group = conflicts[0][1]
        kind = 'check'
        reasons = [{'code': code, 'text': text} for code, _, text in conflicts]
    elif qualifier_kind(record) == 'doubt':
        group, kind, reasons = 'unconfirmed', 'unconfirmed', _reason_for_unconfirmed(record)
    elif qualifier_kind(record) == 'uncertain':
        stem = stem_usage(record)
        if stem and _stem_rank_too_high(record, stem):
            group, kind = 'unconfirmed', 'unconfirmed'
            reasons = [{'code': 'rank', 'text': f"“sp.” follows a genus or family; COL has {stem['scientificName']} as {_article(stem['taxonRank'])} {stem['taxonRank']}"}]
        elif stem:
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
            homonyms = _homonyms(record)
            if homonyms:
                reasons.append({'code': 'homonym', 'text': f"COL has {len(homonyms) + 1} taxa written this way; your name is kept without choosing one"})
            marker_only_added = (not taxon_matching.explicit_markers(split_qualifier(record['label'])[0])
                                 and taxon_matching.explicit_markers(usage.get('scientificName')))
            if marker_only_added:
                reasons.append({'code': 'marker_added', 'text': f"Catalogue of Life writes it {usage.get('scientificName')}"})
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
    """Summarize review groups and the effect of each explicit group choice."""
    decisions = state.get('decisions') or {}
    held_labels = set(unconfirmed(state))
    buckets = {}
    for record in state.get('labels', []):
        classification = (classified or {}).get(record_key(record)) or classify(record)
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
        metrics = {option: Counter() for option in opts}
        auto = by_user = bulk = held = authorship_kept = undecided = 0
        rows = 0
        for record, classification in members:
            rows += int(record.get('rows', 0))
            decision = decisions.get(record_key(record))
            if not decision or record_key(record) in held_labels:
                undecided += 1
            elif str(decision.get('by') or 'user').startswith('auto:'):
                auto += 1
                authorship_kept += int(any(reason['code'] == 'authorship' for reason in classification['reasons']))
            elif str(decision.get('by') or 'user').startswith('bulk:'):
                bulk += 1
            elif decision.get('by', 'user') == 'user':
                by_user += 1
            held += int(record_key(record) in held_labels)
            user_decided = bool(decision and (decision.get('by') or 'user') == 'user')
            if user_decided:
                continue
            metrics['mine'].update(eligible=1)
            target = (stem_usage(record) or (record.get('match') or {}).get('usage')) \
                if (kind == 'uncertain' or qualifier_kind(record) == 'uncertain') else (record.get('match') or {}).get('usage')
            if not target or not target.get('scientificName'):
                metrics.get('col', metrics.get('stem', Counter())).update(kept=1)
                continue
            option = 'stem' if kind == 'uncertain' else 'col'
            counts = metrics.get(option)
            if counts is None:
                continue
            counts.update(eligible=1)
            if kind == 'auto':
                continue  # the automatic group's "Use COL names" is the automatic acceptance: it changes nothing
            # A "sp." name decided by COL's name publishes its stem, in any group.
            is_stem = (kind == 'uncertain' or qualifier_kind(record) == 'uncertain') and bool(stem_usage(record))
            found = change(record, target, 'EXACT' if is_stem else (record.get('match') or {}).get('matchType'))
            source_rank, target_rank = record.get('source_rank'), target.get('taxonRank')
            rank_broader = source_rank in RANK_ORDER and target_rank in RANK_ORDER and taxon_matching.rank_above(target_rank, source_rank)
            rank_change = _rank_change(record, target)
            if is_stem:
                source_is_genus_or_above = source_rank in RANK_ORDER and RANK_ORDER.index(source_rank) <= RANK_ORDER.index('genus')
                stem_rank_differs = source_is_genus_or_above and target_rank in RANK_ORDER and source_rank != target_rank
                if _stem_rank_too_high(record, target) or stem_rank_differs:
                    counts.update(broader=1)
            elif (found and found.get('kind') == 'coarser') or rank_broader:
                counts.update(broader=1)
            elif found and found.get('kind') == 'spelling':
                counts.update(spelling=1)
            elif found or rank_change:
                counts.update(different=1)
            usage_authorship = target.get('scientificNameAuthorship')
            parsed = record.get('parsed') or {}
            supplied = [*record.get('source_authorships', ()), *([parsed['authorship']] if parsed.get('usable') and parsed.get('authorship') else [])]
            # COL's authorship replaces a different one, or another name clears the user's ("Calanus Smith" -> Arthropoda).
            has_supplied = bool(supplied) or bool(record.get('authorships_truncated'))
            changes_taxon = (found and found.get('kind') != 'spelling') or bool(rank_change)
            if (normal(usage_authorship) and not authorship_agrees(record, target)) or (changes_taxon and has_supplied
                                                                                         and not normal(usage_authorship)):
                counts.update(authorship=1)
            # The rank written differs from the supplied one (a "species" beside a "sp." stem says nothing about it).
            sp_noise = is_stem and source_rank in RANK_ORDER and RANK_ORDER.index(source_rank) > RANK_ORDER.index('genus')
            # Rows of one label may give several ranks (no single source_rank): any that differs counts.
            row_ranks = set(record.get('row_ranks') or []) or ({source_rank} if source_rank else set())
            if rank_change or (target_rank and any(rank != target_rank for rank in row_ranks) and not sp_noise):
                counts.update(rank=1)
        signature = None
        if kind == 'check':
            # The signature is the conflict that named the group (a label may have several).
            reason = next((r for r in bucket['reasons'] if r['code'] == bucket['id'].split(':')[1]), None)
            if reason:
                record = members[0][0]
                code = reason['code']
                if code in {'kingdom', 'phylum', 'class'}:
                    signature = {'code': code, 'yours': (record.get('hints') or {}).get(code), 'col': _lineage(record).get(code)}
                elif code == 'mixed':
                    signature = {'code': code, 'yours': ', '.join(record.get('mixed_hints') or []), 'col': None}
                elif code == 'id':
                    idcheck = (record.get('match') or {}).get('idCheck') or {}
                    signature = {'code': code, 'yours': ', '.join((idcheck.get('fields') or {}).keys()) or None,
                                 'col': ((record.get('match') or {}).get('matchedId') or {}).get('scientificName')
                                 or (idcheck.get('usage') or {}).get('scientificName')}
                elif code == 'rank':
                    signature = {'code': code, 'yours': record.get('source_rank'), 'col': _rank_conflict(record)}
                else:
                    signature = {'code': code, 'yours': asserted_name(record),
                                 'col': ((record.get('match') or {}).get('usage') or {}).get('scientificName')}
        default = ('col' if kind == 'auto' else 'stem' if kind == 'uncertain' else
                   ('col' if metrics.get('col', {}).get('eligible') else 'mine') if kind == 'spelling' else
                   'mine' if kind == 'unconfirmed' else None)
        saved_choice = (state.get('group_choices') or {}).get(bucket['id'])
        choice = saved_choice if saved_choice in {*opts, 'manual'} else (('col' if kind == 'auto' else 'stem') if kind in {'auto', 'uncertain'} and auto else None)
        options = []
        for option in opts:
            counts = metrics[option]
            if option == 'mine':
                options.append({'decision': option, 'eligible': counts.get('eligible', 0), 'kept': 0,
                                'broader': 0, 'different': 0, 'spelling': 0, 'authorship': 0, 'rank': 0})
            else:
                options.append({'decision': option, **{key: counts.get(key, 0) for key in
                                ('eligible', 'kept', 'broader', 'different', 'spelling', 'authorship', 'rank')}})
        output.append({'id': bucket['id'], 'kind': kind, 'labels': len(members), 'rows': rows, 'undecided': undecided,
                       'auto': auto, 'by_user': by_user, 'bulk': bulk, 'default': default, 'choice': choice,
                       'options': options,
                       'signature': signature, 'authorship_kept': authorship_kept, 'held': held})
    return output


def build_decision(record, spec, state, by='user', group_kind=None):
    """Build a validated, self-contained snapshot for a user's or system's name choice."""
    if not isinstance(spec, dict):
        raise NameDecisionError('Choose a name decision.')
    requested = spec.get('decision')
    if requested == 'mine':
        requested = own_name_kind(record.get('parsed'), qualifier_kind(record) is not None)
        spec = {**spec, 'decision': requested}
    if requested not in DECISIONS:
        raise NameDecisionError(f'Choose one of: {", ".join(DECISIONS)}.')
    kind = requested
    automatic = str(by).startswith('auto:')
    bulk = str(by).startswith('bulk:')
    if automatic and qualifier_kind(record) == 'doubt':
        raise NameDecisionError('An uncertain identification can only be decided one at a time.')
    if automatic and kind in {'col', 'alternative', 'stem'} and (set(record.get('mixed_hints') or []) & {'kingdom', 'rank'} or _rank_conflict(record)):
        raise NameDecisionError('A name whose rows or rank may mean another taxon takes a COL name only one at a time.')
    if automatic and kind in {'col', 'alternative'} and _homonyms(record):
        raise NameDecisionError('COL has more than one taxon with this name; choose one name at a time.')
    if automatic and kind == 'stem' and _stem_rank_too_high(record, stem_usage(record)):
        raise NameDecisionError('"sp." follows a genus or family; this stem is decided one name at a time.')
    snapshot = {'decision': kind, 'by': by, 'at': timezone.now().isoformat(), 'scientificName': None,
                'scientificNameAuthorship': None, 'taxonRank': None, 'source': 'verbatim' if kind == 'keep' else 'none'}
    parsed, match = record.get('parsed') or {}, record.get('match') or {}
    if kind == 'empty':
        snapshot['scientificName'] = ''
    elif kind == 'corrected':
        if by != 'user':
            raise NameDecisionError('Only you can type a corrected name.')
        try:
            snapshot.update(source='reviewer', scientificName=corrected_name(spec.get('scientificName')))
        except ValueError as exc:
            raise NameDecisionError(str(exc)) from exc
    elif kind in {'parsed', 'stem'}:
        if kind == 'stem':
            usage = stem_usage(record)
            if not usage:
                raise NameDecisionError(f'There is no exact stem name in Catalogue of Life for "{record["label"]}".')
            if automatic and change(record, usage, 'EXACT') is not None:
                raise NameDecisionError('A non-user decision cannot change the asserted name or decide a doubtful identification.')
            confirmation = _stem_confirmation(record, usage)
            if confirmation and not bulk and spec.get('confirm_coarser') is not True:
                raise NameDecisionError(f'"{usage["scientificName"]}" {confirmation}. Confirm it explicitly, or keep your name.')
            snapshot.update(source='col', scientificName=usage['scientificName'], scientificNameAuthorship=usage.get('scientificNameAuthorship'),
                            taxonRank=usage.get('taxonRank'), usageId=usage.get('usageId'), candidates=usage['candidates'],
                            checklist=_checklist(record, state), changeKind=None, nameRules=NAME_RULES_VERSION)
            if confirmation:
                # An explicitly confirmed stem at another or a higher rank ("Anura sp." as COL's order).
                snapshot.update(rankChange=confirmation, changeKind='coarser' if _stem_rank_too_high(record, usage) else 'rank',
                                replaces=confirmation, confirmedCoarser=True)
        else:
            if not parsed.get('usable'):
                raise NameDecisionError(f'The name parser could not split "{record["label"]}".')
            snapshot.update(source='parser', scientificName=parsed['canonical'], scientificNameAuthorship=parsed.get('authorship'), taxonRank=parsed.get('rank'))
    elif kind in {'col', 'alternative'}:
        checklist = _checklist(record, state)
        if kind == 'col':
            usage = match.get('usage')
            # A confirmation names the usage it was shown; COL's pick may have changed since (a new check).
            if usage and spec.get('usage_id') is not None and str(spec['usage_id']) != str(usage.get('id')):
                raise NameDecisionError(f'Catalogue of Life now suggests another name for "{record["label"]}"; choose again.')
        else:
            wanted = str(spec.get('usage_id')) if spec.get('usage_id') is not None else None
            usage = next((item for item in match.get('alternatives') or [] if wanted and str(item['id']) == wanted), None)
            if usage is None and wanted and by == 'user' and spec.get('confirm_coarser') is True:
                # A name the user searched Catalogue of Life for and picked is itself the explicit choice.
                try:
                    usage = {**taxon_matching.resolve_col_usage(wanted), 'matchType': None}
                except TaxonServiceError as exc:
                    raise NameDecisionError(f'Catalogue of Life could not be reached to look up that name: {exc}') from exc
                release_key = usage.get('checklistBankDatasetKey')
                if not release_key or str(release_key) != str(checklist.get('checklistBankDatasetKey')):
                    checklist = {'checklistBankDatasetKey': str(release_key)} if release_key else {}
        if not usage or not usage.get('scientificName'):
            raise NameDecisionError(f'There is no such Catalogue of Life name to accept for "{record["label"]}".')
        match_type = match.get('matchType') if kind == 'col' else usage.get('matchType')
        found = change(record, usage, match_type)
        replaces = found if found and found['confirm'] else None
        if automatic and replaces:
            raise NameDecisionError(f'"{usage["scientificName"]}" {replaces["text"]} for "{record["label"]}"; it is never accepted in bulk.')
        if automatic:
            allowed_spelling = group_kind == 'spelling' and found and found.get('kind') == 'spelling'
            if found and not allowed_spelling:
                raise NameDecisionError('A non-user decision cannot change the asserted name.')
            if not authorship_agrees(record, usage):
                raise NameDecisionError('A non-user decision cannot replace a different authorship.')
        if replaces and not bulk and spec.get('confirm_coarser') is not True:
            raise NameDecisionError(f'"{usage["scientificName"]}" {replaces["text"]} for "{record["label"]}". Confirm that replacement explicitly, or keep your name.')
        if _rank_change(record, usage) and not bulk and spec.get('confirm_coarser') is not True:
            raise NameDecisionError(f'"{usage["scientificName"]}" {_rank_change(record, usage)}. Confirm that explicitly, or keep your name.')
        if _rank_change(record, usage):
            snapshot['rankChange'] = _rank_change(record, usage)
        if (normal(usage.get('scientificNameAuthorship')) and not authorship_agrees(record, usage)
                and not bulk and spec.get('confirm_coarser') is not True):
            raise NameDecisionError(f'Catalogue of Life writes the authorship of "{usage["scientificName"]}" as '
                                    f'"{usage["scientificNameAuthorship"]}", not as in your data. Confirm that replacement explicitly, or keep your name.')
        snapshot.update(source='col', scientificName=usage['scientificName'], scientificNameAuthorship=usage.get('scientificNameAuthorship'),
                        taxonRank=usage.get('taxonRank'), usageId=str(usage['id']) if usage.get('id') is not None else None,
                        taxonomicStatus=usage.get('status'), classification=usage.get('classification') or {}, matchType=match_type, checklist=checklist,
                        changeKind=found['kind'] if found else None, nameRules=NAME_RULES_VERSION)
        if normal(usage.get('scientificNameAuthorship')) and not authorship_agrees(record, usage):
            snapshot['authorshipReplaced'] = True
        if replaces:
            snapshot.update(replaces=replaces['text'], confirmedCoarser=True)
        elif found:
            snapshot['corrects'] = found['text']
    if kind in {'stem', 'col', 'alternative'} and qualifier_kind(record) in {'uncertain', 'doubt'} and same_name(record, {'scientificName': snapshot.get('scientificName')}):
        # A valid row qualifier wins, then the label qualifier; a blank column-only row stays blank.
        snapshot['stemFormula'] = True
        formula = formula_qualifier(record)
        if qualifier_kind(record) == 'doubt':
            formula = doubt_formula(record)
        if formula:
            snapshot['taxonFormula'] = 'A ' + formula
    if str(by).startswith('bulk:'):
        snapshot['batch'] = spec.get('batch')
    return snapshot


def auto_accept(state):
    """Accept safe exact matches and resolvable uncertain stems in the in-memory state."""
    decisions = state.setdefault('decisions', {})
    declined = set(state.get('auto_declined') or [])
    count = 0
    for record in state.get('labels', []):
        label = record_key(record)
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


def _keeps_name(record, decision):
    """A COL name that is the asserted name, or only corrects its spelling, may keep supplied parts COL lacks; a different
    name may not. A confirmed marker or rank change is another taxon: the supplied authorship is not carried over to it."""
    return (decision.get('decision') in {'col', 'alternative', 'stem'} and not decision.get('rankChange')
            and (decision.get('changeKind') == 'spelling'
                 or (decision.get('changeKind') is None
                     and same_name(record, {'scientificName': decision.get('scientificName') or ''}))))


def _label_authorship(record):
    parsed = record.get('parsed') or {}
    return normal(parsed.get('authorship')) if parsed.get('usable') else ''


def row_formula(record, decision, qualifier):
    """Preserve each row's uncertainty; a qualifier on another row never leaks into this one."""
    formula = decision.get('taxonFormula')
    doubtful = bool(formula) and formula[2:] not in UNCERTAIN_QUALIFIERS
    if not decision.get('stemFormula') and not doubtful:
        return formula
    own = _normal_qualifier(taxon_matching._clean_label(qualifier)) if not taxon_matching._is_blank(qualifier) else None
    label_qualifier = _normal_qualifier(record['qualifier']) if record.get('qualifier') else None
    if own in UNCERTAIN_QUALIFIERS and decision.get('stemFormula'):
        return f'A {own}'
    if own in {'cf.', 'aff.', 'nr.', '?.'}:
        return 'A ?' if own == '?.' else f'A {own}'
    if decision.get('stemFormula') and label_qualifier in UNCERTAIN_QUALIFIERS:
        return f'A {label_qualifier}'
    if doubtful and (_DOUBT_MARKER.search(record['label']) or _NEW_SPECIES.search(record['label'])):
        return formula
    return None


_PREVIEW = object()


def reviewed_taxon_id(record, decision, qualifier=_PREVIEW):
    """The release-specific reference for an unqualified, reviewed COL concept."""
    if (decision['decision'] not in {'col', 'alternative'} or decision.get('taxonomicStatus') != 'accepted'
            or not decision.get('scientificName')):
        return None
    # GBIF uses IDs ahead of the name. Keep uncertain identifications out of that path.
    if (qualifier_kind(record) or normal(decision.get('taxonFormula')).casefold() not in {'', 'a'}
            or (qualifier is not _PREVIEW and normal(qualifier))):
        return None
    checklist = decision.get('checklist') or {}
    dataset = str(checklist.get('checklistBankDatasetKey') or '')
    usage = str(decision.get('usageId') or '')
    if re.fullmatch(r'[1-9][0-9]*', dataset) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._~-]*', usage):
        return f'{taxon_matching.CHECKLISTBANK_API}/dataset/{dataset}/taxon/{usage}'
    return None


def decision_fields(record, decision, allowed=None, qualifier=_PREVIEW):
    """The complete write policy for either host, restricted to its target schema.

    COL classification fills blanks only. Uncertainty goes to taxonFormula where supported,
    otherwise identificationQualifier. Existing classification and qualifiers always stand.
    """
    outcomes = written_fields(decision['decision'], decision, keeps_name=_keeps_name(record, decision),
                              label_authorship=_label_authorship(record))
    if decision['decision'] in {'col', 'alternative', 'stem'}:
        for rank in taxon_matching.CLASSIFICATION_RANKS:
            value = (decision.get('classification') or {}).get(rank)
            if value:
                outcomes[rank] = ('yours_or', value)
        formula = decision.get('taxonFormula') if qualifier is _PREVIEW else row_formula(record, decision, qualifier)
        if formula:
            field = 'taxonFormula' if allowed is not None and 'taxonFormula' in allowed else 'identificationQualifier'
            value = formula if field == 'taxonFormula' else (record.get('qualifier') or formula[2:])
            outcomes[field] = ('yours_or', value)
    if allowed is not None and 'taxonID' in allowed:
        taxon_id = reviewed_taxon_id(record, decision, qualifier)
        if taxon_id:
            outcomes['taxonID'] = ('yours_or', taxon_id)
    return {field: outcome for field, outcome in outcomes.items() if allowed is None or field in allowed}


def decision_preview(record, decision, allowed=None):
    """Preview the same outcomes the writer uses, including target-specific columns."""
    if not decision:
        return []
    if allowed is None:
        from api.dwc_dp_specs import get_table_spec
        allowed = set()
        for table in record.get('preview_tables') or record.get('tables') or ('occurrence', 'identification'):
            try:
                allowed.update(get_table_spec(table).fields)
            except KeyError:
                allowed = None
                break
    items = preview_items(decision_fields(record, decision, allowed), record['label'])
    if record.get('source_qualifiers'):
        for item in items:
            if item['field'] in {'taxonFormula', 'identificationQualifier'}:
                item['note'] = 'from each row’s qualifier, or the qualifier in the label'
                formulas = {row_formula(record, decision, qualifier) for qualifier in record['source_qualifiers']}
                if item['value']:
                    formulas.add(item['value'] if item['field'] == 'taxonFormula' else 'A ' + item['value'])
                values = sorted(value if item['field'] == 'taxonFormula' else value[2:] for value in formulas if value)
                if len(values) > 1:
                    item['values'] = values
    return items


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
                        'corrects': found['text'] if found and found['kind'] == 'spelling' else None, 'rank_note': None,
                        # COL's authorship is not the user's: the review asks before writing it over theirs.
                        'authorship_differs': bool(normal(usage.get('scientificNameAuthorship'))) and not authorship_agrees(record, usage),
                        'rank_change': _rank_change(record, usage)})
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


def _entry(record, decisions, held=(), classification=None, release=None):
    """Build one public state entry, including its classification and safe options."""
    parsed, match = record.get('parsed'), record.get('match')
    usage = (match or {}).get('usage') or {}
    choices = col_choices(record)
    main = next((choice for choice in choices if choice['decision'] == 'col'), None)
    decision = with_checklist_release(decisions.get(record_key(record)), release, match)
    classification = classification or classify(record)
    return {**record, 'checked': checked(record), 'decision': decision,
            # An older decision for a coarser or different COL name: the user's name is kept until it is made again.
            'decision_unconfirmed': record_key(record) in held,
            'decision_held': ({'kind': held[record_key(record)]['kind'], 'text': held[record_key(record)]['text']}
                              if record_key(record) in held else None),
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
            'stem': stem_usage(record) if qualifier_kind(record) == 'uncertain' else None,
            # What the decision writes; a held decision is applied as "keep".
            'preview': decision_preview(record, {'decision': 'keep'} if record_key(record) in held else decision),
            'stem_confirm': _stem_confirmation(record, stem_usage(record)) if qualifier_kind(record) == 'uncertain' else None}


NOTICE_RANKS = ('phylum', 'class', 'order', 'family')


def classification_notice(labels):
    """Summarize labels whose phylum, class, order or family differ from COL's or between their rows."""
    examples, count = [], 0
    for record in labels:
        lineage = _lineage(record)
        hints = record.get('hints') or {}
        values_by_rank = record.get('hint_values') or {}
        mixed = any(len(values_by_rank.get(rank) or []) > 1 for rank in NOTICE_RANKS)
        differs = any(hints.get(rank) and lineage.get(rank)
                      and _hint_normal(hints[rank]) != _hint_normal(lineage[rank])
                      for rank in NOTICE_RANKS)
        if not (mixed or differs):
            continue
        source_kingdom = hints.get('kingdom')
        col_kingdom = (lineage or {}).get('kingdom')
        kingdom_differs = bool(source_kingdom and col_kingdom and _hint_normal(source_kingdom) != _hint_normal(col_kingdom))
        if kingdom_differs and not mixed:
            continue
        count += 1
        yours_parts, col_parts = [], []
        # Examples show phylum / class when they differ, else the order or family that does.
        shown = [rank for rank in ('phylum', 'class') if rank in values_by_rank or (hints.get(rank) and lineage.get(rank)
                 and _hint_normal(hints[rank]) != _hint_normal(lineage[rank]))] or \
                [rank for rank in ('order', 'family') if rank in values_by_rank or (hints.get(rank) and lineage.get(rank)
                 and _hint_normal(hints[rank]) != _hint_normal(lineage[rank]))]
        for rank in shown:
            source = hints.get(rank)
            if values_by_rank.get(rank):
                alternatives = values_by_rank[rank]
                source = next((value for value, _ in alternatives
                               if lineage.get(rank) and _hint_normal(value) != _hint_normal(lineage[rank])), alternatives[0][0])
            if source:
                yours_parts.append(source)
            if lineage.get(rank):
                col_parts.append(lineage[rank])
        if yours_parts and col_parts:
            example = {'yours': ' / '.join(yours_parts), 'col': ' / '.join(col_parts)}
            if example not in examples and len(examples) < 2:
                examples.append(example)
    return {'names': count, 'mixed': any(any(len((record.get('hint_values') or {}).get(rank) or []) > 1
                                               for rank in NOTICE_RANKS) for record in labels),
            'examples': examples} if count else None


def set_decisions(state, changes):
    """Validate and save per-label decisions, recording withdrawn automatic choices."""
    if not state.get('labels'):
        raise NameDecisionError('There are no names to review for this plan.')
    if not isinstance(changes, dict) or len(changes) > MAX_LABELS:
        raise NameDecisionError('Name decisions must map labels to decisions.')
    state.setdefault('decisions', {})
    index = {record_key(record): record for record in state['labels']}
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



def bulk_decide(state, group, decision):
    """Apply one eligible group decision and save an undoable batch."""
    if not state.get('labels'):
        raise NameDecisionError('There are no names to review for this plan.')
    if not isinstance(group, str) or not isinstance(decision, str):
        raise NameDecisionError('Bulk decisions need a group and decision.')
    state.setdefault('decisions', {})
    classifications = {record_key(record): classify(record) for record in state.get('labels', [])}
    current_groups = {item['id']: item for item in groups(state, classifications)}
    summary = current_groups.get(group)
    if not summary:
        raise NameDecisionError('That name group is no longer available.')
    legacy_choice = decision in {'parsed', 'keep'}
    if decision not in {option['decision'] for option in summary['options']} and not legacy_choice:
        raise NameDecisionError('That decision is not an option for this group.')
    batch_id = secrets.token_hex(6)
    kind = summary['kind']
    changes = {}
    count = 0
    for record in state.get('labels', []):
        classification = classifications[record_key(record)]
        if classification['group'] != group:
            continue
        previous = state['decisions'].get(record_key(record))
        if previous and previous.get('by', 'user') == 'user':
            continue
        if legacy_choice and not eligible(record, decision, classification, state['decisions']):
            continue
        resolved = decision
        if decision == 'mine':
            resolved = own_name_kind(record.get('parsed'), qualifier_kind(record) is not None)
        if decision == 'col' and kind == 'auto':
            resolved = row_default(record, classification)
        if decision in {'col', 'stem'} and qualifier_kind(record) == 'uncertain':
            if stem_usage(record):
                resolved = 'stem'
            elif decision == 'col' and (record.get('match') or {}).get('usage'):
                resolved = 'col'
        if decision in {'col', 'stem'} and not (stem_usage(record) if resolved == 'stem' else (record.get('match') or {}).get('usage')):
            # No COL name to use: the user's own name, as "Keep original names" would write it.
            resolved = own_name_kind(record.get('parsed'), qualifier_kind(record) is not None)
        changes[record_key(record)] = copy.deepcopy(previous)
        state['decisions'][record_key(record)] = build_decision(record, {'decision': resolved, 'batch': batch_id,
                                                                      'confirm_coarser': True}, state,
                                                              by=f'bulk:{kind}', group_kind=kind)
        count += 1
    if not count:
        raise NameDecisionError('No name in this group can take that decision; decide them one at a time.')
    batch = {'id': batch_id, 'group': group, 'decision': decision, 'count': count,
             'at': timezone.now().isoformat(), 'changes': changes}
    state.setdefault('batches', []).append(batch)
    state['batches'] = state['batches'][-MAX_BATCHES:]
    if not legacy_choice:
        state.setdefault('group_choices', {})[group] = decision
    return {'batch': batch_id, 'count': count}


def group_choice(state, group, choice):
    """Apply a group toggle, or remove non-user choices for a manual review."""
    if not state.get('labels'):
        raise NameDecisionError('There are no names to review for this plan.')
    if not isinstance(group, str) or not isinstance(choice, str):
        raise NameDecisionError('Choose a name group and group decision.')
    classified = {record_key(record): classify(record) for record in state.get('labels', [])}
    summary = next((item for item in groups(state, classified) if item['id'] == group), None)
    if summary is None:
        raise NameDecisionError('That name group is no longer available.')
    option = next((item for item in summary['options'] if item['decision'] == choice), None)
    if choice in {'col', 'stem'} and option is not None and not option['eligible']:
        raise NameDecisionError('No name in this group has a Catalogue of Life name to use.')
    if choice == 'manual':
        declined = set(state.get('auto_declined') or [])
        for record in state.get('labels', []):
            if classified[record_key(record)]['group'] != group:
                continue
            saved = state.get('decisions', {}).get(record_key(record))
            if not saved or (saved.get('by') or 'user') != 'user':
                # Whatever made it, a manually reviewed name is never accepted automatically again.
                declined.add(record_key(record))
                state['decisions'].pop(record_key(record), None)
        state['auto_declined'] = sorted(declined)
    elif choice in {item['decision'] for item in summary['options']}:
        return bulk_decide(state, group, choice)
    else:
        raise NameDecisionError('That decision is not an option for this group.')
    state.setdefault('group_choices', {})[group] = choice
    return {'count': 0}


def undo_batch(state, batch_id):
    """Restore the previous decision for members that still carry the target batch."""
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
    return count


def undo_auto(state, kind):
    """Remove automatic decisions of one group kind and decline their re-acceptance."""
    if not isinstance(kind, str) or kind not in {'auto', 'uncertain'}:
        raise NameDecisionError('Undo kind must be auto or uncertain.')
    by = 'auto:exact' if kind == 'auto' else 'auto:uncertain'
    declined = set(state.get('auto_declined') or [])
    count = 0
    for label, decision in list((state.get('decisions') or {}).items()):
        if decision.get('by') == by:
            state['decisions'].pop(label)
            declined.add(label)
            count += 1
    state['auto_declined'] = sorted(declined)
    return count


MAX_AUTHORSHIPS = 5
MAX_SOURCE_QUALIFIERS = 5
MAX_HINT_VALUES = 10
_OVERLONG_SOURCE_ID = "\0overlong"

def collect_records(tables, max_labels=MAX_LABELS, max_label_chars=MAX_LABEL_CHARS, max_context_chars=MAX_CONTEXT_CHARS):
    """Distinct labels with row counts and consistent classification context, most frequent first."""
    found = {}
    too_long = {'rows': 0, 'hashes': set()}
    for target, columns, rows in tables:
        if 'scientificName' not in columns:
            continue
        name_at = columns.index('scientificName')
        context_at = {rank: columns.index(rank) for rank in (*HINT_RANKS, 'taxonRank', 'scientificNameAuthorship') if rank in columns}
        for row in rows:
            label = normal(row[name_at], max_label_chars)
            if label is None or len(label) > max_label_chars:
                too_long['rows'] += 1
                too_long['hashes'].add(hash(row[name_at]))
                continue
            if not label:
                continue
            key = str(row[columns.index('reviewKey')]) if 'reviewKey' in columns else label
            record = found.setdefault(key, {'label': label, 'rows': 0, 'tables': {}, 'context': defaultdict(set),
                                              'context_counts': defaultdict(Counter), 'context_first': {},
                                              'source_ids': defaultdict(set), 'source_qualifiers': set(),
                                              'source_qualifier_rows': 0, 'source_qualifier_counts': Counter(),
                                              'source_qualifiers_truncated': False,
                                              'has_qualifier_column': False})
            if key != label:
                record['key'] = key
            record['rows'] += 1
            record['tables'][target] = record['tables'].get(target, 0) + 1
            for rank, index in context_at.items():
                hint = normal(row[index], max_context_chars)
                if hint and len(hint) <= max_context_chars:
                    if rank in HINT_RANKS:
                        key = _hint_normal(hint)
                        if key and key not in _PLACEHOLDER_HINTS:
                            record['context_counts'][rank][key] += 1
                            record['context_first'].setdefault((rank, key), hint)
                            record['context'][rank].add(hint)
                    elif rank == 'taxonRank':
                        key = hint.casefold()
                        record['context_counts'][rank][key] += 1
                        record['context_first'].setdefault((rank, key), hint)
                        record['context'][rank].add(key)
                    else:
                        record['context'][rank].add(hint)
                elif rank == 'scientificNameAuthorship' and not taxon_matching._is_blank(row[index]):
                    # Too long to keep, but supplied: it must never count as "no authorship" when COL's is compared.
                    record['authorship_overlong'] = True
            for field in ('scientificNameID', 'taxonID'):
                term = field
                if term in columns:
                    raw = row[columns.index(term)]
                    value = normal(raw, 300)
                    if len(record['source_ids'][field]) < 2:
                        if value and len(value) <= 300:
                            record['source_ids'][field].add(value)
                        elif (value and len(value) > 300) or (value is None and not taxon_matching._is_blank(raw)):
                            record['source_ids'][field].add(_OVERLONG_SOURCE_ID)
            qualifier_term = 'identificationQualifier'
            if qualifier_term in columns:
                record['has_qualifier_column'] = True
                raw = row[columns.index(qualifier_term)]
                value = normal(raw, max_context_chars)
                if value and len(value) <= max_context_chars:
                    record['source_qualifier_rows'] += 1
                    qualifier = _normal_qualifier(value)
                    if qualifier in record['source_qualifiers']:
                        record['source_qualifier_counts'][qualifier] += 1
                    elif len(record['source_qualifiers'] - {'\0overlong'}) < MAX_SOURCE_QUALIFIERS:
                        record['source_qualifiers'].add(qualifier)
                        record['source_qualifier_counts'][qualifier] += 1
                    else:
                        record['source_qualifiers_truncated'] = True
                elif not taxon_matching._is_blank(raw):
                    record['source_qualifier_rows'] += 1
                    record['source_qualifiers'].add('\0overlong')
                    record['source_qualifier_counts']['\0overlong'] += 1
    ordered = sorted(found.values(), key=lambda record: (-record['rows'], record['label']))
    labels = []
    for record in ordered[:max_labels]:
        # Context is a matching hint only when the label's rows agree on it.
        context = {rank: next(iter(values)) for rank, values in record['context'].items() if len(values) == 1}
        # Classification hints that differ only in case or a parenthesised remark ("Animalia"/"animalia") agree.
        for rank in HINT_RANKS:
            values = record['context'].get(rank) or ()
            if len(values) > 1 and len({_hint_normal(value) for value in values}) == 1:
                context[rank] = sorted(values)[0]
        ranks = {value.casefold() for value in record['context'].get('taxonRank') or ()}
        if len(ranks) == 1:
            # "Genus" and "genus" are one supplied rank.
            context['taxonRank'] = ranks.pop()
        qualifier = split_qualifier(record['label'])[1]
        # Supplied authorships are kept for automatic defaults and explicit replacement counts.
        authorships = sorted(record['context'].get('scientificNameAuthorship', ()))
        hint_values = {}
        for rank in ('kingdom', 'phylum', 'class', 'order', 'family'):
            counts = record['context_counts'].get(rank, {})
            if len(counts) > 1:
                hint_values[rank] = [[record['context_first'][(rank, key)], count]
                                     for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_HINT_VALUES]]
        mixed_hints = ['kingdom'] if 'kingdom' in hint_values else []
        # A uninomial's rows giving two ranks at genus or above ("Anura" order and genus) may be two taxa; a "species"
        # beside "Larus sp." says nothing about the uninomial and is ignored.
        high_ranks = {value.casefold() for value in record['context'].get('taxonRank', ())
                      if value.casefold() in RANK_ORDER and RANK_ORDER.index(value.casefold()) <= RANK_ORDER.index('genus')}
        if len(high_ranks) > 1 and len(name_parts(split_qualifier(record['label'])[0])) == 1:
            mixed_hints.append('rank')
        rank_counts = Counter({key: count for key, count in record['context_counts'].get('taxonRank', {}).items()
                               if key in high_ranks})
        source_ids = {}
        for field, values in record['source_ids'].items():
            if len(values) == 1:
                value = next(iter(values))
                if re.match(r'^(urn:lsid:|https?://)', value, re.IGNORECASE):
                    source_ids[field] = value
        item = {'label': record['label'], 'rows': record['rows'], 'tables': record['tables'],
                'hints': {rank: value for rank, value in context.items() if rank in HINT_RANKS},
                # Rows of one label that disagree on these ranks may be different taxa with the same name.
                'mixed_hints': mixed_hints,
                'hint_values': hint_values,
                'rank_values': [[value, count] for value, count in rank_counts.most_common(3)] if 'rank' in mixed_hints else [],
                # Every rank the label's rows give, when they give more than one (for the counts a group choice shows).
                'row_ranks': sorted({key.casefold() for key in record['context_counts'].get('taxonRank', {})})[:20]
                             if len(record['context_counts'].get('taxonRank', {})) > 1 else [],
                'source_rank': context.get('taxonRank', '').lower() or None, 'qualifier': qualifier,
                'source_authorships': authorships[:MAX_AUTHORSHIPS],
                'authorships_truncated': len(authorships) > MAX_AUTHORSHIPS or bool(record.get('authorship_overlong')),
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
        if 'key' in record:
            item['key'] = record['key']
        labels.append(item)
    return {'status': 'pending' if labels else 'none', 'error': '', 'runs': 0,
            'truncated': max(len(ordered) - max_labels, 0),
            'skipped_long': {'labels': len(too_long['hashes']), 'rows': too_long['rows']}, 'col_release': {}, 'labels': labels, 'decisions': {}}


def state_section(state, offset=0, limit=PAGE_SIZE, view='all', group=None, q=''):
    """Bounded name-review state: summary counts and one page of labels.

    A label whose saved decision is unconfirmed (see `unconfirmed`) counts as undecided and is listed as pending.
    """
    labels = state.get('labels', [])
    decisions = state.get('decisions', {})
    held = unconfirmed(state)
    statuses = Counter((record.get('match') or {}).get('status') for record in labels if checked(record))
    classified = {record_key(record): classify(record) for record in labels}
    group_summaries = groups(state, classified)
    summary = {'labels': len(labels), 'rows': sum(record['rows'] for record in labels), 'checked': sum(1 for record in labels if checked(record)),
               'decided': len(decisions) - len(held), 'unconfirmed': len(held), 'truncated': state.get('truncated', 0),
               'skipped_long': state.get('skipped_long') or {'labels': 0, 'rows': 0}, 'max_label_chars': MAX_LABEL_CHARS, 'match_status': dict(statuses),
               'groups': group_summaries, 'unchecked': sum(1 for record in labels if not checked(record)),
               'auto_declined': len(state.get('auto_declined') or []),
               'last_batch': ({key: value for key, value in state['batches'][-1].items() if key != 'changes'} if state.get('batches') else None),
               # Every bulk decision that can still be undone, oldest first.
               'batches': [{key: value for key, value in batch.items() if key != 'changes'} for batch in state.get('batches') or []]}
    summary['classification_notice'] = classification_notice(labels)
    shown = [record for record in labels if (view != 'pending' or record_key(record) not in decisions or record_key(record) in held)
             and (view != 'reviewed' or (record_key(record) in decisions and record_key(record) not in held))
             and (view != 'group' or classified[record_key(record)]['group'] == group)
             and (not q or q.casefold() in record['label'].casefold())]
    try:
        offset = max(int(offset), 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = min(max(int(limit), 1), MAX_PAGE_SIZE)
    except (TypeError, ValueError):
        limit = PAGE_SIZE
    return {'status': state.get('status', 'none'), 'error': state.get('error', ''), 'runs': state.get('runs', 0),
            'plan_id': state.get('plan_id'), 'checklist': (state.get('col_release') or {}).get('alias'), 'summary': summary,
            # Name decisions kept from the previous check when the archive was inspected again.
            'carried': state.get('carried'),
            'page': {'offset': offset, 'limit': limit, 'total': len(shown), 'view': view, 'group': group},
            'labels': [_entry(record, decisions, held, classified[record_key(record)], match_release(record, state))
                       for record in shown[offset:offset + limit]]}
