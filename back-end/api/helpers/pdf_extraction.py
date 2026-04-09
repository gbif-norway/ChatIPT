import hashlib
import io
import json
import logging
import re
import tempfile
import time
from copy import deepcopy
from typing import Any, Dict, List

import pandas as pd
from django.conf import settings
from django.template.loader import render_to_string
from openai import OpenAI

from api.helpers import discord_bot
from api.models import PdfExtraction, Table, UserFile

logger = logging.getLogger(__name__)


class PdfExtractionHardReject(ValueError):
    """Raised when a PDF must be rejected before extraction (e.g., page cap exceeded)."""

PDF_EXTRACTION_MODE_METADATA_ONLY = "metadata_only"
PDF_EXTRACTION_MODE_METADATA_AND_TABLES = "metadata_and_tables"
PDF_EXTRACTION_MODES = {
    PDF_EXTRACTION_MODE_METADATA_ONLY,
    PDF_EXTRACTION_MODE_METADATA_AND_TABLES,
}


PDF_EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "manuscript_title": {"type": ["string", "null"]},
        "manuscript_doi": {"type": ["string", "null"]},
        "journal": {"type": ["string", "null"]},
        "publication_year": {"type": ["integer", "null"]},
        "authors": {"type": "array", "items": {"type": "string"}},
        "abstract": {"type": ["string", "null"]},
        "methods_summary": {"type": ["string", "null"]},
        "study_region": {"type": ["string", "null"]},
        "taxa_summary": {"type": ["string", "null"]},
        "candidate_dataset_count": {"type": "integer"},
        "candidate_dataset_summaries": {"type": "array", "items": {"type": "string"}},
        "recommended_single_dataset": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "summary": {"type": ["string", "null"]},
                        "rationale": {"type": ["string", "null"]},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                "required": ["name", "summary", "rationale", "evidence"],
                },
            ]
        },
        "required_data_for_publication": {"type": "array", "items": {"type": "string"}},
        "intent": {
            "type": "string",
            "enum": ["metadata_only", "extract_data", "both", "unclear"],
        },
        "confidence": {"type": "number"},
        "extracted_tables": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": ["string", "number", "boolean", "null"]},
                        },
                    },
                    "confidence": {"type": "number"},
                    "notes": {"type": ["string", "null"]},
                },
                "required": ["name", "description", "columns", "rows", "confidence", "notes"],
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "snippet": {"type": "string"},
                    "page": {"type": ["integer", "null"]},
                },
                "required": ["snippet", "page"],
            },
        },
    },
    "required": [
        "summary",
        "manuscript_title",
        "manuscript_doi",
        "journal",
        "publication_year",
        "authors",
        "abstract",
        "methods_summary",
        "study_region",
        "taxa_summary",
        "intent",
        "confidence",
        "candidate_dataset_count",
        "candidate_dataset_summaries",
        "recommended_single_dataset",
        "required_data_for_publication",
        "extracted_tables",
        "evidence",
    ],
}


def extract_pdf_for_user_file(
    user_file: UserFile,
    *,
    is_new_dataset: bool = False,
    latest_user_message: str = "",
    conversation_digest: str = "",
    extraction_mode: str = PDF_EXTRACTION_MODE_METADATA_AND_TABLES,
) -> PdfExtraction | None:
    if user_file.file_type != UserFile.FileType.PDF:
        return None
    extraction_mode = str(extraction_mode or PDF_EXTRACTION_MODE_METADATA_AND_TABLES).strip().lower()
    if extraction_mode not in PDF_EXTRACTION_MODES:
        extraction_mode = PDF_EXTRACTION_MODE_METADATA_AND_TABLES

    extraction, _ = PdfExtraction.objects.get_or_create(user_file=user_file)
    extraction.status = PdfExtraction.Status.PENDING
    extraction.error = ""
    extraction.save(update_fields=["status", "error", "updated_at"])

    try:
        pdf_bytes = _read_user_file_bytes(user_file)
        page_count = _get_pdf_page_count(pdf_bytes)
        extraction.page_count = page_count

        max_pages = getattr(settings, "PDF_MAX_PAGES", 40)
        if page_count > max_pages:
            message = (
                f"PDF '{user_file.filename}' has {page_count} pages, which exceeds the "
                f"MVP limit of {max_pages} pages."
            )
            extraction.status = PdfExtraction.Status.FAILED
            extraction.error = message
            extraction.save(update_fields=["status", "error", "page_count", "updated_at"])
            raise PdfExtractionHardReject(message)

        fingerprint = hashlib.sha256(pdf_bytes).hexdigest()
        extraction.fingerprint = fingerprint

        cached = (
            PdfExtraction.objects.filter(
                fingerprint=fingerprint,
                status=PdfExtraction.Status.SUCCESS,
                extracted_json__isnull=False,
                extracted_json__extraction_mode=extraction_mode,
            )
            .exclude(user_file_id=user_file.id)
            .order_by("-updated_at", "-id")
            .first()
        )
        if cached:
            logger.info(
                "PDF extraction cache hit for dataset=%s user_file=%s fingerprint=%s",
                user_file.dataset_id,
                user_file.id,
                fingerprint[:12],
            )
            extraction.model = cached.model
            extraction.openai_file_id = cached.openai_file_id
            extraction.status = PdfExtraction.Status.SUCCESS
            extraction.extracted_json = deepcopy(cached.extracted_json)
            extraction.page_count = cached.page_count
            extraction.error = ""
            extraction.save(
                update_fields=[
                    "model",
                    "openai_file_id",
                    "status",
                    "extracted_json",
                    "page_count",
                    "error",
                    "fingerprint",
                    "updated_at",
                ]
            )
            _materialize_tables_from_extraction(extraction, extraction_mode=extraction_mode)
            return extraction

        model = getattr(settings, "PDF_EXTRACTION_MODEL", "gpt-5.4")
        extraction.model = model

        start = time.monotonic()
        parsed_json, openai_file_id, usage = _run_openai_pdf_extraction(
            pdf_bytes=pdf_bytes,
            filename=user_file.filename,
            model=model,
            is_new_dataset=is_new_dataset,
            existing_tables_count=user_file.dataset.table_set.count(),
            existing_metadata=user_file.dataset.eml or {},
            latest_user_message=latest_user_message or "",
            conversation_digest=conversation_digest or "",
            extraction_mode=extraction_mode,
        )
        duration = time.monotonic() - start

        normalized_json = _normalize_extraction_json(parsed_json, extraction_mode=extraction_mode)
        extraction.openai_file_id = openai_file_id or ""
        extraction.status = PdfExtraction.Status.SUCCESS
        extraction.error = ""
        extraction.extracted_json = normalized_json
        extraction.save(
            update_fields=[
                "status",
                "openai_file_id",
                "model",
                "extracted_json",
                "error",
                "fingerprint",
                "page_count",
                "updated_at",
            ]
        )

        materialized_ids = _materialize_tables_from_extraction(extraction, extraction_mode=extraction_mode)
        if materialized_ids != extraction.extracted_json.get("materialized_table_ids"):
            extracted_json = extraction.extracted_json or {}
            extracted_json["materialized_table_ids"] = materialized_ids
            extraction.extracted_json = extracted_json
            extraction.save(update_fields=["extracted_json", "updated_at"])

        logger.info(
            "PDF extraction finished dataset=%s user_file=%s status=%s duration=%.2fs "
            "usage=%s cache=miss",
            user_file.dataset_id,
            user_file.id,
            extraction.status,
            duration,
            usage,
        )
        return extraction
    except PdfExtractionHardReject:
        raise
    except Exception as exc:
        error_text = str(exc)[:4000]
        extraction.status = PdfExtraction.Status.FAILED
        extraction.error = error_text
        extraction.save(update_fields=["status", "error", "updated_at"])
        logger.exception(
            "PDF extraction failed dataset=%s user_file=%s: %s",
            user_file.dataset_id,
            user_file.id,
            error_text,
        )
        _send_repeated_failure_alert(extraction)
        return extraction


def build_dataset_conversation_digest(dataset, max_messages: int = 8) -> str:
    from api.models import Message

    messages = (
        Message.objects.filter(agent__dataset=dataset)
        .exclude(openai_obj__role=Message.Role.SYSTEM)
        .order_by("-created_at")[:max_messages]
    )

    lines = []
    for message in reversed(list(messages)):
        openai_obj = message.openai_obj or {}
        role = str(openai_obj.get("role") or message.role).strip().lower()
        content = _normalize_content_to_text(openai_obj.get("content"))
        if not content:
            continue
        compact = " ".join(content.split())
        lines.append(f"{role}: {compact[:700]}")

    return "\n".join(lines)


def summarize_extraction_for_note(extraction: PdfExtraction) -> str:
    extracted_json = extraction.extracted_json or {}
    outcome = extracted_json.get("outcome") or "-"
    extraction_mode = extracted_json.get("extraction_mode") or "-"
    summary = str(extracted_json.get("summary") or "").strip()
    confidence = extracted_json.get("confidence")
    table_ids = extracted_json.get("materialized_table_ids") or []
    candidate_count = extracted_json.get("candidate_dataset_count")

    parts = [f"{extraction.user_file.filename}: {extraction.status}/{outcome}/mode={extraction_mode}"]
    if confidence is not None:
        parts.append(f"confidence={confidence}")
    if table_ids:
        parts.append(f"tables={table_ids}")
    if candidate_count and candidate_count > 1:
        parts.append(f"candidate_datasets={candidate_count}")
    if summary:
        parts.append(f"summary={summary[:240]}")
    if extraction.error:
        parts.append(f"error={extraction.error[:240]}")
    return " | ".join(parts)


def _run_openai_pdf_extraction(
    *,
    pdf_bytes: bytes,
    filename: str,
    model: str,
    is_new_dataset: bool,
    existing_tables_count: int,
    existing_metadata: dict,
    latest_user_message: str,
    conversation_digest: str,
    extraction_mode: str,
):
    timeout_seconds = float(getattr(settings, "PDF_EXTRACTION_OPENAI_TIMEOUT_SECONDS", 600.0))
    with OpenAI(timeout=timeout_seconds) as client:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp.flush()
            with open(tmp.name, "rb") as pdf_handle:
                uploaded = client.files.create(file=pdf_handle, purpose="user_data")

        prompt = render_to_string(
            "pdf_parser_prompt.txt",
            context={
                "filename": filename,
                "is_new_dataset": is_new_dataset,
                "existing_tables_count": existing_tables_count,
                "existing_metadata_json": json.dumps(existing_metadata or {}, ensure_ascii=False),
                "latest_user_message": latest_user_message or "",
                "conversation_digest": conversation_digest or "",
                "extraction_mode": extraction_mode,
            },
        )

        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_file", "file_id": uploaded.id},
                    ],
                }
            ],
            text={"format": {"type": "json_object"}},
        )

    parsed = _extract_json_from_response(response)
    usage = None
    if getattr(response, "usage", None):
        usage_obj = response.usage
        usage = {
            "input_tokens": getattr(usage_obj, "input_tokens", None),
            "output_tokens": getattr(usage_obj, "output_tokens", None),
            "total_tokens": getattr(usage_obj, "total_tokens", None),
        }
    return parsed, uploaded.id, usage


def _extract_json_from_response(response) -> dict:
    text = ""
    if getattr(response, "output_text", None):
        text = str(response.output_text)
    else:
        output_items = getattr(response, "output", []) or []
        chunks = []
        for item in output_items:
            contents = getattr(item, "content", None) or []
            for content in contents:
                if getattr(content, "type", "") in {"output_text", "text"}:
                    chunk = getattr(content, "text", "") or ""
                    if chunk:
                        chunks.append(str(chunk))
        text = "\n".join(chunks)

    text = text.strip()
    if not text:
        raise ValueError("OpenAI PDF extraction returned an empty response.")

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("OpenAI PDF extraction response did not contain valid JSON.")
    return json.loads(match.group(0))


def _normalize_extraction_json(payload: dict, *, extraction_mode: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("PDF extraction payload must be a JSON object.")
    extraction_mode = str(extraction_mode or PDF_EXTRACTION_MODE_METADATA_AND_TABLES).strip().lower()
    if extraction_mode not in PDF_EXTRACTION_MODES:
        extraction_mode = PDF_EXTRACTION_MODE_METADATA_AND_TABLES

    tables = [_normalize_extracted_table(table, idx) for idx, table in enumerate(payload.get("extracted_tables") or [])]
    tables = [table for table in tables if table is not None]

    required_data = _normalize_str_list(payload.get("required_data_for_publication"))
    candidate_summaries = _normalize_str_list(payload.get("candidate_dataset_summaries"))

    candidate_count = _safe_int(payload.get("candidate_dataset_count"))
    if candidate_count is None:
        candidate_count = max(1 if candidate_summaries else 0, len(candidate_summaries))
    if candidate_count < len(candidate_summaries):
        candidate_count = len(candidate_summaries)

    confidence = _safe_float(payload.get("confidence"), default=0.0)
    intent = str(payload.get("intent") or "unclear").strip().lower()
    if intent not in {"metadata_only", "extract_data", "both", "unclear"}:
        intent = "unclear"

    threshold = float(getattr(settings, "PDF_TABLE_CONFIDENCE_THRESHOLD", 0.7))
    if extraction_mode == PDF_EXTRACTION_MODE_METADATA_ONLY:
        high_conf_tables = []
        tables = []
        outcome = "SUCCESS_METADATA_ONLY"
    else:
        high_conf_tables = [
            table for table in tables
            if table["confidence"] >= threshold and len(table["rows"]) > 0
        ]
        outcome = "SUCCESS_WITH_TABLES" if high_conf_tables else "SUCCESS_NO_RAW_DATA"

    evidence = payload.get("evidence") or []
    normalized_evidence = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("snippet") or "").strip()
        page = _safe_int(item.get("page"))
        if not snippet:
            continue
        normalized_evidence.append({"snippet": snippet[:500], "page": page})

    return {
        "summary": str(payload.get("summary") or "").strip(),
        "manuscript_title": _safe_text(payload.get("manuscript_title")),
        "manuscript_doi": _normalize_doi(payload.get("manuscript_doi")),
        "journal": _safe_text(payload.get("journal")),
        "publication_year": _safe_int(payload.get("publication_year")),
        "authors": _normalize_str_list(payload.get("authors")),
        "abstract": _safe_text(payload.get("abstract")),
        "methods_summary": _safe_text(payload.get("methods_summary")),
        "study_region": _safe_text(payload.get("study_region")),
        "taxa_summary": _safe_text(payload.get("taxa_summary")),
        "candidate_dataset_count": candidate_count,
        "candidate_dataset_summaries": candidate_summaries,
        "recommended_single_dataset": payload.get("recommended_single_dataset"),
        "required_data_for_publication": required_data,
        "intent": intent,
        "confidence": confidence,
        "extraction_mode": extraction_mode,
        "extracted_tables": tables,
        "high_confidence_table_count": len(high_conf_tables),
        "outcome": outcome,
        "materialized_table_ids": [],
        "evidence": normalized_evidence,
        "raw_parser_output": payload,
    }


def _normalize_extracted_table(table: Any, index: int):
    if not isinstance(table, dict):
        return None
    name = _safe_text(table.get("name")) or f"Extracted table {index + 1}"
    description = _safe_text(table.get("description"))
    notes = _safe_text(table.get("notes"))
    confidence = _safe_float(table.get("confidence"), default=0.0)
    columns = [str(col).strip() for col in (table.get("columns") or []) if str(col).strip()]
    rows = table.get("rows") or []

    if rows and isinstance(rows[0], dict):
        if not columns:
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for key in row.keys():
                    key_text = str(key).strip()
                    if key_text and key_text not in seen:
                        seen.add(key_text)
                        columns.append(key_text)
        normalized_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized_rows.append([_normalize_cell_value(row.get(col)) for col in columns])
    elif rows and isinstance(rows[0], list):
        normalized_rows = [[_normalize_cell_value(value) for value in row] for row in rows if isinstance(row, list)]
        if not columns:
            max_cols = max((len(row) for row in normalized_rows), default=0)
            columns = [f"column_{idx + 1}" for idx in range(max_cols)]
        normalized_rows = [
            row + [""] * (len(columns) - len(row)) if len(row) < len(columns) else row[:len(columns)]
            for row in normalized_rows
        ]
    else:
        normalized_rows = []

    return {
        "name": name[:200],
        "description": description,
        "columns": columns,
        "rows": normalized_rows,
        "confidence": confidence,
        "notes": notes,
    }


def _materialize_tables_from_extraction(
    extraction: PdfExtraction,
    *,
    extraction_mode: str | None = None,
) -> List[int]:
    extracted_json = extraction.extracted_json or {}
    extraction_mode = str(
        extraction_mode
        or extracted_json.get("extraction_mode")
        or PDF_EXTRACTION_MODE_METADATA_AND_TABLES
    ).strip().lower()
    if extraction_mode not in PDF_EXTRACTION_MODES:
        extraction_mode = PDF_EXTRACTION_MODE_METADATA_AND_TABLES

    if extraction_mode == PDF_EXTRACTION_MODE_METADATA_ONLY:
        extracted_json["materialized_table_ids"] = []
        extracted_json["high_confidence_table_count"] = 0
        extracted_json["outcome"] = "SUCCESS_METADATA_ONLY"
        extracted_json["extraction_mode"] = extraction_mode
        extraction.extracted_json = extracted_json
        extraction.save(update_fields=["extracted_json", "updated_at"])
        return []

    extracted_tables = extracted_json.get("extracted_tables") or []
    threshold = float(getattr(settings, "PDF_TABLE_CONFIDENCE_THRESHOLD", 0.7))
    fingerprint = extraction.fingerprint or ""
    materialized_table_ids: List[int] = []

    for table_payload in extracted_tables:
        confidence = _safe_float(table_payload.get("confidence"), default=0.0)
        if confidence < threshold:
            continue
        df = _table_payload_to_dataframe(table_payload)
        if df.empty:
            continue

        title = str(table_payload.get("name") or "Extracted table").strip()[:200]
        notes = str(table_payload.get("notes") or "").strip()
        description_parts = [
            f"Extracted from PDF: {extraction.user_file.filename}",
            f"Confidence: {confidence:.2f}",
            f"Fingerprint: {fingerprint}",
        ]
        if notes:
            description_parts.append(f"Notes: {notes}")
        description = " | ".join(description_parts)[:1900]

        existing = (
            Table.objects.filter(
                dataset_id=extraction.user_file.dataset_id,
                title=title,
                description__contains=fingerprint,
            )
            .order_by("-updated_at", "-id")
            .first()
        )
        if existing:
            existing.df = df
            existing.description = description
            existing.save(update_fields=["df", "description", "updated_at"])
            materialized_table_ids.append(existing.id)
            continue

        table = Table.objects.create(
            dataset_id=extraction.user_file.dataset_id,
            title=title,
            df=df,
            description=description,
        )
        materialized_table_ids.append(table.id)

    extracted_json["materialized_table_ids"] = materialized_table_ids
    extracted_json["high_confidence_table_count"] = len(materialized_table_ids)
    if materialized_table_ids:
        extracted_json["outcome"] = "SUCCESS_WITH_TABLES"
    else:
        extracted_json["outcome"] = "SUCCESS_NO_RAW_DATA"
    extracted_json["extraction_mode"] = extraction_mode
    extraction.extracted_json = extracted_json
    extraction.save(update_fields=["extracted_json", "updated_at"])
    return materialized_table_ids


def _table_payload_to_dataframe(table_payload: dict) -> pd.DataFrame:
    columns = [str(col).strip() for col in (table_payload.get("columns") or []) if str(col).strip()]
    rows = table_payload.get("rows") or []
    if not rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(rows, columns=columns if columns else None)
    if columns:
        for col in columns:
            if col not in df.columns:
                df[col] = ""
        df = df[columns]

    df = df.replace({pd.NA: "", None: ""})
    for col in df.columns:
        df[col] = df[col].astype(str)
    return df


def _read_user_file_bytes(user_file: UserFile) -> bytes:
    user_file.file.open("rb")
    try:
        return user_file.file.read()
    finally:
        user_file.file.close()


def _get_pdf_page_count(pdf_bytes: bytes) -> int:
    # Prefer robust parsing when pypdf is available.
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception:
        pass

    # Fallback heuristic when pypdf is unavailable.
    matches = re.findall(rb"/Type\s*/Page\b", pdf_bytes)
    if matches:
        return len(matches)
    raise PdfExtractionHardReject("Unable to read PDF page count. The file may be encrypted or malformed.")


def _normalize_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text.strip()
        return json.dumps(content, ensure_ascii=False)
    return str(content).strip()


def _normalize_str_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    seen = set()
    for value in values:
        text = _safe_text(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(text)
    return cleaned


def _normalize_cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def _safe_float(value: Any, default: float | None = None) -> float:
    if value is None:
        return 0.0 if default is None else default
    try:
        return float(value)
    except Exception:
        return 0.0 if default is None else default


def _normalize_doi(value: Any) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    text = text.replace("https://doi.org/", "").replace("http://doi.org/", "")
    text = text.replace("doi:", "").strip()
    return text or None


def _send_repeated_failure_alert(extraction: PdfExtraction):
    if not extraction.fingerprint:
        return
    threshold = int(getattr(settings, "PDF_EXTRACTION_FAILURE_ALERT_THRESHOLD", 3))
    failure_count = PdfExtraction.objects.filter(
        fingerprint=extraction.fingerprint,
        status=PdfExtraction.Status.FAILED,
    ).count()
    if failure_count < threshold:
        return
    try:
        discord_bot.send_discord_message(
            "PDF extraction repeatedly failed "
            f"(count={failure_count}, dataset={extraction.user_file.dataset_id}, "
            f"user_file={extraction.user_file_id})."
        )
    except Exception:
        logger.exception("Failed to send repeated PDF extraction failure alert.")
