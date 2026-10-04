"""Lossless checklist packaging and reviewed extraction of actual occurrences.

Taxonomy tables are additional Frictionless resources, never canonical DwC-DP
taxon tables. A standalone checklist uses the generic Data Package profile.
"""
import hashlib
import json
import re
from collections import defaultdict

import pandas as pd

from api.dwca_import import DWC, SourceArchive, SourceTable
from api.dwc_dp_specs import TABLE_SPECS, dwc_dp_schema_snapshot, validate_dwc_dp_resources


def resource_name(table, index):
    return 'taxonomy-taxon' if table.is_core else f'taxonomy-extension-{index}'


def field_names(table):
    # Retain distinct source columns even if loose header aliases share a term.
    return [term if table.terms.count(term) == 1 else f'source_column_{index + 1}'
            for index, term in enumerate(table.terms)]


def taxonomy_tables(archive, report=None):
    from api.dwca_conversion import _key
    core = next(table for table in archive.tables if table.is_core)
    tables = {}
    for t, table in enumerate(archive.tables):
        name = resource_name(table, t)
        names = field_names(table)
        key = 'archive_join_id' if table.is_core else 'source_row_id'
        rows = [[source_id, *row] if table.is_core else
                [_key(archive, 'taxonomy-row', t, n), table.ids[n] if table.ids else '', *row]
                for n, (source_id, row) in enumerate(zip(table.ids if table.is_core else range(len(table.rows)), table.rows))]
        prefix = [key] if table.is_core else [key, 'archive_taxon_id']
        fields = [{'name': column, 'type': 'string', 'title': column.replace('_', ' '),
                   'description': 'Source archive join identifier; a row identity is generated for a loose core without taxonID.' if column.startswith('archive_') else 'Generated source-row identity.'}
                  for column in prefix]
        fields[0]['constraints'] = {'required': True, 'unique': True}
        fields += [{'name': column, 'title': term.rsplit('/', 1)[-1], 'type': 'string',
                    'description': 'Source value copied verbatim; no taxonomic interpretation applied.',
                    **({'dcterms:isVersionOf': term} if term.startswith(('http://', 'https://')) else {}),
                    'sourceTerm': term} for column, term in zip(names, table.terms)]
        schema = {'fields': fields, 'primaryKey': key, 'missingValues': ['']}
        if not table.is_core and table.ids:
            schema['foreignKeys'] = [{'fields': 'archive_taxon_id', 'reference': {
                'resource': 'taxonomy-taxon', 'fields': 'archive_join_id'}}]
        if table.is_core and DWC + 'taxonID' in table.terms:
            ids = [row[table.terms.index(DWC + 'taxonID')] for row in table.rows]
            id_set = set(ids)
            if all(ids) and len(id_set) == len(ids):
                for term in (DWC + 'parentNameUsageID', DWC + 'acceptedNameUsageID', DWC + 'originalNameUsageID'):
                    if term in table.terms and all(not row[table.terms.index(term)] or row[table.terms.index(term)] in id_set for row in table.rows):
                        schema.setdefault('foreignKeys', []).append({'fields': names[table.terms.index(term)],
                            'reference': {'resource': '', 'fields': names[table.terms.index(DWC + 'taxonID')]}})
        tables[name] = {'dataframe': pd.DataFrame(rows, columns=[*prefix, *names]), 'schema': schema,
                        'title': table.name, 'description': 'Additional taxonomy resource; not a standard DwC-DP table.',
                        'sourceRowType': table.row_type, 'sourceTable': table.name, 'sourceJoinBasis': table.join_basis}
    links = (report or {}).get('taxonomy_occurrence_links', [])
    if links:
        tables['taxonomy-occurrence-links'] = {
            'dataframe': pd.DataFrame(links),
            'schema': {'fields': [{'name': field, 'type': 'string'} for field in links[0]],
                       'missingValues': [''], 'primaryKey': 'occurrence_fk', 'foreignKeys': [
                           {'fields': 'archive_taxon_id', 'reference': {'resource': 'taxonomy-taxon', 'fields': 'archive_join_id'}},
                           {'fields': 'occurrence_fk', 'reference': {'resource': 'occurrence', 'fields': 'occurrence_pk'}},
                       ]},
            'description': 'Explicit source Taxon-to-Occurrence attachments; no synonym or parent-name inference.',
        }
    return tables


def taxonomy_link_checks(archive):
    core = next(table for table in archive.tables if table.is_core)
    if DWC + 'taxonID' not in core.terms:
        return {'identifier_present': False, 'relationships': []}
    counts = defaultdict(int)
    for row in core.rows:
        if row[core.terms.index(DWC + 'taxonID')]: counts[row[core.terms.index(DWC + 'taxonID')]] += 1
    checks = []
    for term in (DWC + 'parentNameUsageID', DWC + 'acceptedNameUsageID', DWC + 'originalNameUsageID'):
        if term not in core.terms: continue
        values = [row[core.terms.index(term)] for row in core.rows if row[core.terms.index(term)]]
        checks.append({'term': term, 'nonempty': len(values),
                       'local_matches': sum(counts[value] == 1 for value in values),
                       'ambiguous_local_matches': sum(counts[value] > 1 for value in values),
                       'external_or_unresolved': sum(not counts[value] for value in values)})
    return {'identifier_present': True, 'duplicate_identifiers': sum(count > 1 for count in counts.values()),
            'relationships': checks}


def occurrence_proxy(archive, index):
    """Fill missing classification fields from the exact attached core row."""
    core = next(table for table in archive.tables if table.is_core)
    table = archive.tables[index]
    classification = {field.get('dcterms:isVersionOf') for name in ('occurrence', 'identification')
                      for field in TABLE_SPECS[name].schema['fields']
                      if field.get('name') in {'taxonID', 'scientificName', 'scientificNameID', 'scientificNameAuthorship',
                          'taxonRank', 'kingdom', 'phylum', 'class', 'order', 'family', 'genus', 'subgenus',
                          'specificEpithet', 'infraspecificEpithet', 'vernacularName', 'taxonRemarks', 'nameAccordingTo'}}
    terms = [*table.terms, *(term for term in core.terms if term in classification and term not in table.terms)]
    linked = {identifier: n for n, identifier in enumerate(core.ids)}
    rows, sources, conflicts = [], [], []
    for n, row in enumerate(table.rows):
        core_n = linked[table.ids[n]]
        context = dict(zip(core.terms, core.rows[core_n]))
        own = dict(zip(table.terms, row))
        conflicting = {term for term in classification if own.get(term) and context.get(term) and own[term] != context[term]}
        rows.append([own.get(term, '') or (context.get(term, '') if term in classification and not conflicting else '') for term in terms])
        sources.append({**table.row_sources[n], 'linked_taxon_file': core.row_sources[core_n]['file'],
                        'linked_taxon_data_record': core.row_sources[core_n]['data_record']})
        conflicts.extend({'source_table': table.name, 'source_row': n + 1, 'term': term,
                          'occurrence_value': own[term], 'taxon_value': context[term]}
                         for term in sorted(conflicting))
    proxy = SourceTable(table.name, DWC + 'Occurrence', terms, rows,
                        [f'taxon-occurrence:{index}:{n}' for n in range(len(rows))],
                        is_core=True, row_sources=sources, join_basis=table.join_basis)
    return SourceArchive(archive.files, [proxy], archive.fingerprint, archive.has_meta, archive.uploaded_files), conflicts


def build_taxon_plan(archive):
    from api.dwca_conversion import RULE_VERSION, PRESERVE, _issue, build_plan
    taxonomy_preserve = {**PRESERVE, 'label': 'Keep in taxonomy tables and original files only'}
    issues = [_issue('taxonomy-package', 'Preserve the checklist as taxonomy tables',
        'The target has no standalone Taxon table. Checklist fields and extension links remain verbatim in additional taxonomy tables. A checklist alone produces a generic taxonomy Data Package, not a standard DwC-DP.',
        [{'value': 'confirm', 'label': 'Include the full checklist and its extensions as taxonomy tables'}])]
    if not archive.has_meta:
        issues.append(_issue('loose-links', 'Confirm the Taxon-core loose-file layout',
            'Known filenames identify roles. Extensions join by supplied taxonID, never by row position.',
            [{'value': 'confirm', 'label': 'Confirm these table roles and taxonID joins'}]))
    columns, profiles, nested, conflicts, automatic, warnings = [], [], {}, [], [], []
    for t, table in enumerate(archive.tables):
        profile = {'name': table.name, 'row_type': table.row_type, 'core': table.is_core,
                   'rows': len(table.rows), 'unique_join_ids': len(set(table.ids)), 'join_basis': table.join_basis, 'columns': []}
        for c, (term, field) in enumerate(zip(table.terms, field_names(table))):
            values = [row[c] for row in table.rows if row[c]]
            target = resource_name(table, t) + '.' + field
            column = {'id': f'column:{t}:{c}', 'table': t, 'column': c, 'term': term,
                      'default': target, 'review': False, 'options': [{'value': target, 'label': 'Copy verbatim into taxonomy table'}],
                      'nonempty': len(values), 'distinct': len(set(values)), 'samples': [value[:500] for value in list(dict.fromkeys(values))[:3]]}
            columns.append(column); profile['columns'].append({key: column[key] for key in ('term', 'nonempty', 'distinct', 'samples')})
        profiles.append(profile)
        if table.is_core or table.row_type != DWC + 'Occurrence' or not table.ids or not table.rows:
            continue
        proxy, differences = occurrence_proxy(archive, t)
        inner = build_plan(proxy); nested[str(t)] = inner; conflicts.extend(differences)
        issues.append(_issue(f'table:{t}', f'{table.name}: convert actual occurrence records?',
            'Confirm that these are actual occurrence records, not taxon distribution statements. Empty classification fields use the exact linked Taxon row only when overlapping classifications agree. Conflicting occurrence classifications remain separate, with no Taxon values filled in. '
            + (f'{len(differences)} classification differences are recorded in the report. ' if differences else '')
            + 'Taxonomy tables retain every original value regardless of this choice.',
            [{'value': 'convert', 'label': 'Convert the supplied occurrence records to standard DwC-DP tables'},
            {**PRESERVE, 'label': 'Keep these records in additional taxonomy tables only'}], table=t))
        prefix = f'taxon-occurrence:{t}:'
        for column in inner['columns']:
            outer = {**column, 'id': prefix + column['id'], 'table': t}
            outer['options'] = [taxonomy_preserve if option['value'] == 'preserve' else option for option in column['options']]
            if column['term'] == DWC + 'taxonID' and column['nonempty']:
                external = all(re.fullmatch(r'https?://[^\s/]+(?:/[^\s]*)?|urn:[^\s]+', row[column['column']])
                               for row in proxy.tables[0].rows if row[column['column']])
                outer.update(review=True, default=column['default'] if external else 'preserve',
                             options=outer['options'] if external else [taxonomy_preserve])
                issues.append(_issue(outer['id'], 'taxonID: external identifier required for DwC-DP',
                    'DwC-DP taxonID refers to a globally resolvable external taxon record. Local checklist identifiers remain in taxonomy tables and their explicit attachment links. Confirm external meaning before copying an IRI.',
                    outer['options'], table=t))
            columns.append(outer)
        issues.extend({**issue, 'id': prefix + issue['id'], 'table': t,
                       'options': [taxonomy_preserve if option['value'] == 'preserve' else option for option in issue['options']]}
                      for issue in inner['issues'])
        automatic.extend({**choice, 'id': prefix + choice['id'], 'table': t,
                          'options': [taxonomy_preserve if option['value'] == 'preserve' else option for option in choice['options']]}
                         for choice in inner.get('automatic_choices', []))
        warnings.extend({**notice, 'id': prefix + notice['id'], 'table': t,
                         'source_table': table.name} for notice in inner.get('warnings', []))
    plan = {'version': RULE_VERSION, 'source_sha256': archive.fingerprint, 'schema': dwc_dp_schema_snapshot(),
            'tables': profiles, 'columns': columns, 'issues': issues,
            'automatic_choices': automatic, 'warnings': warnings,
            'taxonomy': {'occurrence_plans': nested, 'classification_conflicts': conflicts,
                         'scientific_hierarchies': {index: inner['scientific_hierarchy'] for index, inner in nested.items()
                                                   if 'scientific_hierarchy' in inner},
                         'link_checks': taxonomy_link_checks(archive),
                         'note': 'Taxonomy resources are additional tables, not standard DwC-DP Taxon tables.'},
            'files': [{'name': name, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
                      for name, content in sorted(archive.files.items())],
            'uploads': [{'name': name, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
                        for name, content in sorted(archive.uploaded_files.items())]}
    plan['id'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def convert_taxon(archive, plan, decisions, *, user_decisions=None):
    from api.dwca_conversion import RULE_VERSION, _key, convert
    user_decisions = dict(decisions if user_decisions is None else user_decisions)
    pieces = defaultdict(list); crosswalk, links, withheld, canonical_columns, hierarchies, occurrence_reports = [], [], [], [], {}, {}
    for index, inner in plan['taxonomy']['occurrence_plans'].items():
        t = int(index)
        if decisions[f'table:{t}'] == 'preserve':
            continue
        proxy, _ = occurrence_proxy(archive, t)
        prefix = f'taxon-occurrence:{t}:'
        inner_decisions = {key[len(prefix):]: value for key, value in user_decisions.items() if key.startswith(prefix)}
        frames, report = convert(proxy, inner, inner_decisions)
        def original_indices(value):
            if isinstance(value, dict):
                return {key: t if key in {'source_table_index', 'table'} and item == 0 else original_indices(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [original_indices(item) for item in value]
            return value
        occurrence_reports[index] = {'source_table': archive.tables[t].name, 'source_table_index': t,
                                     'report': original_indices(report)}
        if 'event_hierarchy' in report:
            hierarchies[index] = {'source_table': archive.tables[t].name, **original_indices(report['event_hierarchy'])}
        offsets = {name: sum(len(piece) for piece in pieces[name]) for name in frames}
        for name, frame in frames.items(): pieces[name].append(frame)
        for trace in report['row_crosswalk']:
            source_n = trace['source_row'] - 1
            trace['archive_join_id'] = archive.tables[t].ids[source_n]
            if trace.get('target_row') is not None:
                trace['target_row'] += offsets.get(trace['target_table'], 0)
            trace['source_table_index'] = t
            trace['source_row_type'] = DWC + 'Occurrence'
            crosswalk.append(trace)
            if trace['target_table'] == 'occurrence':
                n = trace['source_row'] - 1
                links.append({'archive_taxon_id': archive.tables[t].ids[n], 'occurrence_fk': trace['key']['occurrence_pk'],
                              'source_resource': resource_name(archive.tables[t], t),
                              'source_row_id': _key(archive, 'taxonomy-row', t, n)})
                crosswalk.append({**trace, 'target_table': 'taxonomy-occurrence-links', 'target_row': len(links),
                                  'key': {'occurrence_fk': trace['key']['occurrence_pk']}})
        canonical_columns.extend({**column, 'source_basis': 'Occurrence values with reviewed linked Taxon classification context'}
                                 for column in report['columns'])
        withheld.extend({**value, 'source_table_index': t} for value in report['withheld_values'])
    frames = {name: pd.concat(parts, ignore_index=True).fillna('') for name, parts in pieces.items()}
    tables = taxonomy_tables(archive, {'taxonomy_occurrence_links': links})
    for t, table in enumerate(archive.tables):
        name = resource_name(table, t)
        for n, row in enumerate(table.rows):
            crosswalk.append({'source_table': table.name, 'source_table_index': t, 'source_row': n + 1,
                'source_row_type': table.row_type, **table.row_sources[n], 'target_table': name,
                'target_row': n + 1, 'archive_join_id': table.ids[n] if table.ids else '',
                'key': {'archive_join_id': table.ids[n]} if table.is_core else {'source_row_id': _key(archive, 'taxonomy-row', t, n)}})
    columns = [{'source_table': archive.tables[column['table']].name, 'term': column['term'],
                'target': column['default'], 'disposition': 'additional-taxonomy-table+retained',
                'nonempty': column['nonempty'], 'mapped_rows': column['nonempty'], 'retained_only_rows': 0}
               for column in plan['columns'] if column['id'].startswith('column:')]
    validation = validate_dwc_dp_resources(frames) if frames else {'valid': True, 'errors': [], 'warnings': []}
    report = {'plan_id': plan['id'], 'rule_version': RULE_VERSION, 'source_sha256': archive.fingerprint,
              'schema': plan['schema'], 'decisions': user_decisions, 'effective_decisions': decisions,
              'automatic_choices': plan.get('automatic_choices', []), 'warnings': plan.get('warnings', []), 'files': plan['files'],
              'columns': [*columns, *canonical_columns], 'row_crosswalk': crosswalk, 'withheld_values': withheld,
              'preserved_extension_rows': [], 'taxonomy_occurrence_links': links,
              'taxonomy': {'classification_conflicts': plan['taxonomy']['classification_conflicts'],
                           'event_hierarchies': hierarchies,
                           'occurrence_reports': occurrence_reports,
                           'link_checks': plan['taxonomy']['link_checks'],
                           'additional_resources': {name: len(table['dataframe']) for name, table in tables.items()}},
              'output_format': 'dwc-dp' if frames else 'taxonomy-data-package',
              'resources': {**{name: len(df) for name, df in frames.items()},
                            **{name: len(table['dataframe']) for name, table in tables.items()}},
              'validation': validation,
              'limitations': ['Taxonomy tables are additional Frictionless resources, not standard DwC-DP Taxon tables.',
                              'No taxa, distributions, synonyms, or type citations are promoted to occurrence records.',
                              'Source name strings and identifiers are preserved without name matching or taxonomic resolution.']}
    return frames, report
