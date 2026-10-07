"""Server-built quality report handed to the quality gate before its first model turn."""

import datetime
import json
import os
from urllib.parse import urlparse

import pandas as pd
from django.utils import timezone

from api import agent_tools


REPORT_FLAG = 'quality_report'
MAX_SAMPLES = 3
MAX_VALUE_CHARS = 80
MAX_SECTION_CHARS = 12000

# GBIF records these as interpretation notes rather than problems with the data.
INFORMATIONAL_GBIF_ISSUES = frozenset({
    'CONTINENT_DERIVED_FROM_COORDINATES',
    'CONTINENT_DERIVED_FROM_COUNTRY',
    'COUNTRY_DERIVED_FROM_COORDINATES',
    'COORDINATE_ROUNDED',
    'COORDINATE_REPROJECTED',
    'GEODETIC_DATUM_ASSUMED_WGS84',
})


def _clip(text, limit=MAX_SECTION_CHARS):
    return text if len(text) <= limit else text[:limit] + '\n...[section truncated]...'


def _sample_text(sample):
    related = sample.get('relatedData') or {}
    values = ', '.join(
        f"{str(term).split(':', 1)[-1]}={str(value)[:MAX_VALUE_CHARS]!r}"
        for term, value in related.items()
    )
    record = sample.get('recordId')
    prefix = f'record {record}' if record else 'sample'
    return f'{prefix}: {values or "(no values reported)"}'


def render_gbif_validation(validation, skip_files=()):
    status = validation.get('status', 'NOT_RUN')
    metrics = validation.get('metrics') or {}
    lines = [
        f"Status {status}; indexable: {'yes' if metrics.get('indexeable') else 'no'}; "
        f"validation key {validation.get('key')}; archive {validation.get('url')}"
    ]
    unfinished_steps = [
        f"{step.get('stepType')}={step.get('status')}"
        for step in metrics.get('stepTypes') or []
        if step.get('status') != 'FINISHED'
    ]
    if unfinished_steps:
        lines.append(f"Unfinished validator steps: {', '.join(unfinished_steps)}")
    for file in metrics.get('files') or []:
        if file.get('fileName') in skip_files:
            continue
        counts = []
        if file.get('count') is not None:
            counts.append(f"{file['count']} rows")
        if file.get('indexedCount') is not None:
            counts.append(f"{file['indexedCount']} indexed")
        header = f"- {file.get('fileName')}" + (f" ({', '.join(counts)})" if counts else '')
        issues = sorted(file.get('issues') or [], key=lambda issue: -(issue.get('count') or 0))
        if not issues:
            lines.append(header + ': no issues')
            continue
        lines.append(header + ':')
        informational = []
        for issue in issues:
            code = issue.get('issue')
            if code in INFORMATIONAL_GBIF_ISSUES:
                informational.append(f"{code} ({issue.get('count')})")
                continue
            lines.append(f"  * {code} [{issue.get('issueCategory', '')}]: {issue.get('count')} row(s)")
            seen = set()
            for sample in issue.get('samples') or []:
                values = json.dumps(sample.get('relatedData') or {}, sort_keys=True)
                if values in seen:
                    continue
                seen.add(values)
                lines.append(f"      e.g. {_sample_text(sample)}")
                if len(seen) == MAX_SAMPLES:
                    break
        if informational:
            lines.append(f"  Informational only: {', '.join(informational)}")
    return '\n'.join(lines)


def _dwc_dp_section(dataset):
    validation = agent_tools.current_dwc_dp_validation(dataset)
    errors = validation.get('errors') or []
    warnings = validation.get('warnings') or []
    lines = [f"Valid: {'yes' if validation.get('valid') else 'no'}; "
             f"{len(errors)} error(s), {len(warnings)} warning(s)."]
    lines += [f"- ERROR: {error}" for error in errors[:20]]
    lines += [f"- warning: {warning}" for warning in warnings[:30]]
    if len(errors) > 20 or len(warnings) > 30:
        lines.append('- ...more omitted; run ValidateDwcDp for the full list.')
    return '\n'.join(lines)


def _archive_section(dataset):
    try:
        inspection = agent_tools.inspect_publication_artifacts(dataset)
    except Exception as exc:
        return f"Could not inspect the exported archive: {exc}"
    inspection.pop('gbif_validation', None)
    return json.dumps(inspection, ensure_ascii=False)


def _section(build):
    try:
        return build()
    except Exception as exc:
        return f'This check could not run ({type(exc).__name__}: {str(exc)[:300]}); run it yourself if needed.'


def build_gate_report(dataset, gbif_note=None):
    sections = [
        ('DWC-DP VALIDATION OF THE AUTHORITATIVE TABLES', lambda: _dwc_dp_section(dataset)),
        ('SOURCE COVERAGE RECONCILIATION', lambda: agent_tools.source_coverage_report(dataset)),
        ('EXPORTED ARCHIVE AND EML (InspectPublicationArtifacts)', lambda: _archive_section(dataset)),
        (
            'GBIF VALIDATOR RESULT FOR THE CURRENT DwC-A',
            lambda: gbif_note or render_gbif_validation(dataset.dwca_validation or {}),
        ),
    ]
    body = '\n\n'.join(f"## {title}\n{_clip(_section(build))}" for title, build in sections)
    return (
        "QUALITY REPORT (generated by ChatIPT before this stage started, for the current packages).\n"
        "These checks have already been run: do not repeat InspectPublicationArtifacts, "
        "ReconcileSourceCoverage, ValidateDwcDp, or ValidateDwCA for the same unchanged archive. "
        "GBIF sample record IDs are the DwC-A core identifiers. Start from these findings and use "
        "targeted Python only for evidence this report cannot establish.\n\n"
        + body
    )


def _parse_time(value):
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def prepare_gate_report(agent):
    """Return True once the report message exists; False while waiting on GBIF.

    Waiting costs no model turns: the turn worker retries this agent shortly.
    """
    from api.models import Message

    dataset = agent.dataset
    url = dataset.dwca_url
    validation = dataset.dwca_validation or {}
    gbif_note = None
    if not url:
        gbif_note = 'No current DwC-A exists, so it has not been validated.'
    elif validation.get('url') != url or not validation.get('key'):
        try:
            key = agent_tools.submit_gbif_validation(url)
        except Exception as exc:
            gbif_note = f'Could not submit the current DwC-A to the GBIF validator ({exc}). Call ValidateDwCA.'
        else:
            dataset.dwca_validation = {
                'url': url, 'key': key, 'status': 'RUNNING',
                'submitted_at': timezone.now().isoformat(),
            }
            dataset.save(update_fields=['dwca_validation'])
            return False
    elif validation.get('status') not in agent_tools.GBIF_TERMINAL_STATUSES:
        submitted_at = _parse_time(validation.get('submitted_at'))
        if submitted_at is None:
            dataset.dwca_validation = {**validation, 'submitted_at': timezone.now().isoformat()}
            dataset.save(update_fields=['dwca_validation'])
            return False
        waited = (timezone.now() - submitted_at).total_seconds()
        try:
            validation = agent_tools._fetch_gbif_validation(dataset, validation['key'], url)
        except Exception:
            validation = {}
        if validation.get('status') not in agent_tools.GBIF_TERMINAL_STATUSES:
            if waited < agent_tools._gbif_max_wait_seconds():
                return False
            gbif_note = (
                f"GBIF had not finished validating the current DwC-A after {int(waited // 60)} minutes. "
                f"Call ValidateDwCA with validation_key {dataset.dwca_validation.get('key')} to keep waiting."
            )
    Message.objects.create(agent=agent, openai_obj={
        'role': Message.Role.SYSTEM,
        'content': build_gate_report(dataset, gbif_note),
        REPORT_FLAG: True,
    })
    return True


REFINEMENT_TASK = 'Data validation and refinement'
REFINEMENT_REPORT_KIND = 'refinement_report'


def _blank(series):
    return series.isna() | series.astype('string').str.strip().fillna('').eq('')


def _accepted(value):
    return str(value).strip().casefold() in {'true', '1', 'yes'}


def _record_resources(dataset):
    return {
        agent_tools.normalize_resource_name(title)
        for title in dataset.table_set.values_list('title', flat=True)
    }


def provisional_core(dataset):
    """Flatten DwC-DP records into one occurrence-core table, only for GBIF validation."""
    tables = {}
    for table in dataset.table_set.all():
        name = agent_tools.normalize_resource_name(table.title)
        if name in agent_tools.DWC_DP_TABLE_NAMES:
            tables[name] = table.df
    if 'occurrence' in tables:
        base, pk, event_fk, identification_fk, basis = (
            'occurrence', 'occurrence_pk', 'event_fk', 'occurrence_fk', 'Occurrence')
    elif 'material' in tables:
        base, pk, event_fk, identification_fk, basis = (
            'material', 'materialEntity_pk', 'collectionEvent_fk', 'materialEntity_fk', 'MaterialEntity')
    else:
        return None
    core = tables[base].copy().reset_index(drop=True)
    if pk not in core or core.empty:
        return None

    event = tables.get('event')
    if event is not None and event_fk in core and 'event_pk' in event:
        event = event.drop_duplicates('event_pk')
        columns = ['event_pk'] + [c for c in event.columns if c not in core.columns and c != 'event_pk']
        core = core.merge(event[columns], how='left', left_on=event_fk, right_on='event_pk')

    identification = tables.get('identification')
    if identification is not None and identification_fk in identification:
        identification = identification[~_blank(identification[identification_fk])].copy()
        if 'isAcceptedIdentification' in identification:
            identification['_accepted'] = identification['isAcceptedIdentification'].map(_accepted)
            identification = identification.sort_values('_accepted', ascending=False, kind='stable')
        identification = identification.drop_duplicates(identification_fk)
        linked = core[[pk]].merge(
            identification, how='left', left_on=pk, right_on=identification_fk,
        )
        for column in identification.columns:
            if column in {identification_fk, 'identification_pk', '_accepted'}:
                continue
            values = linked[column].values
            if column in core:
                core[column] = core[column].where(~_blank(core[column]), values)
            else:
                core[column] = values

    if base == 'occurrence':
        _fill_collectors(core, tables)
    core['occurrenceID'] = core[pk].astype('string')
    if 'basisOfRecord' not in core:
        core['basisOfRecord'] = basis
    return core


# Agent roles that name the people who recorded or collected an occurrence or its specimen.
COLLECTOR_ROLES = frozenset({'collector', 'collectedby', 'recorder', 'recordedby'})


def _fill_collectors(core, tables):
    """Fill empty recordedBy the way GBIF reads a DwC-DP occurrence.

    A specimen archive stores collectors on material.collectedBy. GBIF falls back to it only
    through an unambiguous link (one material per occurrence via evidenceForOccurrenceID), and
    then to ordered collector agent roles (gbif/pipelines OccurrenceDwcaMapping). No value is
    joined from an ambiguous link.
    """
    def blank():
        # Only rows with neither names nor identifiers are filled, so a name never sits beside another person's identifier.
        names = _blank(core['recordedBy']) if 'recordedBy' in core else pd.Series(True, index=core.index)
        return names & (_blank(core['recordedByID']) if 'recordedByID' in core else True)

    material = tables.get('material')
    linked = pd.Series(pd.NA, index=core.index, dtype='object')
    if (material is not None and 'evidenceForOccurrenceID' in material and 'materialEntity_pk' in material
            and 'occurrenceID' in core):
        material = material[~_blank(material['evidenceForOccurrenceID'])]
        material = material[~material['evidenceForOccurrenceID'].duplicated(keep=False)]
        ids = core['occurrenceID'].where(~_blank(core['occurrenceID']))
        unique = ids.notna() & ~ids.duplicated(keep=False)
        by_occurrence = material.set_index('evidenceForOccurrenceID')
        linked = ids.where(unique).map(by_occurrence['materialEntity_pk'])
        if 'collectedBy' in by_occurrence:
            names = ids.where(unique).map(by_occurrence['collectedBy'])
            filled = blank() & names.notna() & ~_blank(names)
            if filled.any():
                if 'recordedBy' not in core:
                    core['recordedBy'] = pd.NA
                core['recordedBy'] = core['recordedBy'].astype('object').where(~filled, names)
                # An identifier comes along only with the names it belongs to, never beside other supplied names.
                if 'collectedByID' in by_occurrence:
                    identifiers = ids.where(unique).map(by_occurrence['collectedByID'])
                    fill = filled & identifiers.notna() & ~_blank(identifiers)
                    if 'recordedByID' not in core:
                        core['recordedByID'] = pd.NA
                    core['recordedByID'] = core['recordedByID'].astype('object').where(~fill, identifiers)

    agents = tables.get('agent')
    if agents is None or 'agent_pk' not in agents or 'preferredAgentName' not in agents:
        return
    names = agents.drop_duplicates('agent_pk').set_index('agent_pk')['preferredAgentName']
    subjects = (('occurrence-agent-role', 'occurrence_fk', core['occurrence_pk'] if 'occurrence_pk' in core else None),
                ('material-agent-role', 'materialEntity_fk', linked))
    for role_table, subject_fk, subject in subjects:
        roles = tables.get(role_table)
        if roles is None or subject is None or subject_fk not in roles or 'agentRole' not in roles:
            continue
        roles = roles[roles['agentRole'].astype('string').str.strip().str.casefold().isin(COLLECTOR_ROLES)].copy()
        if roles.empty:
            continue
        roles['_order'] = pd.to_numeric(roles['agentRoleOrder'], errors='coerce') if 'agentRoleOrder' in roles else 0
        roles['_name'] = roles['agent_fk'].map(names)
        roles = roles[roles['_name'].notna() & ~_blank(roles['_name'])].sort_values('_order', kind='stable')
        joined = roles.groupby(subject_fk, sort=False)['_name'].agg(lambda values: ' | '.join(dict.fromkeys(values)))
        values = subject.map(joined)
        fill = blank() & values.notna()
        if fill.any():
            if 'recordedBy' not in core:
                core['recordedBy'] = pd.NA
            core['recordedBy'] = core['recordedBy'].astype('object').where(~fill, values)


def _delete_archive(url):
    """Best-effort removal of a provisional archive once GBIF has read it."""
    try:
        from minio import Minio

        bucket = os.getenv('MINIO_BUCKET', '')
        path = urlparse(url).path.lstrip('/')
        if not bucket or not path.startswith(f'{bucket}/'):
            return
        Minio(
            os.getenv('MINIO_URI', ''),
            access_key=os.getenv('MINIO_ACCESS_KEY'),
            secret_key=os.getenv('MINIO_SECRET_KEY'),
        ).remove_object(bucket, path[len(bucket) + 1:])
    except Exception:
        pass


def render_refinement_report(dataset, note=None):
    record = dataset.provisional_dwca_validation or {}
    gbif = note or render_gbif_validation(record.get('validation') or {}, skip_files=('eml.xml',))
    if record.get('note'):
        gbif = f"{record['note']}\n{gbif}"
    return (
        "QUALITY REPORT (completion was not recorded yet). ChatIPT built a provisional DwC-A from "
        "the DwC-DP tables only to run the GBIF validator; it is not published and is not the final "
        "projection. GBIF sample record IDs are occurrence_pk or materialEntity_pk values, and "
        "informational notes need no action. Metadata is not checked here.\n"
        "For each finding, correct the authoritative DwC-DP tables, include it in one consolidated "
        "question to the user, or record it as an accepted limitation in structure_notes. Then call "
        "SetAgentTaskToComplete again; no second report is produced.\n\n"
        f"## DWC-DP VALIDATION\n{_clip(_section(lambda: _dwc_dp_section(dataset)))}\n\n"
        f"## GBIF VALIDATOR RESULT FOR THE PROVISIONAL DwC-A\n{_clip(gbif)}"
    )


def _deliver(dataset, record, note=None):
    record['delivered'] = True
    dataset.provisional_dwca_validation = record
    dataset.save(update_fields=['provisional_dwca_validation'])
    if record.get('url'):
        _delete_archive(record['url'])
    return render_refinement_report(dataset, note)


def _refinement_waiting_payload(record, failed_checks=0):
    return json.dumps({
        'status': 'RUNNING',
        'kind': REFINEMENT_REPORT_KIND,
        'message': (
            'ChatIPT is validating a provisional DwC-A with GBIF and will replace this result with '
            'a QUALITY REPORT automatically. No action is needed until then.'
        ),
        'validation_key': record['key'],
        'url': record['url'],
        'waiting_since': record['submitted_at'],
        'failed_checks': failed_checks,
        'next_recheck_at': (
            timezone.now() + datetime.timedelta(seconds=agent_tools._gbif_recheck_seconds())
        ).isoformat(),
    })


def refinement_report_delivered(agent):
    record = agent.dataset.provisional_dwca_validation or {}
    return record.get('agent_id') == agent.id and record.get('delivered') is True


def start_refinement_report(agent):
    """First refinement completion attempt: validate a provisional archive before completing."""
    from api.helpers.publish import upload_dwca
    from api.dwc_specs import DarwinCoreCoreType

    dataset = agent.dataset
    record = {'agent_id': agent.id, 'created_at': timezone.now().isoformat()}
    core = provisional_core(dataset)
    if core is None:
        return _deliver(dataset, record, (
            'Skipped: the package has no occurrence or material records to validate as a DwC-A.'
        ))
    if {'occurrence', 'material'} <= _record_resources(dataset):
        record['note'] = (
            'Only occurrence rows (with their event and accepted identification) were validated; '
            'material records were not validated separately.'
        )
    try:
        record['url'] = upload_dwca(
            core,
            dataset.title or 'Provisional validation',
            dataset.description or 'Provisional archive for GBIF validation only.',
            DarwinCoreCoreType.OCCURRENCE,
            # The flattened core keeps DwC-DP keys that have no DwC-A term.
            drop_unmapped_columns=True,
        )
        key = agent_tools.submit_gbif_validation(record['url'])
    except Exception as exc:
        return _deliver(dataset, record, f'Could not build or submit the provisional DwC-A: {exc}')
    record.update({'key': key, 'status': 'RUNNING', 'submitted_at': timezone.now().isoformat()})
    dataset.provisional_dwca_validation = record
    dataset.save(update_fields=['provisional_dwca_validation'])
    return _refinement_waiting_payload(record)


def refresh_refinement_report(dataset, waiting):
    """Server-side recheck; returns the waiting payload again or the finished report."""
    record = dataset.provisional_dwca_validation or {}
    if record.get('key') != waiting.get('validation_key'):
        return render_refinement_report(dataset, 'The provisional validation was replaced; no GBIF result.')
    failed_checks = int(waiting.get('failed_checks') or 0)
    try:
        validation = agent_tools.get_gbif_validation(record['key'])
    except Exception as exc:
        failed_checks += 1
        if failed_checks >= 5:
            return _deliver(dataset, record, f'Checking the GBIF validator failed repeatedly: {repr(exc)[:300]}')
        return _refinement_waiting_payload(record, failed_checks)
    record['status'] = validation.get('status')
    if record['status'] in agent_tools.GBIF_TERMINAL_STATUSES:
        record['validation'] = validation
        return _deliver(dataset, record)
    submitted = _parse_time(record.get('submitted_at'))
    if submitted and (timezone.now() - submitted).total_seconds() >= agent_tools._gbif_max_wait_seconds():
        return _deliver(dataset, record, 'GBIF did not finish validating the provisional DwC-A in time.')
    return _refinement_waiting_payload(record, failed_checks)
