# DwC-A to DwC-DP fidelity audit

The conversion report now carries `value_disposition` and `semantic_value_audit`.
`value_disposition.source_terms` accounts for every nonempty source term as
mapped, derived, withheld, retained only in originals, or unverified. Its counts
are source cells, not distinct target rows: several occurrences may share one
event. `emitted_but_flagged_values` annotates mapped values and overlaps the
other counts. Join keys without cell-level evidence remain **unverified**. The
`untraced_or_generated_output_fields` list identifies target fields without an
explicit field-level source trace; generated keys and links are expected there.

`semantic_value_audit` gives bounded examples for invalid `countryCode`, the
exact `dateIdentified` placeholder `0-0-0`, and zeros in elevation/depth.
Each distinct non-code country label on an Occurrence core has a review choice:
keep it in originals, or copy the exact text to `event.country` or
`event.waterBody`. Age-like `eventRemarks` on occurrence rows with unambiguous
event subjects can similarly remain on the event, move unchanged to
`occurrence.lifeStage` or `occurrence.occurrenceRemarks`, or remain only in
originals. The report records these decisions in `reviewed_value_routes`.
Unreviewed invalid country codes and `0-0-0` identification dates are withheld
from their semantic target fields and logged individually in `withheld_values`.
All source bytes stay in `source-originals.zip`. A zero elevation or depth can be genuine, so it is
reported without automatic suppression. Source `individualCount` becomes the
paired occurrence `organismQuantity`/`organismQuantityType=individuals` only
when it is a nonnegative integer and neither quantity field was supplied. This
holds for Occurrence extensions of Event cores as well as Occurrence cores; an
extension row kept in originals is counted as retained.
Zero counts never determine occurrence status.
eMoF vocabulary identifiers (`measurementTypeID`, `measurementValueID`,
`measurementUnitID`) reach the assertion `*IRI` fields; non-IRI tokens such as
`NA` are withheld and logged in `withheld_values`. A supplied
`identificationQualifier` follows the name text in a generated
`verbatimIdentification` and is reported as a derived verbatim copy.
The count transformation reports total mapped rows, its deterministic rule,
and up to 20 exact source/target examples; the existing row crosswalk and
original archive carry the full row provenance without duplicating every cell
in the report.

A source Occurrence table that declares every row `PreservedSpecimen` and has a
complete, unique `(institutionCode, collectionCode, catalogNumber)` triple for
every row now creates one material entity per row. The catalog, preparation,
institution, and collection fields can move onto that material record. The
generated material primary key is an internal key; the converter does not
invent a persistent `materialEntityID`. Duplicated or incomplete catalog
identities still require review.

The converter also emits `agent` and the relevant `*-agent-role` rows from
mapped `*By` and `*ByID` fields when an agent has a single explicit IRI or a
reviewed shared identity. A single explicit agent IRI reuses its Agent record.
Names without identifiers stay in their mapped text fields and originals.
Repeated exact names offer an advanced review choice to confirm one shared
identity for that name across all its mapped mentions; the default creates no
Agent or role rows for those name-only mentions.
Composite names, placeholders, and ID/name lists that cannot be paired safely
remain in the mapped text fields and originals, with skip reasons and examples
in `agent_roles`. The role order is explicit. The converter does not infer
whether a name denotes a person or organization.

Source EML metadata is selected through `meta.xml`'s declared metadata path,
with a single `eml.xml` as fallback. The Data Package descriptor can promote
contributors, keywords, and an unambiguous recognized license URL. Citation,
coverage, and free-text rights are described in the conversion report and the
original EML remains in `source-originals.zip`. A source EML document is placed
at the package root as `eml.xml` only when it passes the package's EML 2.2.0
validation. No EML version conversion or license inference is performed.

## Four production examples

| Dataset | Main fidelity check after reconversion |
| --- | --- |
| 556 | Shared event grouping still requires agreeing source event details; occurrence counts should gain a typed quantity pair. |
| 557 | Per-row event context and counts remain; each non-code `countryCode` label gets a reviewed route to country, water body, or originals. |
| 558 | Occurrence assertions retain their linked subjects and measurement units; collectors and identifiers gain Agent role links only after their repeated exact names are confirmed as shared identities. Source dates absent from the archive remain absent. |
| 559 | Unique preserved specimen catalog identities create material records; counts gain a typed quantity pair. `0-0-0` identification dates are withheld; zero elevation/depth is flagged, age-like event remarks get reviewed routes, and confirmed agents gain roles. |

## Schema status

The package remains pinned to the vendored TDWG `1.0_DEV` profile and its
revision/hash in `dwcDpSchema`. The [DwC-DP guide](https://dwc.tdwg.org/dp/)
refers to a versioned 1.0 profile, but the corresponding
[`rs.tdwg.org` URL](https://rs.tdwg.org/dwc-dp/1.0/dwc-dp-profile.json)
returned 404 when checked on 2026-10-05. The working `gbif/dwc-dp` profile
is labeled `1.0-DEV` and differs in table and field coverage; it is not a
ratified replacement for the pinned snapshot. Update the profile URL, complete
table schemas, converter mappings, and tests together when a published 1.0
artifact becomes available. A successful current export validates against the
pinned prerelease, not against an unpublished final schema.
