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
extension row kept in originals is counted as retained. Beside a different
supplied quantity the count becomes an `individualCount` occurrence-assertion;
`derived_routes` reports how many rows took each route.
Zero counts never determine occurrence status.
eMoF vocabulary identifiers (`measurementTypeID`, `measurementValueID`,
`measurementUnitID`) reach the assertion `*IRI` fields. Missing-value tokens
such as `NA` are empty cells, counted once per column as `empty_placeholder`;
other non-IRI text is withheld and logged in `withheld_values`. A supplied
`identificationQualifier` follows the name text in a generated
`verbatimIdentification` and is reported as a derived verbatim copy; qualifiers
that cannot be placed are counted in `retained_reasons`.
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
mapped `*By` and `*ByID` fields. A single explicit agent IRI reuses its Agent
record; the ID alone decides identity, so two different IDs that share a name
remain two Agents. A name without an identifier links, by default, to one
Agent per exact name within the dataset, with one role row per mention
(production archives otherwise produced 4,209 Agent rows for one collector in
572 and 1,229 for one company in 563). Name agents carry no `agentID` and a
remark saying they stand for an exact name only. A name-only mention is never
merged into an explicit-ID Agent, even when the names are equal: it gets the
separate name Agent. This is a visible automatic choice: `agent-names` keeps
every name without an identifier as text only, and each repeated name has its
own `agent-share:` choice to keep that name as text only. Placeholder names
(`unknown`, `ukjent`, `NA`, `n/a`, `anon.`, `-`, `?`, `not recorded`, the
converter's empty-cell tokens and similar) never become Agents.
Names are compared after collapsing whitespace runs, in the converter's
explicit-ID agents and in role rows alike. A `|`-delimited `*ByID` list of
distinct single agent IRIs links one Agent and one role row per IRI when its
name field is empty or lists the same number of single names. `agentRoleOrder`
is the source ID order. Darwin Core states that list order conveys no meaning,
so names are never paired with IDs by position: an ID's `preferredAgentName`
comes only from mentions where that ID stands alone, and is empty otherwise.
Lists with empty segments, repeated IDs, values that are not absolute IRIs (such
as bare ORCID numbers) or a different number of names, and other composite
names and placeholders, remain in the mapped text fields and originals, with
skip reasons and examples in `agent_roles`. The converter does not infer whether
a name denotes a person or organization.

A foreign key is declared in `datapackage.json` only when both its source
fields and its target fields are present. An empty `recordedByID` column beside
an Agent table that holds only confirmed name-only agents (no `agentID` field)
therefore declares no key to `agent.agentID`.

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
| 558 | Occurrence assertions retain their linked subjects and measurement units; collectors and identifiers gain one Agent per exact name, with a role link per mention. Source dates absent from the archive remain absent. |
| 559 | Unique preserved specimen catalog identities create material records; counts gain a typed quantity pair. `0-0-0` identification dates are withheld; zero elevation/depth is flagged, age-like event remarks get reviewed routes, and agents gain roles. |

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
