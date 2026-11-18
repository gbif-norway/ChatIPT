"""Scientific-name checks for archive conversion.

After inspection a `names` job collects the distinct source name labels of the Occurrence and
Identification tables, parses each with GBIF's name parser and matches it against Catalogue of Life XR
through the publication workflow's matcher (api.taxon_matching). Results and the user's decisions live in
`DwcConversion.name_review`. Nothing here is required for conversion: network failures only record a status,
and `convert()` itself never calls a service.

Policy: exact same-name matches and resolvable uncertain stems may be accepted automatically. Automatic decisions never write
a coarser or different taxon; an explicit group choice can. `verbatimIdentification` is unchanged.
Reviewed decisions are applied to the converted frames by `apply_name_decisions`, a pure function.
"""
import logging
import time
from collections import Counter, defaultdict

from django.conf import settings
from django.utils import timezone

from api import taxon_matching, name_review
from api.taxon_matching import (
    GBIF_PARSER_URL, NAME_RULES_VERSION, TaxonServiceError, col_release, compact_match,
    match_col, parse_names, row_value, split_qualifier,
    summarize_parse,
)

from api.name_review import (
    normal, checked, asserted_name, change,
    replacement, same_name, authorship_agrees, unconfirmed,
    qualifier_kind, formula_qualifier, doubt_formula, stem_usage,
    _hint_normal, _homonyms, _reason_for_unconfirmed, row_default,
    eligible, classify, groups, build_decision,
    auto_accept, col_choices, _entry, classification_notice,
    GROUP_OPTIONS, NameDecisionError,
    MAX_LABEL_CHARS, MAX_CONTEXT_CHARS, MAX_LABELS, PAGE_SIZE, MAX_PAGE_SIZE, MAX_SOURCE_QUALIFIERS,
)

logger = logging.getLogger(__name__)

DWC = 'http://rs.tdwg.org/dwc/terms/'
NAME = DWC + 'scientificName'
# Source tables whose names are reviewed, and the converted tables the decisions are applied to.
SOURCE_TABLES = {DWC + 'Occurrence': 'occurrence', DWC + 'Identification': 'identification'}
OUTPUT_TABLES = ('occurrence', 'identification')
CHUNK = 100  # labels matched, parsed and saved together
MAX_RUNS = 30
LEASE_SECONDS = 900  # a names run is bounded by its budget, so a silent worker is reclaimed sooner than a review
STATUSES = ('none', 'pending', 'running', 'incomplete', 'complete', 'error')
REVIEW_STATUSES = {'review', 'reviewing'}


def enabled():
    return bool(getattr(settings, 'CONVERSION_NAME_CHECKS_ENABLED', False))


def budget_seconds():
    return float(getattr(settings, 'CONVERSION_NAME_BUDGET_SECONDS', taxon_matching.RUN_BUDGET_SECONDS))


# Collection ------------------------------------------------------------------------------------

def collect_state(archive, plan):
    tables = ((SOURCE_TABLES[table.row_type], [term.removeprefix(DWC) for term in table.terms], table.rows)
              for table in archive.tables if table.row_type in SOURCE_TABLES)
    state = {'plan_id': plan['id'], **name_review.collect_records(tables, MAX_LABELS, MAX_LABEL_CHARS, MAX_CONTEXT_CHARS)}
    for record in state['labels']:
        record['preview_tables'] = list(OUTPUT_TABLES)
    return state


def carry_decisions(conversion, fresh, plan):
    """A new inspection's name state with the user's own name decisions carried over from the previous plan.

    A re-inspection (a new rule version, or the user asking) would otherwise drop every name decision. They are
    carried by label when the source is unchanged (same fingerprint) or yields exactly the same labels, and are checked
    again under the current rules: the stamp of the check they passed is dropped, so a COL name that now counts as a
    coarser or different taxon is held until confirmed (an explicit earlier confirmation stands). Bulk decisions are
    not carried (the groups offer them again) and automatic ones are made again from the fresh matches; declined
    automatic acceptances stay declined. User decisions that cannot be carried are counted as `dropped`. Name results
    are not carried either: the names are checked again.
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
                                                   for key in ('hints', 'source_rank', 'source_authorships', 'authorships_truncated', 'qualifier',
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

    The caller holds the conversion lock. When names lose their decision again (Undo all of the automatic acceptances),
    an answer this function recorded itself is withdrawn, so the question is asked again rather than silently leaving
    those names without a scientificName; an answer the user or the AI gave stands.
    """
    from api.conversion_review import apply_decision_changes, latest_sources
    if not every_name_decided(conversion):
        sources = latest_sources(conversion)
        settled = [identifier for identifier in name_question_ids(conversion)
                   if conversion.decisions.get(identifier) == 'preserve' and sources.get(identifier) == 'system']
        if settled:
            apply_decision_changes(conversion, dict.fromkeys(settled), 'system',
                                   rationale='Some scientific names have no decision again, so this fallback applies to rows again.')
        return []
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


# Parsing and matching ----------------------------------------------------------------------------


def check_chunk(items, deadline):
    return taxon_matching.check_name_chunk(items, deadline, matcher=match_col, parser=parse_names)


def _query(record):
    return {**record.get('hints', {}), 'scientificName': split_qualifier(record['label'])[0]}


def _is_budget(exc):
    return 'time budget' in str(exc)


# Decisions ---------------------------------------------------------------------------------------

def set_decisions(conversion, changes):
    state = current(conversion)
    result = name_review.set_decisions(state, changes)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return result


def bulk_decide(conversion, group, decision):
    state = current(conversion)
    result = name_review.bulk_decide(state, group, decision)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return result


def group_choice(conversion, group, choice):
    state = current(conversion)
    result = name_review.group_choice(state, group, choice)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return result


def undo_batch(conversion, batch_id):
    state = current(conversion)
    result = name_review.undo_batch(state, batch_id)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return result


def undo_auto(conversion, kind):
    state = current(conversion)
    result = name_review.undo_auto(state, kind)
    conversion.name_review = state
    conversion.save(update_fields=['name_review', 'updated_at'])
    return result


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


def apply_name_decisions(frames, review_state, source_names):
    """Overlay reviewed names on converted frames; returns (new frames, report section or None).

    Rows are found by their own source scientificName text (`source_names`: per output table, one value per frame row,
    from `row_source_names`; never borrowed from a linked row). verbatimIdentification is never read or changed.
    Whenever a decision writes scientificName it also writes a taxonRank and authorship that belong to that name, from the
    source row, so an occurrence and the identification made from it agree:
    - a parsed split writes its rank; the supplied authorship is kept (a differing one is counted), the parsed one fills a blank;
    - a COL name writes COL's rank and authorship; the supplied ones survive only where COL has none for the same name;
    - "don't publish a name" clears all three; "keep" only fills a blank scientificName with the supplied text, keeping
      the supplied authorship and rank;
    - a corrected name, typed by the user, writes that name with the row's supplied authorship and rank.
    These rules are `taxon_matching.written_fields`, which the publication workflow's review applies too.
    Replaced ranks are counted per entry. A COL decision that replaces the asserted name with a coarser or different taxon
    and was never explicitly confirmed (decided before that check existed) is applied as "keep": the user's own name is
    published, never an empty one. Unreviewed labels leave the converter's output as it is.
    """
    from api.dwc_dp_specs import get_table_spec
    state = review_state or {}
    if not state.get('labels'):
        skipped = state.get('skipped_long') or {}
        # The report still says when every name was too long to check.
        return frames, ({'status': state.get('status'), 'labels': 0, 'checked': 0, 'reviewed': 0, 'unreviewed': 0,
                         'skipped_long': skipped, 'decision_counts': {}, 'names': []} if skipped.get('labels') else None)
    records = {record['label']: record for record in state['labels']}
    decisions = {}
    for label, decision in (state.get('decisions') or {}).items():
        record = records.get(label, {})
        decisions[label] = name_review.with_checklist_release(
            decision, name_review.match_release(record, state), record.get('match'),
        )
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
        formulas = frame['taxonFormula'].tolist() if 'taxonFormula' in frame else None

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
            decided_authorship = decision.get('scientificNameAuthorship') or ''
            record = records.get(label) or {'label': label}
            outcomes = name_review.decision_fields(record, decision, fields)
            formula_written = 0
            for position in rows:
                text, supplied_authorship, supplied_rank, row_qualifier = _source(verbatim[position])
                if supplied_authorship is None and authorship is not None:
                    supplied_authorship = authorship[position]
                if supplied_rank is None and rank is not None:
                    supplied_rank = rank[position]
                supplied_authorship, supplied_rank = normal(supplied_authorship), normal(supplied_rank)
                # The row's own values are its supplied authorship and rank, and the name the converter wrote; a kept
                # name fills a blank scientificName with the row's own text as written.
                new_name = row_value(outcomes['scientificName'], names[position] if names is not None else '', str(text))
                new_authorship = row_value(outcomes['scientificNameAuthorship'], supplied_authorship, str(text)) or ''
                new_rank = row_value(outcomes['taxonRank'], supplied_rank, str(text)) or ''
                if kind == 'parsed' and supplied_authorship and decided_authorship and supplied_authorship != normal(decided_authorship):
                    entry['authorshipKept'] += 1
                if kind in {'col', 'alternative', 'stem'} and supplied_authorship and supplied_authorship != normal(new_authorship):
                    entry['authorshipReplaced'] += 1
                if names is not None:
                    names[position] = new_name
                for field, outcome in name_review.decision_fields(record, decision, fields, row_qualifier).items():
                    if field in taxon_matching.NAME_FIELDS:
                        continue
                    if field == 'taxonID' and formulas is not None and normal(formulas[position]).casefold() not in {'', 'a'}:
                        continue
                    values = column(field)
                    current = values[position]
                    value = row_value(outcome, current, str(text))
                    if not taxon_matching._same_cell(current, value):
                        values[position] = value
                        if field == 'taxonFormula':
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
            if field in frame.columns or any(not taxon_matching._is_blank(value) for value in values):
                changed[field] = values
        result[table] = changed
    section = {
        'status': state.get('status'), 'error': state.get('error') or None, 'checklist': state.get('col_release') or None,
        'labels': len(state['labels']), 'checked': sum(1 for record in state['labels'] if checked(record)),
        'not_reviewed_limit': state.get('truncated', 0), 'skipped_long': state.get('skipped_long'), 'reviewed': len(decisions), 'unreviewed': len(state['labels']) - len(decisions),
        'decision_counts': dict(Counter(decision['decision'] for decision in decisions.values())),
        'ranks_replaced': sum(sum(entry['ranksReplaced'].values()) for entry in entries.values()),
        'unconfirmed_kept': len(held),
        'policy': 'Decisions apply to rows by their own source scientificName text; verbatimIdentification is never changed. '
                  'A reviewed COL usage with accepted status and an unqualified identification can fill a blank DwC-DP taxonID with a release-specific link. '
                  'A decided name is written with its own taxonRank and authorship on occurrence and identification rows alike; replaced ranks are counted. '
                  'A COL name coarser than or different from the supplied name is applied only when the user confirmed it; '
                  'an unconfirmed one keeps the supplied name. '
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


def state_section(conversion, offset=0, limit=PAGE_SIZE, view='all', group=None, q=''):
    from api.models import DwcConversionJob
    section = name_review.state_section(current(conversion), offset, limit, view, group, q)
    section.update(checking=DwcConversionJob.objects.filter(conversion=conversion, action='names').exists(),
                   question_ids=name_question_ids(conversion))
    return section


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
        previous_release = state.get('col_release') or {}
    deadline = time.monotonic() + budget_seconds()
    error = None
    release = {}
    try:
        if any(not item['match'] for item in pending):
            release = col_release(deadline)
    except TaxonServiceError as exc:
        if not previous_release:
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
                if 'match' in parts:
                    index[label]['col_release'] = release
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
