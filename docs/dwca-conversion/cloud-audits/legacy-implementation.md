# BMDE and NBN implementation note

**Local integration:** the importer and converter now call these helpers. BMDE
occurrence assertions and event patches share a reviewed context choice. Missing
optional measurement columns carry empty values; supplied columns require joint
approval. Decimal hours with subminute precision are preserved without rounding.
NBN invalid/unsupported dates are withheld with reasons, and sensitive or
record-management flags receive row review. DELETE/NoObs rows stay in originals.
The original worker handoff follows.

Files: `back-end/api/dwca_legacy.py` and `back-end/api/test_dwca_legacy.py`. No
existing file was edited. The importer and converter do not call these helpers yet.

## Exports

- `LEGACY_FAMILIES`: `bmde:Observation` → `bmde`, `nxf:nxfOccurrence` → `nbn`.
- `LEGACY_DERIVED_TERMS`: the 72 BMDE measurement-group terms, `UTMZone/Easting/Northing`,
  `TimeObservationsStarted/Ended` and the three NBN vague-date terms.
- `legacy_targets(row_type, term, values)` returns `(targets, review_reason)`. Lookup
  uses the exact registered IRI. No term in either family is IRI-exact, so every
  target has a reason. Preserved terms that need an explanation return `([], reason)`.
- `emit_legacy_records(family, source, mapped, subject_table, subject_key, record_key)`.
  `record_key` is unused. Assertions have no primary key, and patches use `subject_key`.
- `legacy_row_review(row_type, source)` returns a reason string or `None`.
- `nbn_event_date(code, start, end)` is a helper shared by the emitter and row review.

## Behaviour

**BMDE with an Occurrence subject.** Each populated measurement group N (1–12) becomes
its own `occurrence-assertion` with `occurrence_fk = subject_key`. Groups are never
merged or deduplicated. The field pairing follows the runtime MoF rule, so
`MeasurementTypeN` goes to `verbatimAssertionType`, not the audit's `assertionType`.
Each group's six columns must be supplied together; the root may pass `''` for a
column that is absent. A group needs both a type and a value. Event values are
rejected with an Occurrence subject.

**BMDE with an Event subject.** This requires an explicit Event-context choice. The
emitter returns at most one patch, `('event', {'event_pk': subject_key, ...})`:

- `siteNumber` from `SurveyAreaIdentifier`.
- `georeferenceRemarks` from `CoordinatesScope`.
- `verbatimCoordinates` set to "zone easting northing", with `verbatimCoordinateSystem`
  `UTM`. All three UTM values are required.
- `eventTime` as `hh:mm` or `hh:mm/hh:mm`, converted from decimal local hours and
  rounded to the minute. An end before the start, a value of 24 or more, or text that
  is not a number is rejected.

Measurement groups are rejected with an Event subject.

**Preserved BMDE terms.** These stay in the originals:

- `individualCount` and `ObservationDescriptor`. No occurrence-patch contract exists,
  and "Presence/Absence" changes what the count means.
- Multinames and taxonomic authority terms.
- Specimen coordinates.
- Survey geometry and completeness, route and sampling structure.
- Effort, duration, observers, distances, markers, the atlas code and protocol terms.
  These would need invented assertion types, protocols or organisms.

`legacy_row_review` flags these rows:

- `RecordPermissions` other than 5.
- `LastModifiedAction` = `DELETE`.
- `NoObs`.
- "not accepted" or "pending review" status.
- A Presence/Absence count.
- An incomplete measurement group.

**NBN.** Only Event subjects are accepted. The emitter returns one event patch with
these fields:

- `eventDate`, for the vague-date codes D, DD, O, OO, Y and YY. Endpoints must be ISO
  `YYYY-MM-DD` dates and must align with the code: equal for D, month bounds for O/OO,
  1 Jan–31 Dec for Y/YY, and start ≤ end. The value is `start`, or `start/end` when
  they differ.
- `verbatimCoordinates` and `verbatimCoordinateSystem`, copied verbatim. A system
  without a grid reference is rejected.
- `locationID`, from `siteFeatureKey`.

Other codes (U, C, CC, -Y, Y-, lower-case variants and so on) are withheld. They get
no date, and row review reports them. Invalid dates, misaligned endpoints, missing
endpoints and a missing code raise `ImportFailure`. `gridReferencePrecision` and
`sensitiveOccurrence` are preserved. Precision never becomes
`coordinateUncertaintyInMeters`, and no `informationWithheld` or `dataGeneralizations`
text is written. Row review flags `sensitiveOccurrence` = true and any value that is
not true/false (the flag is required). It also flags date problems and a grid system
given without a grid reference.

## Integration notes for root

- Merge event patches only into empty or equal fields, and reject conflicts with the
  core (for example, the core `eventDate`).
- On Occurrence cores, BMDE event patches need the event key of the linked occurrence
  plus a per-table Event-context decision. Repeated occurrences produce repeated
  patches for the same event.
- The audit fixture expected `organismQuantity` from `individualCount`. That is not
  implemented here; it would need an approved occurrence-patch contract.
- The XML marks four NBN terms as required: the three date terms and
  `sensitiveOccurrence`. The audit summary says three.

## Verification

The tests were **not run**:

- The Docker daemon started, but `docker compose run back-end` fails because
  `back-end/.env.dev` is missing.
- Building the back-end image failed: `apt-get` got 403 responses from the Debian
  mirrors.
- Pulling `gbifnorway/chatipt-back-end:latest` failed with HTTP 429.

No application or test code was executed. Python was used outside Docker only to read
the audit JSON. The only static check was confirming that
every referenced BMDE/NBN IRI exists in `templates/dwca-conversion/registry.json`.
Run:

```sh
docker compose exec back-end python manage.py test api.test_dwca_legacy
```
