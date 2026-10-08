# Taxon-core conversion

Taxon cores are accepted from meta.xml archives, ZIPs, or loose `taxon.csv`,
`taxon.tsv` and `taxon.txt` files. Loose extensions join by supplied `taxonID`;
manifest extensions join by their declared `coreid`, even when it differs from
the persistent taxon identifier. Missing, duplicate or dangling archive join IDs
are rejected. Unknown loose tables without verified links remain independent
additional tables.

## Package format

The [TDWG guide](https://dwc.tdwg.org/dp/) permits additional resources alongside
standard DwC-DP tables. The pinned 79-table implementation has no standalone
taxon/name-usage table. Its `taxonID` fields refer to external records, rather
than establishing an internal checklist. The converter therefore uses two
explicit output formats:

| Source/review choice | Output |
| --- | --- |
| Checklist alone, or occurrence extraction declined | Frictionless Data Package (`profile: data-package`), labelled **taxonomy data package** |
| Actual Occurrence extension approved for extraction | DwC-DP containing standard occurrence/event tables plus additional taxonomy tables |

A standalone checklist is not labelled or validated as a standard DwC-DP.
Taxonomy conversion does not create fake occurrences for taxa, distribution
statements, vernacular names or type/specimen citations.

## Deterministic taxonomy tables

`taxonomy-taxon.csv` copies every core value as a string, including names,
authorship, taxon identifiers, ranks, parent links, synonym links and unknown
fields. `taxonomy-extension-N.csv` copies every extension row, preserving
multiplicity. Term IRIs identify source columns; field descriptors record those
IRIs. Original bytes, delimiters and headers remain in `source-originals.zip`;
an uploaded ZIP is also retained unchanged.

The core's `archive_join_id` remains distinct from `taxonID`. Extensions have a
generated `source_row_id` and, where verified, an `archive_taxon_id` foreign key.
All tables declare only the empty string as missing; literal `NA` and surrounding
spaces survive. Names are not matched, authorship is not split, synonyms are not
resolved, and taxonomy columns are not coerced to numeric types.

Parent, accepted-name and original-name fields get internal foreign keys only
when the supplied `taxonID` values are complete and unique and every populated
reference resolves exactly. Otherwise the original reference strings remain,
with unresolved/external and ambiguous-local counts in the report. No network
resolution or claim of hierarchy correctness is made.

## Actual occurrence extraction

Only declared `dwc:Occurrence` extensions with verified core attachments expose
this choice. Each extension uses the existing bounded review rules for status,
event grain, numeric fields, scientific names and material evidence. Separate
events preserve occurrence context; grouping requires consistent event values.

Empty classification fields can use the exact attached Taxon row after explicit
approval. If any overlapping classification value disagrees, the converter
retains the occurrence's own classification and fills no Taxon classification
values into that row. Differences are reported. Parent and accepted-name links
are not used to substitute a different taxon.

Local taxon IDs stay in the taxonomy tables. Copying an external IRI into a
standard DwC-DP `taxonID` field requires review of its external meaning; lexical
URI recognition does not prove that it resolves. `taxonomy-occurrence-links.csv`
records every original Taxon-to-Occurrence attachment and its generated output
key, so local identifiers do not need to be promoted to external identifiers.
Row crosswalks record source files/records and linked classification context.

## Validation and evidence

Both formats validate the serialized CSVs, schemas, primary/foreign keys and
original-file hashes offline. Canonical DwC-DP tables retain their existing
validation; additional taxonomy resources use their explicit Frictionless
schemas and cannot replace reserved tables. The report and download name expose
the actual output format.

Synthetic tests in `back-end/api/test_dwca_taxon.py` cover loose files and ZIPs,
synonyms, parent links, external/ambiguous references, extension multiplicity,
unknown fields, literal missing-value tokens, occurrence classification conflicts,
multiple occurrence extensions, reproducibility, broken serialized foreign keys,
queue/export/download behaviour and original ZIP preservation. No real Taxon-core
archive or live model recommendation quality has been benchmarked yet.

## Scientific-name check

Occurrence and Identification names are parsed with GBIF's name parser and
matched against Catalogue of Life XR through GBIF's v2 matcher
(`checklistKey`), in `back-end/api/conversion_names.py` with
`back-end/api/taxon_matching.py`. A match is a suggestion. Only names are
published. COL usage IDs are kept as provenance in the report and never become
`taxonID`. `verbatimIdentification` keeps the source text.

**How COL names are written.** COL names come from the v2 usage `name` with its
trailing authorship removed, not from `canonicalName`:

- **Rank markers are kept.** `canonicalName` drops "subsp."/"var."/"f." and turns
  "Betula pubescens subsp. czerepanovii" into a different botanical combination.
- **The authorship is not a plain suffix (some autonyms).** The canonical name is
  used instead.
- **There is no authorship.** Trailing words the canonical name lacks are dropped,
  such as an author left in a hybrid formula.
- **A parenthesised subgenus is kept only for a subgenus itself.** v2 writes
  "Acartia (Acartiura) longiremis", but the name is published as
  "Acartia longiremis", as the canonical name and most users write it.

The publication workflow's matcher shares these rules.

**Comparing names.** The asserted name is the parsed name, or the label without
its qualifier. It is compared with each COL usage by its parts: the genus or
uninomial, an infrageneric epithet with its marker, and the species and lower
epithets. Rank markers, hybrid signs, authorship and case are ignored.

- "Taraxacum sect. Ruderalia" and "Hieracium subg. Pilosella" keep their
  infrageneric epithet.
- A parenthesised subgenus counts only when no species epithet follows it:
  "Calanus (Calanus)" is the subgenus.
- "f. sp." is one marker.

The same name is never a change, whatever ranks the source supplies: "Larus sp."
accepted as the genus Larus keeps the user's assertion. Otherwise the change has
one of these kinds (`taxon_matching.name_change`):

| Kind | When | Example |
| --- | --- | --- |
| coarser | HIGHERRANK match, fewer parts, or a higher rank | "Calanus" → the phylum Arthropoda; "Taraxacum sect. Ruderalia" → Taraxacum |
| finer | more parts than the user asserted | "Carex nigra" → Carex nigra var. juncea |
| genus | another genus | "Trientalis europaea" → Lysimachia; "Calanus" → the plant genus Cajanus |
| epithet | another epithet in the same genus | "Parus major" → "Parus minor" |
| spelling | see below | Circium → Cirsium; Trema orientalis → Trema orientale |

A change is a **spelling** correction only when all of these hold:

- the match is VARIANT or FUZZY;
- the name has the same number of parts and the same rank;
- the epithets are identical, or differ only by a Latin gender ending
  (-us/-a/-um, -is/-e, -er/-ra/-rum);
- the genus is at most 2 edits away, or at most 1 edit for a genus of 5 letters
  or fewer;
- the usage's kingdom equals the source's kingdom hint, and its class and family
  equal any class and family hints.

A uninomial also needs a class or family hint to qualify.

**Confirming a change.** Every kind except spelling needs the user's explicit
confirmation: a single choice needs `confirm_coarser: true`, and no automatic
or bulk path ever writes such a usage. For those names the suggested action is
**Keep my name**.

### Groups and automatic acceptance

The server sorts every checked name into a group (`conversion_names.classify`)
and computes which group decisions each name can take. The browser only shows
them, so a bulk or automatic path can never write a coarser or different taxon
than the user's name, and never overwrites a name the user decided one at a
time.

| Group | Who is in it | What happens by default | Group decisions | One at a time |
| --- | --- | --- | --- | --- |
| Accepted automatically | EXACT match of the same name (markers kept, a subgenus ignored), no qualifier, no conflict | Accepted (`auto:exact`): COL's name and authorship when the authorships agree and COL has no other taxon written the same way, else the user's name and authorship | Use COL name, Use split, Keep as written | Other COL names, Don't publish a name |
| Uncertain to genus or family | "sp.", "spp.", "indet." names whose stem COL has exactly | Accepted (`auto:uncertain`) as the stem | Publish the genus or family name, Keep as written | Other COL names, Don't publish a name |
| Spelling differs from COL | VARIANT/FUZZY match | "Use COL spelling" pre-selected when a spelling correction exists | Use COL spelling (spelling corrections only), Keep my spelling | Other changes, with confirmation |
| Not confirmed by COL | Homonyms COL can't pick between, higher rank only, not found, an ID not found, "cf."/"aff."/"nr."/"?" | "Keep my names" pre-selected | Keep my names (not for cf. and the like) | Pick a candidate (with kingdom › phylum › class), a coarser name with confirmation |
| Check against your data | EXACT match but COL's name differs from the user's, the source ID points elsewhere, the label's rows disagree on kingdom/phylum/class, the hinted kingdom/phylum/class differs from COL's, or a uninomial's supplied rank (genus or above, or two such ranks across its rows) differs from COL's, also for a "sp." stem | Nothing pre-selected | Use COL names (same name, agreeing authorship; not for mixed rows or a rank conflict), Keep my names | Everything else |

"Keep my name(s)" writes the parsed name split from its authorship when the
split rebuilds the text exactly, else the supplied text. A check group is one
card per conflict signature: in 558 every tree is tagged Animalia, and one
decision covers the 81 names COL places in Plantae ("Sapotaceae sp" among them
is published as its stem).

**Automatic decisions never override the user.** They are made after each
chunk of the name check. Undo all (per group) or a row's Undo removes them and
records the labels in `auto_declined`, so a re-check does not accept them
again. When every name has a decision, automatic or not, the scientificName
fallback question settles itself; when names lose their decision again, that
automatic answer is withdrawn and the question is asked again (an answer the
user gave stands). A bulk decision keeps the snapshots it replaced for its
Undo; the last five can be undone.

**Bulk decisions are batches.** "Apply to N names" covers the group's eligible
names not decided one at a time and records the batch. Its Undo restores only
names that still carry that batch; a name changed since stays as it is.

**Uncertain names ("sp.", "spp.", "indet.").** The label's qualifier, or a
`identificationQualifier` column whose values are all sp./spp./indet. (blank
rows allowed), marks the name as uncertain to its genus or family. Any other
value ("cf.", "?") on any row, or "cf.", "aff.", "nr.", "?" or "sp. nov." anywhere in the name itself, makes it a decision for
the user, with how many rows say so. The stem (`stem` decision) is the COL usage of the same name:
the matcher's own pick, or exact alternatives that agree on one rank. COL's
authorship is written only when the same-name usages agree on it and it agrees
with any authorship the user supplied, so homonyms ("Viola" the plant and the
moth, "Ficus" in 558) are published as the bare genus name. "sp." and "spp." follow a genus or family, so a stem COL has at a
higher rank ("Anura sp.", the order) is decided one name at a time; "indet." may
stop at any rank. When the same-name candidates sit at different ranks ("Anura" the order and
the genus), only the source's own rank settles which one is meant; a genus and
its subgenus of the same name count as one. The stem's lineage is checked
against the source's kingdom, phylum and class like any other name. Every
exact match is also fetched with GBIF's verbose output, because the batch
response leaves out homonyms; the batch's pick stands and only the homonyms are
added. An exact name with a homonym (another authorship, a missing one, or
another lineage down to family) keeps the user's authorship, and so does one
COL places in another family than the source (usually a taxonomic change, so
the name itself is still accepted). Rows of one label that give different
families are a mixed-classification check. The same name parts with another (or no)
explicit rank marker ("subsp. juncea" and "var. juncea"), hybrid sign or "agg." are another name
(change kind `marker`), confirmed one name at a time. DwC-DP has no
identificationQualifier, so identification rows get `taxonFormula` "A sp.",
"A spp." or "A indet." from their own row's qualifier, else the label's (blank
cells only). `verbatimIdentification` still holds the text as written.

**Source IDs are a second step.** A label with one consistent
`scientificNameID` or `taxonID` (an LSID or http URI) whose name match is not
an exact same-name match is matched again with that ID. The result is used only
when it is the user's own name (566: Calanus, Chaetognatha, Crustacea and
Oithona with their WoRMS IDs; 567: Polychaeta, Isopoda). An ID never overrides
an exact name match: "Parathemisto libellula" stays Parathemisto although its
WoRMS ID leads to Themisto, and "Metridia longa" is not narrowed to a
subspecies. GBIF's diagnostics issues are kept; an ID that is not found
(`TAXON_ID_NOT_FOUND`, 567's Oncaea) is a reason under "Not confirmed", and one
that points to another name is a conflict.

**Authorship.** An automatic or bulk COL name is written with COL's authorship
only when every supplied authorship (the label's own or the
`scientificNameAuthorship` column) agrees with it; otherwise the user's is kept
(a different authorship can mean a homonym, or an author error to look at).
The comparison (`taxon_matching.authorships_agree`) works as follows:

- years must be equal when both give one;
- each author's surname must match:
  - exactly;
  - by a curated abbreviation, which matches only its author ("L." Linnaeus,
    "DC." de Candolle, "Lam." Lamarck, "Fabr." Fabricius, "Mill." Miller,
    "Hook." Hooker, "Willd." Willdenow, "Pers." Persoon), so "Lam." is not
    Lamouroux and "Fabr." is not Fabre;
  - or as an abbreviation of at least 4 letters ("Lamour." for Lamouroux);
- within a compound surname, an abbreviated part may be a single letter, so
  "O.P.-Cambridge" and "F.O.P-Cambridge" match Pickard-Cambridge;
- initials given on both sides must be equal: "J.E. Gray" is not "G.R. Gray",
  and "L. Koch" is not "C. L. Koch";
- initials on one side only are accepted for the same full surname with the
  same year; "A.Gray" and "Gray" without a year disagree;
- "L.f." (filius) is a different author from "L.";
- punctuation, spacing, parentheses and brackets around a year do not matter
  ("Lesson, [1830]" is "Lesson, 1830");
- "A in B" counts A, and "A ex B" counts B;
- "et al." compares the first author only;
- when COL has no authorship, nothing is overwritten, so it agrees.

**Stamped decisions.** Every COL decision records the change kind it was
checked against (`changeKind`, null for the same name) and the name rules it
was checked under (`nameRules`, `taxon_matching.NAME_RULES_VERSION`, now 2). It
is trusted from then on. A snapshot without the stamp, or with other name
rules, is checked again against the record's stored usage; an automatic or bulk
one is also checked for authorship agreement. A decision that fails is held:

- it counts as undecided, so it never settles the scientificName fallback;
- it is listed in its group with a banner count;
- at conversion it is applied as **keep**, so the user's own name is published
  and never an empty one;
- the report counts it under `unconfirmed_kept`.

The review lists same-name COL usages (homonyms) inline, with their
kingdom › phylum › class, those from another lineage than the source's last. A
homonym at another rank says so (for example "Anura" the order and the genus).
A changing option names what it would do, for example "replaces your genus with
a phylum", and asks for a second click; so does a same-name COL usage whose
authorship is not the one in the data. Rank and lineage conflicts are checked
for any match that is the user's own name, EXACT or a variant of it.

**Re-inspection carries name decisions** (`conversion_names.carry_decisions`).
A new rule version makes a plan stale, and the re-inspection used to drop every
name decision.
- **When decisions carry:** the user's own decisions carry over by label when
  the source is unchanged (same fingerprint). If the source changed but yields
  exactly the same labels, a decision carries only where that name's hints,
  supplied rank, supplied authorships, qualifiers, source IDs and mixed
  classification are unchanged.
- **Checking again:** when a COL decision is carried, the names are checked
  again even if name checks are switched off.
- **Notice:** the name section says how many decisions were kept, and how
  many of the user's own could not be (`dropped`).
- **What is not carried:**
  - bulk decisions, because the bulk actions are offered again under the
    current rules;
  - automatic decisions, because they are made again from the fresh matches;
  - name results, because the names are checked again.
- **Undo state:** an automatic decision declined with Undo all or per-row undo
  stays declined for labels carried into the re-inspection.
- **Re-checking:** a carried COL decision loses its `changeKind`, so it is
  checked again under the current rules. One that now replaces the name is held
  until confirmed; an explicit earlier confirmation stands.
- **State:** the counts are kept in `carried`.

Other plan choices are not carried: they belong to decision events tied to the
plan id, with AI provenance and evidence-basis hashes.

**Don't publish a name** (decision `empty`) is a secondary option. It leaves
`scientificName`, its authorship and its rank empty. The supplied text fills
`verbatimIdentification` wherever that would otherwise be empty.

The publication workflow's review (`TaxonReviewModal`) uses the same
confirmation. The serializer exposes `replacements` for the suggestion and for
each alternative. `decide` refuses such a usage without `confirm_coarser`. A name
the reviewer searched for and picked is itself an explicit choice.

**Name, rank and authorship are written together.** Whenever a decision writes
`scientificName`, it also writes the `taxonRank` and authorship that belong to
that name. Each frame row reads its supplied authorship and rank from its own
source row, so an occurrence and the identification made from it get identical
values.

| Decision | Rank | Authorship |
| --- | --- | --- |
| Parsed split | The parsed rank | The user's own authorship is kept and differences are counted; the parsed one fills a blank |
| COL name | COL's rank | COL's authorship; the supplied one survives only when COL has none for the same name |

Replaced rank values are counted per name (`ranksReplaced`, `ranks_replaced`).
Replaced authorships are counted as `authorshipReplaced`.

Regression fixtures are trimmed real v2 responses for conversions 558–570
(`back-end/api/testdata/col_v2_real_matches.json`, `col_v2_group_matches.json`,
and `col_v2_id_matches.json` with the name and source-ID queries).
