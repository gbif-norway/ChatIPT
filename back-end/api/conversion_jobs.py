"""A separate durable queue for archive conversion; publication agents never run here."""
import hashlib
import json
import logging
import tempfile
from datetime import timedelta
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from api.dwca_import import ConversionError, ImportFailure, read_inputs, source_zip
from api.dwca_conversion import RULE_VERSION, build_plan, convert
from api.conversion_evidence import publication_metadata, source_eml_content
from api.dwca_eml_descriptor import extract_eml_descriptor_metadata
from api.dwca_value_ledger import build_value_disposition_ledger
from api.dwca_semantic_audit import audit_semantic_values
from api.conversion_names import LEASE_SECONDS
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive, validate_dwc_dp_resources, validate_eml
from api.models import DwcConversion, DwcConversionJob, Table

logger = logging.getLogger(__name__)


def _eml_dataset_metadata(archive):
    # Dataset title and description hold up to 5,000 characters; the evidence summaries are shorter.
    return publication_metadata(archive, 5000)


def classify_failure(exc):
    """A structured record for any job failure (docs/dwca-conversion/tiered-review.md, 2c)."""
    if isinstance(exc, ConversionError):
        record = exc.as_conflict()
        if record['category'] == 'source' and not record['reason'].startswith('Unable to process'):
            record['reason'] = f"Unable to process this archive: {record['reason']}"
        return record
    category = 'source' if isinstance(exc, ImportFailure) else 'transient' if isinstance(exc, (OSError, DatabaseError)) else 'internal'
    message = str(exc)[:2000]
    if category == 'source' and not message.startswith('Unable to process'):
        message = f'Unable to process this archive: {message}'
    return ConversionError(message, category=category).as_conflict()

def _decisions_digest(decisions):
    return hashlib.sha256(json.dumps(decisions or {}, sort_keys=True).encode()).hexdigest()


def _same_internal_failure(previous, record):
    return all(previous.get(key) == record[key] for key in ('category', 'action', 'rule_version', 'decisions_sha256'))


def internal_retry_exhausted(conversion):
    """True when the same rules already failed twice internally on these exact choices."""
    attempt = {'category': 'internal', 'action': 'convert', 'rule_version': RULE_VERSION,
               'decisions_sha256': _decisions_digest(conversion.decisions)}
    return any(conflict.get('repeated') and _same_internal_failure(conflict, attempt) for conflict in conversion.conflicts or [])


def apply_failure(conversion, record, action='convert'):
    """Return to review only when a decision can remedy the failure, or a retry can.

    Internal failures are converter defects. A failed convert returns to review with
    its choices kept and one retry offered; when the same rules fail again on the same
    choices, a further retry waits for a fix (a new RULE_VERSION) or a changed choice.
    A failed inspect keeps the previous plan and choices untouched and offers re-inspection.
    """
    record['action'] = action
    repeated = False
    if record['category'] == 'internal':
        record.update(rule_version=RULE_VERSION, decisions_sha256=_decisions_digest(conversion.decisions))
        repeated = any(_same_internal_failure(previous, record) for previous in conversion.conflicts or [])
        record['repeated'] = repeated
    conversion.error = record['reason'][:5000]
    conversion.conflicts = [record]
    conversion.retryable = record['category'] == 'transient' or (record['category'] == 'internal' and not repeated)
    remedy = record['category'] in {'conflict', 'decision'} and record['decision_ids']
    internal_convert = record['category'] == 'internal' and action == 'convert'
    if conversion.plan and (remedy or internal_convert or record['category'] in {'stale-plan', 'transient'}):
        conversion.status = 'review'
    elif record['category'] in {'source', 'conflict', 'decision'}:
        conversion.status = 'blocked'
    else:
        conversion.status = 'failed'

def load_sources(conversion):
    sources = []
    for source in conversion.dataset.user_files.order_by('id'):
        with source.file.open('rb') as stream:
            sources.append((source.source_manifest['original_name'], stream.read()))
    return read_inputs(sources, drop_unlinked_extension_rows=conversion.drop_unlinked_extension_rows)

def _claim(now):
    """Lock a conversion with a due job, then its job (one lock order: conversion, then job; §5.9)."""
    from django.db.models.functions import Coalesce
    from api.conversion_review import lease_seconds
    review_stale = now - timedelta(seconds=lease_seconds())
    due = (Q(job__claimed_at__isnull=True)
           | Q(job__action__in=['inspect', 'convert'], job__claimed_at__lt=now - timedelta(hours=1))
           | Q(job__action__in=['review', 'chat'], job_seen__lt=review_stale)
           | Q(job__action='names', job_seen__lt=now - timedelta(seconds=LEASE_SECONDS)))
    conversion = (DwcConversion.objects.select_for_update(skip_locked=True, of=('self',))
                  .annotate(job_seen=Coalesce('job__heartbeat_at', 'job__claimed_at'))
                  .filter(job__isnull=False).filter(due).order_by('job__id').first())
    if conversion is None:
        return None, None
    job = DwcConversionJob.objects.select_for_update().get(conversion=conversion)
    job.claimed_at = now
    job.heartbeat_at = None
    job.save(update_fields=['claimed_at', 'heartbeat_at'])
    return conversion, job

def process_next_conversion():
    now = timezone.now()
    with transaction.atomic():
        conversion, job = _claim(now)
        if job is None:
            return False
        if job.action == 'review':
            conversion.status = 'reviewing'
            conversion.save(update_fields=['status', 'updated_at'])
        elif job.action not in {'chat', 'names'}:
            conversion.status = {'inspect': 'inspecting', 'convert': 'converting'}.get(job.action, conversion.status)
            conversion.error = ''; conversion.save(update_fields=['status', 'error', 'updated_at'])
    if job.action in {'review', 'chat'}:
        # AI review and conversation have their own fenced runner and finisher.
        from api.conversion_review import process_job
        process_job(conversion.pk, job.pk, now, job.action)
        return True
    if job.action == 'names':
        # Name checks run beside review: the conversion keeps its status and the choices stay editable.
        from api.conversion_names import process_job as process_names_job
        process_names_job(conversion.pk, job.pk, now)
        return True
    conversion = DwcConversion.objects.select_related('dataset').get(pk=conversion.pk)
    output_content = None
    additional_tables = {}
    try:
        if job.action == 'inspect':
            # Everything is built first, so a failure leaves the previous plan and its choices intact.
            archive = load_sources(conversion)
            plan = build_plan(archive)
            eml_metadata = _eml_dataset_metadata(archive)
            sources, filled = {}, {}
            for field in ('title', 'description'):
                if (getattr(conversion.dataset, field) or '').strip():
                    sources[field] = 'user'
                elif eml_metadata[field]:
                    filled[field] = eml_metadata[field]
                    sources[field] = 'eml'
                else:
                    sources[field] = 'none'
            from api.conversion_names import carry_decisions
            name_review = carry_decisions(conversion, _collect_names(archive, plan), plan)
            for field, value in filled.items():
                setattr(conversion.dataset, field, value)
            conversion.plan = plan
            conversion.metadata_sources = {**sources, **({'truncated_from_eml': sorted(eml_metadata['truncated'])}
                                                         if eml_metadata['truncated'] else {})}
            conversion.decisions = {}; conversion.review = {}; conversion.report = {}
            conversion.conflicts = []; conversion.retryable = False
            conversion.name_review = name_review
            conversion.status = 'review'
        elif job.action == 'convert':
            archive = load_sources(conversion)
            resources, report = convert(archive, conversion.plan, conversion.decisions)
            resources = _apply_names(conversion, archive, resources, report)
            if 'taxonomy' in report:
                from api.dwca_taxon import taxonomy_tables
                additional_tables = taxonomy_tables(archive, report)
            conversion.report = report
            if not report['validation']['valid']:
                # Preflight should make this unreachable; a failing validator indicates a converter defect.
                raise ConversionError('Validation needs attention: ' + '; '.join(report['validation']['errors'][:10]), category='internal')
            source_eml = source_eml_content(archive)
            eml = source_eml if source_eml is not None and not validate_eml(source_eml) else None
            descriptor_metadata = {}
            source_metadata = {}
            metadata_warnings = []
            if source_eml is not None:
                try:
                    promoted = extract_eml_descriptor_metadata(source_eml)
                    descriptor_metadata = promoted['descriptor']
                    source_metadata = promoted['source_metadata']
                    metadata_warnings = promoted['warnings']
                except (ValueError, TypeError) as error:
                    metadata_warnings = [{'field': 'eml.xml', 'reason': f'Metadata could not be promoted: {error}'}]
            sources = conversion.metadata_sources or {}
            report['metadata'] = {
                'eml': 'original EML 2.2.0 included' if eml else 'original metadata retained in source-originals.zip; no replacement metadata invented',
                # Recorded when inspection filled the fields, not inferred from matching text.
                'title_source': sources.get('title', 'user' if (conversion.dataset.title or '').strip() else 'none'),
                'description_source': sources.get('description', 'user' if (conversion.dataset.description or '').strip() else 'none'),
                **({'truncated_from_eml': sources['truncated_from_eml']} if sources.get('truncated_from_eml') else {}),
                'descriptor_fields_from_source_eml': sorted(descriptor_metadata),
                'source_only': source_metadata,
                'warnings': metadata_warnings,
            }
            report['value_disposition'] = build_value_disposition_ledger(conversion.plan, report, resources)
            report['semantic_value_audit'] = audit_semantic_values(archive)
            from api.conversion_review import report_section
            report.update(report_section(conversion))
            report['uploads'] = conversion.plan['uploads']
            originals = [('source-originals.zip', source_zip(archive.files)),
                         ('conversion-report.json', json.dumps(report, ensure_ascii=False, indent=2).encode())]
            if len(archive.uploaded_files) == 1 and next(iter(archive.uploaded_files)).lower().endswith(('.zip', '.dwca')):
                originals.append(('uploaded-archive.zip', next(iter(archive.uploaded_files.values()))))
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'dwc-dp.tar.gz'
                create_dwc_dp_archive(output, resources, conversion.dataset.title, conversion.dataset.description,
                    dataset_id=str(conversion.dataset_id), include_eml=eml is not None, eml_content=eml,
                    additional_files=originals,
                    declare_additional_resources=True, additional_tables=additional_tables,
                    descriptor_metadata=descriptor_metadata)
                report['archive_validation'] = validate_dwc_dp_archive(output, require_eml=eml is not None,
                    allow_generic=report.get('output_format') == 'taxonomy-data-package')
                output_content = output.read_bytes()
            conversion.conflicts = []; conversion.retryable = False
            conversion.status = 'complete'
        else:
            raise ImportFailure('Unknown conversion job action.')
    except Exception as exc:
        logger.exception('Conversion %s failed', conversion.pk)
        apply_failure(conversion, classify_failure(exc), job.action)
    old_output = conversion.output_file.name
    try:
        with transaction.atomic():
            DwcConversion.objects.select_for_update().filter(pk=conversion.pk).first()
            claimed_job = DwcConversionJob.objects.select_for_update().filter(pk=job.pk, claimed_at=now).first()
            if not claimed_job:
                return True
            if conversion.status == 'complete':
                # Store only validated outputs, replacing any prior generated tables together.
                conversion.output_file.save(f'dataset-{conversion.dataset_id}-{conversion.plan["id"][:12]}.tar.gz', ContentFile(output_content), save=False)
                conversion.dataset.table_set.all().delete()
                for name, dataframe in resources.items():
                    Table.objects.create(dataset=conversion.dataset, title=name, df=dataframe)
                for name, table in additional_tables.items():
                    Table.objects.create(dataset=conversion.dataset, title=name, df=table['dataframe'])
                if old_output and old_output != conversion.output_file.name:
                    transaction.on_commit(lambda: conversion.output_file.storage.delete(old_output), robust=True)
            elif job.action == 'inspect' and conversion.status == 'review':
                conversion.dataset.save(update_fields=['title', 'description'])
            conversion.save()
            if _chain_review(conversion, claimed_job, job.action) or _chain_names(conversion, claimed_job, job.action):
                return True
            claimed_job.delete()
            _post_failure_opener(conversion, job.action)
    except Exception as exc:
        logger.exception('Could not store conversion %s', conversion.pk)
        if conversion.output_file.name and conversion.output_file.name != old_output:
            try:
                conversion.output_file.storage.delete(conversion.output_file.name)
            except Exception:
                logger.exception('Could not clean up conversion output')
        with transaction.atomic():
            DwcConversion.objects.select_for_update().filter(pk=conversion.pk).first()
            claimed_job = DwcConversionJob.objects.select_for_update().filter(pk=job.pk, claimed_at=now).first()
            if claimed_job:
                record = ConversionError(f'Could not save output: {exc}'[:2000], category='transient').as_conflict()
                DwcConversion.objects.filter(pk=conversion.pk).update(status='review' if conversion.plan else 'failed', error=record['reason'],
                                                                      conflicts=[record], retryable=True, updated_at=timezone.now())
                claimed_job.delete()
    return True

def _collect_names(archive, plan):
    """The labels to check; collection reads only the archive, and a defect here never blocks inspection."""
    from api.conversion_names import collect_state
    try:
        return collect_state(archive, plan)
    except Exception:
        logger.exception('Could not collect scientific names')
        return {}

def _apply_names(conversion, archive, resources, report):
    """Overlay reviewed name decisions on the converted tables, record them, and validate the result again."""
    from api.conversion_names import apply_name_decisions, current, row_source_names
    state = current(conversion)
    sources = row_source_names(archive, report.get('row_crosswalk'), resources) if state.get('decisions') else {}
    resources, section = apply_name_decisions(resources, state, sources)
    if section is not None:
        report['name_review'] = section
        if section['reviewed']:
            report['validation'] = validate_dwc_dp_resources(resources)
    return resources

def _chain_names(conversion, job, action):
    """After a successful inspect (and any automatic AI review, which goes first), keep the job as a name check."""
    from api.conversion_names import pending
    if action != 'inspect' or conversion.status != 'review' or not pending(conversion):
        return False
    job.action = 'names'; job.claimed_at = None; job.heartbeat_at = None
    job.save(update_fields=['action', 'claimed_at', 'heartbeat_at'])
    return True

def _chain_review(conversion, job, action):
    """After a successful inspect, keep the job as an automatic AI review when there is work for it."""
    from api.conversion_review import should_auto_review
    if action != 'inspect' or conversion.status != 'review' or not should_auto_review(conversion):
        return False
    job.action = 'review'; job.claimed_at = None; job.heartbeat_at = None
    job.save(update_fields=['action', 'claimed_at', 'heartbeat_at'])
    conversion.status = 'reviewing'
    conversion.save(update_fields=['status', 'updated_at'])
    return True

def _post_failure_opener(conversion, action):
    """Explain a convert failure in the conversation: a remedy in review, or why the source must change."""
    if action != 'convert':
        return
    from api import conversion_chat
    from api.conversion_review import on_conflicts_recorded
    if conversion.status == 'review' and any(conflict.get('decision_ids') for conflict in conversion.conflicts):
        on_conflicts_recorded(conversion)
    elif conversion.status == 'blocked':
        conversion_chat.post_blocked_opener(conversion)
