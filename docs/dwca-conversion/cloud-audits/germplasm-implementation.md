# Germplasm extension helpers

**Local integration:** the converter now calls these helpers. Score types use
`verbatimAssertionType`; material scores check exact accession identifiers and
optional protocol links require a unique converted trait ID. Trial patches reject
conflicts and inconsistent years/dates. Altitude requires a finite number without
the worker's arbitrary elevation bounds. The original worker handoff follows.

Files: `back-end/api/dwca_germplasm.py` and `back-end/api/test_dwca_germplasm.py`. The rules come from
`remaining-fields.json`, the CLOUD_IMPLEMENTATION Germplasm task and `coordinator-review.md`. No runtime
files were edited. Root must wire the helpers into `dwca_conversion.py`.

## Exports

- `GERMPLASM_FAMILIES` maps the four exact row types to family names:
  `germplasm-accession`, `germplasm-score`, `germplasm-trait` and `germplasm-trial`.
- `germplasm_targets(row_type, term, values)` returns `(targets, review_reason)`:
  - Matching uses exact registered IRIs only.
  - Every target is a non-exact alias and carries a reason. The one exception is Trial `dwc:locationID`, which is IRI-exact but still reviewed.
  - Preserved columns return `[]`, sometimes with an explanation.
- `GERMPLASM_DERIVED_TERMS` holds columns that the emitter reads from `source`. The planner returns `[]` for them so they never collide in `mapped`:
  - the 31 Accession statement terms;
  - Score `measurementTraitName`;
  - Trial `geo:lat`, `geo:lon` and `geo:alt`.
- `emit_germplasm_records(family, source, mapped, subject_table, subject_key, record_key)`.

## Allowed subjects and output

| Family | Subject | Output |
| --- | --- | --- |
| Accession | `material` only | Optional `material-identifier` from `germplasmID`, plus one `material-assertion` per nonempty approved statement term. Each assertion has `assertionTypeIRI` = the exact source IRI. Rows follow the registered order, and `germplasmType#storageCondition` is included. |
| Score | `material`, `occurrence`, `event` | One `{subject}-assertion` row. The planner uses neutral `occurrence-assertion` fields; `{subject}-assertion` is also accepted in `mapped`. |
| Trait | `protocol` | One `protocol` row with `protocol_pk = subject_key`. It has no link rows and needs a name, method, source or remarks. |
| Trial | `event` | An `event` patch (`event_pk = subject_key`), an `event-identifier`, and a report as `bibliographic-resource` + `event-reference`. The reference row uses `reference_pk = record_key` and leaves `relationshipType` empty. |

The emitter raises `ImportFailure` in these cases:

- the subject does not match the family, or the subject key is empty;
- a target is not whitelisted, or values are not text;
- one row has contradictory values;
- the row is empty. Nothing is emitted for it; the table should be preserved instead.

## Decisions to review

- **Score `measurementType`.** It maps to `assertionType`, as in the audit and the `mof-measurement-type` catalogue rule. The current generic MoF path uses `verbatimAssertionType` instead. Root should choose one convention.
- **Score `measurementTraitName`.** It fills `assertionType` only when that field is empty. If `measurementType` differs, the row is rejected rather than overwritten.
- **Score required values.** A Score row needs both a value and a type.
- **Material subject check.** The emitter cannot check `germplasmID` against the material's identifiers, because it has no Django state. Root must do this before it offers or approves a material subject. `germplasmID`, `germplasmIdentifier`, `measurementTraitID`, `measurementTraitIdentifier`, `measurementTrial*`, `measurementByInstituteID` and `measurementGrowthStage` are preserved.
- **Protocol links.** The emitter never writes `assertionProtocol_fk`. A reviewed trait-ID protocol link is left to root.
- **Accession preservation.** `dwc:locationID`, `collectingInstituteID` and `geo:*` are preserved; there are no event patches from Accession. `germplasmID` is preserved when any value is a `data.gbif.org/occurrences` URL.
- **Accession records.** No material, acquisition, parent, relationship, agent or safety-duplicate records are created. Each source row emits its own rows, with no deduplication.
- **Trial checks.**
  - `year` must be an integer string.
  - `geo:lat` and `geo:lon` must both be present, numeric and within range.
  - `geo:alt` must be numeric (between -11000 and 9000). It fills both elevation fields.
  - No geodetic datum is inferred.
  - Consistency of `year` with `eventDate`, and conflicts with existing event values, are left to root's patch merge.
- **Trial spellings.** Trial matches only `measurementTrail*`. Score `measurementTrial*` IRIs never match Trial, and the reverse also holds; tests cover both.
- **Trial reports.** Report text is copied verbatim to `bibliographicCitation`, including URL-shaped values. No `referenceID` is substituted.
- **Assertion IDs.** `assertionID` uniqueness across rows is root's responsibility.

## Tests

`api.test_dwca_germplasm` has 20 tests, and all passed. They did **not** run in the Docker Compose `back-end` service, because the snapshot lacks `back-end/.env.dev`, `back-end/app/` settings and `start.sh`. Instead they ran in a plain `python:3.12-slim` container with Django 6.0.4, lxml 6.1.3 and a minimal throwaway settings module (not committed). The schema-validity check reads the pinned table-schema JSON directly: field names, required fields, integer/number lexical forms and bounds, and PK uniqueness. It does not run Frictionless or full package validation. The application suite was not run.
