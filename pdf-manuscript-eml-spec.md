# PDF Manuscript Ingestion + EML Enrichment Spec (Draft v0.3)

## Status
- Draft for discussion.
- Scope is intentionally MVP-first, with explicit open questions at the end.

## Confirmed Decisions (2026-04-08)
- Users can create a dataset from a PDF-only upload.
- PDF should be sent to OpenAI as file input.
- The extraction step should try to produce one or more raw tables.
- If no raw biodiversity data is found, keep the dataset/conversation saved but stop further workflow progression and notify user to upload a new dataset/PDF with actual data.
- Manuscript authors should become dataset creators; authenticated user remains contact.
- DOI should be stored in EML metadata only (not forcibly appended into `dataset.description`).
- If multiple candidate datasets are detected, parser should return them and the agent should ask the user whether to choose one or combine.
- Extraction should be synchronous for MVP.
- Enforce a PDF cap of 40 pages for MVP.
- PDFs over 40 pages are hard-rejected.
- Citation style should follow the manuscript citation.
- No mandatory standalone “metadata review” UI step in MVP.
- Use existing `rejected_at` when no raw biodiversity data is extractable from PDF.
- Persist all extracted candidate tables immediately as `Table` records.
- On every PDF upload (dataset creation or later paperclip upload), run PDF extraction by sending the PDF as OpenAI file input.
- For additional PDF uploads on existing datasets, include conversation history + user message + PDF file input so model can infer intent (metadata-only vs data extraction vs both).
- Prefer a multi-table creation tool shape (`CreateNewTables`) over single-table for parser-driven table creation.
- Only high-confidence extracted tables are materialized as `Table` records; low-confidence candidates are ignored.
- If a new dataset upload yields zero high-confidence data tables, mark dataset with `rejected_at`.

## Why
Current onboarding assumes tabular uploads and blocks dataset creation when no table can be loaded.
This prevents a paper-first workflow where users start from a manuscript PDF and then derive both:
- richer metadata (EML),
- and a concrete plan for what data must be extracted for GBIF publication.

## Current System Baseline
- Upload accepts tabular + tree extensions in frontend (`front-end/app/utils/uploadConstraints.js`).
- Backend infers file type as `tabular`, `tree`, or `unknown` (`back-end/api/models.py`).
- Dataset creation fails when no `Table` is created (`back-end/api/serializers.py`).
- Agent task sequence is spreadsheet-centric and starts at `Data structure exploration` (`back-end/api/fixtures/tasks.yaml`).
- EML generation currently supports:
- title, description (abstract),
- temporal/geographic/taxonomic scope,
- methodology,
- users (mapped to creators/personnel),
- contact from authenticated user profile,
- rendered via `make_eml` (`back-end/api/helpers/publish.py`).
- EML template does not currently include manuscript citation/DOI fields (`back-end/api/templates/eml.xml`).

## Product Goals
- Allow PDF-only dataset onboarding (no spreadsheet required at start).
- Send uploaded PDF files to OpenAI as file input for manuscript understanding.
- Extract raw data tables from manuscript content when possible.
- Auto-populate as much metadata as possible from manuscript content:
- abstract-derived dataset description,
- manuscript DOI in dataset metadata,
- manuscript authors as dataset creators (subject to user confirmation),
- methods text derived from manuscript methods section,
- journal + citation captured in EML-compatible fields.
- If only PDFs are uploaded, produce a clear “data extraction plan” for one dataset at a time.
- Keep one dataset object per workflow, even if manuscript suggests multiple possible datasets.

## Non-Goals (MVP)
- Fully automated perfect publication-ready row extraction from arbitrary PDF tables/figures.
- Multi-dataset branching from one manuscript.
- Replacing the full existing agent workflow for tabular-first users.

## Key Clarification on Citation in EML
- Dataset-level citation is represented in GBIF-profile EML under:
- `<additionalMetadata><metadata><gbif><citation>...`
- Record-level literature references should still use the Darwin Core `references` extension (`references.xml`) when needed.

## Proposed MVP Behavior

## 1) Upload and Dataset Creation
- Accept `.pdf` in frontend and backend file type inference.
- Allow dataset creation when:
- at least one valid tabular file exists, or
- at least one PDF exists.
- Keep rejection when files are neither tabular/tree/pdf.
- For PDF-only datasets, create dataset and initial agent instead of failing on “no tables”.
- Enforce 40-page limit on PDFs for MVP (hard reject files above this with clear message).

## 2) New PDF Extraction Service
- Add backend service: `api/helpers/pdf_extraction.py`.
- On PDF upload, run synchronous extraction pipeline:
- compute file fingerprint (SHA-256),
- cache/reuse prior extraction by fingerprint,
- upload file to OpenAI Files API,
- run extraction prompt using file input,
- request strict JSON output via schema, including extracted tabular outputs where possible.
- Store normalized extraction result in DB for prompting and EML mapping.
- For existing datasets, pass a concise conversation digest + latest user message to the parser call so it can decide whether to:
- only enrich metadata,
- create new tables,
- or do both.

### Suggested extracted JSON shape
- `manuscript_title`
- `manuscript_doi`
- `journal`
- `publication_year`
- `authors` (ordered list of names, optional ORCIDs/emails when present)
- `abstract`
- `methods_summary`
- `study_region`
- `taxa_summary`
- `candidate_dataset_count`
- `candidate_dataset_summaries` (array)
- `recommended_single_dataset` (object)
- `required_data_for_publication` (array of gaps: what must be extracted/provided)
- `extracted_tables` (array of tables; each with `name`, `columns`, `rows`, and extraction confidence/notes)
- `intent` (`metadata_only|extract_data|both|unclear`)
- `confidence` (0-1)
- `evidence` (short snippets + page numbers)

### Extraction outcomes
- `SUCCESS_WITH_TABLES`: one or more tables extracted; continue workflow.
- `SUCCESS_NO_RAW_DATA`: manuscript understood but no raw biodiversity data table recoverable; save extraction summary, set `rejected_at`, and notify user to upload another dataset/PDF.
- `FAILED`: parse failure (corrupt/encrypted/unreadable, model/tool failure); save error and notify user.
- Confidence gating rule:
- materialize only tables that meet configured confidence threshold,
- ignore uncertain/non-data candidates,
- if zero tables pass threshold on new dataset creation, treat as `SUCCESS_NO_RAW_DATA`.

## 3) Workflow Routing
- Introduce dataset source mode:
- `tabular_only`
- `pdf_only`
- `hybrid`
- Add first task for PDF-bearing datasets:
- `Manuscript extraction and dataset scoping`.
- Behavior:
- If `pdf_only`: this task runs first and produces metadata draft + extraction plan.
- If `hybrid`: task runs first but can immediately leverage uploaded tables.
- If `tabular_only`: task is skipped.
- Keep existing downstream tasks, but update task text to handle “tables may be absent yet”.
- If parser found multiple candidate datasets/tables, first task asks user whether to:
- publish one candidate dataset, or
- combine selected candidates into one publication dataset.
- Persist all candidate extracted tables immediately; agent can delete/discard tables after user direction.

## 3.2) Additional PDF Uploads on Existing Datasets
- Trigger on every newly uploaded PDF file in chat.
- Parser inputs:
- conversation digest (recent turns + current task context),
- latest user message,
- PDF file input.
- Parser decides intent:
- `metadata_only`: update manuscript-derived metadata candidate fields only; no new tables.
- `extract_data`: create one or more new tables and return summary.
- `both`: do both.
- `unclear`: return summary + explicit clarification question for the user.
- Assistant response requirement:
- always summarize what was extracted (metadata and/or tables),
- if multiple datasets/tables are inferred, ask what to keep/discard/proceed with.

## 3.1) Dedicated PDF Parser Prompt Contract
- Add a separate prompt template for extraction, e.g. `api/templates/pdf_parser_prompt.txt`.
- This prompt is not the same as the conversational agent `prompt.txt`.
- It should:
- include selected style/quality constraints from `prompt.txt` (anti-hallucination, explicit uncertainty, user-facing clarity),
- explicitly describe PDF parsing goals and output schema,
- request table extraction where possible,
- request clear “no raw biodiversity data” reasoning when applicable,
- request concise summary text suitable for user-facing assistant message.
- Inputs to parser prompt:
- PDF file input,
- optional user message attached to upload (if any),
- workflow context (`is_new_dataset`, existing tables count, existing metadata).
- Outputs from parser prompt:
- strict structured JSON for storage + table materialization,
- and a short narrative summary used in assistant response.

## 4) Agent Context Updates
- Extend prompt template to include:
- uploaded PDF list,
- extracted manuscript metadata block,
- extracted “required data for publication” checklist,
- confidence and uncertainty notes.
- Add explicit instruction:
- for `pdf_only`, do not fabricate occurrence rows;
- guide user to provide/confirm extractable data;
- keep one dataset scope.

## 5) EML/Data Model Enrichment
- Extend `dataset.eml` schema with manuscript-aware fields:
- `manuscript_doi`
- `manuscript_title`
- `journal`
- `publication_year`
- `dataset_citation`
- `abstract_source` (`user`, `manuscript`, `mixed`)
- `methods_source` (`user`, `manuscript`, `mixed`)
- `creators_source` (`user_profile`, `manuscript`, `mixed`)
- Update `SetEML` tool to accept and validate these fields.

### EML XML mapping (proposed)
- `dataset.description` from manuscript abstract if user has not provided better text.
- DOI:
- add as `<alternateIdentifier>` with canonical DOI URL (`https://doi.org/...`), or as plain DOI string if URL unavailable.
- Authors:
- map manuscript authors to `<creator>` entries (ordered as in manuscript).
- preserve authenticated user as `<contact>` by default.
- Dataset citation:
- add manuscript-style citation to `<additionalMetadata><metadata><gbif><citation>...`.
- Methods summary:
- map to `<methods><methodStep><description><para>...`.
- Journal mention:
- include in citation and optionally in abstract tail sentence.

## 6) “One Dataset at a Time” Rule
- If extraction returns multiple candidate datasets:
- select one recommended candidate automatically,
- present the alternatives briefly to user,
- continue only with selected one unless user overrides.

## API and Schema Changes

## Backend Models
- `UserFile.FileType`: add `PDF`.
- Add `PDF_EXTENSIONS = {'.pdf'}`.
- Add `Dataset.source_mode` (enum).
- Add `PdfExtraction` model (proposed):
- `user_file` (OneToOne),
- `status` (`pending|success|failed`),
- `fingerprint`,
- `openai_file_id`,
- `model`,
- `extracted_json` (JSONField),
- `error`,
- `created_at`, `updated_at`.

## Serializers / Views
- `UserFileSerializer.create`:
- detect PDF,
- trigger extraction service,
- do not attempt DataFrame creation.
- when file is PDF, always run extraction service synchronously before returning.
- `DatasetSerializer.create`:
- remove hard requirement for `dataset.table_set.exists()` when PDF present.
- if initial upload includes PDFs, trigger first assistant step that summarizes extracted outputs and asks user what to keep/proceed with.
- `MessageViewSet.perform_create`:
- include note for newly uploaded PDFs with extraction summary.
- if multiple candidate datasets/tables detected, append explicit question asking user which to proceed with and what can be discarded.

## Agent Tooling Additions
- Add tool `CreateNewTables` (preferred over `CreateNewTable`):
- input: `agent_id`, `tables` array.
- each table item:
- `title` (string),
- `csv` (string),
- optional `description`,
- optional `source_user_file_id`,
- optional `provenance_note`.
- behavior:
- parse CSV to pandas (`dtype='str'`),
- materialize each as `Table` row for dataset,
- return created `table_ids` + row/column counts + per-table parse warnings.
- Rationale:
- parser may extract multiple candidate datasets/tables in one pass.
- Keep existing Python tool for manual transformations; use `CreateNewTables` for deterministic parser-to-table ingestion.

## Frontend Changes
- Add `.pdf` to allowed/accepted extension list.
- Update composer copy:
- “Upload data files, tree files, or manuscript PDFs.”
- Update empty table state:
- from “Upload a spreadsheet...” to PDF-aware guidance.
- Show PDF extraction status chips:
- `Processing PDF`,
- `Metadata extracted`,
- `Needs review`.
- For PDF-only datasets, surface extracted metadata preview early in the UI.
- For multi-table extraction from one PDF, surface a compact summary card (table names, row counts, biodiversity signal) before or alongside tabs.

## Prompt/Task Changes
- Add new task text for manuscript extraction/scoping.
- Update spreadsheet-specific wording in existing tasks to:
- “uploaded files/tables/manuscript” instead of only “spreadsheet”.
- Keep strict anti-hallucination rule:
- if data rows are missing, agent must ask user for source data or confirm acceptable abstraction.
- For PDF parser prompt:
- explicitly instruct model to choose intent (`metadata_only|extract_data|both|unclear`) based on user message + conversation context.
- instruct it to call/create tables only when extraction is sufficiently grounded in PDF evidence.

## OpenAI Integration Design

## Current implementation
- Main conversational loop uses OpenAI Responses API with a backend compatibility adapter.
- PDF extraction uses OpenAI Responses API with file input.
- The backend stores assistant/tool messages in a stable internal shape (`role`, `content`, `tool_calls`, `tool_call_id`) so existing frontend flows remain unchanged.
- GPT-5 model family is used for PDF parsing.

## Why this design
- Single OpenAI API surface for conversation and extraction.
- Keeps legacy message contracts stable while reducing migration risk.
- Allows incremental UI improvements later without blocking backend transport migration.

## Validation and Guardrails
- Max PDF pages: 40 (MVP cap).
- Max file size follows OpenAI input constraints; enforce practical app-side cap if needed.
- Reject encrypted or unreadable PDFs with actionable error.
- Store extraction confidence and uncertain fields explicitly.
- Require user confirmation before final publication for manuscript-derived metadata.
- Never overwrite user-edited metadata silently.

## Observability
- Log per-PDF extraction lifecycle:
- upload detected,
- extraction started/finished/failed,
- model + duration + token usage,
- cache hit/miss.
- Add Discord/internal alert on repeated extraction failures.

## Security and Privacy
- PDFs may contain personal/sensitive info.
- Define retention policy for:
- raw uploaded PDFs,
- OpenAI file IDs,
- extracted snippets/evidence.
- Add redaction rules for logs (no raw manuscript content in plain logs).

## Test Plan
- Unit:
- file type inference includes PDF,
- dataset creation succeeds for pdf-only,
- extraction parser normalizes DOI/authors/citation,
- EML rendering includes new citation/alternateIdentifier fields.
- Integration:
- create dataset with only PDF,
- agent prompt includes extracted manuscript context,
- `SetEML` persists manuscript-derived fields,
- DwCA generation emits enriched EML.
- Frontend:
- extension acceptance,
- error states for unsupported PDFs/failed extraction,
- PDF-only empty table messaging.

## Rollout Plan
- Feature flag: `ENABLE_PDF_PIPELINE`.
- Phase 1:
- accept PDF upload + extraction + metadata population.
- Phase 2:
- strengthen extraction-plan UX and optional table extraction helpers.
- Phase 3:
- consider full Responses API migration for conversation runtime.

## Open Questions for Decision
1. What exact confidence threshold should we use for table materialization in MVP (e.g. `0.7`)?
