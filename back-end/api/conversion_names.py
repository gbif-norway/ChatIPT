"""Scientific-name checks for archive conversion.

After inspection a `names` job collects the distinct source name labels of the Occurrence and
Identification tables, parses each with GBIF's name parser and matches it against Catalogue of Life XR
through the publication workflow's matcher (api.taxon_matching). Results and the user's decisions live in
`DwcConversion.name_review`. Nothing here is required for conversion: network failures only record a status,
and `convert()` itself never calls a service.

Policy: a suggestion is never written without a user decision. `verbatimIdentification` keeps the source
text and is never changed. Only names are published; COL usage ids are provenance in the report, never taxonID.
Reviewed decisions are applied to the converted frames by `apply_name_decisions`, a pure function.
"""
import logging
import time
from collections import Counter, defaultdict

from django.conf import settings
from django.utils import timezone

from api import taxon_matching
from api.taxon_matching import HINT_RANKS, TaxonServiceError, col_release, match_col, split_qualifier

logger = logging.getLogger(__name__)

DWC = 'http://rs.tdwg.org/dwc/terms/'
NAME = DWC + 'scientificName'
GBIF_PARSER_URL = 'https://api.gbif.org/v1/parser/name'
# Source tables whose names are reviewed, and the converted tables the decisions are applied to.
SOURCE_TABLES = {DWC + 'Occurrence': 'occurrence', DWC + 'Identification': 'identification'}
OUTPUT_TABLES = ('occurrence', 'identification')
MAX_LABELS = 5000  # most frequent first; the rest keep the converter's output
CHUNK = 100  # labels matched, parsed and saved together
MAX_RUNS = 30
LEASE_SECONDS = 900  # a names run is bounded by its budget, so a silent worker is reclaimed sooner than a review
PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
DECISIONS = ('parsed', 'col', 'alternative', 'keep', 'empty')
STATUSES = ('none', 'pending', 'running', 'incomplete', 'complete', 'error')
REVIEW_STATUSES = {'review', 'reviewing'}
# GBIF's parser reports a rank marker rather than a rank; only these are mapped to a DwC taxonRank.
RANK_MARKERS = {'sp.': 'species', 'subsp.': 'subspecies', 'var.': 'variety', 'f.': 'form',
                'subvar.': 'subvariety', 'subf.': 'subform', 'agg.': 'species aggregate'}


class NameDecisionError(ValueError):
    pass


def enabled():
    return bool(getattr(settings, 'CONVERSION_NAME_CHECKS_ENABLED', False))


def budget_seconds():
    return float(getattr(settings, 'CONVERSION_NAME_BUDGET_SECONDS', taxon_matching.RUN_BUDGET_SECONDS))


def normal(value):
    return ' '.join(str(value if value is not None else '').split())


# Collection ------------------------------------------------------------------------------------

def collect_state(archive, plan):
    """Distinct labels with row counts and consistent classification context, most frequent first."""
    found = {}
    for table in archive.tables:
        target = SOURCE_TABLES.get(table.row_type)
        if target is None or NAME not in table.terms:
            continue
        name_at = table.terms.index(NAME)
        context_at = {rank: table.terms.index(DWC + rank) for rank in (*HINT_RANKS, 'taxonRank') if DWC + rank in table.terms}
        for row in table.rows:
            label = normal(row[name_at])
            if not label:
                continue
            record = found.setdefault(label, {'label': label, 'rows': 0, 'tables': {}, 'context': defaultdict(set)})
            record['rows'] += 1
            record['tables'][target] = record['tables'].get(target, 0) + 1
            for rank, index in context_at.items():
                if normal(row[index]):
                    record['context'][rank].add(normal(row[index]))
    ordered = sorted(found.values(), key=lambda record: (-record['rows'], record['label']))
    labels = []
    for record in ordered[:MAX_LABELS]:
        # Context is a matching hint only when the label's rows agree on it.
        context = {rank: next(iter(values)) for rank, values in record['context'].items() if len(values) == 1}
        qualifier = split_qualifier(record['label'])[1]
        labels.append({'label': record['label'], 'rows': record['rows'], 'tables': record['tables'],
                       'hints': {rank: value for rank, value in context.items() if rank in HINT_RANKS},
                       'source_rank': context.get('taxonRank', '').lower() or None, 'qualifier': qualifier})
    return {'plan_id': plan['id'], 'status': 'pending' if labels else 'none', 'error': '', 'runs': 0,
            'truncated': max(len(ordered) - MAX_LABELS, 0), 'col_release': {}, 'labels': labels, 'decisions': {}}


def current(conversion):
    """The stored name review when it belongs to the conversion's current plan."""
    state = conversion.name_review or {}
    plan_id = (conversion.plan or {}).get('id')
    return state if plan_id and state.get('plan_id') == plan_id else {}


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
    return {key: usage.get(key) for key in ('id', 'scientificName', 'scientificNameAuthorship', 'taxonRank', 'status')}


def compact_match(summary):
    return {'matchType': summary.get('matchType'), 'status': summary.get('status'), 'confidence': summary.get('confidence'),
            'usage': compact_usage(summary.get('usage')), 'acceptedUsage': compact_usage(summary.get('acceptedUsage')),
            'alternatives': [{**compact_usage(alternative), 'matchType': alternative.get('matchType')}
                             for alternative in (summary.get('alternatives') or [])[:3]],
            'hintOnly': bool(summary.get('hintOnly'))}


def _query(record):
    return {**record.get('hints', {}), 'scientificName': split_qualifier(record['label'])[0]}


def _is_budget(exc):
    return 'time budget' in str(exc)


# Decisions ---------------------------------------------------------------------------------------

def _checklist(state):
    release = state.get('col_release') or {}
    return {'checklistKey': release.get('checklistKey') or taxon_matching.col_checklist_key(), 'alias': release.get('alias')}


def build_decision(record, spec, state, by='user'):
    """A self-contained snapshot of one decision: later re-checks cannot change what was approved."""
    if not isinstance(spec, dict) or spec.get('decision') not in DECISIONS:
        raise NameDecisionError(f'Choose one of: {", ".join(DECISIONS)}.')
    kind = spec['decision']
    snapshot = {'decision': kind, 'by': by, 'at': timezone.now().isoformat(), 'scientificName': None,
                'scientificNameAuthorship': None, 'taxonRank': None, 'source': 'verbatim' if kind == 'keep' else 'none'}
    parsed, match = record.get('parsed') or {}, record.get('match') or {}
    if kind == 'empty':
        snapshot['scientificName'] = ''
    elif kind == 'parsed':
        if not parsed.get('usable'):
            raise NameDecisionError(f'The name parser could not split "{record["label"]}".')
        snapshot.update(source='parser', scientificName=parsed['canonical'], scientificNameAuthorship=parsed.get('authorship'),
                        taxonRank=parsed.get('rank'))
    elif kind in {'col', 'alternative'}:
        if kind == 'col':
            usage = match.get('usage')
        else:
            usage = next((item for item in match.get('alternatives') or [] if item['id'] == spec.get('usage_id')), None)
        if not usage or not usage.get('scientificName'):
            raise NameDecisionError(f'There is no such Catalogue of Life name to accept for "{record["label"]}".')
        snapshot.update(source='col', scientificName=usage['scientificName'], scientificNameAuthorship=usage.get('scientificNameAuthorship'),
                        taxonRank=usage.get('taxonRank'), usageId=usage.get('id'), taxonomicStatus=usage.get('status'),
                        matchType=match.get('matchType') if kind == 'col' else usage.get('matchType'), checklist=_checklist(state))
    return snapshot


def set_decisions(conversion, changes):
    """Record decisions from {label: {decision, usage_id?} | None}; None withdraws a decision."""
    state = current(conversion)
    if not state.get('labels'):
        raise NameDecisionError('There are no names to review for this plan.')
    if not isinstance(changes, dict) or len(changes) > MAX_LABELS:
        raise NameDecisionError('Name decisions must map labels to decisions.')
    index = {record['label']: record for record in state['labels']}
    for label, spec in changes.items():
        if label not in index:
            raise NameDecisionError(f'"{str(label)[:80]}" is not a name in this archive.')
        if spec is None:
            state['decisions'].pop(label, None)
        else:
            state['decisions'][label] = build_decision(index[label], spec, state)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])


def bulk_acceptable(record, decisions):
    """Exact COL matches of names written as in the label, with no qualifier such as "sp." or "cf."."""
    match = record.get('match') or {}
    return (record['label'] not in decisions and match.get('matchType') == 'EXACT' and bool(match.get('usage'))
            and not match.get('hintOnly') and not record.get('qualifier'))


def parse_acceptable(record, decisions):
    """A split that rebuilds the supplied text exactly and separates an authorship."""
    parsed = record.get('parsed') or {}
    return record['label'] not in decisions and bool(parsed.get('lossless')) and bool(parsed.get('splits')) and not record.get('qualifier')


def bulk_decide(conversion, kind):
    state = current(conversion)
    if not state.get('labels'):
        raise NameDecisionError('There are no names to review for this plan.')
    rules = {'exact_col': ('col', bulk_acceptable), 'parsed': ('parsed', parse_acceptable)}
    if kind not in rules:
        raise NameDecisionError('Unknown bulk action.')
    decision, acceptable = rules[kind]
    count = 0
    for record in state.get('labels', []):
        if acceptable(record, state['decisions']):
            state['decisions'][record['label']] = build_decision(record, {'decision': decision}, state, by=f'bulk:{kind}')
            count += 1
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

def apply_name_decisions(frames, name_review):
    """Overlay reviewed names on converted frames; returns (new frames, report section or None).

    Rows are found by their own `verbatimIdentification` (never borrowed from a linked row), which is never changed. A decided name replaces
    scientificName; authorship and rank only fill blank cells (a differing supplied authorship is kept and
    counted), so supplied values are never overwritten. Unreviewed labels leave the converter's output as it is.
    """
    from api.dwc_dp_specs import get_table_spec
    state = name_review or {}
    if not state.get('labels'):
        return frames, None
    decisions = state.get('decisions') or {}
    entries = {label: {'label': label, 'decision': decision['decision'], 'source': decision.get('source'),
                       'scientificName': decision.get('scientificName'), 'scientificNameAuthorship': decision.get('scientificNameAuthorship'),
                       'taxonRank': decision.get('taxonRank'), 'colUsageId': decision.get('usageId'), 'checklist': decision.get('checklist'),
                       'matchType': decision.get('matchType'), 'decidedBy': decision.get('by'), 'decidedAt': decision.get('at'),
                       'rows': {}, 'authorshipKept': 0}
               for label, decision in decisions.items()}
    result = dict(frames)
    for table in OUTPUT_TABLES:
        frame = frames.get(table)
        # Each row is keyed on its own source name text only; a row without one is left untouched.
        if frame is None or not decisions or not len(frame) or 'verbatimIdentification' not in frame.columns:
            continue
        fields = set(get_table_spec(table).fields)
        verbatim = frame['verbatimIdentification'].tolist()
        positions = defaultdict(list)
        for position, value in enumerate(verbatim):
            if normal(value) in decisions:
                positions[normal(value)].append(position)
        if not positions:
            continue
        columns = {}

        def column(field):
            if field not in fields:
                return None
            if field not in columns:
                columns[field] = frame[field].tolist() if field in frame.columns else [''] * len(frame)
            return columns[field]

        for label, rows in positions.items():
            decision, entry = decisions[label], entries[label]
            kind = decision['decision']
            entry['rows'][table] = len(rows)
            names, authorship, rank = column('scientificName'), column('scientificNameAuthorship'), column('taxonRank')
            for position in rows:
                if names is not None:
                    if kind == 'keep':
                        # The supplied text fills a blank scientificName; a name the converter already wrote stands.
                        names[position] = names[position] if normal(names[position]) else str(verbatim[position])
                    else:
                        names[position] = decision['scientificName'] or ''
                supplied = decision.get('scientificNameAuthorship')
                if supplied and authorship is not None:
                    if not normal(authorship[position]):
                        authorship[position] = supplied
                    elif normal(authorship[position]) != normal(supplied):
                        entry['authorshipKept'] += 1
                if decision.get('taxonRank') and rank is not None and not normal(rank[position]):
                    rank[position] = decision['taxonRank']
        changed = frame.copy()
        for field, values in columns.items():
            changed[field] = values
        result[table] = changed
    section = {
        'status': state.get('status'), 'error': state.get('error') or None, 'checklist': state.get('col_release') or None,
        'labels': len(state['labels']), 'checked': sum(1 for record in state['labels'] if checked(record)),
        'not_reviewed_limit': state.get('truncated', 0), 'reviewed': len(decisions), 'unreviewed': len(state['labels']) - len(decisions),
        'decision_counts': dict(Counter(decision['decision'] for decision in decisions.values())),
        'policy': 'verbatimIdentification keeps the source text. Names only are published; COL usage ids are provenance, not taxonID. '
                  'Unreviewed labels are exactly as the converter produced them.',
        'entries': [entries[label] for label in sorted(entries)],
    }
    return result, section


def public_report(report):
    """The report as shown in state: the per-label entries stay in the downloadable report only."""
    section = report.get('name_review')
    if not isinstance(section, dict) or 'entries' not in section:
        return report
    return {**report, 'name_review': {key: value for key, value in section.items() if key != 'entries'}}


# State -------------------------------------------------------------------------------------------

def _entry(record, decisions):
    parsed, match = record.get('parsed'), record.get('match')
    usage = (match or {}).get('usage') or {}
    return {**record, 'checked': checked(record), 'decision': decisions.get(record['label']),
            'rank_mismatch': bool(record.get('source_rank') and usage.get('taxonRank') and record['source_rank'] != usage['taxonRank']),
            'offers': {'parsed': bool(parsed and parsed.get('usable')), 'col': bool(usage.get('scientificName'))},
            'bulk': {'col': bulk_acceptable(record, decisions), 'parsed': parse_acceptable(record, decisions)}}


def state_section(conversion, offset=0, limit=PAGE_SIZE, view='all'):
    """Bounded name-review state: summary counts and one page of labels."""
    state = current(conversion)
    labels = state.get('labels', [])
    decisions = state.get('decisions', {})
    statuses = Counter((record.get('match') or {}).get('status') for record in labels if checked(record))
    summary = {'labels': len(labels), 'rows': sum(record['rows'] for record in labels), 'checked': sum(1 for record in labels if checked(record)),
               'decided': len(decisions), 'truncated': state.get('truncated', 0), 'match_status': dict(statuses),
               'bulk_col': sum(1 for record in labels if bulk_acceptable(record, decisions)),
               'bulk_parsed': sum(1 for record in labels if parse_acceptable(record, decisions))}
    shown = [record for record in labels if view != 'pending' or record['label'] not in decisions]
    offset = max(int(offset), 0)
    limit = min(max(int(limit), 1), MAX_PAGE_SIZE)
    from api.models import DwcConversionJob
    checking = DwcConversionJob.objects.filter(conversion=conversion, action='names').exists()
    return {'status': state.get('status', 'none'), 'checking': checking, 'error': state.get('error', ''), 'runs': state.get('runs', 0),
            'plan_id': state.get('plan_id'), 'checklist': (state.get('col_release') or {}).get('alias'), 'summary': summary,
            'page': {'offset': offset, 'limit': limit, 'total': len(shown), 'view': view},
            'labels': [_entry(record, decisions) for record in shown[offset:offset + limit]]}


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
    from api.conversion_review import Fenced, fence
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
                    'match': 'match' in record, 'parse': 'parsed' in record}
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
        results = defaultdict(dict)
        try:
            need = [item for item in chunk if not item['match']]
            for item, summary in zip(need, match_col([item['query'] for item in need], deadline=deadline) if need else []):
                results[item['label']]['match'] = compact_match(summary)
        except TaxonServiceError as exc:
            error = exc
        try:
            need = [item for item in chunk if not item['parse']]
            # A qualified label ("cf.", "sp.") is not split: the parser reads qualifiers as ranks.
            plain = [item for item in need if not item['qualifier']]
            for item in need:
                if item['qualifier']:
                    results[item['label']]['parsed'] = {'type': None, 'usable': False, 'canonical': None, 'authorship': None, 'rank': None,
                                                       'lossless': False, 'splits': False, 'reason': 'qualifier'}
            for item, parsed in zip(plain, parse_names([item['label'] for item in plain], deadline=deadline)):
                results[item['label']]['parsed'] = parsed
        except TaxonServiceError as exc:
            error = error or exc
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
    with fence(conversion_id, job_id, claim, 'names', REVIEW_STATUSES) as (conversion, _):
        state = current(conversion)
        if not state or state['plan_id'] != plan_id:
            raise Fenced()
        if release and not state.get('col_release'):
            state['col_release'] = release
        if all(checked(record) for record in state['labels']):
            state.update(status='complete', error='')
        elif error is not None and not _is_budget(error):
            state.update(status='error', error=f'Name checks stopped: {error}'[:500])
        else:
            state.update(status='incomplete', error='')
        conversion.save(update_fields=['name_review', 'updated_at'])


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
