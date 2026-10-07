"""Compact value coverage and output provenance for DwC-A conversion reports.

This module deliberately consumes the conversion plan/report instead of trying
to reinterpret source values. Counts are source nonempty-cell counts unless a
row-level withheld record provides stronger evidence.
"""
from __future__ import annotations

from collections import Counter


def build_value_disposition_ledger(plan, report, resources=None):
    """Return a deterministic, per-source-term disposition ledger.

    ``resources`` may be the emitted resource mapping returned by ``convert``
    (resource name -> DataFrame/list of rows). Without it, output-field
    provenance is limited to targets declared by the mapping report.

    Value counts use source nonempty cells. The flagged count is an annotation
    over emitted cells, not an additional mutually exclusive category. A join
    without per-cell consumption evidence is explicitly left unverified.
    """
    plan_columns = plan.get('columns', [])
    report_columns = report.get('columns', [])
    tidy = report.get('tidy') or {}
    warnings = {item.get('id') for item in [*plan.get('warnings', []), *report.get('warnings', [])]}
    withheld = Counter()
    for item in report.get('withheld_values', []):
        key = (item.get('source_table_index'), item.get('term'))
        withheld[key] += 1

    summaries = {(item.get('source_table'), item.get('term')): item for item in report_columns}
    output_fields = Counter()
    traced_resources = {}
    for entry in report.get('row_crosswalk', []):
        source_index = entry.get('source_table_index')
        if source_index is not None and entry.get('target_table'):
            traced_resources.setdefault(source_index, set()).add(entry['target_table'])
    result = []
    for column in sorted(plan_columns, key=lambda c: (c.get('table', -1), c.get('column', -1), c.get('term', ''))):
        table_index = column.get('table')
        table_name = next((t.get('name') for t in plan.get('tables', []) if t.get('index') == table_index), None)
        # Plan table profiles historically are positional and may omit index.
        if table_name is None and isinstance(table_index, int) and 0 <= table_index < len(plan.get('tables', [])):
            table_name = plan['tables'][table_index].get('name')
        summary = summaries.get((table_name, column.get('term')), {})
        total = max(0, int(column.get('nonempty', 0) or 0))
        tidy_groups = [group for group in tidy.get('groups', [])
                       if group.get('table') == table_index and group.get('column') == column.get('column')]
        tidied_values = sum(value.get('changed_rows', 0) for group in tidy_groups if group.get('applied')
                            for value in group.get('values', []) if value.get('applied')
                            and value.get('fields', {}).get(group.get('field', '')) != '')
        tidy_cleared = sum(value.get('changed_rows', 0) for group in tidy_groups if group.get('applied')
                           for value in group.get('values', []) if value.get('applied')
                           and value.get('fields', {}).get(group.get('field', '')) == '')
        withheld_count = min(total, withheld[(table_index, column.get('term'))])
        decision = report.get('effective_decisions', {}).get(column.get('id'), column.get('default', 'preserve'))
        disposition = summary.get('disposition', '')
        target = summary.get('target', decision)
        derived = disposition == 'derived' or str(target).startswith('derived ')
        review = bool(column.get('review') and column.get('id') not in report.get('effective_decisions', {}))
        available = max(0, total - withheld_count)
        mapped_count = derived_count = originals_count = unknown_count = 0
        if decision == 'join':
            # Join columns do not become values in output fields. Without a
            # cell-level consumption record, don't assert either mapping or loss.
            unknown_count = available
        elif disposition == 'retained-unmapped' or (decision == 'preserve' and target == 'preserve'):
            originals_count = available
        elif summary.get('mapped_rows') is not None:
            consumed = min(available, max(0, int(summary.get('mapped_rows', 0) or 0)))
            if derived:
                derived_count = consumed
            else:
                mapped_count = consumed
            originals_count = available - consumed
        elif disposition in {'mapped+retained', 'derived'}:
            if derived:
                derived_count = available
            else:
                mapped_count = available
        elif decision == 'preserve':
            originals_count = available
        elif decision not in {'preserve', 'join'} and isinstance(target, str) and '.' in target:
            mapped_count = available
        else:
            unknown_count = available

        emitted_count = min(available, mapped_count + derived_count)
        emitted_flagged = emitted_count if column.get('id') in warnings else 0
        status = ('withheld-invalid' if withheld_count == total and total else
                  'needs-review' if review else
                  'originals-only' if originals_count == total and total else
                  'unverified' if unknown_count else
                  'transformed/derived' if derived_count or (column.get('tidy_added') and mapped_count) else
                  'mapped' if mapped_count else 'mixed')
        targets = []
        target_counts = summary.get('target_counts') or {}
        mapped_examples = summary.get('derived_value_examples') or summary.get('mapped_values') or []
        if target_counts:
            targets = sorted(target_counts)
        elif mapped_examples:
            for example in mapped_examples[:1]:
                target_table = example.get('target_table')
                for field in example.get('target_fields', {}):
                    candidate = f'{target_table}.{field}'
                    if candidate not in targets:
                        targets.append(candidate)
        elif isinstance(target, str):
            for part in target.split(' → '):
                if '.' in part and not part.startswith('http'):
                    candidate = part.strip()
                    if candidate not in targets:
                        targets.append(candidate)
        for candidate in targets:
            output_fields[candidate] += target_counts.get(candidate, emitted_count)
        output_resource_names = sorted(traced_resources.get(table_index, ()))
        entry = {
            'source_table_index': table_index,
            'source_table': table_name,
            'source_term': column.get('term'),
            'source_column': column.get('column'),
            'nonempty_values': total,
            'mapped_values': mapped_count,
            'derived_values': derived_count,
            'withheld_invalid_values': withheld_count,
            'emitted_but_flagged_values': emitted_flagged,
            'originals_only_values': originals_count,
            # Missing-value tokens (NA, null, ...) left empty in identifier fields; part of originals_only_values.
            'empty_placeholder_values': min(originals_count, int(summary.get('empty_placeholder', 0) or 0)),
            'unverified_values': unknown_count,
            'status': status,
            'needs_review': review,
            'output_fields': targets,
            'source_table_output_resources': output_resource_names,
            'count_basis': 'source nonempty cells; mapped row counts where reported; withheld values counted individually; warning flags annotate emitted values and overlap mapped/derived counts',
        }
        if tidy:
            entry.update(tidied_values=tidied_values, tidy_cleared_values=tidy_cleared,
                         source_nonempty_values=total + tidy_cleared)
            if column.get('tidy_added'):
                entry['tidy_added'] = True
            entry['count_basis'] += '; tidy counts are changed source cells, with cleared values added back to source nonempty counts'
        result.append(entry)

    ledger = {'source_terms': result, 'output_fields': [
        {'field': field, 'source_term_count': count}
        for field, count in sorted(output_fields.items())
    ]}
    if resources is not None:
        emitted = {}
        generated_fields = Counter()
        for name, rows in sorted(resources.items()):
            try:
                emitted[name] = len(rows)
            except TypeError:
                emitted[name] = 0
            if hasattr(rows, 'columns'):
                fields = list(rows.columns)
                for field in fields:
                    try:
                        series = rows[field]
                        count = int((series.notna() & series.astype(str).str.len().gt(0)).sum())
                    except (AttributeError, TypeError, ValueError):
                        count = 0
                    qualified = f'{name}.{field}'
                    if qualified not in output_fields:
                        generated_fields[qualified] += count
            else:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    for field, value in row.items():
                        qualified = f'{name}.{field}'
                        if value not in (None, '') and qualified not in output_fields:
                            generated_fields[qualified] += 1
        ledger['emitted_resources'] = emitted
        ledger['untraced_or_generated_output_fields'] = [
            {'field': field, 'nonempty_output_values': count}
            for field, count in sorted(generated_fields.items())
        ]
    return ledger
