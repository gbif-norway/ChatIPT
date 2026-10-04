# Review of cloud audit deliverables

The 2 October 2026 cloud audits use TDWG snapshot
`76898192fd298c2aa170a7059e1bdadf3ee2a828` and the downloaded registry XMLs.
Local checks verified all 383 term entries, complete source coverage, referenced
target fields, target types and IRI-equality claims. Four Humboldt source hashes
were also checked. This verifies evidence, not semantic equivalence or every
plain-language prerequisite.

## Adopted catalogue corrections

- Humboldt's 43 exact survey pairs are confirmed. Integer, numeric and boolean
  fields need lexical/type checks, bounds, value/unit pairs and cross-field
  consistency checks. Exact IRI equality alone does not make them unconditional.
- `survey-target.isSurveyTargetFullyReported` is required. Habitat has no source
  flag; missing or invalid flags in any dimension require a reviewed assertion
  before creating a target. Never default the flag. Degree-of-establishment has
  no attested target-type example; absence from examples is not a schema ban, but
  the label needs policy review and cannot be substituted with establishmentMeans.
- Germplasm Trial spells five IRIs `measurementTrail*`; Score uses
  `measurementTrial*`. They must not be normalized or joined by term basename.
  Trait Score also includes nine DwC measurement terms omitted from the coarse
  catalogue. Accession has no institution/catalogue-number fields in this XML.
- EOL media needs policy/provenance and reviewed subject links. Legacy namespaces
  are aliases, and literal MIME/language values must not enter IRI fields.
  EOL media-to-reference links have no table in the pinned snapshot.

## Runtime scope and differences

Twenty extension row types now have executable paths, including the nine audited
Humboldt, EOL, germplasm, BMDE and NBN families. GBIF references and alternative identifiers are
implemented separately from the EOL reference row type.

The current converter uses UUID5 keys derived from the archive checksum and row
context. The Humboldt audit's SHA-derived keys and direct eventID keys are
proposals from the earlier research catalogue, not runtime key conventions.
Its duplicate-survey collapsing proposal is available only as an explicit decision:
repeated identical rows can still represent different surveys, so the default retains them.
The runtime currently preserves source multiplicity for media and references;
it does not merge media merely because mediaID or descriptions agree. Only
complete policy/provenance descriptions share target records.

The audits' full `conditional` catalogue remains a proposal. Implemented runtime aliases
require individual approval. Material rows currently require an explicit review
decision and supplied material identifiers. The germplasm audit's specimen-class
basisOfRecord prerequisite alone does not describe this implemented behavior.
On Occurrence cores, Humboldt requires reviewed grouping with complete identical
event coverage. Survey creation requires a survey event-category decision; supplied
non-survey categories cannot be overwritten. Direct fields have executable type,
bound, unit and contradiction checks. Single-dimension scopes with a valid flag
convert directly; missing flags, combined dimensions and degree of establishment
require reviewed completeness assertions.

Germplasm material scores require exact identifier matches, and optional protocol
links require one converted Trait Descriptor ID match. Trial patches reject core
conflicts and inconsistent year/date combinations. Score types use
`verbatimAssertionType` consistently with MoF/BMDE. The worker's arbitrary altitude
bounds were removed; finite numeric altitudes remain explicit reviewed aliases.
BMDE times convert only at exact whole-minute precision, never by rounding.
NBN unsupported or invalid dates are individually withheld even when other approved
row values convert. Sensitive flags and BMDE record-management states trigger row
review; DELETE and NoObs rows cannot be converted. Source metadata does not become
inferred absence, withholding prose, coordinate uncertainty or completeness.

The Humboldt Event fixture intentionally contains an orphan extension row.
The current importer rejects unresolved core joins before planning. Therefore
that entire fixture is an import-rejection case today; its proposed per-row
survey output is not a runtime test oracle. Other fixture keys, review counts
and outputs also need adaptation before they become executable tests.

## Verification

The original application suite passed 97 tests in the backend Compose container.
New helper and integration tests exercise all nine additional families, including
serialized combined packages, exact subject links, withheld values and conflict
rejection. Original cloud audit fixture outputs remain research expectations;
local integration fixtures use actual UUID5 keys and implemented review choices.
Cloud snapshots omitted `.env.dev`; no credentials were copied. Worker tests used
temporary Docker environments, and final application verification runs in the
repository's Compose services.

Final integration checks passed **188 backend tests**, including import, review,
job processing, canonical validation, serialized downloads and source coverage.
The 12 frontend utility tests, lint and production build also passed in Compose.

Repeat the evidence checks using the downloaded registry directory:

```sh
docker compose run --rm --no-deps \
  -v "$PWD/docs/dwca-conversion/cloud-audits":/audits:ro \
  -v /Users/rukayasj/Projects/sandbox/dwc-dp-guides/audit/2026-10-02:/registry:ro \
  --entrypoint python back-end scripts/validate_dwca_audits.py \
  --audits /audits --registry /registry \
  --schemas /app/api/templates/dwc-dp/table-schemas
```
