"""Which source column each published column came from (``GET /api/tables/{id}/lineage/``).

Two kinds of evidence, in this order:

* ``recorded``: the DwC-A conversion report (``DwcConversion.report['columns']``) names the
  source file, source term and output ``table.field`` of every converted column. It is used
  only while the conversion is complete, because the output tables are replaced in the same
  transaction that stores a complete report, and a later re-plan clears the report.
* ``matched``: the publication workflow records no column-level mapping (the agent writes
  tables with Python; its coverage report is free text). A published column is attributed to
  a source column when (nearly) all of its distinct values occur in that source column.

Sources for matching are the tabular uploads (re-read from the stored files, so they still
exist after the final step deletes the working tables). Each upload's distinct values are
cached per upload, so a table change does not re-read files. Values always come from the
upload; a live table with the sheet's name, row count and column labels only lends its
``table_id`` (otherwise ``table_id`` is null). Only a dataset without readable tabular uploads
falls back to its live non-package tables.

Matching is deliberately conservative: a wrong attribution is worse than none.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from api import column_stats
from api.dwc_dp_specs import RESERVED_TABLE_NAMES
from api.source_coverage import normalize_value

logger = logging.getLogger(__name__)

CACHE_VERSION = 2
CACHE_TIMEOUT_SECONDS = column_stats.CACHE_TIMEOUT_SECONDS
TAXONOMY_PREFIX = 'taxonomy-'

# A published column needs this many distinct values before a value match means anything.
MIN_DISTINCT = 5
# Share of the published column's distinct values that must occur in the source column.
COVERAGE = 0.95
# Candidates within this margin of the best (coverage and reverse coverage) are ties.
TIE_MARGIN = 0.01
# More equally good candidates than this means the values do not identify a source.
MAX_TIES = 3
# Small whole numbers (counts, codes, sequence keys) recur by chance in unrelated columns:
# such a column needs more distinct values and must use most of the source column's values.
COMMONPLACE = re.compile(r'^[+-]?\d{1,4}(?:\.0*)?$')
COMMONPLACE_SHARE = 0.8
COMMONPLACE_MIN_DISTINCT = 10
COMMONPLACE_REVERSE = 0.8

# Work caps for very large tables. Rows beyond MAX_ROWS are sampled evenly across the table.
MAX_ROWS = 300_000
SAMPLE_VALUES = 5_000
MAX_SOURCE_COLUMNS = 400
# Distinct values (exact plus normalised) one upload's index may hold, as 8-byte hashes
# (about 16 MB), and all uploads of a dataset together (about 32 MB). Columns past a budget
# are left out of matching, and no further uploads are read once the total is used up.
MAX_INDEX_VALUES = 2_000_000
MAX_TOTAL_INDEX_VALUES = 4_000_000
# Uploads larger than this are not re-read for lineage: the shared reader loads a whole workbook
# (every sheet) before any budget applies. Without readable uploads, the live tables are used.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Live tables with more cells than this are not unpickled for lineage.
MAX_LIVE_TABLE_CELLS = 20_000_000
UNREADABLE_RETRY_SECONDS = 10 * 60

_NUMBER = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$')
_MIDNIGHT = re.compile(r'^(\d{4}-\d{2}-\d{2}) 00:00(?::00(?:\.0+)?)?$')

NOTE_NORMALISED = 'values match after ignoring differences in upper/lower case, spacing or number format'


def is_published(title):
    return title in RESERVED_TABLE_NAMES or str(title or '').startswith(TAXONOMY_PREFIX)


def unavailable():
    return {'available': False, 'columns': {}}


# ---------------------------------------------------------------------------
# Recorded provenance (DwC-A conversion report)
# ---------------------------------------------------------------------------

def short_term(term):
    text = str(term or '').rstrip('/#')
    return re.split(r'[/#]', text)[-1] or text


def parse_target(text):
    """(table, field) from a report target such as ``occurrence.sex`` or
    ``derived verbatim copy → occurrence.verbatimIdentification``; None otherwise."""
    if not isinstance(text, str):
        return None
    derived = '→' in text
    if derived:
        text = text.rsplit('→', 1)[1]
    text = text.strip()
    table, dot, field = text.partition('.')
    if not dot or not table or not field or re.search(r'\s', table):
        return None
    return table, field, derived


def recorded_lineage(report, title, labels):
    """{column label: [entry]} for one output table from a complete conversion report."""
    labels = set(labels)
    columns = defaultdict(list)
    for item in (report or {}).get('columns', []) or []:
        if not isinstance(item, dict) or item.get('disposition') == 'retained-unmapped':
            continue
        targets = [item.get('target'), item.get('also_copied_to')]
        targets += [target for target, count in (item.get('target_counts') or {}).items() if count]
        for target in targets:
            parsed = parse_target(target)
            if not parsed:
                continue
            table, field, derived = parsed
            if table != title or field not in labels:
                continue
            entry = {
                'table_id': None,
                'table': str(item.get('source_table') or ''),
                'column': short_term(item.get('term')),
                'basis': 'recorded',
                'note': 'derived from this column' if derived or item.get('disposition') == 'derived' else '',
            }
            if entry not in columns[field]:
                columns[field].append(entry)
    return dict(columns)


# ---------------------------------------------------------------------------
# Value matching
# ---------------------------------------------------------------------------

@dataclass
class Source:
    """One source sheet or table.

    ``columns`` is [(label, exact hashes, normalised hashes)]: sorted unique uint64 arrays of the
    column's distinct cell texts and of their normalised keys. ``row_count`` is the sheet's rows;
    ``rows_sampled`` says only MAX_ROWS of them, spread across the sheet, were read.
    """
    table_id: int | None
    label: str
    columns: list
    row_count: int = 0
    rows_sampled: bool = False


def sample_rows(df):
    """(rows, sampled): the whole frame, or MAX_ROWS rows spread evenly across it."""
    if len(df.index) <= MAX_ROWS:
        return df, False
    positions = np.unique(np.linspace(0, len(df.index) - 1, MAX_ROWS).astype(np.int64))
    return df.iloc[positions], True


def value_hashes(values):
    """Sorted unique 64-bit hashes of texts (deterministic across processes)."""
    if len(values) == 0:
        return np.array([], dtype=np.uint64)
    return np.unique(pd.util.hash_array(np.asarray(list(values), dtype=object)))


def distinct_texts(series):
    """Distinct populated cell texts, as the rows API compares them."""
    return column_stats.value_counts(series).index.to_numpy(dtype=object)


def normal_keys(values):
    """Light normalisation: whitespace, case, ISO ``T``/midnight time, number spelling.

    Numbers go through ``source_coverage.normalize_value`` so ``5``, ``5.0`` and ``5.00`` agree.
    """
    if len(values) == 0:
        return np.array([], dtype=object)
    texts = pd.Series(values, dtype='string').str.replace(r'\s+', ' ', regex=True).str.strip()
    keys = (
        texts.str.replace(r'(\d)T(\d)', r'\1 \2', regex=True)
        .str.replace(_MIDNIGHT.pattern, r'\1', regex=True)
        .str.casefold()
    )
    numeric = texts.str.fullmatch(_NUMBER.pattern).fillna(False).to_numpy(dtype=bool)
    if numeric.any():
        keys = keys.astype(object)
        keys[numeric] = [normalize_value(text) for text in texts[numeric]]
    return keys.fillna('').to_numpy(dtype=object)


def sample(values):
    """At most SAMPLE_VALUES distinct values, spread evenly over the sorted values."""
    if len(values) <= SAMPLE_VALUES:
        return list(values), False
    ordered = sorted(values)
    picks = np.linspace(0, len(ordered) - 1, SAMPLE_VALUES).astype(int)
    return [ordered[i] for i in picks], True


@dataclass
class PublishedColumn:
    exact: set
    normal: set
    distinct: int
    sampled: bool
    commonplace: bool


def _published_columns(df, labels):
    columns = {}
    df, _ = sample_rows(df)
    for position, label in enumerate(labels):
        values = distinct_texts(df.iloc[:, position])
        if len(values) < MIN_DISTINCT:
            continue
        picked, sampled = sample(values)
        normal = {key for key in normal_keys(picked) if key}
        if len(normal) < MIN_DISTINCT:
            continue
        commonplace = sum(bool(COMMONPLACE.match(value)) for value in picked) >= COMMONPLACE_SHARE * len(picked)
        columns[label] = PublishedColumn(set(picked), normal, len(values), sampled, commonplace)
    return columns


def _candidates(column, exact_hits, normal_hits, source_distinct):
    candidates = []
    for cid in set(exact_hits) | set(normal_hits):
        exact_rate = exact_hits.get(cid, 0) / len(column.exact)
        normal_rate = normal_hits.get(cid, 0) / len(column.normal)
        if exact_rate >= COVERAGE:
            exact, rate, hits, size, distinct = True, exact_rate, exact_hits[cid], len(column.exact), source_distinct[cid][0]
        elif normal_rate >= COVERAGE:
            exact, rate, hits, size, distinct = False, normal_rate, normal_hits[cid], len(column.normal), source_distinct[cid][1]
        else:
            continue
        # Share of the source column's distinct values that the published column uses
        # (scaled up when only a sample of the published values was compared).
        reverse = min(1.0, rate * column.distinct / max(distinct, 1)) if column.sampled else min(1.0, hits / max(distinct, 1))
        if column.commonplace and (column.distinct < COMMONPLACE_MIN_DISTINCT or reverse < COMMONPLACE_REVERSE):
            continue
        candidates.append({'cid': cid, 'exact': exact, 'rate': rate, 'reverse': reverse, 'hits': hits, 'size': size})
    candidates.sort(key=lambda c: (not c['exact'], -c['rate'], -c['reverse'], c['cid']))
    if not candidates:
        return []
    best = candidates[0]
    tied = [
        c for c in candidates
        if c['exact'] == best['exact']
        and best['rate'] - c['rate'] <= TIE_MARGIN
        and best['reverse'] - c['reverse'] <= TIE_MARGIN
    ]
    # Too many equally good sources: the values do not say where the column came from.
    return [] if len(tied) > MAX_TIES else tied


def _note(candidate, sampled, published_rows=None, source=None):
    parts = []
    if not candidate['exact']:
        parts.append(NOTE_NORMALISED)
    if candidate['hits'] < candidate['size']:
        parts.append(f"{candidate['hits']} of {candidate['size']} distinct values found")
    if sampled:
        parts.append(f'checked on {SAMPLE_VALUES:,} of the distinct values')
    if published_rows and published_rows > MAX_ROWS:
        parts.append(f'checked on {MAX_ROWS:,} of the {published_rows:,} rows, spread across the table')
    if source is not None and source.rows_sampled:
        parts.append(f'source checked on {MAX_ROWS:,} of its {source.row_count:,} rows')
    return '; '.join(parts)


def _hash_lookup(texts):
    """(sorted hashes, texts in the same order) for a set of wanted texts."""
    ordered = sorted(texts)
    if not ordered:
        return np.array([], dtype=np.uint64), []
    hashes = pd.util.hash_array(np.asarray(ordered, dtype=object))
    return hashes, ordered


def matched_lineage(df, labels, sources):
    """{column label: [entry]} inferred from distinct-value containment."""
    published = _published_columns(df, labels)
    if not published or not sources:
        return {}
    exact_hashes, exact_texts = _hash_lookup(set().union(*(column.exact for column in published.values())))
    normal_hashes, normal_texts = _hash_lookup(set().union(*(column.normal for column in published.values())))

    index_exact, index_normal = defaultdict(list), defaultdict(list)
    source_columns, source_distinct = [], []
    for source in sources:
        for column_label, values, keys in source.columns:
            if len(source_columns) >= MAX_SOURCE_COLUMNS:
                break
            cid = len(source_columns)
            source_columns.append((source, column_label))
            source_distinct.append((len(values), len(keys)))
            for position in np.flatnonzero(np.isin(exact_hashes, values, assume_unique=False)):
                index_exact[exact_texts[position]].append(cid)
            for position in np.flatnonzero(np.isin(normal_hashes, keys, assume_unique=False)):
                index_normal[normal_texts[position]].append(cid)

    result = {}
    for label, column in published.items():
        exact_hits, normal_hits = Counter(), Counter()
        for value in column.exact:
            exact_hits.update(index_exact.get(value, ()))
        for key in column.normal:
            normal_hits.update(index_normal.get(key, ()))
        entries = []
        for candidate in _candidates(column, exact_hits, normal_hits, source_distinct):
            source, column_label = source_columns[candidate['cid']]
            entries.append({
                'table_id': source.table_id,
                'table': source.label,
                'column': column_label,
                'basis': 'matched',
                'note': _note(candidate, column.sampled, len(df.index), source),
            })
        if entries:
            result[label] = entries
    return result


def _display_columns(raw_columns):
    from api.models import Table

    return Table.display_columns(raw_columns)


# ---------------------------------------------------------------------------
# Source indexes (cached per upload / per live table) and payload
# ---------------------------------------------------------------------------

def sheet_index(df, budget=None):
    """Hashed distinct texts and normalised keys per column of one sheet, within the work caps.

    ``budget`` is a dict {'columns': n, 'values': n} shared by the sheets of one upload, so the
    caps hold for the whole file; columns past either cap are left out.
    """
    budget = budget if budget is not None else {'columns': MAX_SOURCE_COLUMNS, 'values': MAX_INDEX_VALUES}
    labels = _display_columns(df.columns)
    rows, rows_sampled = sample_rows(df)
    columns = []
    for position, label in enumerate(labels):
        if budget['columns'] <= 0 or budget['values'] <= 0:
            break
        values = distinct_texts(rows.iloc[:, position])
        if len(values) == 0:
            continue
        exact = value_hashes(values)
        keys = value_hashes(set(normal_keys(values)) - {''})
        if len(exact) + len(keys) > budget['values']:
            budget['values'] = 0
            break
        budget['columns'] -= 1
        budget['values'] -= len(exact) + len(keys)
        columns.append((label, exact, keys))
    return {'labels': labels, 'row_count': int(len(df.index)), 'rows_sampled': rows_sampled, 'columns': columns}


def source_from_df(table_id, label, df):
    index = sheet_index(df)
    return Source(table_id, label, index['columns'], index['row_count'], index['rows_sampled'])


def _digest(*parts):
    return hashlib.sha256('|'.join(str(part) for part in parts).encode()).hexdigest()[:32]


def upload_index(user_file):
    """{'sheets': [{sheet, labels, row_count, columns}]} for a tabular upload, or None when unreadable.

    Cached by upload id, stored file name and upload time (an upload's file never changes in
    place), so a table change never re-reads the file.
    """
    from django.core.cache import cache

    key = f'table-lineage-upload:v{CACHE_VERSION}:{user_file.pk}:{_digest(user_file.file.name, user_file.uploaded_at.isoformat())}'
    index = cache.get(key)
    if index is not None:
        return index.get('sheets') if 'sheets' in index else None
    # Concurrent cold requests in this process read the file once.
    with column_stats.single_flight(key):
        index = cache.get(key)
        if index is not None:
            return index.get('sheets') if 'sheets' in index else None
        try:
            size = user_file.file.size
        except Exception:  # missing file: the read below reports it
            size = None
        if size is not None and size > MAX_UPLOAD_BYTES:
            logger.info('Lineage skips upload %s: %s bytes is over the %s byte limit', user_file.pk, size, MAX_UPLOAD_BYTES)
            cache.set(key, {'error': 'too large'}, CACHE_TIMEOUT_SECONDS)
            return None
        try:
            _, frames = user_file.extract_data()
            budget = {'columns': MAX_SOURCE_COLUMNS, 'values': MAX_INDEX_VALUES}
            index = {'sheets': [
                {'sheet': str(sheet), **sheet_index(df, budget)}
                for sheet, df in (frames or {}).items()
                if df is not None and not getattr(df, 'empty', True)
            ]}
            cache.set(key, index, CACHE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning('Lineage could not read upload %s: %s', user_file.pk, exc)
            # Retry a missing or broken file now and then, not on every request.
            cache.set(key, {'error': str(exc)[:200]}, UNREADABLE_RETRY_SECONDS)
            return None
    return index['sheets']


def table_index(table):
    """Index of a live non-package table (used only when a dataset has no readable uploads)."""
    from django.core.cache import cache

    key = f'table-lineage-table:v{CACHE_VERSION}:{table.pk}:{table.updated_at.isoformat()}'
    index = cache.get(key)
    if index is None:
        with column_stats.single_flight(key):
            index = cache.get(key)
            if index is None:
                empty = {'labels': [], 'row_count': 0, 'rows_sampled': False, 'columns': []}
                if (table.row_count or 0) * max(len(table.columns or []), 1) > MAX_LIVE_TABLE_CELLS:
                    index = empty
                else:
                    # A short-lived instance: the caller's (deferred) table never holds the DataFrame,
                    # so frames do not pile up while the other tables are indexed.
                    df = type(table).objects.only('df').get(pk=table.pk).df
                    index = sheet_index(df) if df is not None else empty
                    del df
                cache.set(key, index, CACHE_TIMEOUT_SECONDS)
    return index


def upload_label(filename, sheet):
    return filename if str(sheet) == filename else f'{filename} - {sheet}'


def _live_match(live_tables, sheet):
    """The live table still showing this upload sheet unchanged in shape: same title, rows and column labels.

    It only lends its id; values always come from the upload, so a working table the agent
    cleaned in place never stands in for the original.
    """
    for table in live_tables:
        if table.title == sheet['sheet'] and table.row_count == sheet['row_count'] and table.columns == sheet['labels']:
            return table
    return None


def _live_tables(dataset):
    return [
        table for table in dataset.table_set.defer('df').order_by('id')
        if not is_published(table.title) and not table.title.endswith('_tmp')
    ]


def source_tables(dataset):
    """Upload sheets of a publication dataset (see module docstring), within MAX_SOURCE_COLUMNS."""
    from api.models import UserFile

    live = _live_tables(dataset)
    sources, read_any, budget, values_left = [], False, MAX_SOURCE_COLUMNS, MAX_TOTAL_INDEX_VALUES

    def within_values(columns):
        """The leading columns that fit in what is left of the total value budget."""
        nonlocal values_left
        kept = []
        for column in columns:
            size = len(column[1]) + len(column[2])
            if size > values_left:
                values_left = 0
                break
            values_left -= size
            kept.append(column)
        return kept

    for user_file in dataset.user_files.order_by('uploaded_at', 'id'):
        if budget <= 0 or values_left <= 0:
            break  # no more columns would be compared: do not read further files
        if user_file.file_type != UserFile.FileType.TABULAR:
            continue
        sheets = upload_index(user_file)
        if sheets is None:
            continue
        read_any = True
        for sheet in sheets:
            if budget <= 0 or values_left <= 0:
                break
            columns = within_values(sheet['columns'][:budget])
            budget -= len(columns)
            match = _live_match(live, sheet)
            sources.append(Source(
                match.id if match else None, upload_label(user_file.filename, sheet['sheet']), columns,
                sheet['row_count'], sheet.get('rows_sampled', False),
            ))
    if not read_any:
        for table in live:
            if budget <= 0 or values_left <= 0:
                break
            index = table_index(table)
            columns = within_values(index['columns'][:budget])
            budget -= len(columns)
            sources.append(Source(table.id, table.title, columns, index['row_count'], index.get('rows_sampled', False)))
    return sources


def cache_key(table):
    """Changes when this table, the dataset's uploads or non-package tables, or its conversion change.

    Other package tables are left out: saving them changes neither the sources nor this table.
    """
    from api.models import DwcConversion

    dataset = table.dataset
    digest = hashlib.sha256(f'{table.pk}|{table.updated_at.isoformat()}\n'.encode())
    for live in _live_tables(dataset):
        digest.update(f't{live.pk}|{live.title}|{live.row_count}|{live.columns}|{live.updated_at.isoformat()}\n'.encode())
    for row in dataset.user_files.order_by('id').values_list('id', 'file', 'uploaded_at'):
        digest.update(f'f{row[0]}|{row[1]}|{row[2].isoformat()}\n'.encode())
    conversion = DwcConversion.objects.filter(dataset=dataset).only('id', 'status', 'updated_at').first()
    if conversion:
        digest.update(f'c{conversion.pk}|{conversion.status}|{conversion.updated_at.isoformat()}\n'.encode())
    return f'table-lineage:v{CACHE_VERSION}:{table.pk}:{digest.hexdigest()[:32]}'


def compute_lineage(table):
    from api.models import Dataset, DwcConversion

    if not is_published(table.title):
        return unavailable()
    dataset = table.dataset
    columns = {}
    if dataset.workflow_type == Dataset.WorkflowType.DWCA_CONVERSION:
        conversion = DwcConversion.objects.filter(dataset=dataset).first()
        if conversion and conversion.status == DwcConversion.Status.COMPLETE:
            columns = recorded_lineage(conversion.report, table.title, table.columns)
        # The conversion's sources are archive files, not tables: nothing to match against.
    else:
        df = table.df
        if df is not None and len(df.columns):
            labels = table.columns or _display_columns(df.columns)
            columns = matched_lineage(df, labels, source_tables(dataset))
    if not columns:
        return unavailable()
    order = {label: i for i, label in enumerate(table.columns)}
    return {'available': True, 'columns': dict(sorted(columns.items(), key=lambda item: order.get(item[0], len(order))))}


def table_lineage(table):
    """The lineage payload, cached until the table, its sources or the conversion change."""
    from django.core.cache import cache

    if not is_published(table.title):
        return unavailable()
    key = cache_key(table)
    payload = cache.get(key)
    if payload is None:
        payload = compute_lineage(table)
        cache.set(key, payload, CACHE_TIMEOUT_SECONDS)
    return payload
