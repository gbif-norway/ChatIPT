# Frontend Changes – Multi-file Upload UX

This doc tracks the client-side updates required for ad hoc multi-file uploads in the chat interface.

## Goals
- Let users attach several spreadsheets and tree files from the chat composer (“paperclip” flow).
- Show every uploaded file in the conversation UI with filename, upload time, and type badge.
- Update dataset cards, sidebar history, and mailto helpers to rely purely on `user_files`.
- Surface basic validation feedback immediately when an upload fails (size, format, parse errors).

## Implementation To-Do
- [x] Consume `user_files` from the Dataset API in `Dataset.js`, `Sidebar.js`, and dashboard cards.
- [x] Wire the chat paperclip control to send `FormData` with `files[]` plus optional message text.
- [x] Display uploaded file chips in the composer with remove (X) controls prior to sending.
- [x] Provide an uploads panel (right-hand side) summarising filenames, detected types, and timestamps.
- [x] Add client-side extension checks to mirror backend acceptance (csv/tsv/txt/xlsx/*, newick/tre/nex).
- [x] Update docs/tooltips to clarify “Upload data files or phylogenetic tree files”.
- [x] Confirm empty-state messaging when no tables are produced (e.g. tree-only uploads).

## Open Questions
- (None for this milestone – duplicate detection, drag-and-drop uploads, and tree file visualisation are deferred.)

