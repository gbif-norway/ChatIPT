"""Archive evidence for option requirements (docs/dwca-conversion/tiered-review.md, step 2a).

Each check mirrors a convert() failure. Requirements are plan-only conditions, so
availability can be re-evaluated after any decision without reloading the archive.
An option is removed only when no permitted choices can make it valid.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from itertools import combinations

from api.dwca_germplasm import G
from api.dwca_humboldt import ECO
from api.dwca_import import DWC, ImportFailure
from api.dwca_legacy import NBN_DATE, TIMES, UTM, emit_legacy_records

EXAMPLES = 5
# Columns sharing one target are evaluated in every combination up to this size; beyond it only alone.
MAX_SHARED = 6
SEQUENCE = 'http://rs.gbif.org/terms/dna_sequence'


def _short(term):
    return term.rsplit('/', 1)[-1]


def _requirement(conditions, reason, evidence=None, when=None):
    return {'conditions': conditions, 'reason': reason, **({'evidence': evidence} if evidence else {}),
            **({'when': when} if when else {})}


def _shown(value):
    return '<withheld as invalid>' if value is None else value


class Preflight:
    def __init__(self, archive, core, columns, issues, row_issues):
        from api import dwca_conversion
        self.conversion = dwca_conversion
        self.archive, self.core = archive, core
        self.ci = archive.tables.index(core)
        self.issues = {issue['id']: issue for issue in [*issues, *row_issues]}
        self.by_table = defaultdict(list)
        for column in columns:
            self.by_table[column['table']].append(column)
        self.requirements = defaultdict(lambda: defaultdict(list))
        self.unavailable = defaultdict(dict)
        self.unavailable_tables = {}  # Extensions no permitted choice can convert: {index: reason}.
        self.core_row = {source_id: n for n, source_id in enumerate(core.ids)}

    # Helpers -----------------------------------------------------------------
    def require(self, decision_id, value, requirement):
        self.requirements[decision_id][value].append(requirement)

    def block(self, decision_id, value, reason, evidence=None):
        self.unavailable[decision_id][value] = {'reason': reason, **({'evidence': evidence} if evidence else {})}

    def options(self, decision_id):
        issue = self.issues.get(decision_id)
        return [option['value'] for option in issue['options']] if issue else []

    def column(self, t, term):
        return next((column for column in self.by_table[t] if column['term'] == term), None)

    @staticmethod
    def targets(column):
        return [option['value'] for option in column['options']]

    def copied(self, t, target, value):
        return self.conversion._copied(self.archive.tables[t].row_type, target, value)

    def family(self, table):
        return self.conversion.SUPPORTED_EXTENSIONS.get(table.row_type)

    def tables(self, family):
        return [t for t, table in enumerate(self.archive.tables) if not table.is_core and self.family(table) == family]

    def preserved_row(self, t, n):
        issue = self.issues.get(f'row:{t}:{n}')
        return bool(issue) and [option['value'] for option in issue['options']] == ['preserve']

    def assignments(self, t, target):
        """Every way the candidate columns can map to target: (columns, conditions selecting exactly them)."""
        sharing = [column for column in self.by_table[t] if target in self.targets(column)]
        sizes = range(1, len(sharing) + 1) if len(sharing) <= MAX_SHARED else (1,)
        for size in sizes:
            for subset in combinations(sharing, size):
                yield list(subset), ([{'type': 'target_in', 'column': column['id'], 'targets': [target]} for column in subset] +
                                     [{'type': 'target_not_in', 'column': column['id'], 'targets': [target]}
                                      for column in sharing if column not in subset])

    def combined(self, t, columns, target, row):
        """convert()'s value for target from these columns: None when every supplied value is withheld."""
        value = None
        for column in sorted(columns, key=lambda item: item['column']):
            copied = self.copied(t, target, row[column['column']])
            if copied is None:
                continue
            if copied and not value:
                value = copied
            elif value is None:
                value = copied
        return value

    def _group_conflicts(self, t, groups, columns, target, fill=None):
        rows = self.archive.tables[t].rows
        bad = []
        for key, members in groups.items():
            values = set()
            for n in members:
                value = self.combined(t, columns, target, rows[n])
                # convert() fills an empty or withheld value before records are compared.
                values.add(fill if fill and not value else value)
            if len(values) > 1:
                bad.append({'group': key, 'values': sorted(map(_shown, values))[:EXAMPLES],
                            'source_rows': [n + 1 for n in members[:EXAMPLES]]})
        return bad

    def mapping_conflicts(self, decision, value, t, groups, prefix, skip, label, when=(), fills=None):
        """Each mapping of a group-level field whose combined values disagree within a group is unavailable."""
        targets = sorted({target for column in self.by_table[t] for target in self.targets(column)
                          if target.startswith(prefix) and target not in skip})
        for target in targets:
            for columns, selected in self.assignments(t, target):
                bad = self._group_conflicts(t, groups, columns, target, (fills or {}).get(target))
                if bad:
                    names = ', '.join(_short(column['term']) for column in columns)
                    self.require(decision, value, _requirement(
                        [{'type': 'unsatisfiable'}],
                        f'{names} values for {target} disagree within {len(bad)} {label} groups; change this mapping or keep '
                        'the column in the originals.',
                        {'groups': len(bad), 'examples': bad[:EXAMPLES]}, when=[*when, *selected]))

    # Checks ------------------------------------------------------------------
    def run(self):
        self.duplicate_targets()
        self.event_grain()
        self.legacy_event_patches()
        self.parent_link()
        for t, table in enumerate(self.archive.tables):
            if f'material:{t}' in self.issues:
                self.material(t)
            if table.is_core:
                continue
            family = self.family(table)
            if family == 'humboldt':
                self.humboldt(t)
            elif family == 'assertion':
                self.assertions(t)
            elif family in {'media', 'eol-media'}:
                self.media(t)
            elif family == 'molecular':
                self.molecular(t)
            elif family == 'occurrence':
                self.occurrence_events(t)
            elif family == 'germplasm-score':
                self.trait_link(t)
            elif family == 'nbn':
                self.nbn_dates(t)
            if family in {'germplasm-accession', 'germplasm-score'}:
                self.germplasm_material(t)
        return self

    def duplicate_targets(self):
        """Two columns copied to one field conflict when both supply different values."""
        skipped = {'preserve', 'join', 'derive', self.conversion.PARENT_LINK}
        for t, columns in self.by_table.items():
            rows = self.archive.tables[t].rows
            by_target = defaultdict(list)
            for column in columns:
                for target in self.targets(column):
                    if target not in skipped:
                        by_target[target].append(column)
            for target, sharing in by_target.items():
                for i, first in enumerate(sharing):
                    for second in sharing[i + 1:]:
                        bad = [n + 1 for n, row in enumerate(rows) if (a := self.copied(t, target, row[first['column']]))
                               and (b := self.copied(t, target, row[second['column']])) and a != b]
                        if not bad:
                            continue
                        evidence = {'rows': len(bad), 'source_rows': bad[:EXAMPLES]}
                        names = [_short(column['term']) for column in (first, second)]
                        if names[0] == names[1]:
                            names = [first['term'], second['term']]
                        for this, other in ((first, second), (second, first)):
                            self.require(this['id'], target, _requirement(
                                [{'type': 'target_not_in', 'column': other['id'], 'targets': [target]}],
                                f'{names[0]} and {names[1]} supply different values for {target} '
                                f'in {len(bad)} rows. Map only one of them to this field.', evidence))

    def event_grain(self):
        if self.core.row_type != DWC + 'Occurrence':
            return
        depth_targets = {'event.' + field for field in self.conversion.DEPTH_FIELDS}
        for value in self.conversion.COMBINED_GRAINS:
            if value not in self.options('event-grain'):
                continue
            identifier = self.column(self.ci, DWC + 'eventID')
            missing = [n + 1 for n, row in enumerate(self.core.rows) if identifier is None or not row[identifier['column']]]
            if missing:
                self.block('event-grain', value, f'{len(missing)} occurrences supply no eventID.', {'source_rows': missing[:EXAMPLES]})
                continue
            self.require('event-grain', value, _requirement(
                [{'type': 'target_in', 'column': identifier['id'], 'targets': ['event.eventID']}],
                'Combining events by eventID requires eventID mapped to the event identifier.'))
            groups = defaultdict(list)
            for n, row in enumerate(self.core.rows):
                groups[row[identifier['column']]].append(n)
            groups = {key: members for key, members in groups.items() if len(members) > 1}
            # Depth children hold their own depth values, so only the combined event's fields must agree.
            split = value == self.conversion.DEPTH_SPLIT
            self.mapping_conflicts('event-grain', value, self.ci, groups, 'event.',
                                   {'event.eventID', *(depth_targets if split else ())}, 'eventID',
                                   fills={'event.eventCategory': 'occurrence'})
            if split:
                depths = [column for column in self.by_table[self.ci] if depth_targets & set(self.targets(column))]
                self.require('event-grain', value, _requirement(
                    [{'type': 'any', 'conditions': [{'type': 'target_in', 'column': column['id'], 'targets': sorted(depth_targets)}
                                                    for column in depths]}],
                    'A child event per depth requires a depth column mapped to an event depth field.'))

    def occurrence_events(self, t):
        """Copying event details from Occurrence rows onto their linked event needs agreement (convert's event patch)."""
        decision = f'occurrence-events:{t}'
        if 'patch' not in self.options(decision):
            return
        table = self.archive.tables[t]
        by_event = defaultdict(list)
        for n, source_id in enumerate(table.ids):
            by_event[source_id].append(n)
        extension = [column for column in self.by_table[t] if column['default'] != 'join']
        sources = [(t, column) for column in extension] + [(self.ci, column) for column in self.by_table[self.ci]]

        def supplied(origin, column, target, event):
            rows = [table.rows[n] for n in by_event[event]] if origin == t else [self.core.rows[self.core_row[event]]]
            return {value for row in rows if (value := self.copied(origin, target, row[column['column']]))}

        def chosen(column, target):
            return {'type': 'target_in', 'column': column['id'], 'targets': [target]}

        for column in extension:
            for target in self.targets(column):
                if not target.startswith('event.'):
                    continue
                name = _short(column['term'])
                bad = [{'event': event, 'values': sorted(values)[:EXAMPLES], 'source_rows': [n + 1 for n in by_event[event][:EXAMPLES]]}
                       for event in by_event if len(values := supplied(t, column, target, event)) > 1]
                if bad:
                    self.require(decision, 'patch', _requirement([{'type': 'unsatisfiable'}],
                        f'{name} differs between occurrences of the same event in {len(bad)} events, so it cannot be copied onto the event.',
                        {'events': len(bad), 'examples': bad[:EXAMPLES]}, when=[chosen(column, target)]))
                for core_column in self.by_table[self.ci]:
                    if target not in self.targets(core_column):
                        continue
                    bad = [{'event': event, 'event_value': sorted(own)[0], 'occurrence_values': sorted(values)[:EXAMPLES]}
                           for event in by_event if (values := supplied(t, column, target, event))
                           and (own := supplied(self.ci, core_column, target, event)) and values != own]
                    if bad:
                        self.require(decision, 'patch', _requirement([{'type': 'unsatisfiable'}],
                            f'{name} on occurrence rows disagrees with the linked event in {self.core.name} for {len(bad)} events.',
                            {'events': len(bad), 'examples': bad[:EXAMPLES]}, when=[chosen(column, target), chosen(core_column, target)]))
        # Another Occurrence extension copying onto the same events must agree with this one.
        for other in self.tables('occurrence'):
            if other == t or 'patch' not in self.options(f'occurrence-events:{other}'):
                continue
            rows = self.archive.tables[other].rows
            theirs_by_event = defaultdict(list)
            for n, source_id in enumerate(self.archive.tables[other].ids):
                theirs_by_event[source_id].append(n)
            for column in extension:
                for target in self.targets(column):
                    if not target.startswith('event.'):
                        continue
                    for other_column in self.by_table[other]:
                        if other_column['default'] == 'join' or target not in self.targets(other_column):
                            continue
                        bad = [event for event in by_event if event in theirs_by_event
                               and (mine := supplied(t, column, target, event))
                               and (theirs := {value for n in theirs_by_event[event]
                                               if (value := self.copied(other, target, rows[n][other_column['column']]))})
                               and mine != theirs]
                        if bad:
                            self.require(decision, 'patch', _requirement([{'type': 'unsatisfiable'}],
                                f'{_short(column["term"])} disagrees with {self.archive.tables[other].name} for {len(bad)} events, '
                                'so both cannot be copied onto the same events.', {'events': len(bad), 'examples': bad[:EXAMPLES]},
                                when=[chosen(column, target), chosen(other_column, target),
                                      {'type': 'decision_in', 'id': f'occurrence-events:{other}', 'values': ['patch']}]))
            # A year copied by one extension must also fit an eventDate copied by the other.
            for year_column in extension:
                if 'event.year' not in self.targets(year_column):
                    continue
                for date_column in self.by_table[other]:
                    if date_column['default'] == 'join' or 'event.eventDate' not in self.targets(date_column):
                        continue
                    bad = [event for event in by_event if event in theirs_by_event
                           and any(self.conversion._year_disagrees(year, date)
                                   for year in supplied(t, year_column, 'event.year', event)
                                   for n in theirs_by_event[event]
                                   if (date := self.copied(other, 'event.eventDate', rows[n][date_column['column']])))]
                    if bad:
                        condition = [chosen(year_column, 'event.year'), chosen(date_column, 'event.eventDate'),
                                     {'type': 'decision_in', 'id': f'occurrence-events:{other}', 'values': ['patch']}]
                        reason = (f'The year disagrees with the eventDate in {self.archive.tables[other].name} for {len(bad)} events, '
                                  'so both cannot be copied onto the same events.')
                        self.require(decision, 'patch', _requirement([{'type': 'unsatisfiable'}], reason,
                                                                     {'events': len(bad), 'examples': bad[:EXAMPLES]}, when=condition))
                        # The other extension sees the same clash from its side.
                        self.require(f'occurrence-events:{other}', 'patch', _requirement(
                            [{'type': 'unsatisfiable'}], reason, {'events': len(bad), 'examples': bad[:EXAMPLES]},
                            when=[chosen(year_column, 'event.year'), chosen(date_column, 'event.eventDate'),
                                  {'type': 'decision_in', 'id': decision, 'values': ['patch']}]))
        # An event without a supplied category gets the missing-category choice before details are copied.
        category = next((column for column in self.by_table[self.ci] if 'event.eventCategory' in self.targets(column)), None)
        for column in extension:
            if 'event.eventCategory' not in self.targets(column):
                continue
            for core_mapped in ([True, False] if category else [False]):
                filled = [event for event in by_event if not (core_mapped and supplied(self.ci, category, 'event.eventCategory', event))]
                values = sorted({value for event in filled for value in supplied(t, column, 'event.eventCategory', event)})
                if not values:
                    continue
                when = [chosen(column, 'event.eventCategory')] + ([] if not category else [
                    chosen(category, 'event.eventCategory') if core_mapped else
                    {'type': 'target_not_in', 'column': category['id'], 'targets': ['event.eventCategory']}])
                self.require(decision, 'patch', _requirement(
                    [{'type': 'decision_in', 'id': 'event-category', 'values': values}] if len(values) == 1 else [{'type': 'unsatisfiable'}],
                    f'Occurrence rows give the event category {" or ".join(values)} for events without one; the missing-category choice must agree.',
                    {'events': len(filled), 'categories': values}, when=when))
        # A year must agree with the eventDate it ends up beside, wherever each comes from.
        years = [(origin, column) for origin, column in sources if 'event.year' in self.targets(column)]
        dates = [(origin, column) for origin, column in sources if 'event.eventDate' in self.targets(column)]
        for year_origin, year_column in years:
            for date_origin, date_column in dates:
                if t not in {year_origin, date_origin}:
                    continue
                bad = [event for event in by_event
                       if any(self.conversion._year_disagrees(year, date)
                              for year in supplied(year_origin, year_column, 'event.year', event)
                              for date in supplied(date_origin, date_column, 'event.eventDate', event))]
                if bad:
                    self.require(decision, 'patch', _requirement([{'type': 'unsatisfiable'}],
                        f'The year disagrees with the eventDate for {len(bad)} events.', {'events': len(bad), 'examples': bad[:EXAMPLES]},
                        when=[chosen(year_column, 'event.year'), chosen(date_column, 'event.eventDate')]))
        # A coordinate pair must not be assembled from the occurrence rows and the event.
        pair = ('event.decimalLatitude', 'event.decimalLongitude')
        coordinates = {origin: [(column, target) for column in (extension if origin == t else self.by_table[self.ci])
                                for target in pair if target in self.targets(column)] for origin in (t, self.ci)}
        mixed = []
        for event in by_event:
            given = {origin: {target for column, target in coordinates[origin] if supplied(origin, column, target, event)} for origin in (t, self.ci)}
            if given[t] and given[self.ci] and not (given[t] <= given[self.ci] or given[self.ci] <= given[t]):
                mixed.append(event)
        if mixed:
            self.require(decision, 'patch', _requirement([{'type': 'unsatisfiable'}],
                f'For {len(mixed)} events, the occurrence rows and the event each supply only part of the coordinate pair.',
                {'events': len(mixed), 'examples': mixed[:EXAMPLES]},
                when=[{'type': 'any', 'conditions': [chosen(column, target) for column, target in coordinates[origin]]} for origin in (t, self.ci)]))

    def parent_link(self):
        if self.core.row_type != DWC + 'Occurrence':
            return
        column = self.column(self.ci, self.conversion.PARENT)
        if column and self.conversion.PARENT_LINK in self.targets(column):
            self.require(column['id'], self.conversion.PARENT_LINK, _requirement(
                [{'type': 'decision_in', 'id': 'event-grain', 'values': list(self.conversion.COMBINED_GRAINS)}],
                'Parent links on an Occurrence core require events combined by supplied eventID.'))

    def material(self, t):
        decision = f'material:{t}'
        if 'by_id' not in self.options(decision):
            return
        table = self.archive.tables[t]
        identifier_target = 'material.materialEntityID'
        identifiers = [column for column in self.by_table[t] if identifier_target in self.targets(column)]
        event_column = self.column(t, DWC + 'eventID') if table.is_core else None
        can_combine_events = 'by_id' in self.options('event-grain')
        valid, reasons = 0, []
        for columns, selected in self.assignments(t, identifier_target):
            names = ', '.join(_short(column['term']) for column in columns)
            values = [self.combined(t, columns, identifier_target, row) for row in table.rows]
            missing = [n + 1 for n, value in enumerate(values) if not value]
            groups = defaultdict(list)
            for n, value in enumerate(values):
                groups[value].append(n)
            groups = {key: members for key, members in groups.items() if len(members) > 1}
            if missing:
                reason = f'With {names} as the identifier, {len(missing)} rows have no material identifier (e.g. {missing[:EXAMPLES]}).'
            elif table.is_core:
                spans = [key for key, members in groups.items() if event_column is None or
                         len({table.rows[n][event_column['column']] for n in members}) > 1]
                reason = (f'{len(spans) or len(groups)} {names} values occur in more than one collection event '
                          f'(e.g. {(spans or list(groups))[:EXAMPLES]}).') if spans or (groups and not can_combine_events) else None
            else:
                spans = [key for key, members in groups.items() if len({table.ids[n] for n in members}) > 1]
                reason = f'{len(spans)} {names} values occur in more than one core event (e.g. {spans[:EXAMPLES]}).' if spans else None
            if reason:
                reasons.append(reason)
                self.require(decision, 'by_id', _requirement([{'type': 'unsatisfiable'}], reason, when=selected))
                continue
            valid += 1
            if table.is_core and groups:
                # Depth children are separate collection events, so they suffice only when no identifier spans depths.
                within_depth = all(len({self.conversion._depth_key(table, table.rows[n]) for n in members}) == 1
                                   for members in groups.values())
                self.require(decision, 'by_id', _requirement(
                    [{'type': 'decision_in', 'id': 'event-grain',
                      'values': ['by_id', *([self.conversion.DEPTH_SPLIT] if within_depth else [])]}],
                    'Rows sharing a material identifier must share one combined event'
                    + ('.' if within_depth else ' and depth.'), when=selected))
            self.mapping_conflicts(decision, 'by_id', t, groups, 'material.', {identifier_target}, 'material identifier', when=selected)
        if not valid:
            self.block(decision, 'by_id', ' '.join(reasons) or 'No column supplies a material identifier.')
            return
        self.require(decision, 'by_id', _requirement(
            [{'type': 'any', 'conditions': [{'type': 'target_in', 'column': column['id'], 'targets': [identifier_target]}
                                            for column in identifiers]}],
            'Combining material requires a material identifier mapped to materialEntityID.'))

    def humboldt(self, t):
        table = self.archive.tables[t]
        decision = f'table:{t}'
        roles = self.options(decision)
        signature = [column['column'] for column in self.by_table[t] if column['default'] != 'join']
        core_event = None
        identifier = self.column(self.ci, DWC + 'eventID')
        if identifier is not None:
            core_event = {source_id: self.core.rows[n][identifier['column']] for n, source_id in enumerate(self.core.ids)}
        if 'humboldt-merge' in roles:
            by_event = defaultdict(set)
            for row, source_id in zip(table.rows, table.ids):
                by_event[source_id].add(tuple(row[c] for c in signature))
            bad = [key for key, values in by_event.items() if len(values) > 1]
            if bad:
                self.block(decision, 'humboldt-merge', f'{len(bad)} events have differing survey rows; only identical rows can be combined.',
                           {'events': len(bad), 'examples': bad[:EXAMPLES]})
        if 'humboldt-grouped' in roles:
            self.require(decision, 'humboldt-grouped', _requirement(
                [{'type': 'decision_in', 'id': 'event-grain', 'values': ['by_id']}],
                'Occurrence-core survey grouping requires events combined by supplied eventID.'))
            expected, attached, signatures = defaultdict(set), defaultdict(set), defaultdict(set)
            for source_id in self.core.ids:
                expected[core_event[source_id]].add(source_id)
            for row, source_id in zip(table.rows, table.ids):
                attached[core_event[source_id]].add(source_id)
                signatures[core_event[source_id]].add(tuple(row[c] for c in signature))
            bad = [key for key in attached if attached[key] != expected[key] or len(signatures[key]) != 1]
            if bad:
                self.block(decision, 'humboldt-grouped', f'{len(bad)} eventIDs lack complete identical survey coverage of their occurrences.',
                           {'events': len(bad), 'examples': bad[:EXAMPLES]})
        survey = self.column(t, ECO + 'surveyID')
        if survey is not None:
            identity = {'humboldt-survey': lambda n: n, 'humboldt-merge': lambda n: table.ids[n],
                        'humboldt-grouped': lambda n: core_event[table.ids[n]]}
            for role in roles:
                if role not in identity or role in self.unavailable.get(decision, {}):
                    continue
                surveys = defaultdict(set)
                for n, row in enumerate(table.rows):
                    if row[survey['column']]:
                        surveys[row[survey['column']]].add(identity[role](n))
                repeated = sorted(value for value, members in surveys.items() if len(members) > 1)
                if repeated:
                    self.require(decision, role, _requirement(
                        [{'type': 'target_not_in', 'column': survey['id'], 'prefixes': ['survey.']}],
                        f'{len(repeated)} surveyID values would identify several separate surveys. Keep surveyID in the '
                        'originals, or combine identical survey rows.', {'surveyIDs': len(repeated), 'examples': repeated[:EXAMPLES]}))
        self.survey_category(t)

    def survey_category(self, t):
        decision = f'hum-category:{t}'
        if decision not in self.issues:
            return
        table = self.archive.tables[t]
        category = self.column(self.ci, DWC + 'eventCategory')
        supplied = [self.core.rows[self.core_row[source_id]][category['column']] if category else '' for source_id in table.ids]
        mapped = ([{'type': 'target_in', 'column': category['id'], 'targets': ['event.eventCategory']}] if category else [])
        if self.core.row_type == DWC + 'Event':
            if any(value and value != 'survey' for value in supplied):
                for value in ('require', 'confirm'):
                    self.block(decision, value, 'A supplied non-survey eventCategory cannot be overwritten.')
                return
            fill = {'type': 'decision_in', 'id': 'event-category', 'values': ['survey']}
            if any(not value for value in supplied):
                self.require(decision, 'require', _requirement([fill], 'Some linked events have no category; '
                             'requiring survey events needs survey as the missing-category choice.'))
            if category and 'survey' in supplied:
                for value in ('require', 'confirm'):
                    self.require(decision, value, _requirement([{'type': 'any', 'conditions': [*mapped, fill]}],
                                 'Supplied survey categories must stay mapped to eventCategory.'))
            return
        if any(value != 'survey' for value in supplied):
            self.block(decision, 'require', 'Occurrence-core events are occurrence context events unless every linked row supplies survey.')
        elif mapped:
            self.require(decision, 'require', _requirement(mapped, 'Supplied survey categories must stay mapped to eventCategory.'))
        if any(value and value != 'survey' for value in supplied):
            self.block(decision, 'confirm', 'A supplied non-survey eventCategory cannot be overwritten.')
        elif mapped and 'survey' in supplied:
            self.require(decision, 'confirm', _requirement(mapped, 'Supplied survey categories must stay mapped to eventCategory.'))

    def assertions(self, t):
        table = self.archive.tables[t]
        decision = f'table:{t}'
        explicit_column = self.column(t, DWC + 'occurrenceID')
        if explicit_column is None:
            return
        explicit = [(n, row[explicit_column['column']]) for n, row in enumerate(table.rows) if row[explicit_column['column']]]
        if not explicit:
            return
        roles = self.options(decision)
        if 'occurrence-assertion' in roles:
            core_column = self.column(self.ci, DWC + 'occurrenceID')
            matches = defaultdict(list)
            if core_column is not None:
                for row, source_id in zip(self.core.rows, self.core.ids):
                    if row[core_column['column']]:
                        matches[row[core_column['column']]].append(source_id)
            bad = [n + 1 for n, value in explicit if len(matches[value]) != 1 or matches[value][0] != table.ids[n]]
            if core_column is None or bad:
                self.block(decision, 'occurrence-assertion', 'Supplied assertion occurrenceIDs must identify exactly one '
                           'occurrence, which must be the attached core row.', {'source_rows': bad[:EXAMPLES], 'rows': len(bad)})
            else:
                self.require(decision, 'occurrence-assertion', _requirement(
                    [{'type': 'target_in', 'column': core_column['id'], 'targets': ['occurrence.occurrenceID']}],
                    'Supplied assertion occurrenceIDs resolve through the core occurrenceID, which must stay mapped.'))
        if 'declared-assertions' in roles:
            matches = defaultdict(list)
            columns = {}
            for o in self.tables('occurrence'):
                column = self.column(o, DWC + 'occurrenceID')
                if column is None:
                    continue
                columns[o] = column
                for row, source_id in zip(self.archive.tables[o].rows, self.archive.tables[o].ids):
                    if row[column['column']]:
                        matches[row[column['column']]].append((o, source_id))
            bad, needed = [], set()
            for n, value in explicit:
                found = matches[value]
                if len({o for o, _ in found}) > 1:
                    continue  # Global uniqueness then depends on which tables convert; checked during conversion.
                if len(found) != 1 or found[0][1] != table.ids[n]:
                    bad.append(n + 1)
                else:
                    needed.add(found[0][0])
            if bad:
                self.block(decision, 'declared-assertions', 'Supplied assertion occurrenceIDs must identify exactly one converted '
                           'occurrence in the archive, within the attached event.', {'source_rows': bad[:EXAMPLES], 'rows': len(bad)})
            for o in sorted(needed):
                self.require(decision, 'declared-assertions', _requirement(
                    [{'type': 'decision_in', 'id': f'table:{o}', 'values': ['occurrence']},
                     {'type': 'target_in', 'column': columns[o]['id'], 'targets': ['occurrence.occurrenceID']}],
                    f'Assertion occurrenceIDs resolve to {self.archive.tables[o].name}, which must convert with occurrenceID mapped.'))

    def media(self, t):
        table = self.archive.tables[t]
        decision = f'table:{t}'
        candidates = [(column, [target for target in self.targets(column) if target.startswith('media.')]) for column in self.by_table[t]]
        sets = defaultdict(list)
        for n, row in enumerate(table.rows):
            # A value counts only for the media fields that would copy it (typed fields withhold invalid values).
            supplied = []
            for column, targets in candidates:
                surviving = tuple(target for target in targets if self.copied(t, target, row[column['column']]))
                if surviving:
                    supplied.append((column['id'], surviving))
            sets[tuple(supplied)].append(n)
        for role in self.options(decision):
            if not role.startswith('media-'):
                continue
            for supplied, members in sets.items():
                # A row retained by its own row decision needs no media value.
                retainable = [n for n in members if 'preserve' in self.options(f'row:{t}:{n}')]
                fixed = [n for n in members if n not in retainable]
                mapped = {'type': 'any', 'conditions': [{'type': 'target_in', 'column': identifier, 'targets': list(targets)}
                                                        for identifier, targets in supplied]}
                retained = {'type': 'all', 'conditions': [{'type': 'decision_in', 'id': f'row:{t}:{n}', 'values': ['preserve']}
                                                          for n in retainable]}
                evidence = {'rows': len(members), 'source_rows': [n + 1 for n in members[:EXAMPLES]]}
                if not supplied and fixed:
                    self.block(decision, role, f'{len(fixed)} media rows have no mappable media value.', evidence)
                elif not supplied:
                    self.require(decision, role, _requirement([retained], f'{len(members)} media rows have no mappable media value '
                                 'and must be retained in originals.', evidence))
                elif fixed:
                    self.require(decision, role, _requirement([mapped], f'{len(members)} media rows have values only in columns '
                                 'that must stay mapped to media fields.', evidence))
                else:
                    self.require(decision, role, _requirement([{'type': 'any', 'conditions': [mapped, retained]}],
                                 f'{len(members)} media rows need their media columns mapped, or the rows retained in originals.', evidence))

    def molecular(self, t):
        table = self.archive.tables[t]
        column = self.column(t, SEQUENCE)
        missing = [n + 1 for n, row in enumerate(table.rows) if column is None or not row[column['column']]]
        if missing:
            self.block(f'table:{t}', 'molecular', f'{len(missing)} rows supply no DNA sequence.', {'source_rows': missing[:EXAMPLES]})
        else:
            self.require(f'table:{t}', 'molecular', _requirement(
                [{'type': 'target_in', 'column': column['id'], 'targets': ['nucleotide-sequence.sequence']}],
                'Each analysis needs its DNA sequence mapped.'))

    def trait_link(self, t):
        decision = f'trait-link:{t}'
        if 'exact' not in self.options(decision):
            return
        table = self.archive.tables[t]
        trait = self.column(t, G + 'measurementTraitID')
        matches, columns = defaultdict(lambda: defaultdict(int)), {}
        for o in self.tables('germplasm-trait'):
            column = next((item for item in self.by_table[o] if 'protocol.protocolID' in self.targets(item)), None)
            if column is None:
                continue
            columns[o] = column
            for n, row in enumerate(self.archive.tables[o].rows):
                if row[column['column']] and not self.preserved_row(o, n):
                    matches[row[column['column']]][o] += 1
        supplied = sorted({row[trait['column']] for n, row in enumerate(table.rows) if row[trait['column']] and not self.preserved_row(t, n)})

        def converts(o):
            return [{'type': 'decision_in', 'id': f'table:{o}', 'values': ['germplasm-trait']},
                    {'type': 'target_in', 'column': columns[o]['id'], 'targets': ['protocol.protocolID']}]

        def excluded(o):
            return {'type': 'any', 'conditions': [{'type': 'decision_in', 'id': f'table:{o}', 'values': ['preserve']},
                                                  {'type': 'target_not_in', 'column': columns[o]['id'], 'targets': ['protocol.protocolID']}]}

        alternatives, bad = {}, []
        for value in supplied:
            # Exactly one converted Trait Descriptor row may carry each ID: one table with a single
            # match converts, and every other table carrying the ID does not supply it.
            options = [{'type': 'all', 'conditions': [*converts(o), *(excluded(other) for other in matches[value] if other != o)]}
                       for o, count in sorted(matches[value].items()) if count == 1]
            if not options:
                bad.append(value)
            else:
                condition = options[0] if len(options) == 1 else {'type': 'any', 'conditions': options}
                alternatives[json.dumps(condition, sort_keys=True)] = condition
        if bad or not columns:
            self.block(decision, 'exact', f'{len(bad)} trait IDs do not match exactly one converted Trait Descriptor.',
                       {'examples': bad[:EXAMPLES]})
            return
        self.require(decision, 'exact', _requirement(list(alternatives.values()),
                     'Each score trait ID must match exactly one converted Trait Descriptor.'))

    def germplasm_material(self, t):
        table = self.archive.tables[t]
        decision = f'table:{t}'
        role = 'germplasm-accession' if self.family(table) == 'germplasm-accession' else 'germplasm-score-material'
        if role not in self.options(decision):
            return
        material, target = f'material:{self.ci}', 'material.materialEntityID'
        linked = {self.core_row[source_id] for source_id in table.ids}
        usable = []
        for columns, selected in self.assignments(self.ci, target):
            values = [self.combined(self.ci, columns, target, row) for row in self.core.rows]
            if all(values[n] for n in linked):
                sharing = defaultdict(list)
                for n, value in enumerate(values):
                    sharing[value].append(n)
                usable.append(({'type': 'all', 'conditions': selected}, values, sharing))
        if not usable:
            self.block(decision, role, 'Linked core rows need a supplied material identifier.')
            return
        conditions = [{'type': 'decision_in', 'id': material, 'values': ['per_row', 'by_id']},
                      {'type': 'any', 'conditions': [selected for selected, _, _ in usable]}]
        if role == 'germplasm-accession':
            self.require(decision, role, _requirement(conditions, 'Accessions attach to approved core material with a mapped identifier.'))
            return
        germplasm = self.column(t, G + 'germplasmID')
        accessions = defaultdict(lambda: defaultdict(list))
        for o in self.tables('germplasm-accession'):
            column = next((item for item in self.by_table[o] if 'material-identifier.identifier' in self.targets(item)), None)
            if column:
                for row, source_id in zip(self.archive.tables[o].rows, self.archive.tables[o].ids):
                    if row[column['column']]:
                        accessions[self.core_row[source_id]][row[column['column']]].append(
                            {'type': 'all', 'conditions': [{'type': 'decision_in', 'id': f'table:{o}', 'values': ['germplasm-accession']},
                                                           {'type': 'target_in', 'column': column['id'], 'targets': ['material-identifier.identifier']}]})
        alternatives, bad = {}, []
        for n, (row, source_id) in enumerate(zip(table.rows, table.ids)):
            if self.preserved_row(t, n):
                continue
            value = row[germplasm['column']] if germplasm else ''
            core_n = self.core_row[source_id]
            # A row matches its material's identifier, an accession of that core row, or, when material
            # is combined by identifier, an accession of another core row sharing that material.
            options = list(accessions[core_n].get(value, [])) if value else []
            for selected, values, sharing in usable:
                if not value:
                    continue
                if values[core_n] == value:
                    options.append(selected)
                for other in sharing[values[core_n]]:
                    if other != core_n:
                        options += [{'type': 'all', 'conditions': [selected, {'type': 'decision_in', 'id': material, 'values': ['by_id']}, accession]}
                                    for accession in accessions[other].get(value, [])]
            if not options:
                bad.append(n + 1)
            else:
                condition = options[0] if len(options) == 1 else {'type': 'any', 'conditions': options}
                alternatives[json.dumps(condition, sort_keys=True)] = condition
        if bad:
            self.block(decision, role, 'Score germplasmIDs must exactly match an identifier of their linked core material.',
                       {'source_rows': bad[:EXAMPLES], 'rows': len(bad)})
            return
        self.require(decision, role, _requirement([*conditions, *alternatives.values()],
                                                  'Score germplasmIDs resolve through approved material identifiers.'))

    def legacy_event_patches(self):
        """Extension event values must agree with their event (convert's add_extension conflict and year checks).

        A patch reaches the event of its own core row; with events combined by eventID it reaches the
        event every row of that eventID shares (with depth children, still that combined event).
        """
        patches = []
        for t in [*self.tables('nbn'), *self.tables('bmde')]:
            table = self.archive.tables[t]
            family = self.family(table)
            role = family + '-context'
            if role not in self.options(f'table:{t}'):
                continue
            derived = []
            for group, label in (((NBN_DATE, 'NBN vague date'),) if family == 'nbn' else ((UTM, 'UTM coordinates'), (TIMES, 'observation times'))):
                columns = [column for column in self.by_table[t] if column['term'] in group and 'derive' in self.targets(column)]
                if columns:
                    derived.append((group, f'{table.name} {label}', [{'type': 'target_in', 'column': column['id'], 'targets': ['derive']}
                                                                    for column in columns]))
            mapped = [(column, target) for column in self.by_table[t] for target in self.targets(column) if target.startswith('event.')]
            for n, (row, source_id) in enumerate(zip(table.rows, table.ids)):
                core_n = self.core_row.get(source_id)
                if core_n is None or self.preserved_row(t, n):
                    continue
                issue = self.issues.get(f'row:{t}:{n}')
                rows = [{'type': 'decision_in', 'id': issue['id'], 'values': [option['value'] for option in issue['options']
                                                                              if option['value'] != 'preserve']}] if issue else []
                base = [{'type': 'decision_in', 'id': f'table:{t}', 'values': [role]}]
                for group, label, selected in derived:
                    source = {term: row[table.terms.index(term)] if term in table.terms else '' for term in group}
                    try:
                        records = emit_legacy_records(family, source, {}, 'event', 'event', 'row')
                    except ImportFailure:
                        continue  # Invalid or unsupported values are withheld or rejected separately, never patched.
                    for _, record in records:
                        for field, value in record.items():
                            if field != 'event_pk' and value:
                                patches.append({'field': field, 'value': value, 'label': label, 'core': core_n,
                                                'when': base + selected, 'rows': rows, 'source': (table.name, n + 1)})
                for column, target in mapped:
                    value = self.copied(t, target, row[column['column']])
                    if value:
                        patches.append({'field': target.split('.', 1)[1], 'value': value, 'label': f"{table.name} {_short(column['term'])}",
                                        'core': core_n, 'when': base + [{'type': 'target_in', 'column': column['id'], 'targets': [target]}],
                                        'rows': rows, 'source': (table.name, n + 1)})
        if not patches:
            return
        conflicts = defaultdict(list)  # (labels, when) -> [(row conditions, evidence)]

        def conflict(first, second, when, rows, evidence):
            key = (first, second, json.dumps(when, sort_keys=True))
            conflicts[key].append((rows, evidence))

        core_columns = defaultdict(list)
        for column in self.by_table[self.ci]:
            for target in self.targets(column):
                if target.startswith('event.'):
                    core_columns[target.split('.', 1)[1]].append(column)
        for patch in patches:
            row = self.core.rows[patch['core']]
            checks = [(column, 'event.' + patch['field'], self.copied(self.ci, 'event.' + patch['field'], row[column['column']]))
                      for column in core_columns[patch['field']]]
            if patch['field'] == 'eventDate':
                checks += [(column, 'event.year', self.copied(self.ci, 'event.year', row[column['column']])) for column in core_columns['year']]
            for column, target, value in checks:
                if not value:
                    continue
                if target == 'event.year':
                    bad = not re.fullmatch(r'-?\d{1,4}', value) or self.conversion._year_disagrees(value, patch['value'])
                else:
                    bad = value != patch['value']
                if bad:
                    conflict(patch['label'], f"{self.core.name} {_short(column['term'])}",
                             patch['when'] + [{'type': 'target_in', 'column': column['id'], 'targets': [target]}], patch['rows'],
                             {'field': target, 'values': [patch['value'], value], 'source_rows': [patch['source'], (self.core.name, patch['core'] + 1)]})
        identifier = self.column(self.ci, DWC + 'eventID') if self.core.row_type == DWC + 'Occurrence' else None
        grains = [value for value in self.conversion.COMBINED_GRAINS if value in self.options('event-grain')]
        buckets = defaultdict(list)
        for patch in patches:
            buckets[(patch['field'], 'row', patch['core'])].append(patch)
            event_id = self.core.rows[patch['core']][identifier['column']] if identifier else ''
            if event_id and grains:
                buckets[(patch['field'], 'id', event_id)].append(patch)
        for (field, scope, _), members in buckets.items():
            for first, second in combinations(members, 2):
                if first['value'] == second['value'] or (scope == 'id' and first['core'] == second['core']):
                    continue
                grain = [{'type': 'decision_in', 'id': 'event-grain', 'values': grains}] if scope == 'id' else []
                when = [*first['when'], *(condition for condition in second['when'] if condition not in first['when']), *grain]
                conflict(first['label'], second['label'], when, first['rows'] + second['rows'],
                         {'field': 'event.' + field, 'values': [first['value'], second['value']], 'source_rows': [first['source'], second['source']]})
        for (first, second, when), found in conflicts.items():
            when = json.loads(when)
            rows = [] if any(not conditions for conditions, _ in found) else [
                {'type': 'any', 'conditions': [{'type': 'all', 'conditions': conditions} for conditions in
                                               {json.dumps(conditions, sort_keys=True): conditions for conditions, _ in found}.values()]}]
            field = found[0][1]['field']
            reason = (f'{first} and {second} supply different {field} values for the same event ({len(found)} case{"s" if len(found) != 1 else ""}). '
                      'Keep one of them in the originals' + (', keep events separate' if any(c.get('id') == 'event-grain' for c in when) else '')
                      + ', or correct the source.')
            evidence = {'conflicts': len(found), 'examples': [item for _, item in found[:EXAMPLES]]}
            # The requirement sits on every choice that applies it, conditioned on the others.
            for condition in when:
                placements = ([(condition['id'], value) for value in condition['values']] if condition['type'] == 'decision_in'
                              else [(condition['column'], target) for target in condition['targets']])
                for decision, value in placements:
                    others = [other for other in when if other.get('id', other.get('column')) != decision]
                    self.require(decision, value, _requirement([{'type': 'unsatisfiable'}], reason, evidence, when=others + rows))

    def nbn_dates(self, t):
        present = [column for column in self.by_table[t] if column['term'] in NBN_DATE and 'derive' in self.targets(column)]
        for column in present:
            others = [other for other in present if other is not column]
            if not others:
                continue
            self.require(column['id'], 'derive', _requirement(
                [{'type': 'target_in', 'column': other['id'], 'targets': ['derive']} for other in others],
                'NBN vague date columns must be approved together.'))
            self.require(column['id'], 'preserve', _requirement(
                [{'type': 'target_not_in', 'column': other['id'], 'targets': ['derive']} for other in others],
                'NBN vague date columns must be retained together.'))


def preflight(archive, core, columns, issues, row_issues):
    return Preflight(archive, core, columns, issues, row_issues).run()
