# Darwin Core Archive conversion

This directory records the mapping audit for a separate DwC-A to DwC-DP workflow.
The initial output target is ChatIPT's vendored TDWG `1.0_DEV` schemas at
`76898192fd298c2aa170a7059e1bdadf3ee2a828`.

Mapping research is divided between core terms, extension families, and semantic
review policy. The implementation must preserve original files and metadata,
record every applied rule and unresolved source field, and execute transformations
deterministically. Model recommendations do not authorize dropping data or
inventing relationships.

Reference sources are pinned in
`/Users/rukayasj/Projects/sandbox/dwc-dp-guides/PROVENANCE.md`.

Three Claude Code research workers produced [core terms](core-catalogue.json),
[extension families](extension-catalogue.json), and a [semantic review policy](review-policy.md)
with [benchmark cases](benchmark-cases.json). The catalogues contain 168 proposed
rules and cover all 38 current production registry families plus 20 additional
sandbox families. Every named target was checked against the 79 pinned table
schemas. These are research proposals: their plain-language conditions are not
executable rules or a claim that every family is implemented.

The first implementation is a separate workflow selected by
`Dataset.workflow_type=dwca_conversion`. It accepts one ZIP/.dwca archive or
loose UTF-8 CSV/TSV/TXT files with optional XML metadata. Event, Occurrence and
Taxon cores are accepted. Taxon cores produce lossless additional taxonomy
tables; a checklist alone uses the generic taxonomy Data Package format, while
reviewed actual Occurrence extensions can also produce standard DwC-DP tables.
See [Taxon-core conversion](taxon-conversion.md). Loose roles use known filenames; joins use supplied core
identifiers, with a mandatory layout confirmation. A generated row number is
never allowed to stand in for a missing identifier when linking extensions.

Implemented extension paths are Occurrence on Event, Identification History,
MeasurementOrFact/eMoF, ResourceRelationship, DNA-derived data, Audiovisual/Audubon,
GBIF Multimedia, GBIF Images, GBIF Alternative Identifiers, GBIF Literature
References, Humboldt, EOL Media/References, Germplasm Accession/Score/Trait/Trial,
BMDE and NBN eXchange Format: twenty row-type families. Physical
material records require an explicit decision when material identifiers exist.
Unknown terms and unsupported extensions stay in the originals automatically when
no conversion is available. Taxon archives additionally retain every parsed source
column and extension in linked taxonomy tables. Organism
and interaction transformations from the research catalogues need
additional executable rules and fixtures. Media policies and provenance are
supported when their supplied fields have audited targets; core-level provenance
and licensing proposals still need implementation.

Media subject links require a choice between the linked occurrence, its event,
or an explicitly unlinked media resource. Each source media/reference row keeps
its own identity; identical media policy/provenance descriptions share records
with every source row retained in the crosswalk. Literal values in resource-valued
media terms require approval of an explicit alias. Mixed IRI/literal columns stay
in originals. Identifiers never become parent-resource or relationship keys merely
because the pinned schemas annotate several fields with the same term.

The [cloud worker results](cloud-workers.md) add term-by-term Humboldt, EOL,
germplasm, BMDE and NBN audits. Their 383 term entries have been checked against
the downloaded XMLs and pinned target fields. Reviewed portions now have executable
rules and integration fixtures; the full catalogues remain research proposals.
[Coordinator review](cloud-audits/coordinator-review.md) records runtime boundaries
and differences from the proposals.

Humboldt's 43 direct survey fields copy automatically when type, bound, value/unit
and consistency checks pass. Failed values are withheld individually. Survey scope
uses the exact ` | ` separator, and a single dimension with a valid supplied
completeness flag needs no extra scope decision. Missing flags, habitat, multiple
dimensions and degree of establishment require review. A combined target's
completeness is an explicit reviewer assertion, never a default. Surveys need a
reviewed survey event. Source rows remain separate unless identical merging is
chosen; Occurrence-core survey grouping requires complete identical event coverage.

The [Humboldt coverage assessment](humboldt-coverage.md) distinguishes the
57-field registered archive extension from the broader current TDWG vocabulary.
Parent Event links resolve against unique persistent eventIDs. Supplied
`eco:surveyID` is supported, ten IRI scope properties have reviewed imports, and a
strictly valid `ecoiri:samplingPerformedBy` copies to the agent identifier field.
The remaining supplemental properties have explicit preservation reasons in the
[vocabulary audit](humboldt-vocabulary-audit.json).
[Independent review](humboldt-review.md) verifies a real Event-core archive with
389 parent links and 390 Humboldt surveys. A separate
[scientific consistency audit](humboldt-scientific-consistency.md) checks supported
date bounds, spatial separation, scope and completeness claims. Findings appear
as nonblocking notices in the interface and report. No values are inherited or
repaired; full spatial containment and scientific equivalence remain unverified.

Rule version 9 combines the rule 8 review policy and hierarchy audits with
Taxon-core conversion and repeated AI advice batches. Nested occurrence plans
retain their automatic choices, source notices and hierarchy reports. Taxon
name-usage relationships remain separate from Event parent links.

EOL Text accounts remain in originals. Taxon-page signals and missing required
media details trigger row review. EOL references require source identifiers, and
full citations plus structured fields require separate approval. Germplasm
accessions attach only to reviewed material; passport statements retain their exact
source property IRIs. Scores have reviewed material/occurrence/event subjects;
material scores require an exact accession identifier match. Optional trait
protocol links require a unique exact match to a converted Trait Descriptor ID.
Trials patch existing Event cores, rejecting conflicts and year/date disagreement.
Incomplete Score rows and identifier-only Trait rows require preservation decisions
and do not prevent valid rows in the same file from converting.

BMDE's twelve measurement groups stay separate and attach only to occurrences.
Its UTM coordinates stay verbatim, and decimal local hours convert only when exact
whole-minute values can be represented without rounding. DELETE/NoObs rows stay
in originals. NBN grid references keep their original precision; sensitive or
invalid flags require row review. Only aligned D/DD/O/OO/Y/YY date codes convert;
other or invalid dates are withheld with reasons. Neither family invents absence,
coordinate uncertainty, survey completeness, agents or organisms. All non-exact
aliases and derived rules in these families require individual approval.

Rule version 12 adds a third `event-grain` choice for Occurrence cores where rows sharing an
eventID were sampled at different depths (for example one cast sampling several depths). It is
offered only when the source shows such a group. Occurrences sharing an eventID form one event; each
distinct supplied depth (`verbatimDepth`, minimum/maximum depth or distance above surface) becomes a
child event inside it that holds only those depth values, its eventCategory and its parent link,
and its occurrences link to it. The combined event carries no depth range and no child eventID is
created. Children are keyed by the source depth values, so retaining a depth column in the originals or
withholding an invalid value never merges distinct depths. Event details supplied by extensions (such as
NBN dates) patch the eventID event, never a depth child. Material can be combined by identifier only when
each identifier stays within one depth. Because it asserts that each depth is a separate sampling
action, it is a confirmed choice.

Rule version 13 treats conventional empty reference tokens as absent only when
they do not match a real source identifier. This allows mixed event and occurrence
measurements to use their respective explicit subjects, and parent-event links to
resolve, while preserving the original cell text. Independently resolvable parent
links are emitted even when other source links are missing, ambiguous or cyclic;
the report identifies each withheld link. Single absolute agent IRIs in mapped
`*ByID` fields produce deduplicated `agent` rows. A preferred name is included
only when paired single names agree for that ID. Conflicting preferred names
are never guessed. Name-only agents (one per exact name by default) and `|` ID lists follow the policy in
[fidelity-audit.md](fidelity-audit.md).

Rule version 11 fixes two silent losses found on a production archive. Event details (dates,
places, coordinates) supplied on Occurrence extension rows of an Event core are copied onto the
linked event when every occurrence of that event agrees with the others and with the event itself
(including year against eventDate and complete coordinate pairs); otherwise each occurrence row can
become its own event inside the linked event (a confirmed choice), or the details stay in the
originals. The report counts only values actually written. The supplied scientificName text is
always kept in verbatimIdentification; an exactly matching supplied scientificNameAuthorship is
removed from scientificName. Columns without a target are flagged `no-target` (the Data Package has
no field) or `unsupported` (not mapped yet) and are summarised rather than repeated as notices.

Rule version 10 adds the [tiered review contract](tiered-review.md): every question has a
kind and per-option assertion flags, row questions with identical meaning are grouped,
convert-time failures are precomputed as option requirements evaluated against the
effective decisions, and remaining failures are categorised (decision, conflict,
source, internal, transient, stale plan) with the choices that can remedy them.

The [streamlined review policy](streamlined-review.md) separates required choices
from conversion notices. Direct numeric, integer and boolean mappings copy
compatible cells automatically and record every withheld value; `NA` is never
silently recoded. Float-shaped years and unknown date tokens copy unchanged with
notices. Preserve-only outcomes, known extension roles and supported exact copies
no longer require redundant confirmation. Missing status/category, merging,
subject changes and reconstructed completeness remain explicit decisions.
Loose tables use canonical order (core first, then extension filenames), so upload
selection order cannot change internal keys. Exact ZIP bytes remain separately
recorded even when directory entry order differs.

Mapping plans record exact source IRIs, column profiles, grouping checks,
closed mapping choices, and file checksums. The converter uses explicit row
context and excludes technical keys and relationship fields from direct term
copying. Shared event/material identifiers are combined only when all approved
mapped values agree. Identification history is retained without inferring an
accepted determination; repeated core/history classifications are not merged
by name similarity. Identical sequence strings and complete protocol descriptions
share target records, while analyses retain source row multiplicity.

After inspection, an automatic AI reviewer (`gpt-6-sol`, high effort, Flex) checks the
remaining choices against bounded evidence packets: column profiles, sample rows, option
requirements, group evidence, pinned target definitions and the archive's EML. It may
apply only non-assertion options on AI-reviewable issues, with high confidence and cited
evidence, and never merges events (`event-grain`) until the offline benchmark supports it.
Everything else is escalated with its recommendation to a conversation that asks the user
in plain language. The conversation records answers to questions it asked through the
same validation; options that assert a new fact are applied only when the user clicks
Confirm on that exact option. Every decision has a provenance event (user, AI reviewer,
chat or system), later changes invalidate AI choices that depended on earlier ones, and
the dataset cost ceiling covers all conversion calls. Review and conversation run as
`review` and `chat` jobs on the conversion queue. See
[AI reviewer and conversation fallback](ai-review-and-chat.md). Tests use mocked model
responses; `CONVERSION_AI_REVIEW_ENABLED` is always off under the test runner.

The export validates the serialized tar.gz and includes `datapackage.json`,
mapped CSV resources, `conversion-report.json` (decisions, column dispositions,
checksums, a row crosswalk, per-column copied/retained counts, reviewed extension
subjects, withheld-value reasons and explicitly preserved rows), and
`source-originals.zip`. ZIP uploads also
include the exact original ZIP bytes as `uploaded-archive.zip`. Ancillary resources
are declared and checksum-checked. Original valid EML 2.2.0 can be included as
`eml.xml`; other original metadata stays in the originals, without creating a
replacement author or title. Structural validation does not establish semantic
equivalence, and preserved-only fields are explicitly reported.

Conversion jobs run through the existing `run_agent_turns` worker, with their own
queue and state. They never create publication agents or DwC-A/GBIF publication
tasks. Plans are checked for staleness, downloads use dataset ownership checks,
and failed export/storage work leaves sources available for retry.

Run migrations and workers through the normal Docker Compose startup. Targeted checks:

```sh
docker compose exec back-end python manage.py test api.test_dwca_review_policy api.test_dwca_scientific api.test_dwca_scientific_conversion api.test_dwca_hierarchy api.test_dwca_humboldt_vocabulary api.test_dwca_conversion api.test_dwca_media api.test_dwca_references api.test_dwca_humboldt api.test_dwca_eol api.test_dwca_germplasm api.test_dwca_legacy api.test_dwca_extensions api.test_dwca_taxon api.test_dwca_tiered_review api.test_conversion_review api.test_conversion_chat api.test_dwc_dp_validation api.test_agent_turns api.test_source_coverage --noinput
docker compose exec front-end npm test
docker compose exec front-end npm run lint
docker compose exec front-end npm run build
```

To rebuild the offline header lookup, copy the audited registry directory into
the backend container and run `python scripts/build_dwca_registry.py <audit-directory>`.
The generated registry records the exact URLs/checksums and prefers current
production definitions over sandbox/older definitions. Explicit `meta.xml` IRIs
are never rewritten by header lookup. Bump `RULE_VERSION` when changing executable
mapping behavior, and update profile/table schemas as one snapshot.

The local Akagera benchmark produced nine valid resources with 10,365 occurrences,
997 distinct sequence strings, and all 11,155 identification-history rows retained.
Repeated event and material identifiers remain because source coordinate uncertainty
conflicts block automatic event merging. The nine previously uploaded DwC-DP
tables are a comparison dataset, not a verified gold standard. Vocabulary choices
and other schema gaps are detailed in [core-notes.md](core-notes.md); do not turn
those proposals into automatic transformations without resolving the gaps.
