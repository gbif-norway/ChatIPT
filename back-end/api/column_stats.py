"""Column statistics, value breakdowns, filters and sorting for the table explorer.

Everything here is a pure function over a pandas DataFrame and the table's display labels.
Columns are addressed by position, because raw column names may be duplicated or blank
(``Table.columns`` holds the unique display labels).

Cell text always matches ``Table.search_rows``: ``astype('string')``, blanks are null/NaN,
whitespace is stripped, and an empty result counts as blank. A value reported here can be
sent back as an ``is`` filter and finds exactly the rows that were counted.
"""
import heapq
import json
import logging
import math
import re
import threading
import time
import unicodedata
import warnings
from collections import OrderedDict
from decimal import Decimal
from contextlib import contextmanager

import numpy as np
import pandas as pd

TOP_VALUES = 10
VALUE_CHARS = 300
NUMERIC_BINS = 12
DATE_BUCKETS = 40
VARIANT_GROUPS = 20
VARIANT_MEMBERS = 10
KIND_SAMPLE = 1000
CACHE_TIMEOUT_SECONDS = 24 * 60 * 60

FILTER_OPS = ('is', 'is_not', 'equals', 'contains', 'blank', 'not_blank', 'duplicated', 'variant_of')
VALUE_OPS = ('is', 'is_not', 'equals', 'contains', 'variant_of')
MAX_FILTERS = 50
MAX_LABEL_CHARS = 2000
MAX_FILTER_VALUE_CHARS = 1000

logger = logging.getLogger(__name__)
_SURROGATE = re.compile('[\ud800-\udfff]')
_INTEGER_PATTERN = r'[+-]?\d+'

# Strict ISO 8601: year, year-month, calendar date, date-time (``T`` or the space pandas
# writes), and ``start/end`` intervals of those. Nothing looser: "12" or "May" are text.
_TIME = (
    r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:[.,]\d+)?)?'
    r'(?:Z|[+-](?:[01]\d|2[0-3])(?::?[0-5]\d)?)?'
)


def _iso_part(prefix):
    return (
        rf'(?P<{prefix}y>\d{{4}})'
        rf'(?:-(?P<{prefix}m>0[1-9]|1[0-2])'
        rf'(?:-(?P<{prefix}d>0[1-9]|[12]\d|3[01])'
        rf'(?:[T ]{_TIME})?)?)?'
    )


ISO_DATE_PATTERN = rf'{_iso_part("a")}(?:/{_iso_part("b")})?'
_DAYS_IN_MONTH = np.array([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])


# ---------------------------------------------------------------------------
# Cell text and value counts
# ---------------------------------------------------------------------------

def _clean_texts(texts):
    """Strip, and make lone surrogates (from surrogateescape decoding) the '?' the rows view shows."""
    values = np.array([text.strip() for text in texts], dtype=object)
    if len(values) and _SURROGATE.search(''.join(values)):
        values = pd.Series(values, dtype=object).str.replace(_SURROGATE, '?', regex=True).to_numpy(dtype=object)
    return values


def clean_text(value):
    return _clean_texts([value])[0]


def _factorized_text(series):
    """(codes, clean distinct texts); code -1 is a null cell."""
    codes, uniques = pd.factorize(series.astype('string'))
    return codes, _clean_texts(uniques)


def cell_text(series):
    """Per-row cell text ('' means blank): ``search_rows`` text, with lone surrogates as '?'.

    Stripping and surrogate replacement run on distinct texts only.
    """
    codes, clean = _factorized_text(series)
    return pd.Series(np.append(clean, '')[codes], index=series.index, dtype=object)


def value_counts(series):
    """Populated values (as ``cell_text``) and their row counts, in no particular order."""
    codes, clean = _factorized_text(series)
    if len(clean) == 0:
        return pd.Series([], dtype='int64', index=pd.Index([], dtype=object))
    counts = np.bincount(codes[codes >= 0], minlength=len(clean))
    by_text = pd.Series(counts, index=pd.Index(clean, dtype=object), dtype='int64')
    by_text = by_text[by_text.index != '']
    if by_text.index.has_duplicates:
        by_text = by_text.groupby(level=0, sort=False).sum()
    return by_text


def by_value(counts):
    """Values in ascending codepoint order."""
    return counts.sort_index(kind='stable')


def by_count(counts, limit=None):
    """Most common first, ties by value. With ``limit`` only the first ``limit`` are ordered."""
    if limit is None or len(counts) <= limit:
        return by_value(counts).sort_values(ascending=False, kind='stable')
    threshold = np.partition(counts.to_numpy(), len(counts) - limit)[len(counts) - limit]
    above = counts[counts > threshold]
    tied = counts.index[counts.to_numpy() == threshold]
    chosen = heapq.nsmallest(limit - len(above), tied)
    candidates = pd.concat([above, counts.loc[chosen]])
    return by_value(candidates).sort_values(ascending=False, kind='stable').head(limit)


def safe_text(value):
    """Text that can always be JSON encoded (lone surrogates become '?')."""
    return value.encode('utf-8', 'replace').decode('utf-8')


def value_entry(value, count):
    truncated = len(value) > VALUE_CHARS
    return {
        'value': safe_text(value[:VALUE_CHARS] if truncated else value),
        'count': int(count),
        'truncated': truncated,
    }


# ---------------------------------------------------------------------------
# Kind detection
# ---------------------------------------------------------------------------

def parse_numbers(values):
    """Float array for value texts; NaN where a text is not a plain number ("1,5", "5 cm")."""
    if len(values) == 0:
        return np.array([], dtype=float)
    parsed = pd.to_numeric(pd.Series(values, dtype=object), errors='coerce')
    return pd.Series(parsed).to_numpy(dtype=float, na_value=np.nan)


def _month_index(years, months):
    return (years - 1970) * 12 + (months - 1)


def _valid_days(years, months, days):
    leap = (years % 4 == 0) & ((years % 100 != 0) | (years % 400 == 0))
    limit = _DAYS_IN_MONTH[months - 1] + ((months == 2) & leap)
    return days <= limit


def _part_bounds(parts, prefix):
    """(valid, start_day, end_day) numpy arrays for one side of an ISO value."""
    years = parts[f'{prefix}y'].astype('float').to_numpy()
    has_month = parts[f'{prefix}m'].notna().to_numpy()
    has_day = parts[f'{prefix}d'].notna().to_numpy()
    months = parts[f'{prefix}m'].fillna('1').astype('float').to_numpy()
    days = parts[f'{prefix}d'].fillna('1').astype('float').to_numpy()
    years = np.nan_to_num(years).astype('int64')
    months = months.astype('int64')
    days = days.astype('int64')
    valid = _valid_days(years, months, days)
    start_month = _month_index(years, months).astype('datetime64[M]')
    start = start_month.astype('datetime64[D]') + (days - 1)
    year_end = _month_index(years, np.full_like(months, 12)).astype('datetime64[M]')
    end = np.where(
        has_day,
        start,
        np.where(
            has_month,
            (start_month + 1).astype('datetime64[D]') - 1,
            (year_end + 1).astype('datetime64[D]') - 1,
        ),
    )
    return valid, start, end


def parse_dates(values):
    """(start_days, end_days) datetime64[D] arrays when every value is strict ISO 8601, else None."""
    if len(values) == 0:
        return None
    texts = pd.Series(values, dtype='string')
    if not texts.str.fullmatch(ISO_DATE_PATTERN).fillna(False).all():
        return None
    parts = texts.str.extract(f'^{ISO_DATE_PATTERN}$')
    valid, start, end = _part_bounds(parts, 'a')
    interval = parts['by'].notna().to_numpy()
    if interval.any():
        b_valid, b_start, b_end = _part_bounds(parts, 'b')
        valid &= np.where(interval, b_valid, True)
        start = np.where(interval, np.minimum(start, b_start), start)
        end = np.where(interval, np.maximum(end, b_end), end)
    if not valid.all():
        return None
    return start, end


def detect_kind(values):
    """('number', floats) | ('date', (starts, ends)) | ('text', None) | ('empty', None)."""
    if len(values) == 0:
        return 'empty', None
    # A sample rejects most text columns before parsing every distinct value.
    sample = values[:KIND_SAMPLE]
    if np.isfinite(parse_numbers(sample)).all():
        numbers = parse_numbers(values)
        if np.isfinite(numbers).all():
            return 'number', numbers
    if parse_dates(sample) is not None:
        dates = parse_dates(values)
        if dates is not None:
            return 'date', dates
    return 'text', None


def column_is_numeric(text):
    """Whether every populated cell of a cell_text Series is a finite number."""
    populated = text[text != '']
    if populated.empty:
        return False
    return bool(np.isfinite(parse_numbers(populated.drop_duplicates().to_numpy(dtype=object))).all())


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def _number(value):
    value = float(value)
    if value.is_integer() and abs(value) < 2 ** 53:
        return int(value)
    return value


def _weighted_median(values, weights):
    order = np.argsort(values, kind='stable')
    ordered = values[order]
    cumulative = np.cumsum(weights[order])
    total = int(cumulative[-1])

    def at(position):
        return float(ordered[np.searchsorted(cumulative, position, side='right')])

    lower, upper = at((total - 1) // 2), at(total // 2)
    middle = (lower + upper) / 2
    return middle if math.isfinite(middle) else lower / 2 + upper / 2


def numeric_summary(values, weights):
    weights = np.asarray(weights, dtype='int64')
    low, high = float(values.min()), float(values.max())
    total = int(weights.sum())
    # Values near the float limit overflow sums and spans; work in scaled units there.
    scale = max(abs(low), abs(high)) or 1.0
    with np.errstate(over='ignore', invalid='ignore'):
        mean = float(np.dot(values, weights.astype(float)) / total)
        if not math.isfinite(mean):
            mean = float(np.dot(values / scale, weights.astype(float)) / total) * scale
        finite_span = math.isfinite(high - low)
    if low == high:
        histogram = [{'start': _number(low), 'end': _number(high), 'count': total}]
    else:
        integral = bool(np.all(np.mod(values, 1) == 0))
        bins = min(NUMERIC_BINS, len(values))
        if integral and finite_span and high - low + 1 <= NUMERIC_BINS:
            edges = np.arange(low, high + 2)
        elif finite_span:
            edges = np.histogram_bin_edges(values, bins=bins, range=(low, high))
        else:
            edges = np.linspace(low / scale, high / scale, bins + 1) * scale
            edges[0], edges[-1] = low, high
        counts, edges = np.histogram(values, bins=edges, weights=weights)
        histogram = [
            {'start': _number(edges[i]), 'end': _number(edges[i + 1]), 'count': int(round(counts[i]))}
            for i in range(len(counts))
        ]
    return {
        'min': _number(low),
        'max': _number(high),
        'median': _number(_weighted_median(values, weights)),
        'mean': mean,
        'histogram': histogram,
    }


def date_summary(dates, weights):
    starts, ends = dates
    weights = np.asarray(weights, dtype='int64')
    first, last = starts.min(), ends.max()
    start_months = starts.astype('datetime64[M]').astype('int64')
    first_month, last_month = int(start_months.min()), int(start_months.max())
    if last_month - first_month < 24:
        offsets = start_months - first_month
        counts = np.bincount(offsets, weights=weights, minlength=last_month - first_month + 1)
        histogram = [
            {'label': str(np.datetime64(first_month + i, 'M')), 'count': int(counts[i])}
            for i in range(len(counts))
        ]
    else:
        years = starts.astype('datetime64[Y]').astype('int64') + 1970
        first_year, last_year = int(years.min()), int(years.max())
        width = max(1, math.ceil((last_year - first_year + 1) / DATE_BUCKETS))
        offsets = (years - first_year) // width
        counts = np.bincount(offsets, weights=weights)
        histogram = []
        for i, count in enumerate(counts):
            bucket_start = first_year + i * width
            bucket_end = min(bucket_start + width - 1, last_year)
            label = str(bucket_start) if width == 1 else f'{bucket_start}–{bucket_end}'
            histogram.append({'label': label, 'count': int(count)})
    return {'min': str(first), 'max': str(last), 'histogram': histogram}


# ---------------------------------------------------------------------------
# Variant groups
# ---------------------------------------------------------------------------

_PUNCTUATION = re.compile(r'[^\w\s]|_')
_ASCII_PUNCTUATION = bytes(code for code in range(128) if _PUNCTUATION.match(chr(code)))


def _variant_key(value):
    if not isinstance(value, str):
        return ''
    if value.isascii():
        # NFKC is the identity and casefold is lower() for ASCII; translate drops the same characters.
        value = value.lower().encode('ascii').translate(None, _ASCII_PUNCTUATION).decode('ascii')
    else:
        value = _PUNCTUATION.sub('', unicodedata.normalize('NFKC', value).casefold())
    return ' '.join(value.split())


def variant_keys(values):
    """Purely textual normalisation: NFKC, case-fold, drop punctuation, collapse whitespace."""
    return np.array([_variant_key(value) for value in values], dtype=object)


def variant_key(value):
    return variant_keys([value])[0]


def variant_groups(counts):
    """(groups, total group count) for a column's value counts."""
    if len(counts) < 2:
        return [], 0
    keys = variant_keys(counts.index.to_numpy(dtype=object))
    if not pd.Index(keys).has_duplicates:
        return [], 0
    frame = pd.DataFrame({'value': counts.index.to_numpy(dtype=object), 'count': counts.to_numpy(), 'key': keys})
    frame = frame[frame['key'] != '']
    sizes = frame.groupby('key', sort=False)['value'].transform('size')
    frame = frame[sizes > 1]
    if frame.empty:
        return [], 0
    summary = frame.groupby('key', sort=True).agg(rows=('count', 'sum'), values=('value', 'size'))
    summary = summary.sort_values(['rows', 'values'], ascending=[False, False], kind='stable')
    groups = []
    for key in summary.index[:VARIANT_GROUPS]:
        members = frame[frame['key'] == key].sort_values(['count', 'value'], ascending=[False, True], kind='stable')
        listed = members.head(VARIANT_MEMBERS)
        groups.append({
            'values': [value_entry(value, count) for value, count in zip(listed['value'], listed['count'])],
            'count': int(summary.at[key, 'rows']),
            'value_count': int(summary.at[key, 'values']),
        })
    return groups, int(len(summary.index))


# ---------------------------------------------------------------------------
# Column stats payload
# ---------------------------------------------------------------------------

def distinctness(populated, unique):
    if populated == 0:
        return 'empty'
    if unique == 1:
        return 'constant'
    if unique == populated:
        return 'all_distinct'
    return 'repeats'


def column_summary(series, name, position, plain=False):
    """One column's stats. ``plain`` skips kind detection, summaries and variant groups."""
    counts = value_counts(series)
    populated = int(counts.sum())
    unique = int(len(counts))
    repeated = counts[counts > 1]
    if plain:
        kind, parsed = ('empty' if unique == 0 else 'text'), None
    else:
        kind, parsed = detect_kind(counts.index.to_numpy(dtype=object))
    distinct = distinctness(populated, unique)
    top = by_count(counts, TOP_VALUES)
    groups, group_count = [], 0
    # Number columns are skipped: dropping punctuation would pair "1.0" with "10" and "-5" with "5".
    if not plain and (kind == 'text' or (kind == 'date' and distinct != 'all_distinct')):
        groups, group_count = variant_groups(counts)
    weights = counts.to_numpy()
    return {
        'name': name,
        'position': position,
        'kind': kind,
        'populated': populated,
        'blank': int(len(series.index)) - populated,
        'unique': unique,
        'distinctness': distinct,
        'duplicated_values': int(len(repeated)),
        'duplicated_rows': int(repeated.sum()),
        'top_values': [value_entry(value, count) for value, count in top.items()],
        'numeric': numeric_summary(parsed, weights) if kind == 'number' else None,
        'date': date_summary(parsed, weights) if kind == 'date' else None,
        'variant_groups': groups,
        'variant_group_count': group_count,
    }


def column_stats(df, labels):
    """The per-column part of the column-stats payload."""
    columns = []
    for position, label in enumerate(labels):
        series = df.iloc[:, position]
        try:
            columns.append(column_summary(series, label, position))
        except Exception:
            # One odd column must not take down the whole table's stats.
            logger.exception('Column stats failed for column %s; falling back to a plain summary', position)
            columns.append(column_summary(series, label, position, plain=True))
    return {'row_count': int(len(df.index)), 'columns': columns}


_flights_lock = threading.Lock()
_flights = {}


@contextmanager
def single_flight(key):
    """Serialise work on one key within this process so concurrent cold requests compute once."""
    with _flights_lock:
        entry = _flights.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _flights_lock:
            entry[1] -= 1
            if entry[1] == 0:
                _flights.pop(key, None)


def cache_key(table_id, updated_at):
    return f'table-column-stats:v1:{table_id}:{updated_at.isoformat()}'


def version(updated_at):
    return updated_at.isoformat().replace('+00:00', 'Z')


# ---------------------------------------------------------------------------
# Column values (paged breakdown for one column)
# ---------------------------------------------------------------------------

def column_values(series, search='', offset=0, limit=50, sort='count'):
    counts = value_counts(series)
    populated = int(counts.sum())
    matched = counts
    if search:
        needle = search.casefold()
        keys = pd.Series(counts.index.to_numpy(dtype=object), dtype='string').str.casefold()
        hits = keys.str.contains(needle, regex=False).to_numpy(dtype=bool, na_value=False)
        matched = counts[hits]
    ordered = by_value(matched) if sort == 'value' else by_count(matched)
    page = ordered.iloc[offset:offset + limit]
    return {
        'populated': populated,
        'blank': int(len(series.index)) - populated,
        'unique': int(len(counts)),
        'count': int(len(matched)),
        'offset': offset,
        'limit': limit,
        'results': [value_entry(value, count) for value, count in page.items()],
    }


# ---------------------------------------------------------------------------
# Row filters and sorting
# ---------------------------------------------------------------------------

def parse_filters(raw, labels):
    """Validate the rows API ``filters`` JSON; raises ValueError with a user-facing message."""
    try:
        filters = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        raise ValueError('Filters must be a JSON list.')
    if not isinstance(filters, list):
        raise ValueError('Filters must be a JSON list.')
    if len(filters) > MAX_FILTERS:
        raise ValueError(f'Use at most {MAX_FILTERS} filters.')
    parsed = []
    for item in filters:
        if not isinstance(item, dict):
            raise ValueError('Each filter must be an object with column and op.')
        column, op = item.get('column'), item.get('op')
        if isinstance(column, str) and len(column) > MAX_LABEL_CHARS:
            raise ValueError(f'Column names are limited to {MAX_LABEL_CHARS} characters.')
        if not isinstance(column, str) or column not in labels:
            raise ValueError(f'Unknown column: {column}')
        if op not in FILTER_OPS:
            raise ValueError(f'Unknown filter op: {op}')
        value = item.get('value')
        if op in VALUE_OPS:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f'The {op} filter needs a text value.')
            value = clean_text(str(value))
            if len(value) > MAX_FILTER_VALUE_CHARS:
                raise ValueError(f'Filter values are limited to {MAX_FILTER_VALUE_CHARS} characters.')
        parsed.append({'position': labels.index(column), 'op': op, 'value': value})
    return parsed


def _variant_mask(text, target):
    codes, uniques = pd.factorize(text)
    keys = variant_keys(np.asarray(uniques, dtype=object))
    return (keys == target)[codes] & (text != '').to_numpy()


def filter_mask(df, filters):
    """Boolean ndarray of rows matching every parsed filter."""
    mask = np.ones(len(df.index), dtype=bool)
    texts = {}
    for item in filters:
        position, op, value = item['position'], item['op'], item['value']
        if position not in texts:
            texts[position] = cell_text(df.iloc[:, position])
        text = texts[position]
        if op in ('is', 'is_not'):
            matches = is_mask(df.iloc[:, position], text, value.strip())
            mask &= matches if op == 'is' else ~matches
            continue
        if op == 'equals':
            mask &= equals_mask(df.iloc[:, position], text, value.strip())
            continue
        if op == 'contains':
            matches = text.str.casefold().str.contains(value.strip().casefold(), regex=False)
        elif op == 'blank':
            matches = text == ''
        elif op == 'not_blank':
            matches = text != ''
        elif op == 'duplicated':
            matches = (text != '') & text.duplicated(keep=False)
        else:  # variant_of
            key = variant_key(value)
            if key:
                mask &= _variant_mask(text, key)
                continue
            # Punctuation-only values normalise to nothing; match them exactly instead.
            matches = text == value.strip()
        mask &= np.asarray(matches.to_numpy(dtype=bool, na_value=False))
    return mask


def _numeric_keys(texts, column_text):
    """Exact sort keys for numeric texts: Python ints when the column is all integers beyond
    float precision, floats otherwise."""
    numbers = parse_numbers(texts)
    if len(numbers) and np.abs(numbers).max() >= 2 ** 53:
        distinct = pd.Series(column_text[column_text != ''].drop_duplicates(), dtype=object)
        if distinct.str.fullmatch(_INTEGER_PATTERN).all():
            return pd.Series([int(text) for text in texts], dtype=object)
    return pd.Series(numbers)


def js_number(value):
    """``String(value)`` in JavaScript for a number parsed from JSON."""
    if isinstance(value, int) and abs(value) <= 2 ** 53:
        return str(value)
    value = float(value)
    if value == 0:
        return '0'
    sign = '-' if value < 0 else ''
    decimal = Decimal(repr(abs(value))).as_tuple()
    point = decimal.exponent + len(decimal.digits)
    digits = ''.join(str(digit) for digit in decimal.digits).rstrip('0')
    if len(digits) <= point <= 21:
        return sign + digits + '0' * (point - len(digits))
    if 0 < point <= 21:
        return sign + digits[:point] + '.' + digits[point:]
    if -6 < point <= 0:
        return sign + '0.' + '0' * -point + digits
    exponent = point - 1
    mantissa = digits if len(digits) == 1 else digits[0] + '.' + digits[1:]
    return f"{sign}{mantissa}e{'+' if exponent > 0 else '-'}{abs(exponent)}"


def _json_cells(series):
    """Cell values as ``Table.row_page`` serialises them (inf as null, ISO dates, str fallback)."""
    frame = series.reset_index(drop=True).to_frame('v')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', FutureWarning)  # row_page does the same replace
        frame = frame.replace([np.inf, -np.inf], np.nan)
    if frame['v'].dtype == object:
        frame['v'] = frame['v'].map(lambda value: safe_text(value) if isinstance(value, str) else value)
    payload = frame.to_json(orient='records', date_format='iso', force_ascii=False, default_handler=str)
    return [row['v'] for row in json.loads(payload)]


def _display_matches(json_value, needle, number):
    """Whether the browser would show ``json_value`` as ``needle``."""
    if json_value is None or isinstance(json_value, (list, dict)):
        return False
    if isinstance(json_value, bool):
        return needle == ('true' if json_value else 'false')
    if isinstance(json_value, (int, float)):
        return math.isfinite(number) and float(json_value) == number and js_number(json_value) == needle
    return str(json_value).strip() == needle


def equivalent_mask(series, text, needle, numeric=True):
    """Rows the user would read as ``needle``: the rows view shows that text for the cell, or
    (number columns, when ``numeric``) the cell is numerically equal. Works on distinct
    (text, type) pairs."""
    populated = (text != '').to_numpy(dtype=bool)
    text_codes, distinct_texts = pd.factorize(text)
    number = float(parse_numbers([needle])[0])
    matched = np.zeros(len(distinct_texts), dtype=bool)
    if numeric and math.isfinite(number) and column_is_numeric(text):
        matched |= parse_numbers(np.asarray(distinct_texts, dtype=object)) == number
    mask = matched[text_codes]

    # A str cell is shown as its own (stripped) text, which already failed the exact match;
    # other cells may display differently (12.0 as "12", True as "true", ISO timestamps).
    values = series.astype(object) if isinstance(series.dtype, pd.CategoricalDtype) else series
    if values.dtype == object:
        types = values.map(type)
        candidate = populated & (types != str).to_numpy(dtype=bool)
        type_codes = pd.factorize(types)[0].astype('int64')
    else:
        candidate = populated
        type_codes = np.zeros(len(text_codes), dtype='int64')
    if candidate.any():
        groups = text_codes.astype('int64') * (int(type_codes.max()) + 1) + type_codes
        rows = np.flatnonzero(candidate)
        group_ids, first = np.unique(groups[rows], return_index=True)
        representatives = rows[first]
        cells = _json_cells(values.iloc[representatives])
        hits = np.array([_display_matches(cell, needle, number) for cell in cells], dtype=bool)
        if hits.any():
            mask |= candidate & np.isin(groups, group_ids[hits])
    return mask & populated


def is_mask(series, text, needle):
    """Rows an ``is`` filter selects: the exact cell text when any row has it (so top_values and
    column-values round-trip with the same counts), otherwise displayed or numeric equivalents."""
    exact = (text == needle).to_numpy(dtype=bool)
    if exact.any() or not needle:
        return exact
    return equivalent_mask(series, text, needle)


def equals_mask(series, text, needle):
    """Rows an ``equals`` filter selects, for following links between tables: the exact cell
    text, or a non-text cell the rows view displays as ``needle`` (a float 12.0 shown as "12").
    Unlike ``is`` it never compares text numerically, so "1" does not find "001" — the same
    comparison the package explorer uses when it counts linked rows."""
    exact = (text == needle).to_numpy(dtype=bool)
    if exact.any() or not needle:
        return exact
    return equivalent_mask(series, text, needle, numeric=False)


def sort_positions(df, position, order='asc', mask=None):
    """Row positions (within ``mask``) sorted by one column, numeric-aware, blanks last.

    Numeric ties (e.g. "1" and "1.0") are broken by the cell text; remaining ties keep file order.
    """
    text = cell_text(df.iloc[:, position])
    rows = np.flatnonzero(mask) if mask is not None else np.arange(len(df.index))
    selected = text.iloc[rows]
    populated = (selected != '').to_numpy(dtype=bool)
    filled, blanks = rows[populated], rows[~populated]
    texts = selected[populated].to_numpy(dtype=object)
    ascending = order != 'desc'
    if column_is_numeric(text):
        frame = pd.DataFrame({'key': _numeric_keys(texts, text), 'text': texts})
        ordered = frame.sort_values(['key', 'text'], ascending=ascending, kind='stable').index.to_numpy()
    else:
        keys = pd.Series(pd.Series(texts, dtype=object).str.casefold().to_numpy(dtype=object))
        ordered = keys.sort_values(ascending=ascending, kind='stable').index.to_numpy()
    return np.concatenate([filled[ordered], blanks])


def canonical_query(search, column, exact, filters, sort, order):
    """A hashable description of a rows query (filters already parsed)."""
    return json.dumps(
        [search or '', column or '', bool(exact),
         [[f['position'], f['op'], f['value']] for f in filters], sort or '', order or 'asc'],
        ensure_ascii=True,
    )


class PositionsCache:
    """Small in-process LRU of filtered/sorted row positions, bounded by entries, bytes and age."""

    def __init__(self, max_entries=64, max_bytes=64 * 2 ** 20, ttl=300):
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if time.monotonic() - entry[0] > self.ttl:
                self._drop(key)
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def set(self, key, positions):
        if positions.nbytes > self.max_bytes:
            return
        positions.setflags(write=False)
        with self._lock:
            if key in self._entries:
                self._drop(key)
            self._entries[key] = (time.monotonic(), positions)
            self._bytes += positions.nbytes
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def _drop(self, key):
        self._bytes -= self._entries.pop(key)[1].nbytes


row_positions_cache = PositionsCache()


def compact_positions(positions, row_count):
    return np.asarray(positions, dtype=np.int32 if row_count < 2 ** 31 else np.int64)
