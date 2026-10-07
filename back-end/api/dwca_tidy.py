"""Deterministic, reviewable normalisation of DwC cell values."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from api.dwca_import import DWC

TIDY_VERSION = '1'
AUTO, SUGGEST = 'auto', 'suggest'
_DATA = Path(__file__).resolve().parent / 'templates' / 'dwca-conversion'


@lru_cache(maxsize=1)
def _tables():
    countries = json.loads((_DATA / 'tidy-countries.json').read_text(encoding='utf-8'))['countries']
    vocabularies = json.loads((_DATA / 'tidy-vocabularies.json').read_text(encoding='utf-8'))['vocabularies']
    waters = json.loads((_DATA / 'tidy-water-bodies.json').read_text(encoding='utf-8'))['names']
    alpha2, alpha3, country_names = set(), {}, {}
    for item in countries:
        a2 = item['alpha_2']
        alpha2.add(a2)
        alpha3[item['alpha_3']] = a2
        for label in [*item.get('names', []), *item.get('aliases', [])]:
            country_names[key(label)] = a2
    vocab = {}
    for name, data in vocabularies.items():
        lookup = {}
        for concept in data['concepts']:
            value = concept['value']
            for label in [concept.get('concept', ''), value, *concept.get('aliases', [])]:
                if label:
                    lookup[key(label)] = value
                    if name in {'establishmentMeans', 'degreeOfEstablishment', 'pathway', 'basisOfRecord'}:
                        lookup[compact(label)] = value
        vocab[name] = lookup
    return {'alpha2': alpha2, 'alpha3': alpha3, 'country_names': country_names,
            'vocab': vocab, 'waters': {item.casefold() for item in waters}}


def normalize_space(value):
    if '://' in value:
        return value.strip()
    return '\n'.join(re.sub(r'[ \t]+', ' ', line) for line in value.strip().split('\n'))


def key(value):
    return normalize_space(value).casefold().rstrip('.').strip()


def compact(value):
    return re.sub(r'[\s_-]+', '', key(value))


def life_stage_word(word):
    """Whether a word is a vendored life-stage concept name, value, or alias."""
    return key(word) in _tables()['vocab'].get('lifeStage', {})


def life_stage_values():
    return set(_tables()['vocab'].get('lifeStage', {}).values())


def sex_values():
    return set(_tables()['vocab'].get('sex', {}).values())


def value_id(group_id, value):
    return f'{group_id}:{hashlib.sha256(value.encode()).hexdigest()[:16]}'


_PROTECTED = set('''catalogNumber otherCatalogNumbers recordNumber fieldNumber institutionCode collectionCode ownerInstitutionCode associatedOccurrences associatedSequences associatedReferences associatedMedia associatedTaxa associatedOrganisms references dynamicProperties informationWithheld dataGeneralizations scientificName scientificNameAuthorship acceptedNameUsage parentNameUsage originalNameUsage nameAccordingTo namePublishedIn namePublishedInYear higherClassification kingdom phylum class order superfamily family subfamily tribe subtribe genus genericName subgenus infragenericEpithet specificEpithet infraspecificEpithet cultivarEpithet taxonRank verbatimTaxonRank vernacularName nomenclaturalCode taxonomicStatus nomenclaturalStatus taxonRemarks identificationQualifier typeStatus identificationReferences eventDate eventTime startDayOfYear endDayOfYear year month day dateIdentified modified georeferencedDate measurementDeterminedDate relationshipEstablishedDate decimalLatitude decimalLongitude geodeticDatum footprintWKT footprintSRS pointRadiusSpatialFit footprintSpatialFit'''.split())
_NUMERIC = set('minimumElevationInMeters maximumElevationInMeters minimumDepthInMeters maximumDepthInMeters minimumDistanceAboveSurfaceInMeters maximumDistanceAboveSurfaceInMeters coordinateUncertaintyInMeters coordinatePrecision organismQuantity sampleSizeValue individualCount'.split())
_VARIANTS = set('lifeStage sex reproductiveCondition behavior vitality establishmentMeans degreeOfEstablishment pathway preparations disposition organismQuantityType sampleSizeUnit georeferenceVerificationStatus identificationVerificationStatus waterBody islandGroup island continent country stateProvince county municipality'.split())
_ELEV = {'minimumElevationInMeters', 'maximumElevationInMeters'}
_DEPTH = {'minimumDepthInMeters', 'maximumDepthInMeters'}
_NULL = {'na', 'n/a', 'n.a', 'null', 'none', 'nan', 'unknown', 'unknown or unrecorded', 'not recorded', 'not known', 'not available', 'unk', 'ukjent', 'okänd', '-', '--', '---', '?', '??', ''}
_SPLIT = re.compile(r'\s*(?:\+|/|&|\||;|,|\band\b)\s*', re.I)
_WATER_RE = re.compile(r'\b(oceans?|seas?|gulf|channel|strait|bight|fjord|bay)\b', re.I)


def _protected(term, name):
    if not term.startswith(DWC) or name.endswith(('ID', 'IDs')) or name in _PROTECTED:
        return True
    if name.startswith('verbatim'):
        return True
    if name.startswith('measurement') and name != 'measurementRemarks':
        return True
    if name.startswith('relationship') and name != 'relationshipRemarks':
        return True
    return False


def _is_zero(value):
    try:
        return value.strip() != '' and float(value.replace(',', '.')) == 0
    except (ValueError, OverflowError):
        return False


def _rule(name, value, data, row_type):
    w = normalize_space(value)
    k = key(w)
    vocab = data['vocab'].get(name)
    if vocab is not None:
        hit = vocab.get(k)
        if hit is None and name in {'establishmentMeans', 'degreeOfEstablishment', 'pathway', 'basisOfRecord'}:
            hit = vocab.get(compact(w))
        if hit is not None:
            return 'vocabulary', AUTO, {name: hit}, False
        if name in {'lifeStage', 'sex'}:
            parts = [part for part in _SPLIT.split(w) if part.strip()]
            mapped = [vocab.get(key(part)) for part in parts]
            if len(parts) >= 2 and all(mapped):
                values = list(dict.fromkeys(mapped))
                return 'vocabulary', AUTO, {name: ' | '.join(values)}, False
    # countryCode NA is Namibia (lower-case na is not); a bare NA in country is ambiguous, so it stays as written.
    if k in _NULL and not (name in {'countryCode', 'country'} and w == 'NA'):
        return 'empty-placeholder', AUTO, {name: ''}, False
    if name in {'countryCode', 'country'}:
        alpha2, alpha3, names = data['alpha2'], data['alpha3'], data['country_names']
        if name == 'countryCode':
            upper = w.upper()
            if len(w) == 2 and w == upper and upper in alpha2:
                pass
            elif upper in alpha2:
                return 'country-code', AUTO, {name: upper}, False
            elif upper in alpha3:
                return 'country-code', AUTO, {name: alpha3[upper]}, False
            elif k in names:
                return 'country-name', AUTO, {name: names[k], 'country': w}, False
        else:
            if k in names:
                return 'country-code-fill', AUTO, {name: w, 'countryCode': names[k]}, False
            if len(w) == 2 and w == w.upper() and w in alpha2 and w != 'NA':
                return 'country-code-fill', AUTO, {name: w, 'countryCode': w}, False
        # Only curated sea names move by themselves ('North Atlantic Ocean (other parts)' counts); other text
        # with a water word, such as 'United Kingdom (English Channel)', is only suggested.
        if k in data['waters'] or re.sub(r'\s*\([^)]*\)$', '', k) in data['waters']:
            return 'water-body', AUTO, {name: '', 'waterBody': w}, True
        if _WATER_RE.search(w) and not re.match(r'other\s+', w, re.I):
            return 'water-body-suggestion', SUGGEST, {name: '', 'waterBody': w}, True
    if row_type == DWC + 'Occurrence' and name in {'eventRemarks', 'occurrenceRemarks'}:
        hit = data['vocab']['lifeStage'].get(k)
        if hit is not None:
            return 'life-stage-remark', AUTO, {name: '', 'lifeStage': hit}, True
    if name in _NUMERIC:
        if name != 'individualCount' and re.fullmatch(r'[+-]?\d+,(?:\d{1,2}|\d{4,})', w):
            return 'decimal-comma', AUTO, {name: w.replace(',', '.')}, False
        if name != 'individualCount' and re.fullmatch(r'[+-]?\d{1,3},\d{3}', w):
            return 'thousands-or-decimal', SUGGEST, {name: w.replace(',', '.')}, False
        if re.fullmatch(r'[+-]?\d+[,.]', w):
            return 'trailing-separator', SUGGEST, {name: w[:-1]}, False
    if w != value:
        return 'whitespace', AUTO, {name: w}, False
    return None


def _model_valid(item):
    if not isinstance(item, dict) or not isinstance(item.get('fields'), dict) or not item['fields']:
        return False
    if item.get('tier') not in {AUTO, SUGGEST}:
        return False
    return all(isinstance(field, str) and field and ':' not in field and '/' not in field
               and isinstance(value, str) for field, value in item['fields'].items())


def _title(rule, field, values, n):
    examples, raw_examples = [], []
    for item in sorted(values, key=lambda v: (-v['rows'], v['value']))[:4]:
        old = item['value'].encode('unicode_escape').decode('ascii').replace("'", "\\'")
        new = item['fields'].get(field, '')
        new = new.encode('unicode_escape').decode('ascii').replace("'", "\\'")
        examples.append(f"'{old} → {new}'")
        raw_examples.append(f"'{old}'")
    sample = ', '.join(examples)
    if rule == 'vocabulary': return f'{field}: standard terms used ({sample}) in {n:,} rows.'
    if rule == 'empty-placeholder': return f"{field}: placeholders such as {', '.join(raw_examples)} left empty ({n:,} rows)."
    if rule == 'country-name':
        codes = list(dict.fromkeys(v['fields'][field] for v in values))[:4]
        return f"countryCode held country names in {n:,} rows. The names now go to country and countryCode gets ISO codes: {', '.join(codes)}."
    if rule == 'country-code': return f'{field}: codes written in the standard two-letter form ({sample}) in {n:,} rows.'
    if rule == 'country-code-fill':
        codes = list(dict.fromkeys(v['fields']['countryCode'] for v in values))[:4]
        return f"countryCode filled from the country names in {n:,} rows: {', '.join(codes)}."
    if rule == 'water-body': return f'{field}: sea and ocean names moved to waterBody ({sample}) in {n:,} rows.'
    if rule == 'water-body-suggestion': return f'{field}: these may name a sea or ocean rather than a country; they could move to waterBody ({sample}).'
    if rule == 'model-suggestion': return f'{field}: possible readings to check ({sample}).'
    if rule == 'life-stage-remark': return f'{field}: life stages moved to lifeStage ({sample}) in {n:,} rows.'
    if rule == 'decimal-comma': return f'{field}: decimal commas changed to points ({sample}) in {n:,} rows.'
    if rule in {'thousands-or-decimal', 'trailing-separator'}:
        item = sorted(values, key=lambda v: (-v['rows'], v['value']))[0]
        return f"{field}: '{item['value']}' may be a number written with a comma; suggested '{item['fields'][field]}'."
    if rule == 'zero-placeholder' and values and values[0].get('tier') == SUGGEST:
        return f'{field} is 0 on every row, as is another elevation or depth column. If 0 means "not recorded", it can be left empty ({n:,} rows).'
    if rule == 'zero-placeholder': return f'{field} was 0 on every row, together with the other elevation or depth columns, so it is treated as not recorded and left empty ({n:,} rows).'
    if rule == 'variant': return f'{field}: spelling variants made the same ({sample}) in {n:,} rows.'
    if rule == 'whitespace': return f'{field}: extra spaces removed in {n:,} rows.'
    if rule == 'encoding': return f'{field}: some characters look damaged by a file-encoding problem ({sample}); suggested repairs are listed.'
    return f'{field}: values interpreted ({sample}) in {n:,} rows.'


def tidy_archive(archive, overrides=None, model_changes=None):
    """Return a copied archive view and a deterministic, reviewable change table."""
    data = _tables()
    overrides = overrides or {}
    proposed = {}
    column_values = {}
    for t, table in enumerate(archive.tables):
        for c, term in enumerate(table.terms):
            name = term.rsplit('/', 1)[-1]
            if _protected(term, name):
                continue
            counts = Counter(row[c] if c < len(row) else '' for row in table.rows)
            column_values[t, c] = counts
            for value, rows in counts.items():
                result = _rule(name, value, data, table.row_type)
                if result:
                    rule, tier, fields, move = result
                    if fields.get(name) != value or len(fields) > 1:
                        proposed[t, c, value] = {'rule': rule, 'tier': tier, 'fields': fields,
                                                 'move': move, 'rows': rows, 'by': 'rule'}
    # Zero elevation/depth columns are interpreted together, at table scope.
    for t, table in enumerate(archive.tables):
        zero_cols = {'elev': [], 'depth': []}
        for c, term in enumerate(table.terms):
            name = term.rsplit('/', 1)[-1]
            if name in _ELEV | _DEPTH:
                counts = column_values.get((t, c), Counter())
                if counts and all(_is_zero(value) for value in counts):
                    zero_cols['elev' if name in _ELEV else 'depth'].append(c)
        if zero_cols['elev'] and zero_cols['depth']:
            # All four zero on every row is the placeholder pattern of 572 (cleared, with undo); fewer zero columns
            # could be a real shoreline or surface record, so those are only suggested.
            tier = AUTO if len(zero_cols['elev']) + len(zero_cols['depth']) == 4 else SUGGEST
            for c in zero_cols['elev'] + zero_cols['depth']:
                name = table.terms[c].rsplit('/', 1)[-1]
                for value, rows in column_values[t, c].items():
                    proposed[t, c, value] = {'rule': 'zero-placeholder', 'tier': tier,
                        'fields': {name: ''}, 'move': False, 'rows': rows, 'by': 'rule'}
    # Variant rules use distinct source values and ignore values already claimed by an earlier rule.
    for (t, c), counts in column_values.items():
        name = archive.tables[t].terms[c].rsplit('/', 1)[-1]
        if name not in _VARIANTS:
            continue
        buckets = defaultdict(list)
        for value, count in counts.items():
            if (t, c, value) not in proposed:
                w = normalize_space(value)
                buckets[key(w)].append((w, value, count))
        for forms in buckets.values():
            if len({form for form, _, _ in forms}) < 2:
                continue
            winner = sorted(forms, key=lambda item: (-item[2], item[0]))[0][0]
            for form, value, rows in forms:
                if form != winner:
                    proposed[t, c, value] = {'rule': 'variant', 'tier': AUTO,
                        'fields': {name: winner}, 'move': False, 'rows': rows, 'by': 'rule'}
    # Encoding suggestions come last and only see values not changed by any earlier rule.
    for (t, c), counts in column_values.items():
        name = archive.tables[t].terms[c].rsplit('/', 1)[-1]
        if name in _NUMERIC:
            continue
        for value, rows in counts.items():
            if (t, c, value) in proposed:
                continue
            w = normalize_space(value)
            if any('\x80' <= ch <= '\x9f' for ch in w):
                fixed = ''.join(bytes([ord(ch)]).decode('cp437') if '\x80' <= ch <= '\x9f' else ch for ch in w)
                if fixed != value:
                    proposed[t, c, value] = {'rule': 'encoding', 'tier': SUGGEST,
                        'fields': {name: fixed}, 'move': False, 'rows': rows, 'by': 'rule'}
    for item in model_changes or []:
        if not _model_valid(item):
            continue
        try:
            t, c, value = item['table'], item['column'], item['value']
            if (not isinstance(t, int) or isinstance(t, bool) or t < 0
                    or not isinstance(c, int) or isinstance(c, bool) or c < 0
                    or not isinstance(value, str)):
                continue
            name = archive.tables[t].terms[c].rsplit('/', 1)[-1]
            if _protected(archive.tables[t].terms[c], name):
                continue
        except (IndexError, TypeError):
            continue
        # The model read the value after the space clean-up, so its answer also covers source spellings that differ
        # only in spacing ('fad ' read as 'fad'); it replaces that clean-up, never another rule.
        sources = [raw for raw in column_values.get((t, c), {})
                   if raw == value and (t, c, raw) not in proposed
                   or normalize_space(raw) == value and proposed.get((t, c, raw), {}).get('rule') == 'whitespace']
        fields = dict(item['fields'])
        if name not in fields:
            fields[name] = value
        for raw in sources:
            if fields[name] == raw and len(fields) == 1:
                continue
            proposed[t, c, raw] = {'rule': 'model', 'tier': item['tier'], 'fields': fields,
                'move': bool(item.get('move')), 'rows': column_values[t, c][raw],
                'by': 'model', 'confidence': str(item.get('confidence', '')), 'note': str(item.get('note', ''))}

    grouped = defaultdict(list)
    for (t, c, value), change in proposed.items():
        group_rule = change['rule']
        if change['by'] == 'model':
            group_rule = 'model' if change['tier'] == AUTO else 'model-suggestion'
        grouped[t, c, group_rule].append((value, change))
    records, group_dicts = {}, []
    for (t, c, rule), changes in sorted(grouped.items()):
        table = archive.tables[t]
        term = table.terms[c]
        field = term.rsplit('/', 1)[-1]
        gid = f'tidy:{t}:{c}:{rule}'
        values = []
        for value, change in sorted(changes, key=lambda pair: (-pair[1]['rows'], pair[0])):
            vid = value_id(gid, value)
            selected = overrides.get(vid, overrides.get(gid, 'on' if change['tier'] == AUTO else 'off')) == 'on'
            record = {'id': vid, 'value': value, 'rows': change['rows'], 'fields': dict(change['fields']),
                      'applied': bool(selected), 'changed_rows': 0, 'conflict_rows': 0, 'agree_rows': 0,
                      'tier': change['tier'], 'move': change['move'], 'value_text': change['fields'].get(field, '')}
            if change['by'] == 'model':
                record.update(confidence=change['confidence'], note=change['note'])
            values.append(record)
            records[t, c, value] = (record, change)
        group = {'id': gid, 'table': t, 'table_name': table.name, 'column': c, 'term': term, 'field': field,
                 'rule': rule, 'tier': changes[0][1]['tier'], 'by': changes[0][1]['by'],
                 'move': any(change['move'] for _, change in changes), 'applied': any(v['applied'] for v in values),
                 'rows': sum(v['rows'] for v in values), 'changed_rows': 0, 'conflict_rows': 0,
                 'title': '', 'values': values}
        group['_key'] = (t, c, rule)
        group_dicts.append(group)

    # Copy only changed tables. Pass one rewrites non-moves; moves wait for conflict checks.
    copies = {}
    by_column = defaultdict(dict)
    for (t, c, value), pair in records.items():
        by_column[t, c][value] = pair
    for (t, c), value_records in by_column.items():
        if not any(record['applied'] for record, _ in value_records.values()):
            continue
        if t not in copies:
            copies[t] = [list(row) for row in archive.tables[t].rows]
        field = archive.tables[t].terms[c].rsplit('/', 1)[-1]
        for r, row in enumerate(archive.tables[t].rows):
            value = row[c] if c < len(row) else ''
            pair = value_records.get(value)
            if pair is None:
                continue
            record, change = pair
            if not record['applied'] or change['move']:
                continue
            target = change['fields'][field]
            if target != value:
                while len(copies[t][r]) <= c:
                    copies[t][r].append('')
                copies[t][r][c] = target
                record.setdefault('_changed', set()).add(r)

    additions = defaultdict(lambda: {'cells': {}, 'groups': set()})
    terms_to_columns = []
    for t, table in enumerate(archive.tables):
        mapping = {}
        for c, term in enumerate(table.terms):
            mapping.setdefault(term, c)
        terms_to_columns.append(mapping)
    for (t, c), value_records in by_column.items():
        field = archive.tables[t].terms[c].rsplit('/', 1)[-1]
        for r, original_row in enumerate(archive.tables[t].rows):
            value = original_row[c] if c < len(original_row) else ''
            pair = value_records.get(value)
            if pair is None:
                continue
            record, change = pair
            if not record['applied']:
                continue
            other_fields = {f: v for f, v in change['fields'].items() if f != field}
            if not change['move'] and not other_fields:
                continue
            plans, row_conflict, row_agree = [], False, False
            for f, wanted in other_fields.items():
                target_term = DWC + f
                target_c = terms_to_columns[t].get(target_term)
                if target_c is None:
                    current = additions[t, target_term]['cells'].get(r, '')
                else:
                    row = copies.get(t, archive.tables[t].rows)[r]
                    current = row[target_c] if target_c < len(row) else ''
                if current == '':
                    plans.append((f, target_term, target_c, wanted))
                elif current == wanted:
                    row_agree = True
                else:
                    row_conflict = True
            if row_conflict:
                # A change applies to a row as a whole, so a conflict keeps every source value of that row.
                record.setdefault('_conflicts', set()).add(r)
                if not change['move'] and change['fields'].get(field, value) != value:
                    copies[t][r][c] = value
                    record.get('_changed', set()).discard(r)
                continue
            if row_agree:
                record.setdefault('_agrees', set()).add(r)
            changed_rows = record.setdefault('_changed', set())
            for f, target_term, target_c, wanted in plans:
                if not wanted:
                    continue
                if target_c is None:
                    additions[t, target_term]['cells'][r] = wanted
                    group_rule = change['rule']
                    if change['by'] == 'model':
                        group_rule = 'model' if change['tier'] == AUTO else 'model-suggestion'
                    additions[t, target_term]['groups'].add(f'tidy:{t}:{c}:{group_rule}')
                else:
                    if t not in copies:
                        copies[t] = [list(row) for row in archive.tables[t].rows]
                    while len(copies[t][r]) <= target_c:
                        copies[t][r].append('')
                    copies[t][r][target_c] = wanted
                changed_rows.add(r)
            if change['move'] and not row_conflict:
                if t not in copies:
                    copies[t] = [list(row) for row in archive.tables[t].rows]
                copies[t][r][c] = ''
                if value != '': changed_rows.add(r)
    for record, _change in records.values():
        record['changed_rows'] = len(record.get('_changed', set()))
        record['conflict_rows'] = len(record.get('_conflicts', set()))
        record['agree_rows'] = len(record.get('_agrees', set()))

    added_columns = []
    for (t, term), info in sorted(additions.items()):
        if not info['cells']:
            continue
        c = len(archive.tables[t].terms) + sum(1 for item in added_columns if item['table'] == t)
        for r, value in info['cells'].items():
            while len(copies[t][r]) <= c:
                copies[t][r].append('')
            copies[t][r][c] = value
        added_columns.append({'table': t, 'column': c, 'term': term,
                              'groups': sorted(info['groups'])})
    # The original columns precede new columns; synthetic IRIs are sorted per table.
    for t in copies:
        if t not in {a['table'] for a in added_columns}:
            continue
        synthetic = [a for a in added_columns if a['table'] == t]
        synthetic.sort(key=lambda a: a['term'])
        for a in synthetic:
            a['column'] = len(archive.tables[t].terms) + synthetic.index(a)
        # Rebuild rows as original columns plus sorted synthetic columns.
        for r, row in enumerate(copies[t]):
            original = row[:len(archive.tables[t].terms)]
            extras = []
            for a in synthetic:
                extras.append(additions[t, a['term']]['cells'].get(r, ''))
            copies[t][r] = original + extras
    added_columns.sort(key=lambda a: (a['table'], a['term']))

    # Roll up counters and construct grouped, serialisable output.
    for group in group_dicts:
        t, c, rule = group.pop('_key')
        # An applied value that changed no cell (its fills all agreed already) is not a change worth showing.
        group['values'] = [v for v in group['values'] if not v['applied'] or v['changed_rows'] or v['conflict_rows']]
        group['applied'] = any(v['applied'] for v in group['values'])
        group['rows'] = sum(v['rows'] for v in group['values'])
        group['changed_rows'] = sum(v['changed_rows'] for v in group['values'])
        group['conflict_rows'] = sum(v['conflict_rows'] for v in group['values'])
        # Cells of this column rewritten, and cells left empty (placeholders cleared or values moved out).
        own = [(v['fields'].get(group['field'], v['value']), v) for v in group['values'] if v['applied']]
        group['tidied_rows'] = sum(v['changed_rows'] for text, v in own if text not in ('', v['value']))
        group['cleared_rows'] = sum(v['changed_rows'] for text, v in own if text == '')
        # An undone or suggested group still says what it would change.
        n = group['changed_rows'] if group['tier'] == AUTO and group['changed_rows'] else group['rows']
        group['title'] = _title(rule, group['field'], group['values'], n)
        for item in group['values']:
            for private in ('tier', 'move', 'value_text', '_changed', '_conflicts', '_agrees'):
                item.pop(private, None)
    group_dicts = [group for group in group_dicts if group['values']]
    source_columns = {str(t): len(table.terms) for t, table in enumerate(archive.tables)}
    digest_payload = [TIDY_VERSION, [(g['id'], v['value'], v['fields'])
                     for g in group_dicts for v in g['values'] if v['applied']], added_columns]
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    table = {'version': TIDY_VERSION, 'source_sha256': archive.fingerprint, 'sha256': digest,
             'groups': group_dicts, 'added_columns': added_columns, 'source_columns': source_columns}
    new_tables = list(archive.tables)
    for t, rows in copies.items():
        original = archive.tables[t]
        terms = list(original.terms)
        synthetic = sorted((item for item in added_columns if item['table'] == t), key=lambda item: item['term'])
        terms.extend(item['term'] for item in synthetic)
        new_tables[t] = replace(original, terms=terms, rows=rows)
    view = replace(archive, tables=new_tables, tidy=table)
    return view, table


def settled_values(archive, t, c):
    """Values of a column the tidy-up has dealt with: applied changes and open suggestions.

    Undone changes are not settled, nor are values some rows kept as written because of a conflict.
    """
    groups = (getattr(archive, 'tidy', None) or {}).get('groups', [])
    return {value['value'] for group in groups if group['table'] == t and group['column'] == c
            for value in group['values'] if (value['applied'] and not value['conflict_rows']) or group['tier'] == SUGGEST}


def column_note(archive, t, c):
    """Return tidy provenance for a synthetic column, if present."""
    if not archive.tidy:
        return None
    item = next((item for item in archive.tidy.get('added_columns', [])
                 if item['table'] == t and item['column'] == c), None)
    return {'tidy_added': {'term': item['term'], 'groups': list(item['groups'])}} if item else None


def summarize(table, value_limit=30):
    """Bound group value samples while preserving all other table metadata."""
    if value_limit < 0:
        raise ValueError('value_limit must not be negative')
    result = json.loads(json.dumps(table))
    for group in result.get('groups', []):
        values = group.get('values', [])
        group['more_values'] = max(0, len(values) - value_limit)
        group['values'] = values[:value_limit]
    return result
