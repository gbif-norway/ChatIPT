# EOL implementation note

**Local integration:** the importer and converter now call these helpers. Row
review handles Text accounts, taxon-page signals and missing required identifiers
or media details. Full citations plus structured fields need separate approval.
Serialized combined-package validation passes in the repository's Compose service.
The original worker handoff follows.

Scope: `back-end/api/dwca_eol.py` and `back-end/api/test_dwca_eol.py`. No runtime
integration: `dwca_conversion.py`, `dwca_import.py` and other existing files are
unchanged. Sources: `remaining-fields.md`/`.json` (EOL Media and EOL References) and
the pinned 79-table schemas.

## Exports

- `EOL_FAMILIES`: `http://eol.org/schema/media/Document` → `eol-media`,
  `http://eol.org/schema/reference/Reference` → `eol-reference`.
- `eol_targets(row_type, term, values)` → `(targets, review_reason)` for a whole column.
- `eol_media_row_review(source)` → reason or `None`.
- `emit_eol_records(family, source, mapped, subject_table, subject_key, record_key)`.
- No `DERIVED_TERMS`. `pageStart`/`pageEnd` → `pages` composition is not implemented.

## Media planning

- Terms whose definitions agree with Simple Multimedia/Audubon go through the existing
  `media_targets`. This covers identifier, type, format, title, description, modified,
  rights, bibliographicCitation, creator, accessURI, furtherInformationURL, derivedFrom,
  CreateDate, Rating, UsageTerms and Owner. The result is
  `media`/`usage-policy`/`provenance` targets, with the literal/IRI split for format,
  rights and creator.
- `dcterms:language` is routed by value shape. A column of only IRIs goes to
  `media.languageIRI` (exact). A column of only literals goes to `media.language` with a
  review reason. A mixed column has no target and is preserved.
- These are reviewed aliases, never exact matches:
  - legacy `audubon_core/subtype` → `subtypeLiteral`/`subtypeIRI`, split by shape;
  - `Iptc4xmpExt:CVterm` → `subjectCategoryIRI`, for IRI-only columns.
- EOL namespaces stay distinct. `eol:referenceID`, `eol:agentID`, `eol:thumbnailURL` and
  `dwc:taxonID` have no target and carry a reason. In particular, `referenceID` is never
  used as a relationship key.
- Other terms are preserved without a reason: audience, publisher, contributor, spatial,
  LocationCreated and geo.
- Lookups use only the registered term IRIs. A term from another namespace with the
  same basename gets no match.
- `eol_media_row_review` flags the following:
  - Text rows (literal `Text` or the dcmitype IRI);
  - an empty source-required identifier, type or `xmpRights:UsageTerms`;
  - taxon-page signals (`taxonID`, or an SPMInfoItems CVterm).
- `emit_eol_records('eol-media', …)` raises `ImportFailure`. Root's existing media
  emitter and subject choices handle media rows.

## References

- Exact matches copy with no reason: `dcterms:title`, `bibo:pages`, `bibo:volume`,
  `bibo:edition`.
- These need review:
  - `dcterms:identifier` → `referenceID`;
  - `publicationType` → `referenceType`;
  - `authorList` → `author`;
  - `editorList` → `editor`;
  - `dcterms:publisher` → `publisher`/`publisherID`, split by shape, with mixed columns
    preserved;
  - `full_reference` → `bibliographicCitation`. Its reason states EOL's rule that
    full_reference makes the structured fields be ignored.
- These are preserved with a reason: `created` (never `issued`), `doi`, `uri` (never
  `referenceID`), `pageStart` and `pageEnd`. These are preserved without a reason:
  language, primaryTitle and localityName.
- The emitter accepts only `bibliographic-resource` fields from the whitelist. Each
  nonempty mapped value must equal the source value of a term approved for that field.
  This blocks DOI → referenceID and created → issued. The emitter also rejects:
  - filling both publisher and publisherID;
  - a missing source `dcterms:identifier`;
  - an all-empty description;
  - an unknown subject;
  - an empty subject key or record key.
- Each source row produces one `bibliographic-resource` row (`reference_pk = record_key`)
  and one join row (`occurrence`/`event`/`material`/`protocol` `-reference`).
  `relationshipType` is not set. There is no deduplication and no media-to-reference link.

## Tests run

These ran in Docker, in a `python:3.12-slim` image with `back-end/requirements.txt`
installed. The Compose `back-end` service could not be used:

- the build's apt step has no network;
- `.env.dev` and the `app` settings package are absent from the snapshot;
- `api/templates/extensions/` is also absent.

The tests ran on a scratch copy of `back-end` with three temporary stand-ins:

- minimal Django settings (sqlite, `api` app not installed);
- the missing extension XMLs, copied from `sources/extension`;
- `ggbn_*` copies of the GGBN XMLs under that prefix.

Results:

- `api.test_dwca_eol`: 18 passed.
- `api.test_dwca_media` and `api.test_dwca_references`: 27 passed (regression check).

The full suite and conversion runtime were not run. Root should rerun these in the real
Compose service.
