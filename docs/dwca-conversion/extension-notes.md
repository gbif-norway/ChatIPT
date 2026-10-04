# DwC-A extension mapping notes

Companion to `extension-catalogue.json`. Target: ChatIPT's vendored TDWG DwC-DP
`1.0_DEV` (79 table schemas) at `76898192fd298c2aa170a7059e1bdadf3ee2a828`,
`back-end/api/templates/dwc-dp`. The newer 81-table GBIF artefacts were not used.

Sources: `/Users/rukayasj/Projects/sandbox/dwc-dp-guides/audit/2026-10-02/`
(`production-extensions.json`, `sandbox-extensions.json`, `registry-files.json`,
`extension/*.xml`) and the TDWG conceptual model guide
(`tdwg-dwc-standard/docs/cm/index.md`). Term lists were read from each XML's
`qualName` attributes; target fields and their `dcterms:isVersionOf` were read
from the pinned table schemas. No host commands were run, so the JSON file was
checked by inspection only.

The later [field audits](cloud-workers.md) have been checked locally against the
downloaded XML and pinned schemas. They refine this coarse catalogue for Humboldt,
EOL, germplasm, BMDE and NBN. [Coordinator review](cloud-audits/coordinator-review.md)
records where their proposals differ from the implemented runtime; this document
is a research plan, not an implementation-coverage claim.

## Conventions

- **Dispositions.** `automatic`: lossless, no judgement. `conditional`: deterministic,
  but only when the listed checks pass; otherwise the row or value falls to review or
  preservation. `review`: needs a human (optionally aided by a model recommendation);
  nothing is written until approved, and the approval is logged. `unsupported`: no
  home in the pinned schema; preserve and report.
- **Field-level vs row-level.** Field rules marked `automatic` apply only after a
  row-level rule (e.g. `mof-subject-occurrence-core`, `hum-survey-row`) has placed the row.
- **Pairwise lists.** In multi-term rules, `source_terms[i]` maps to `targets[i]`,
  unless the rule's conditions say otherwise (`releve-event-assertions`, and the
  MoF field rules, which list both the occurrence- and event-assertion alternatives).
- **Preservation.** `gen-preserve-unconsumed` applies everywhere: every unused
  column, unplaced row and unknown row type is kept verbatim and reported with counts.
- **Subject.** `gen-attachment-is-not-subject`: placing a row on the table that matches
  the core is recorded as `subject_basis=core_attachment`. Moving it to another subject
  needs an explicit source signal named in a rule, or human approval.
- **IDs.** Source IDs populate DwC-DP ID fields only when non-empty and unique;
  required `*_pk` values are minted deterministically (`gen-weak-key-uniqueness`).

## What the pinned schema cannot hold

These gaps account for most `unsupported` dispositions:

- **No taxon / name-usage table.** Taxon cores and every taxon-subject extension
  (Distribution, VernacularName, Description, SpeciesProfile, TypesAndSpecimen,
  Plinian, ISSG, COL NameRelation, TCS relations, Chromosomes, Ellenberg, GISIN)
  have nowhere to go. `identification-taxon` is part of a determination, not a checklist.
- **No parent link between assertions.** `parentMeasurementID` can't be represented.
- **No permit, loan or lab-process tables.** This affects GGBN Permit, Loan and Cloning.
- **No occurrence link on `nucleotide-analysis`.** The only occurrence↔sequence link is
  `identification.nucleotideAnalysis_fk` / `nucleotideSequence_fk`, which asserts that
  the determination was based on the sequence.
- **No taxon or location fields on `media`.**

## Version grouping

Versions are detected by term set (`gen-version-by-term-set`), because one row type
can cover materially different term sets.

| Family (row type) | Versions present | Material differences |
| --- | --- | --- |
| MeasurementOrFact | 2015, 2022-02-02, 2024-02-19, **2025-07-10**, 2026-05-26 (sandbox) | 2024-02-19 adds `parentMeasurementID`; 2025-07-10 adds `verbatimMeasurementType`. 2026 sandbox has the same terms as 2025. |
| ExtendedMeasurementOrFact | 2016, **2023-08-28** | Same 13 terms. |
| ResourceRelationship | 2015, 2018-01-18, 2022-02-02, 2024-02-19, **2025-07-10**, 2026-05-26 (sandbox) | 2015 has no `resourceID` and includes `scientificName`. 2018 adds `resourceID` and removes `scientificName`. 2022+ adds `relationshipOfResourceID`. |
| Identification History | 2015, 2022-02-02, 2024-02-19, **2025-07-10**, 2026-05-26 (sandbox) | 2015 is a smaller set and its subject includes MaterialSample. 2025 adds superfamily/tribe/subtribe. **2026 sandbox adds `identificationType` and `isAcceptedIdentification`.** |
| DNA derived data | 2021-07-05, 2022-02-23, 2024-04-17, **2024-07-11**, 2026-04-14 (sandbox) | 2021/2022 use `https://w3id.org/gensc/terms/MIXS:N` IRIs and lack samp_taxon_id/neg_cont_type/pos_cont_type. 2024-04-17 switches to `https://w3id.org/mixs/N`. 2024-07-11 adds `dwc:occurrenceID`. 2026 sandbox equals 2024-07-11. |
| Audiovisual (ac:Multimedia) | audubon 2015, audubon 2020-10-06, audiovisual 2023-09-05, audiovisual 2024_11_07, 2026-01-23 (sandbox), **2026-02-24** | Two term sets: 96 terms (2015, 2020, **2024_11_07**) and 157 terms (2023-09-05, 2026-*). The 157-term set adds ROI, temporal-segment and frequency terms. Registry date order is not term-set order. |
| Humboldt (eco:Event) | 2024-04-16, **2025-07-10**, 2026-05-26 (sandbox) | Same 57 terms. File sizes differ (definitions/vocabularies); no structural change. |
| ChronometricAge | zooarchnet 2018 (own row type), zooarchnet 2020-10-06 (deprecated), 2021-03-27, **2024-03-11**, 2026-05-26 (sandbox) | The 2018 version uses the zooarchnet namespace and max/min ages. 2021+ uses chrono:, earliest/latest. 2026 sandbox widens the subject to Event. |
| Distribution | 2015, 2020-07-15, **2022-02-02** | 2022 adds degreeOfEstablishment, pathway. |
| TypesAndSpecimen | 2015, 2026-03-09 (sandbox), **2026-05-05** | 2026 adds institutionID, collectionID. |
| GGBN Permit | 2016, **2022-08-08** | Same 5 terms. |
| Event / Occurrence / Taxon cores | many | Handled by the core-term catalogue. |

## Focus families

### MeasurementOrFact
- **Deterministic target.** Use the assertion table matching the core: `occurrence-assertion.occurrence_fk` for Occurrence cores, `event-assertion.event_fk` for Event cores. Every MoF term has a same-meaning field: type→`assertionType`, verbatim type→`verbatimAssertionType`, value, accuracy→`assertionError`, unit, determinedBy→`assertionBy`, determinedDate→`assertionMadeDate`, method→`assertionProtocols`, remarks, ID→`assertionID` (when unique).
- **Not deterministic.** Choosing the semantic subject. Occurrence-core facts may really concern the specimen (`material-assertion`), the organism's permanent traits (`organism-assertion`) or the collecting conditions. Event-core facts may concern the survey design or the protocol.
- **Review queue.** `mof-subject-retarget-review` lists the triggers: a material exists for the occurrence; organismID is present together with a trait-like type; the event has a survey; or the type names gear, mesh, volume, duration or effort. A model may recommend a move; only a human applies it.
- **Unsupported.** Taxon-core MoF and `parentMeasurementID`.

### ExtendedMeasurementOrFact
- **Explicit occurrence link.** `dwc:occurrenceID` on a row is the publisher's own link. It is used only when it resolves to exactly one emitted occurrence inside the coreid event's subtree.
- **Event-level facts.** An empty occurrenceID on an Event core produces an event assertion.
- **Dangling occurrenceID.** Goes to review (`emof-unresolved-occurrence`). It must never create an occurrence or fall back silently to the event.
- **IRI columns.** `measurementTypeID`, `measurementValueID` and `measurementUnitID` go to the `*IRI` fields only when the value is an absolute IRI.
- **Sampling facts.** OBIS-style gear and effort facts are review candidates for protocol or survey.

### ResourceRelationship
- **Always lossless.** Every row with a `resourceID` goes to the generic `resource-relationship` table. `relationshipOfResource` is copied verbatim to `relationshipType` and never normalised. `relationshipOfResourceID` goes to `relationshipTypeIRI` if it is an IRI.
- **Related resource.** `relatedResourceID` is split by resolution: an emitted record goes to `relatedResourceID`, anything else to `externalRelatedResourceID`. Resource types are filled only from unambiguous resolution.
- **Empty `resourceID`.** Always the case for 2015 files. The row goes to review; the coreid is not assumed to be the subject.
- **Promotion is review-only.**
  - `organism-interaction` requires two resolvable occurrences, a shared event, and an observed-act predicate. Habitual or taxon-level predicates ("host of", "parasitoid of", "pollinator of members of taxon") are excluded, following the table comment and conceptual model §2.3. Interaction meaning is never derived from arbitrary strings.
  - `organism-relationship` requires a permanent relation between emitted organisms.
  - Material derivation/part-of goes to `material.derivedFromMaterialEntityID` / `isPartOfMaterialEntityID`.
  - The generic row is kept even after promotion.

### Identification History
- **Placement.** Only Occurrence cores get `identification` rows with `occurrence_fk`. Other cores go to review, because an event is not a valid basis for an identification. All same-named DwC terms copy verbatim, including the 2026 sandbox `identificationType` and `isAcceptedIdentification`.
- **Accepted flag.** `isAcceptedIdentification` is set to true only if the source says so or `identificationID` equals the core's `identificationID`. False is never inferred.
- **Duplicates of the core determination.** Merge only on an exact `identificationID` match. Otherwise emit both rows and flag them; similarity-based merging is not allowed.
- **Review.** `identificationQualifier` (deriving `taxonFormula` is interpretive), and whether the evidence was the material rather than the occurrence.
- **Unsupported.** Name-usage terms (accepted/parent/original name usage, `taxonomicStatus`, `higherClassification`, `verbatimTaxonRank`, `*ID` variants, `taxonConceptID`).

### DNA-derived data and GGBN
- **Exact IRI match.** The pinned `molecular-protocol` carries the MIxS, GBIF, GGBN and MIQE term IRIs as `dcterms:isVersionOf`. 121 of the 123 latest terms map one-to-one by IRI equality (`dna-mixs-fields`, `dna-gbif-ggbn-miqe-fields`).
- **Legacy IRIs.** The 2021/2022 `gensc` IRIs map through a logged accession-number normalisation (`dna-legacy-gensc-fields`, conditional).
- **Rows created per DNA row:**
  - one `molecular-protocol` row (shared only for exact-duplicate tuples, because many fields are per-sample);
  - one `nucleotide-sequence` row (`sequence` ← `dna_sequence`);
  - one `nucleotide-analysis` row linking the protocol and sequence to the event.
- **Review.** Linking to the occurrence via `identification.nucleotideAnalysis_fk`, and to material via `materialEntity_fk`. `dwc:occurrenceID` (2024-07-11+) is offered to the reviewer as evidence and is never used to create records.
- **Open question.** The pinned `molecular-protocol` also has `DNA_sequence` (isVersionOf `gbif:dna_sequence`). This catalogue writes the sequence only to `nucleotide-sequence.sequence`. Confirm this before implementation.
- **GGBN** (designed for Material Sample cores). Permit, Loan and Cloning are unsupported. MaterialSample, Preparation, Preservation, Amplification and GelImage need a reviewed per-archive column plan (`ggbn-material-review`). Their MIxS terms use `http://gensc.org/ns/mixs/` IRIs, which must not be normalised automatically.

### Audiovisual / multimedia
- **Audiovisual Core.**
  - One `media` row per extension row. 60 terms map by exact IRI equality.
  - Rights terms go to shared `usage-policy` rows, attribution/source terms to shared `provenance` rows, each deduplicated by exact tuple.
  - The core link is `occurrence-media` or `event-media`, according to the core type.
  - `dc:type` is used only when `dcterms:type` is absent. `Iptc4xmpExt:CVterm` is split into IRI and literal. `derivedFrom` is used only if it is an identifier. `hasROI` goes to review.
- **What the media depicts is not known from attachment.** `associatedSpecimenReference`, `associatedObservationReference` and `preparations` trigger review for `material-media` or a different occurrence.
- **AC terms with no media field** (taxon, identification, location, temporal coverage, `IDofContainingCollection`) are preserved. Turning them into identifications or events would create new assertions.
- **GBIF Simple Multimedia / Images.**
  - `dcterms:identifier` is the file URL, so it goes to `accessURI`.
  - `dcterms:creator` and `dcterms:source` are literals, so they go to `provenance.creator` / `source`.
  - `license` and `rightsHolder` go to `usage-policy`.
  - `contributor`, `publisher`, `audience` and image geo terms are preserved.
- **Taxon-core media** of any kind is unsupported; no orphan media rows are emitted.

### Humboldt
- **Direct fields.** 43 survey pairs match by IRI equality. Typed fields need lexical/type checks and bounds; value/unit pairs and contradictory flags need checks before copying. Survey creation, event category and duplicate/conflicting row grain need explicit decisions. Occurrence-core attachment always requires review.
- **Single-dimension scopes** can become one `survey-target` with descriptors only after list, scope and completeness checks. `isSurveyTargetFullyReported` is required. Habitat has no source flag, so it needs a reviewed assertion; a missing or invalid flag is never defaulted. Degree-of-establishment needs target-type policy review.
- **Multi-dimension scopes go to review.** Humboldt keeps one fully-reported flag per dimension, while `survey-target` has one flag per target. Any automatic split or merge changes what absence can be inferred.
- **`occurrence.surveyTarget_fk`** is never inferred.
- **Nested events.** Humboldt rows on nested events become nested surveys whose hierarchy is visible only through `event.parentEvent_fk`.

### Checklist and taxon-oriented families
All are unsupported against the pinned 79 tables: the Taxon core and every extension attached to it, plus the taxon-subject families listed under "What the pinned schema cannot hold".

- **No fabricated records.** Distribution rows must never become occurrences, events or surveys, even though they carry `eventDate`, `occurrenceStatus` and `countryCode`. They are statements about a taxon in an area, not evidence of an organism at a place and time.
- **No fabricated relationships.** COL name relations must not be copied into `resource-relationship`, because their identifiers are names with no emitted record.
- **TypesAndSpecimen.** If its `occurrenceID` resolves inside the archive, a reviewer may choose to set `typeStatus` on that material or identification. Nothing is automatic.

## Other production families (brief)
- **ChronometricAge (chrono) — conditional.** `chronometric-age` with the required `event_fk`: the coreid event for Event cores, or the occurrence's event for Occurrence cores (logged, because the age concerns the material). 18 terms map by IRI.
- **ZooArchNet ChronometricAge (2018) and ChronometricDate — review.** Deprecated namespace, and max/min ages are not the same as earliest/latest.
- **Alternative Identifiers — conditional.** Mapped to `occurrence-identifier` / `event-identifier`. `dcterms:format` must not be used as `identifierType`.
- **Literature References — conditional.** Mapped to `bibliographic-resource` plus `occurrence-reference` / `event-reference`. `relationshipType` stays empty.
- **Relevé — conditional.** Each populated cover, height or boolean column becomes one `event-assertion`, with units taken from the term names. `project` and `syntaxonName` go to review.
- **Germplasm (Accession/Score/Trait/Trial), EOL media/reference** now have [term-by-term audits](cloud-audits/remaining-fields.md), with conditional copies, review aliases and preservation paths. They remain unimplemented. Trial uses registered `measurementTrail*` IRIs while Score uses `measurementTrial*`; Accession lacks the institution/catalogue-number overlap assumed by the original coarse proposal. Nordgen germplasm 0.1 is unsupported (deprecated).

## Sandbox-only families
- **Same row type as production, newer term sets.** These sandbox versions (Occurrence, Event, Taxon, MoF, RR, Identification, Humboldt, ChronometricAge 2026-05-26; DNA 2026-04-14) are handled by the production family rules through term-set detection.
  - The only version change that affects mapping is the Identification History 2026 version. It adds `identificationType` and `isAcceptedIdentification`, which map directly to the pinned `identification` fields and make the accepted flag explicit.
- **Relevant to conversion:**
  - **AgentActions — conditional.** Maps to `agent` plus `occurrence-agent-role` / `identification-agent-role`. `agentRoleOrder` is required; use the display order or row order. Agents are never merged by name.
  - **Links — review.**
  - **Audubon Service Access Point — review.** Each access point could become a media variant row.
  - **BMDE and NBN eXchange Format — review.** Not term-audited.
- **Not relevant (unsupported):** Plinian (8 families), COL NameRelation, TCS relations, ISSG Distribution/Pathways, Chromosomes, Ellenberg, GISIN. All are taxon-subject.

## Cases that always need model or human review
1. Any re-targeting of MoF/eMoF rows away from the core-matched table.
2. ResourceRelationship rows with no `resourceID`, and any promotion to interaction, organism relationship or material derivation.
3. eMoF `occurrenceID` values that don't resolve.
4. Humboldt scopes spanning more than one dimension, and any occurrence-to-survey-target link.
5. DNA links to identification or material.
6. Media whose AC terms point at a specimen or another observation.
7. Identification qualifiers turned into `taxonFormula`, and specimen-based evidence for identifications.
8. GGBN lab-process extensions and all unaudited families.

A model may draft a recommendation for any of these. The recommendation, the evidence it cites and the human decision are logged, and nothing is dropped while waiting for a decision.

## Open questions for implementation
- Should `molecular-protocol.DNA_sequence` stay empty when `nucleotide-sequence.sequence` is filled? The catalogue assumes yes.
- Weak foreign keys such as `identifiedByID` and `assertionByID` reference `agent.agentID`. Does the pinned profile validation require those agent rows to exist? If so, apply the core catalogue's agent policy; this catalogue never creates agents from names.
- How should the conversion report be stored, and where are review queues kept? Shared with the core-term and review-policy work.

## Priority order for implementation
1. Shared machinery: row-type and term-set detection, coreid resolution, deterministic ID minting with uniqueness checks, preservation report, review-queue log.
2. MoF and eMoF to `occurrence-assertion` / `event-assertion`. These are the most common extensions and fully deterministic once placed.
3. Audiovisual, Simple Multimedia and Images to `media`, `occurrence-media`/`event-media`, `usage-policy` and `provenance`.
4. Generic ResourceRelationship to `resource-relationship`, with no promotion.
5. Identification History to `identification`, including accepted-flag and duplicate handling coordinated with the core mapping.
6. Humboldt to `survey`, plus single-dimension survey targets.
7. DNA-derived data to `molecular-protocol`, `nucleotide-sequence` and `nucleotide-analysis` (latest IRIs first, then the legacy gensc normalisation).
8. ChronometricAge, Alternative Identifiers, References, Relevé, AgentActions.
9. Review workflows: MoF re-targeting, RR promotion, multi-dimension Humboldt scopes, DNA–identification links, AV subject review, GGBN column plans.
10. Unsupported reporting for checklist and taxon families. It can ship with step 1, since these families only need preservation and a clear report.
