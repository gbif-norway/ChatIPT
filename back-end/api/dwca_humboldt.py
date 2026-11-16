"""Humboldt survey copies and reviewed scope reconstruction, using exact IRIs."""
from decimal import Decimal, InvalidOperation
import math
import re
import uuid

from api.dwca_import import ImportFailure, REGISTRY
from api.dwc_dp_specs import TABLE_SPECS

ECO = 'http://rs.tdwg.org/eco/terms/'
ECOIRI = 'http://rs.tdwg.org/eco/iri/'
HUMBOLDT_ROW = ECO + 'Event'
HUMBOLDT_FAMILIES = {HUMBOLDT_ROW: 'humboldt'}
_REGISTERED = {term for candidates in REGISTRY['row_types'][HUMBOLDT_ROW].values() for term in candidates}
# DwC-DP 1.0 keeps most Humboldt properties on survey, but moves the event-level ones (event duration,
# weather, extreme conditions, vegetation cover) to event and abundanceCap to survey-target.
TARGET_TABLES = ('survey', 'event', 'survey-target')
TARGET_TABLE = {field['dcterms:isVersionOf']: table for table in reversed(TARGET_TABLES)
                for field in TABLE_SPECS[table].schema['fields']
                if field.get('dcterms:isVersionOf') in _REGISTERED and not field['name'].endswith(('_pk', '_fk'))
                and (table == 'survey' or field['dcterms:isVersionOf'].startswith(ECO))}
DIRECT = {term: TABLE_SPECS[table].field_descriptors[name] for term, table in TARGET_TABLE.items()
          for name, field in TABLE_SPECS[table].field_descriptors.items()
          if field.get('dcterms:isVersionOf') == term and not name.endswith(('_pk', '_fk'))}
DIMENSIONS = {
    'Taxonomic': 'taxon', 'Habitat': 'habitat', 'LifeStage': 'lifeStage',
    'DegreeOfEstablishment': 'degreeOfEstablishment', 'GrowthForm': 'growthForm',
}
SCOPE_TERMS = _REGISTERED - set(DIRECT)
_SURVEY_FIELDS = {field['name']: field for field in TABLE_SPECS['survey'].schema['fields']}
# Current-vocabulary properties outside the registered XML, imported only by exact IRI.
# eco:surveyID is a literal identifier of the survey this extension row describes.
SUPPLEMENTAL = {ECO + 'surveyID': _SURVEY_FIELDS['surveyID']}
# Audited IRI alias: an agent IRI is an agent identifier, never the literal agent name.
IRI_DIRECT = {ECOIRI + 'samplingPerformedBy': _SURVEY_FIELDS['samplingPerformedByID']}
COPIED = {**DIRECT, **SUPPLEMENTAL, **IRI_DIRECT}
TARGET_TABLE.update({term: 'survey' for term in (*SUPPLEMENTAL, *IRI_DIRECT)})
# abundanceCap belongs to a survey target, which needs a completeness flag; one is made only from a reviewed scope.
_NO_TARGET = ('The abundance cap describes a survey target, and a survey target is created only from a scope with a '
              '"fully reported" answer, so this value stays only in your original files.')
IRI_SCOPE_TERMS = {ECOIRI + stem + dimension + 'Scope' for stem in ('target', 'excluded') for dimension in
                   ('Taxonomic', 'Habitat', 'LifeStage', 'DegreeOfEstablishment', 'GrowthForm')}
_IRI = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:[^\s<>"{}|\\^`]+')
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?')
_PAIRS = {ECO + field: ECO + field.removesuffix('Value') + 'Unit' for field in
          ('geospatialScopeAreaValue', 'totalAreaSampledValue', 'eventDurationValue', 'samplingEffortValue')}
_CONTRADICTIONS = {
    'isAbsenceReported': 'absentTaxa', 'hasNonTargetTaxa': 'nonTargetTaxa',
    'isAbundanceCapReported': 'abundanceCap', 'hasVouchers': 'voucherInstitutions',
    'hasMaterialSamples': 'materialSampleTypes', 'isSamplingEffortReported': 'samplingEffortValue',
}


def valid_value(field, value):
    kind = field['type']
    if kind == 'string' or not value:
        return True
    if kind == 'boolean':
        return value.lower() in {'true', 'false'}
    if kind == 'integer' and not re.fullmatch(r'[+-]?[0-9]+', value):
        return False
    if kind in {'integer', 'number'}:
        if not _NUMBER.fullmatch(value):
            return False
        try:
            number = Decimal(value)
        except InvalidOperation:
            return False
        constraints = field.get('constraints', {})
        return number.is_finite() and math.isfinite(float(number)) and number >= Decimal(str(constraints.get('minimum', '-Infinity'))) and number <= Decimal(str(constraints.get('maximum', 'Infinity')))
    return False


def is_iri(value):
    """One absolute IRI, without whitespace or list separators. No resolution or normalization."""
    return bool(_IRI.fullmatch(value))


def blocked_fields(source):
    """Reasons for withholding individual direct values, without normalizing them."""
    blocked = {term: 'Not a valid value for this field.' for term, field in DIRECT.items()
               if source.get(term) and not valid_value(field, source[term])}
    for term in IRI_DIRECT:
        if source.get(term) and not is_iri(source[term]):
            blocked[term] = 'This column needs one web link (IRI) per cell; text and lists are not copied into it.'
    for value, unit in _PAIRS.items():
        if source.get(value) and not source.get(unit, '').strip():
            blocked[value] = 'This value has no unit in your files.'
    for flag, detail in _CONTRADICTIONS.items():
        flag, detail = ECO + flag, ECO + detail
        if source.get(flag, '').lower() == 'false' and source.get(detail):
            reason = 'A true/false flag says false, but the row also gives the detail that the flag says is not there.'
            blocked[flag] = blocked[detail] = reason
    total, area = ECO + 'totalAreaSampledValue', ECO + 'geospatialScopeAreaValue'
    if source.get(total) and source.get(area) and total not in blocked and area not in blocked:
        if source.get(_PAIRS[total]) == source.get(_PAIRS[area]) and Decimal(source[total]) > Decimal(source[area]):
            blocked[total] = blocked[area] = 'The area sampled is larger than the survey area, in the same units.'
    return blocked


def humboldt_targets(term, sources, blocked_counts=None):
    if term in SCOPE_TERMS:
        return ['derive'], None
    if term in IRI_SCOPE_TERMS:
        # IRI scopes stay distinct from literal scopes; they become surveyTargetValueIRI descriptors without a literal label.
        return ['derive'], ('These scope columns hold web links (IRIs) to vocabulary terms. They are converted only for rows that '
                            'have no text scope, with one link per cell and the usual completeness flags; each link becomes part of '
                            'the survey target. Confirm, or leave them out of the Data Package.')
    field = COPIED.get(term)
    if not field:
        entry = AUDIT.get(term)
        return [], (entry['reason'] if entry else None)
    count = blocked_counts.get(term, 0) if blocked_counts is not None else sum(term in blocked_fields(source) for source in sources)
    reason = (f'{count} {"row has" if count == 1 else "rows have"} a value that is not valid, has no unit, or contradicts another column. Only the values that pass are '
              'copied; the others are listed in the report and stay only in your original files.' if count else None)
    if term in IRI_DIRECT:
        reason = ' '.join(filter(None, ('These links identify who did the sampling, so they are saved as the identifier of the sampler '
                                        '(samplingPerformedByID), not as their name.', reason)))
    if term in SUPPLEMENTAL:
        repeated = _repeated(source.get(term, '') for source in sources)
        if repeated:
            reason = ' '.join(filter(None, (f'{repeated} surveyID values appear on several rows, but separate surveys cannot share an '
                                            'identifier. Combine the rows into one survey per event (possible only when they are identical), '
                                            'or leave the column out of the Data Package.', reason)))
    return [f"{TARGET_TABLE[term]}.{field['name']}"], reason


def _repeated(values):
    seen, repeated = set(), set()
    for value in values:
        if value:
            (repeated if value in seen else seen).add(value)
    return len(repeated)


def _elements(value):
    if not value:
        return []
    elements = value.split(' | ')
    if any(not item or item != item.strip() for item in elements) or len(set(elements)) != len(elements):
        raise ImportFailure('A scope list has an empty item, spaces around an item, or the same item twice. Correct your files, '
                            'or leave the scope out of the Data Package.')
    return elements


def _literal_scope(source):
    return any(source.get(ECO + stem + dimension + 'Scope') for stem in ('target', 'excluded') for dimension in DIMENSIONS)


def _iri_elements(value):
    if not value:
        return []
    if not is_iri(value):
        raise ImportFailure('A scope column for web links (IRIs) must hold one link per cell; lists and text are not split or '
                            'converted. Leave the scope out of the Data Package.')
    return [value]


def scope_state(source, *, literal_scope_present=False):
    dimensions, reasons = [], []
    # IRI scopes are used only when no literal scope is supplied in the row: pairing literals with IRIs is unresolved policy.
    namespace = ECO if literal_scope_present or _literal_scope(source) else ECOIRI
    for dimension, label in DIMENSIONS.items():
        include = source.get(namespace + 'target' + dimension + 'Scope', '')
        exclude = source.get(namespace + 'excluded' + dimension + 'Scope', '')
        if not include and not exclude:
            continue
        flag = source.get(ECO + 'is' + dimension + 'ScopeFullyReported', '')
        split = _iri_elements if namespace == ECOIRI else _elements
        try:
            included, excluded = split(include), split(exclude)
            if set(included) & set(excluded):
                raise ImportFailure('The same item is listed as both included in and excluded from the survey scope.')
        except ImportFailure as error:
            return [], str(error), False
        if not included:
            return [], ('This scope lists only what the survey excluded, not what it included. Leave it out of the Data Package, '
                        'or correct your files.'), False
        if namespace == ECO and any(';' in item or ',' in item for item in included + excluded):
            reasons.append('Some scope values contain commas or semicolons. Only " | " separates list items, so confirm that the '
                           'commas and semicolons are part of the values.')
        if flag.lower() not in {'true', 'false'}:
            reasons.append(f'The {label} scope has no valid "fully reported" flag (true or false), so you need to say whether it was fully reported.')
        if dimension == 'DegreeOfEstablishment':
            reasons.append('Confirm that this scope is about degree of establishment (such as native or introduced), not establishment means.')
        dimensions.append({'dimension': dimension, 'label': label, 'include': included, 'exclude': excluded, 'flag': flag,
                           'namespace': namespace})
    if len(dimensions) > 1:
        reasons.append('This row has scopes for several things (for example taxa and life stages), which become one combined survey '
                       'target. Say whether that combined target was fully reported.')
    return dimensions, ' '.join(dict.fromkeys(reasons)) or None, True


def scope_review(source):
    dimensions, reason, convertible = scope_state(source)
    if not reason:
        return None
    options = [{'value': 'preserve', 'label': 'Leave the scope out of the Data Package'}]
    if convertible and dimensions:
        options += [{'value': 'reported-true', 'label': 'Convert it: everything in this scope was fully reported'},
                    {'value': 'reported-false', 'label': 'Convert it: this scope was not fully reported'}]
    return {'reason': reason, 'options': options}


def emit_humboldt_records(source, mapped, event_key, survey_key, scope_decision='source', *, original_source=None):
    """Return records, consumed source terms, and explicit withheld-value reasons."""
    if not event_key or not survey_key:
        raise ImportFailure('Humboldt conversion requires a reviewed, emitted survey event.')
    allowed = {(TARGET_TABLE[term], field['name']) for term, field in COPIED.items()}
    if set(mapped) - set(TARGET_TABLES) or {(table, name) for table, values in mapped.items() for name in values} - allowed:
        raise ImportFailure('Humboldt values contain an unaudited target.')
    blocked = blocked_fields(source)
    survey = {'survey_pk': survey_key, 'event_fk': event_key}
    copied = {'survey': survey, 'event': {'event_pk': event_key}, 'survey-target': {}}
    consumed, withheld = set(), dict(blocked)
    for term, field in COPIED.items():
        value = mapped.get(TARGET_TABLE[term], {}).get(field['name'], '')
        if value and term not in blocked:
            if source.get(term) != value:
                raise ImportFailure('A Humboldt target differs from its approved source value.')
            copied[TARGET_TABLE[term]][field['name']] = value
            consumed.add(term)
    # Event-level values describe the survey's own event, so they are written onto that event record.
    records = [('survey', survey), *([('event', copied['event'])] if len(copied['event']) > 1 else [])]
    literal_scope_present = _literal_scope(source if original_source is None else original_source)
    dimensions, reason, convertible = scope_state(source, literal_scope_present=literal_scope_present)
    if dimensions and scope_decision != 'preserve':
        if not convertible or (reason and scope_decision not in {'reported-true', 'reported-false'}):
            raise ImportFailure('Answer the survey scope question before converting.')
        if scope_decision == 'source':
            flag = dimensions[0]['flag']
        elif scope_decision in {'reported-true', 'reported-false'}:
            flag = scope_decision.removeprefix('reported-')
        else:
            raise ImportFailure('Unsupported survey scope decision.')
        target_key = str(uuid.uuid5(uuid.UUID(survey_key), 'survey-target'))
        records += [('survey-target', {'surveyTarget_pk': target_key, 'isSurveyTargetFullyReported': flag, **copied['survey-target']}),
                    ('survey-survey-target', {'survey_fk': survey_key, 'surveyTarget_fk': target_key})]
        for dimension in dimensions:
            for mode in ('include', 'exclude'):
                value_field = 'surveyTargetValueIRI' if dimension['namespace'] == ECOIRI else 'surveyTargetValue'
                for value in dimension[mode]:
                    records.append(('survey-target-descriptor', {'surveyTarget_fk': target_key,
                                    'surveyTargetType': dimension['label'], value_field: value,
                                    'includeOrExclude': mode}))
            for stem in ('target', 'excluded', 'is'):
                suffix = 'ScopeFullyReported' if stem == 'is' else 'Scope'
                term = (ECO if stem == 'is' else dimension['namespace']) + stem + dimension['dimension'] + suffix
                if source.get(term):
                    # Source flags replaced by an explicit assertion remain in the report.
                    if stem == 'is' and scope_decision != 'source':
                        withheld[term] = 'Original dimension flag retained; target flag is an explicit reviewed assertion.'
                    else:
                        consumed.add(term)
    if copied['survey-target'] and not any(name == 'survey-target' for name, _ in records):
        for term, table in TARGET_TABLE.items():
            if table == 'survey-target' and term in consumed:
                consumed.discard(term)
                withheld[term] = _NO_TARGET
    for term in (SCOPE_TERMS | IRI_SCOPE_TERMS) - consumed:
        if source.get(term) and term not in withheld:
            withheld[term] = ('A row with both a text scope and a web-link (IRI) scope keeps only the text one; the link stays only in your original files.'
                              if term in IRI_SCOPE_TERMS and literal_scope_present else
                              reason or 'This scope value is not converted under your answer, so it stays only in your original files.')
    return records, consumed, withheld


def _audit(term, disposition, target, prerequisites, reason):
    return term, {'term': term, 'namespace': 'ecoiri' if term.startswith(ECOIRI) else 'eco',
                  'disposition': disposition, 'target': target, 'prerequisites': prerequisites, 'reason': reason}


# eco:SurveyTarget records have their own grain: several descriptor rows share one surveyTargetID and link to
# surveys. A Humboldt eco:Event extension row describes one survey and no registered SurveyTarget source table is
# supported, so survey targets are never synthesized from these values.
_TARGET_MODEL = ('These columns describe survey targets, which in a Data Package need their own table of targets linked to '
                 'surveys. ChatIPT cannot build that table from rows that each describe one survey, so the values are left out '
                 'of the Data Package.')
# The pinned survey table has only a literal field: IRIs are not copied into literal fields, nor literals treated as IRIs.
_NO_IRI_FIELD = ('The Data Package has only a text field for this, so web links (IRIs) are not copied into it and the values '
                 'are left out of the Data Package.')
# Audit of current-vocabulary properties outside the registered 57-field XML (hc 2026-05-26 term list,
# DwC-DP 1.0, rs.tdwg.org 88a4b91f). Dispositions: imported, reviewed-import (column review required), preserved.
AUDIT = dict([
    _audit(ECO + 'surveyID', 'imported', 'survey.surveyID',
           'Exact term IRI in a Humboldt eco:Event extension row with a reviewed survey role. Distinct surveys must not share a value.',
           'A literal identifier of the survey that the extension row describes.'),
    *[_audit(ECO + name, 'preserved', target, 'A SurveyTarget source table model with surveyTargetID keys and survey links.', _TARGET_MODEL)
      for name, target in (('surveyTargetID', 'survey-target.surveyTargetID'),
                           ('surveyTargetType', 'survey-target-descriptor.surveyTargetType'),
                           ('surveyTargetValue', 'survey-target-descriptor.surveyTargetValue'),
                           ('surveyTargetUnit', 'survey-target-descriptor.surveyTargetUnit'),
                           ('includeOrExclude', 'survey-target-descriptor.includeOrExclude'))],
    _audit(ECO + 'isSurveyTargetFullyReported', 'preserved', 'survey-target.isSurveyTargetFullyReported',
           'A SurveyTarget source table model. The flag belongs to one complete target and is never copied to, or inferred for, other targets.',
           _TARGET_MODEL + ' Completeness flags are never copied or guessed.'),
    *[_audit(term, 'reviewed-import', 'survey-target-descriptor.surveyTargetValueIRI',
             'Humboldt eco:Event row; no literal scope in that row; one absolute IRI per cell; an inclusion for each dimension; '
             'a valid eco:is...ScopeFullyReported flag or an explicit completeness assertion (always required for combined dimensions '
             'and degree of establishment).',
             'IRI scope elements become descriptors with surveyTargetValueIRI and the dimension label. Mixed literal/IRI rows keep the IRI in the originals.')
      for term in sorted(IRI_SCOPE_TERMS)],
    _audit(ECOIRI + 'samplingPerformedBy', 'reviewed-import', 'survey.samplingPerformedByID',
           'Humboldt eco:Event row; one absolute IRI per cell; column review confirms the IRI identifies the sampling agent.',
           'The agent IRI is an identifier, so it is copied to the identifier field, never to the literal agent name.'),
    *[_audit(ECOIRI + name, 'preserved', 'survey-target-descriptor.' + field, 'A SurveyTarget source table model.', _TARGET_MODEL)
      for name, field in (('surveyTargetType', 'surveyTargetTypeIRI'), ('surveyTargetValue', 'surveyTargetValueIRI'),
                          ('surveyTargetUnit', 'surveyTargetUnitIRI'))],
    *[_audit(ECOIRI + name, 'preserved', '', 'A pinned target field with IRI semantics, or a reviewed linked-record design.', _NO_IRI_FIELD
             + (' The link is not used as the unit of the text value, which still needs a unit written as text.'
                if name.endswith('Unit') else '')
             + (' Links to methods would need their own method descriptions, not a copied link.' if name.endswith('Protocol') or name.endswith('Protocols') else ''))
      for name in ('absentTaxa', 'compilationSourceTypes', 'compilationTypes', 'eventDurationUnit', 'geospatialScopeAreaUnit',
                   'inventoryTypes', 'materialSampleTypes', 'nonTargetTaxa', 'protocolNames', 'samplingEffortProtocol',
                   'samplingEffortUnit', 'taxonCompletenessProtocols', 'taxonCompletenessReported', 'totalAreaSampledUnit')],
])
