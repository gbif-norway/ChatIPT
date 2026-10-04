"""Advisory parent/child scientific consistency checks for resolved Event hierarchies.

Pure and deterministic. Inputs are never mutated and source values are never
inherited, rolled up, normalized or repaired; parsed copies are used only for
comparison and every check reports the exact supplied strings. Statuses:
contradiction, conflict-signal, reporting-gap, not-demonstrated, incomparable, compatible.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from decimal import Decimal

from api.dwca_humboldt import DIMENSIONS, DIRECT, ECO, ECOIRI, valid_value

DWC = 'http://rs.tdwg.org/dwc/terms/'
STATUSES = ('contradiction', 'conflict-signal', 'reporting-gap', 'not-demonstrated', 'incomparable', 'compatible')
FINDINGS = {'contradiction', 'conflict-signal', 'reporting-gap'}
WGS84 = {'WGS84', 'WGS 84', 'EPSG:4326', 'World Geodetic System 1984'}
EARTH_RADIUS = 6371008.8  # mean radius; tolerance below covers ellipsoid differences
SCOPE_TERMS = [ns + stem + dim + 'Scope' for dim in DIMENSIONS for ns in (ECO, ECOIRI) for stem in ('target', 'excluded')]
FLAG_TERMS = [ECO + 'is' + dim + 'ScopeFullyReported' for dim in DIMENSIONS]
# Humboldt hierarchy guidance 3.2.4: populate at the highest applicable level and all child events.
INFERENCE_TERMS = SCOPE_TERMS + FLAG_TERMS + [ns + term for ns in (ECO, ECOIRI)
                                              for term in ('taxonCompletenessReported', 'taxonCompletenessProtocols')]
# Only comparisons bounded by the ancestor's geospatial scope; sampled areas are never compared or summed.
AREAS = (('geospatialScopeArea', 'geospatialScopeArea'), ('totalAreaSampled', 'geospatialScopeArea'))

_DATE = re.compile(r'(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?)?)?')
_NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?')
_EARLIEST_OFFSET, _LATEST_OFFSET = timedelta(hours=14), timedelta(hours=12)  # UTC+14 .. UTC-12


def _span(text):
    """UTC (low, high) bounds of one ISO 8601 date/date-time at its supplied precision, or None."""
    match = _DATE.fullmatch(text)
    if not match:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    try:
        start = datetime(int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0))
        if second: end = start + timedelta(seconds=1)
        elif minute: end = start + timedelta(minutes=1)
        elif day: end = start + timedelta(days=1)
        elif month: end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
        else: end = datetime(start.year + 1, 1, 1)
        if zone:
            if zone != 'Z':
                sign = -1 if zone[0] == '-' else 1
                digits = zone[1:].replace(':', '')
                if int(digits[2:]) >= 60:
                    return None
                offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
                if offset > timedelta(hours=14):
                    return None
                start, end = start - sign * offset, end - sign * offset
            return start, end
        # Local time with unknown offset: bounded envelope over all real UTC offsets.
        return start - _EARLIEST_OFFSET, end + _LATEST_OFFSET
    except (ValueError, OverflowError):
        return None


def parse_event_date(value):
    """(start_min, start_max, end_min, end_max) in naive UTC, or None if unsupported/invalid."""
    parts = value.split('/')
    if not value or len(parts) > 2:
        return None
    if len(parts) == 2:
        first, last = parts
        if re.fullmatch(r'\d{2}(-\d{2})?', last) and re.fullmatch(r'\d{4}(-\d{2}){1,2}', first):
            # Abbreviated end (2007-11-13/15): only trailing date components are replaced.
            head = first.split('-')
            last = '-'.join(head[:len(head) - len(last.split('-'))] + last.split('-'))
            if len(last.split('-')) != len(head):
                return None
        first_span, last_span = _span(first), _span(last)
    else:
        first_span = last_span = _span(value)
    if not first_span or not last_span or first_span[0] > last_span[1]:
        return None
    return first_span[0], first_span[1], last_span[0], last_span[1]


def _nearest(links, has):
    """Nearest ancestor with a property for every child, iteratively and in linear time."""
    nearest = {}
    for start in links:
        chain, node = [], start
        while node in links and node not in nearest:
            chain.append(node); node = links[node]
        found = nearest.get(node) if node in links else None
        for member in reversed(chain):
            parent = links[member]
            found = parent if has(parent) else found
            nearest[member] = found
    return nearest


def _temporal_bounds(links, dates):
    """Strongest possible temporal bounds across all ancestors, with their sources.

    These are comparison constraints, never inherited event values. Invalid dates
    supply no bound; the nearest-ancestor check still reports those as incomparable.
    """
    bounds = {}
    for start in links:
        chain, node = [], start
        while node in links and node not in bounds:
            chain.append(node); node = links[node]
        low, high = bounds.get(node, (None, None))
        for member in reversed(chain):
            parent = links[member]
            date = dates[parent]
            if date:
                if low is None or date[0] > low[0]:
                    low = (date[0], parent)
                if high is None or date[3] < high[0]:
                    high = (date[3], parent)
            bounds[member] = (low, high)
    return bounds


def _check(child, parent, ancestor, kind, status, reason, evidence, rows=None):
    return {'child': child, 'parent': parent, 'ancestor': ancestor, 'kind': kind, 'status': status,
            'reason': reason, 'evidence': evidence, **({'survey_rows': rows} if rows is not None else {})}


def _temporal(values, child, ancestor):
    term = DWC + 'eventDate'
    evidence = {'child': values[child].get(term, ''), 'ancestor': values[ancestor].get(term, '') if ancestor is not None else ''}
    if not evidence['child'] or not evidence['ancestor']:
        return 'incomparable', 'eventDate is missing on the child or on every ancestor.', evidence
    c, a = parse_event_date(evidence['child']), parse_event_date(evidence['ancestor'])
    if not c or not a:
        return 'incomparable', 'An eventDate is not a supported ISO 8601 date, date-time or interval.', evidence
    if c[1] < a[0] or c[2] > a[3]:
        return 'contradiction', ('As supplied in eventDate, every reading places the child outside its ancestor. '
            'Check whether the parent date records only a start rather than the full interval.'), evidence
    if c[0] >= a[1] and c[3] <= a[2]:
        return 'compatible', 'Every reading of the child interval lies within every reading of the ancestor interval.', evidence
    return 'not-demonstrated', 'The supplied precision or time zones allow readings both inside and outside the ancestor.', evidence


SPATIAL_TERMS = [DWC + term for term in ('decimalLatitude', 'decimalLongitude', 'coordinateUncertaintyInMeters', 'geodeticDatum',
                                         'footprintWKT', 'dataGeneralizations', 'informationWithheld', 'coordinatePrecision')]


def _coordinate_rounding(values):
    """Conservative location tolerance for rounding; no source value is changed."""
    precision = values.get(DWC + 'coordinatePrecision', '')
    if precision and (not _NUMBER.fullmatch(precision) or not 0 < float(precision) <= 360):
        return None
    allowance = 0.0
    for name in ('decimalLatitude', 'decimalLongitude'):
        exponent = Decimal(values[DWC + name]).as_tuple().exponent
        # A whole-degree value may have been rounded to a whole degree. This
        # allowance changes only the comparison tolerance, never a source value.
        step = 360.0 if exponent >= 3 else 0.0 if exponent < -324 else float(Decimal(1).scaleb(exponent))
        step = max(step, float(precision)) if precision else step
        allowance += 111700 * step / 2  # safe metres/degree upper bound for WGS84
    return allowance


def _circle(values):
    lat, lon, radius = (values.get(DWC + term, '') for term in ('decimalLatitude', 'decimalLongitude', 'coordinateUncertaintyInMeters'))
    if not all(_NUMBER.fullmatch(value) for value in (lat, lon, radius)):
        return None, 'Coordinates or a positive coordinateUncertaintyInMeters are missing or invalid.'
    lat, lon, radius = float(lat), float(lon), float(radius)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and radius > 0 and math.isfinite(radius)):
        return None, 'Coordinates or a positive coordinateUncertaintyInMeters are missing or invalid.'
    if values.get(DWC + 'geodeticDatum', '') not in WGS84:
        return None, 'geodeticDatum is missing or not a supported WGS84 alias.'
    return (math.radians(lat), math.radians(lon), radius), None


def _spatial(values, child, ancestor):
    evidence = {side: {term: values[node].get(term, '') for term in SPATIAL_TERMS if values[node].get(term)}
                for side, node in (('child', child), ('ancestor', ancestor)) if node is not None}
    if ancestor is None:
        return 'incomparable', 'No ancestor supplies coordinates.', evidence
    (c, c_reason), (a, a_reason) = _circle(values[child]), _circle(values[ancestor])
    if not c or not a:
        return 'incomparable', c_reason or a_reason, evidence
    c_rounding, a_rounding = _coordinate_rounding(values[child]), _coordinate_rounding(values[ancestor])
    if c_rounding is None or a_rounding is None:
        return 'incomparable', 'Supplied coordinatePrecision is invalid; no spatial claim is made.', evidence
    # Enclosing circles only bound the locations; overlap never demonstrates containment.
    h = math.sin((a[0] - c[0]) / 2) ** 2 + math.cos(c[0]) * math.cos(a[0]) * math.sin((a[1] - c[1]) / 2) ** 2
    distance = 2 * EARTH_RADIUS * math.asin(min(1.0, math.sqrt(h)))
    tolerance = 0.01 * distance + 5 + c_rounding + a_rounding
    disjoint = distance > c[2] + a[2] + tolerance
    evidence['calculated'] = {'distance_m': round(distance, 1), 'tolerance_m': round(tolerance, 1),
                              'circles_disjoint': disjoint}
    if any(values[child].get(term) or values[ancestor].get(term) for term in SPATIAL_TERMS[4:7]):
        return 'incomparable', 'A footprint or generalized/withheld location means the circles may not bound the true extents.', evidence
    if not disjoint:
        return 'not-demonstrated', 'Enclosing circles overlap; point-radius data cannot demonstrate containment.', evidence
    return 'contradiction', 'Both enclosing circles are disjoint beyond a conservative geodesic tolerance.', evidence


def _survey(rows):
    """(values, refs, ambiguous) for an event's survey rows; identical rows compare once."""
    refs = [{'table': row['table'], 'row': row['row']}
            for row in sorted(rows, key=lambda row: (row['table'], row['row']))]
    distinct = {tuple(sorted(row['values'].items())) for row in rows}
    if len(distinct) > 1:
        return None, refs, True
    return (dict(next(iter(distinct))) if distinct else {}), refs, False


def _tokens(value):
    items = value.split(' | ')
    if any(not item or item != item.strip() for item in items) or len(set(items)) != len(items):
        return None
    return frozenset(items)


def _scope(values):
    """Whole declared scope as exact token sets per term, or None if any list is malformed."""
    scope = {}
    for term in SCOPE_TERMS:
        if values.get(term):
            tokens = _tokens(values[term])
            if tokens is None:
                return None
            scope[term] = tokens
    return scope


def _survey_checks(child, parent, ancestor, child_rows, ancestor_rows):
    c_values, c_refs, c_ambiguous = _survey(child_rows)
    a_values, a_refs, a_ambiguous = _survey(ancestor_rows)
    rows = {'child': c_refs, 'ancestor': a_refs}
    if c_ambiguous or a_ambiguous:
        return [_check(child, parent, ancestor, 'survey-ambiguous', 'incomparable',
                       'An event has several distinct survey rows; none is selected for comparison.', {}, rows)]
    checks = []
    for term in INFERENCE_TERMS:
        if a_values.get(term) and not c_values.get(term):
            counterpart = (ECOIRI + term[len(ECO):] if term.startswith(ECO) else ECO + term[len(ECOIRI):])
            evidence = {'term': term, 'ancestor': a_values[term]}
            if counterpart in INFERENCE_TERMS and c_values.get(counterpart):
                checks.append(_check(child, parent, ancestor, 'inference-term', 'incomparable',
                    'The child supplies only the literal/IRI counterpart; the two representations are not assumed equivalent.',
                    {**evidence, 'child_counterpart': {counterpart: c_values[counterpart]}}, rows))
            else:
                checks.append(_check(child, parent, ancestor, 'inference-term', 'reporting-gap',
                    'The ancestor declares this scope/completeness term; the child supplies neither representation. Nothing is inherited.',
                    evidence, rows))
    for dim in DIMENSIONS:
        for ns in (ECO, ECOIRI):
            target, excluded = c_values.get(ns + 'target' + dim + 'Scope'), a_values.get(ns + 'excluded' + dim + 'Scope')
            if not target or not excluded:
                continue
            evidence = {'child': {ns + 'target' + dim + 'Scope': target}, 'ancestor': {ns + 'excluded' + dim + 'Scope': excluded}}
            target_set, excluded_set = _tokens(target), _tokens(excluded)
            if target_set is None or excluded_set is None:
                checks.append(_check(child, parent, ancestor, 'scope-collision', 'incomparable', 'A scope list is malformed.', evidence, rows))
            elif target_set & excluded_set:
                checks.append(_check(child, parent, ancestor, 'scope-collision', 'conflict-signal',
                    'The child targets an exact token its ancestor excludes; the child may not contribute to the ancestor.',
                    {**evidence, 'tokens': sorted(target_set & excluded_set)}, rows))
    pairs = [(term, 'true', 'false') for term in FLAG_TERMS]
    pairs += [(ns + 'taxonCompletenessReported', 'reportedComplete', 'reportedIncomplete') for ns in (ECO, ECOIRI)]
    for term, strong, weak in pairs:
        a_value, c_value = a_values.get(term, ''), c_values.get(term, '')
        lexical = (a_value.lower(), c_value.lower()) if term in FLAG_TERMS else (a_value, c_value)
        if lexical != (strong, weak):
            continue
        a_scope, c_scope = _scope(a_values), _scope(c_values)
        evidence = {'term': term, 'ancestor': a_value, 'child': c_value}
        if not a_scope or not c_scope or a_scope != c_scope:
            checks.append(_check(child, parent, ancestor, 'completeness', 'incomparable',
                'Completeness weakens, but the declared scopes are not identical (or are malformed).', evidence, rows))
        else:
            checks.append(_check(child, parent, ancestor, 'completeness', 'conflict-signal',
                'The ancestor reports complete and the child incomplete for the same declared scope; the ancestor inference may not hold.', evidence, rows))
    for child_area, ancestor_area in AREAS:
        terms = {'child': (ECO + child_area + 'Value', ECO + child_area + 'Unit'),
                 'ancestor': (ECO + ancestor_area + 'Value', ECO + ancestor_area + 'Unit')}
        c_value, c_unit = (c_values.get(term, '') for term in terms['child'])
        a_value, a_unit = (a_values.get(term, '') for term in terms['ancestor'])
        if not c_value or not a_value:
            continue
        evidence = {'child': {terms['child'][0]: c_value, terms['child'][1]: c_unit},
                    'ancestor': {terms['ancestor'][0]: a_value, terms['ancestor'][1]: a_unit}}
        kind = f'area:{child_area}>{ancestor_area}'
        if not (valid_value(DIRECT[terms['child'][0]], c_value) and
                valid_value(DIRECT[terms['ancestor'][0]], a_value)) or not c_unit or c_unit != a_unit:
            checks.append(_check(child, parent, ancestor, kind, 'incomparable',
                'Values are invalid, or units are missing or not identical strings; no conversion is applied.', evidence, rows))
        elif Decimal(c_value) > Decimal(a_value):
            checks.append(_check(child, parent, ancestor, kind, 'conflict-signal',
                'The child area exceeds the ancestor area in the same supplied unit.', evidence, rows))
        else:
            checks.append(_check(child, parent, ancestor, kind, 'compatible', 'No area conflict in the same supplied unit.', evidence, rows))
    return checks


def audit_hierarchy(nodes, links, event_values, surveys):
    """Advisory consistency checks for every resolved child -> parent link."""
    values = {position: dict(event_values.get(position, {})) for position in range(len(nodes))}
    date_anc = _nearest(links, lambda node: bool(values[node].get(DWC + 'eventDate')))
    dates = {node: parse_event_date(value.get(DWC + 'eventDate', '')) for node, value in values.items()}
    date_bounds = _temporal_bounds(links, dates)
    place_anc = _nearest(links, lambda node: bool(values[node].get(DWC + 'decimalLatitude') or values[node].get(DWC + 'decimalLongitude')))
    survey_anc = _nearest(links, lambda node: bool(surveys.get(node)))
    checks = []
    for child in sorted(links):
        parent = links[child]
        status, reason, evidence = _temporal(values, child, date_anc[child])
        checks.append(_check(child, parent, date_anc[child], 'temporal', status, reason, evidence))
        if status != 'contradiction' and dates[child]:
            low, high = date_bounds[child]
            offender = (low[1] if low and dates[child][1] < low[0] else
                        high[1] if high and dates[child][2] > high[0] else None)
            if offender is not None:
                checks.append(_check(child, parent, offender, 'temporal-ancestor', 'contradiction',
                    'As supplied in eventDate, the child cannot lie within a more distant ancestor. '
                    'Check whether that ancestor date records only a start.',
                    {'child': values[child][DWC + 'eventDate'], 'ancestor': values[offender][DWC + 'eventDate']}))
        status, reason, evidence = _spatial(values, child, place_anc[child])
        checks.append(_check(child, parent, place_anc[child], 'spatial', status, reason, evidence))
        if survey_anc[child] is not None:
            checks.extend(_survey_checks(child, parent, survey_anc[child], surveys.get(child, []), surveys[survey_anc[child]]))
    counts = {status: sum(check['status'] == status for check in checks) for status in STATUSES}
    # Findings call attention to source claims; they do not require a conversion decision.
    findings = any(counts[status] for status in FINDINGS) or any(check['kind'] == 'survey-ambiguous' for check in checks)
    return {'checks': checks, 'counts': counts, 'has_findings': findings}
