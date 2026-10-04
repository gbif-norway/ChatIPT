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
DIRECT = {field['dcterms:isVersionOf']: field for field in TABLE_SPECS['survey'].schema['fields']
          if field.get('dcterms:isVersionOf') in _REGISTERED
          and not field['name'].endswith(('_pk', '_fk'))}
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
    blocked = {term: 'Value does not meet the pinned type or bound.' for term, field in DIRECT.items()
               if source.get(term) and not valid_value(field, source[term])}
    for term in IRI_DIRECT:
        if source.get(term) and not is_iri(source[term]):
            blocked[term] = 'An ecoiri: value must be one absolute IRI; literals and lists are not copied into identifier fields.'
    for value, unit in _PAIRS.items():
        if source.get(value) and not source.get(unit, '').strip():
            blocked[value] = 'The source value requires a supplied unit.'
    for flag, detail in _CONTRADICTIONS.items():
        flag, detail = ECO + flag, ECO + detail
        if source.get(flag, '').lower() == 'false' and source.get(detail):
            reason = 'A false reporting/presence flag conflicts with the supplied detail.'
            blocked[flag] = blocked[detail] = reason
    total, area = ECO + 'totalAreaSampledValue', ECO + 'geospatialScopeAreaValue'
    if source.get(total) and source.get(area) and total not in blocked and area not in blocked:
        if source.get(_PAIRS[total]) == source.get(_PAIRS[area]) and Decimal(source[total]) > Decimal(source[area]):
            blocked[total] = blocked[area] = 'Sampled area exceeds scope area in the same supplied units.'
    return blocked


def humboldt_targets(term, sources, blocked_counts=None):
    if term in SCOPE_TERMS:
        return ['derive'], None
    if term in IRI_SCOPE_TERMS:
        return ['derive'], ('IRI scopes denote resources and stay distinct from literal scopes. A row converts them only when it has no literal '
                            'scope statement, each cell is one absolute IRI, and the usual completeness rules pass; they become '
                            'surveyTargetValueIRI descriptors without a literal label. Confirm, or keep them in the originals.')
    field = COPIED.get(term)
    if not field:
        entry = AUDIT.get(term)
        return [], (entry['reason'] if entry else None)
    count = blocked_counts.get(term, 0) if blocked_counts is not None else sum(term in blocked_fields(source) for source in sources)
    reason = (f'{count} source rows fail a type, unit or consistency check. Mapping copies compatible values only; flagged values stay in originals and the report.' if count else None)
    if term in IRI_DIRECT:
        reason = ' '.join(filter(None, ('Audited alias: an ecoiri:samplingPerformedBy IRI identifies the sampling agent, so it is copied to '
                                        'samplingPerformedByID, never to the literal samplingPerformedBy name.', reason)))
    if term in SUPPLEMENTAL:
        repeated = _repeated(source.get(term, '') for source in sources)
        if repeated:
            reason = ' '.join(filter(None, (f'{repeated} surveyID values occur on several rows. Separate surveys cannot share an identifier; '
                                            'combine one survey per event (identical rows only) or keep the column in the originals.', reason)))
    return ['survey.' + field['name']], reason


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
        raise ImportFailure('Survey scopes contain empty, padded or duplicate list elements; correct the source or preserve the scope.')
    return elements


def _literal_scope(source):
    return any(source.get(ECO + stem + dimension + 'Scope') for stem in ('target', 'excluded') for dimension in DIMENSIONS)


def _iri_elements(value):
    if not value:
        return []
    if not is_iri(value):
        raise ImportFailure('An ecoiri: scope must contain one absolute IRI. Lists and literals are not split or converted; preserve the scope.')
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
                raise ImportFailure('The same scope element is both included and excluded.')
        except ImportFailure as error:
            return [], str(error), False
        if not included:
            return [], 'An exclusion-only scope has no supplied inclusion universe. Preserve it or correct the source.', False
        if namespace == ECO and any(';' in item or ',' in item for item in included + excluded):
            reasons.append('Confirm that punctuation within scope values is literal; only the exact " | " separator will be split.')
        if flag.lower() not in {'true', 'false'}:
            reasons.append(f'{dimension} has no valid completeness flag; a reviewed assertion is required.')
        if dimension == 'DegreeOfEstablishment':
            reasons.append('Confirm degreeOfEstablishment as the target-type label; it is not establishmentMeans.')
        dimensions.append({'dimension': dimension, 'label': label, 'include': included, 'exclude': excluded, 'flag': flag,
                           'namespace': namespace})
    if len(dimensions) > 1:
        reasons.append('Multiple dimensions will form one combined target. Its single completeness flag needs an explicit assertion.')
    return dimensions, ' '.join(dict.fromkeys(reasons)) or None, True


def scope_review(source):
    dimensions, reason, convertible = scope_state(source)
    if not reason:
        return None
    options = [{'value': 'preserve', 'label': 'Retain scope in originals only'}]
    if convertible and dimensions:
        options += [{'value': 'reported-true', 'label': 'One combined target; I confirm it is fully reported'},
                    {'value': 'reported-false', 'label': 'One combined target; I confirm it is not fully reported'}]
    return {'reason': reason, 'options': options}


def emit_humboldt_records(source, mapped, event_key, survey_key, scope_decision='source', *, original_source=None):
    """Return records, consumed source terms, and explicit withheld-value reasons."""
    if not event_key or not survey_key:
        raise ImportFailure('Humboldt conversion requires a reviewed, emitted survey event.')
    allowed = {field['name'] for field in COPIED.values()}
    if set(mapped) - {'survey'} or set(mapped.get('survey', {})) - allowed:
        raise ImportFailure('Humboldt values contain an unaudited target.')
    blocked = blocked_fields(source)
    survey = {'survey_pk': survey_key, 'event_fk': event_key}
    consumed, withheld = set(), dict(blocked)
    for term, field in COPIED.items():
        value = mapped.get('survey', {}).get(field['name'], '')
        if value and term not in blocked:
            if source.get(term) != value:
                raise ImportFailure('A Humboldt target differs from its approved source value.')
            survey[field['name']] = value
            consumed.add(term)
    records = [('survey', survey)]
    literal_scope_present = _literal_scope(source if original_source is None else original_source)
    dimensions, reason, convertible = scope_state(source, literal_scope_present=literal_scope_present)
    if dimensions and scope_decision != 'preserve':
        if not convertible or (reason and scope_decision not in {'reported-true', 'reported-false'}):
            raise ImportFailure('Resolve the Humboldt scope review before creating survey targets.')
        if scope_decision == 'source':
            flag = dimensions[0]['flag']
        elif scope_decision in {'reported-true', 'reported-false'}:
            flag = scope_decision.removeprefix('reported-')
        else:
            raise ImportFailure('Unsupported survey scope decision.')
        target_key = str(uuid.uuid5(uuid.UUID(survey_key), 'survey-target'))
        records += [('survey-target', {'surveyTarget_pk': target_key, 'isSurveyTargetFullyReported': flag}),
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
    for term in (SCOPE_TERMS | IRI_SCOPE_TERMS) - consumed:
        if source.get(term) and term not in withheld:
            withheld[term] = ('Literal and IRI scopes in one row are not paired; the IRI scope stays in the originals.'
                              if term in IRI_SCOPE_TERMS and literal_scope_present else
                              reason or 'Scope value has no emitted target under the approved decision.')
    return records, consumed, withheld


def _audit(term, disposition, target, prerequisites, reason):
    return term, {'term': term, 'namespace': 'ecoiri' if term.startswith(ECOIRI) else 'eco',
                  'disposition': disposition, 'target': target, 'prerequisites': prerequisites, 'reason': reason}


_TARGET_MODEL = ('eco:SurveyTarget records have their own grain: several descriptor rows share one surveyTargetID and link to surveys. '
                 'A Humboldt eco:Event extension row describes one survey, and no registered SurveyTarget source table is supported, '
                 'so these values stay in the originals. Survey targets are never synthesized from them.')
_NO_IRI_FIELD = ('The pinned survey table has only a literal field for this property. IRIs are not copied into literal fields, and '
                 'literals are not treated as IRIs; values stay in the originals.')
# Audit of current-vocabulary properties outside the registered 57-field XML (hc 2026-05-26 term list,
# DwC-DP 76898192). Dispositions: imported, reviewed-import (column review required), preserved.
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
           _TARGET_MODEL + ' Completeness flags are not overwritten or guessed.'),
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
             + (' A supplied value is not treated as the unit of the literal measurement; the literal value still needs a literal unit.'
                if name.endswith('Unit') else '')
             + (' Protocol IRIs would need reviewed protocol records rather than a copied link.' if name.endswith('Protocol') or name.endswith('Protocols') else ''))
      for name in ('absentTaxa', 'compilationSourceTypes', 'compilationTypes', 'eventDurationUnit', 'geospatialScopeAreaUnit',
                   'inventoryTypes', 'materialSampleTypes', 'nonTargetTaxa', 'protocolNames', 'samplingEffortProtocol',
                   'samplingEffortUnit', 'taxonCompletenessProtocols', 'taxonCompletenessReported', 'totalAreaSampledUnit')],
])
