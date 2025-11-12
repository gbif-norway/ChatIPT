# Backend Changes – Multi-file Upload Support

Check `README.md` for high-level project context.

## Goals
- Accept multiple tabular or tree files during dataset onboarding and throughout the chat flow.
- Track uploads independently from `Dataset` so we can rebuild tables or review metadata per file.
- Keep existing pandas ingestion working for spreadsheets while paving the way for phylogenetic parsing.
- Expose a clear API surface the frontend can rely on (`user_files`, roll-back helpers, uploads API).

## Current Backend Plan
- Introduce a lean `UserFile` model (`dataset`, `file`, `uploaded_at`) with helpers to infer type from extension.
- Migrate legacy data by backfilling `Dataset.file` values into the new `UserFile` table, then drop the old field.
- Refactor ingestion so dataset creation delegates to `UserFileSerializer`, which:
  - Stores the upload,
  - Runs basic dataframe extraction for tabular files,
  - Creates `Table` records per sheet just like the legacy flow.
- Return the full `user_files` collection on dataset endpoints; callers decide how to display names.
- Expose a `/api/user-files/` endpoint so the chat flow can add uploads after dataset creation.
- Update the roll-back tooling and prompt templates to operate on the full `UserFile` collection.
- Broadcast uploaded filenames to Discord so support staff see all inputs.

## Upload-to-Message Handling
- Uploading files remains a separate step from posting a chat message, but the message endpoint now inspects recent activity on the dataset to see which files and tables appeared after the previous user message.
- When a new user message is stored (including empty text), the backend appends an automated note that lists those filenames, includes any freshly created table IDs, and embeds a 200-character preview for tree files.
- Agents refresh their table linkage before every GPT call and regenerate the system prompt so it always includes the latest tables.
- The prompt template marks tables created after the latest non-system message as `(NEWLY CREATED)` and prints created/updated timestamps, making recent uploads obvious to GPT-5.
- This keeps GPT-5 aware of new data without persisting long-lived `UserFile` ↔ `Table` relationships.

## Notes
- All uploaded files are first-class; there is no “main file” concept anymore.
- Tree files (nexus/newick/tre) are stored for metadata only today—parsing remains a follow-up task.
- Dataset serializers return the entire `user_files` list so frontends can pick whichever naming scheme they prefer.
- Legacy Django templates now list every uploaded file instead of relying on a single filename.
- `UserFileSerializer` treats `dataset` as server-assigned so dataset creation can stream uploads without pre-existing IDs, while the `/api/user-files/` endpoint still injects the association.

## Open Questions / Next Steps
- Decide where tree parsing or validation utilities should live once we start ingesting their contents.
- Confirm whether we need per-table provenance (`Table.source_file`) once ad hoc uploads land.
- Coordinate with the frontend so chat uploads (paperclip) use the new API contract for multi-file payloads.

