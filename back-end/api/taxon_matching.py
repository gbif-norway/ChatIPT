"""Match verbatim taxon labels against Catalogue of Life XR.

GBIF.org interprets occurrences against COL XR by default, so the primary suggestion comes from
GBIF's v2 matcher with the COL checklistKey: the same engine and taxonomy copy GBIF indexes with.
When that finds nothing or only a higher rank, ChecklistBank's newer XR release and the legacy
GBIF Backbone are consulted as review aids only; they may not yet be reflected on GBIF.org.

A match suggests a name. It never asserts that the identification is correct, so suggestions are
stored for review and only reviewer decisions are written back to the data.
"""

import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from django.conf import settings


GBIF_MATCH_URL = "https://api.gbif.org/v2/species/match"
GBIF_MATCH_METADATA_URL = "https://api.gbif.org/v2/species/match/metadata"
CHECKLISTBANK_API = "https://api.checklistbank.org"
USER_AGENT = "ChatIPT (GBIF Norway; https://chatipt.svc.gbif.no)"
BATCH_SIZE = 100
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 30
# A matching run happens inside one agent turn; what does not finish is matched on the next call.
RUN_BUDGET_SECONDS = 180
# Lookups made while a reviewer waits in the browser.
INTERACTIVE_BUDGET_SECONDS = 10
MAX_ALTERNATIVES = 5
# Exact alternatives are possible homonyms of the user's name, so more of them are kept; beyond this a match says so.
MAX_EXACT_ALTERNATIVES = 20
CONCURRENT_REQUESTS = 4
PROGRESS_CHUNK = 25

# Hints the matcher accepts; only pass values taken from the source data or confirmed by a reviewer.
HINT_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")
# Classification ranks written back when the target table's schema has a column for them.
CLASSIFICATION_RANKS = (
    "kingdom", "phylum", "class", "order", "superfamily", "family",
    "subfamily", "tribe", "subtribe", "genus", "subgenus",
)

MATCH_STATUS = {
    "EXACT": "exact",
    "VARIANT": "variant",
    "FUZZY": "variant",
    "CANONICAL": "variant",
    "HIGHERRANK": "higher_rank",
    "AMBIGUOUS": "ambiguous",
    "NONE": "none",
}

_NEW_SPECIES_RE = re.compile(r"\s+sp(?:ec)?\.?\s*n(?:ov)?\.?$", re.IGNORECASE)
_TRAILING_SP_RE = re.compile(r"\s+(sp|spp|indet)\.?(?:\s*\d+)?$", re.IGNORECASE)
_INNER_QUALIFIER_RE = re.compile(r"^(\S+)\s+(cf|aff|nr)\.?\s+(\S.*)$", re.IGNORECASE)


class TaxonServiceError(RuntimeError):
    pass


def col_checklist_key():
    return getattr(settings, "GBIF_COL_CHECKLIST_KEY", "7ddf754f-d193-4cc9-b351-99906754a03b")


def col_checklistbank_dataset():
    return getattr(settings, "CHECKLISTBANK_COL_DATASET", "3LXR")


def split_qualifier(label):
    """Return (name to match, identification qualifier) for one verbatim label.

    "Dinychus sp." is matched as the genus with qualifier "sp."; "Genus cf. species" is matched as
    "Genus species" with qualifier "cf. species". "sp. n." marks a newly described species and is
    dropped without a qualifier, because it does not express uncertainty.
    """
    name = " ".join(str(label or "").split())
    if _NEW_SPECIES_RE.search(name):
        return _NEW_SPECIES_RE.sub("", name).strip(), None
    trailing = _TRAILING_SP_RE.search(name)
    if trailing:
        return name[:trailing.start()].strip(), f"{trailing.group(1).lower()}."
    inner = _INNER_QUALIFIER_RE.match(name)
    if inner:
        genus, qualifier, rest = inner.groups()
        return f"{genus} {rest}".strip(), f"{qualifier.lower()}. {rest.split()[0]}"
    return name, None


# Bump when name comparison or authorship agreement changes: stored automatic and bulk COL decisions are
# re-checked for name replacements and authorship changes under the current rules.
NAME_RULES_VERSION = 2

# Highest first. Ranks outside this list are never compared.
RANK_ORDER = (
    "domain", "superkingdom", "kingdom", "subkingdom", "infrakingdom", "superphylum", "phylum", "subphylum",
    "infraphylum", "parvphylum", "superclass", "megaclass", "gigaclass", "class", "subclass", "infraclass",
    "subterclass", "superorder", "order", "suborder", "infraorder", "parvorder", "superfamily", "family",
    "subfamily", "tribe", "subtribe", "genus", "subgenus", "section", "subsection", "series", "species aggregate",
    "species", "subspecies", "variety", "subvariety", "form", "subform",
)
# Markers between the parts of a name; skipped when comparing names. "f. sp." (forma specialis) is one marker.
_NAME_MARKERS = {
    "subsp.", "ssp.", "var.", "subvar.", "f.", "fo.", "forma", "subf.", "f.sp.", "agg.", "nothosubsp.", "nothovar.",
    "×", "x", "cv.",
}
# Infrageneric markers: the epithet after them is part of the name ("Taraxacum sect. Ruderalia").
_INFRAGENERIC = {"subg.": "subg", "subgen.": "subg", "sect.": "sect", "subsect.": "subsect", "ser.": "ser", "subser.": "subser"}
_SUBGENUS = re.compile(r"\([A-Z][a-z-]+\)")
# Latin adjective endings that differ only by grammatical gender; an epithet may change ending with its genus.
_GENDER_ENDINGS = (("us", "a", "um"), ("is", "e"), ("er", "ra", "rum"))


def name_parts(name):
    """Lower-cased parts of a name for comparison: genus (or uninomial), infrageneric epithet, species and lower epithets.

    Rank markers, hybrid signs and authorship are left out. An infrageneric epithet is kept with its marker
    ("subg.pilosella", "sect.ruderalia"); a parenthesised subgenus counts only when no species epithet follows it
    ("Calanus (Calanus)" is the subgenus, "Acartia (Acartiura) longiremis" the species Acartia longiremis).
    """
    parts, subgenus, infrageneric, previous = [], None, None, None
    for token in str(name or "").replace("×", " × ").split():
        lowered, before, previous = token.casefold(), previous, token.casefold()
        if lowered == "sp." and before == "f.":
            continue  # "f. sp." is one marker
        if lowered in _INFRAGENERIC and len(parts) == 1:
            infrageneric = _INFRAGENERIC[lowered]
            continue
        if lowered in _NAME_MARKERS:
            continue
        if not parts:
            parts.append(lowered)
        elif infrageneric:
            if not (token[:1].isupper() and token.replace("-", "").isalpha()):
                break
            parts.append(f"{infrageneric}.{lowered}")
            infrageneric = None
        elif len(parts) == 1 and subgenus is None and _SUBGENUS.fullmatch(token):
            subgenus = lowered.strip("()")
        elif token[:1].islower() and token.replace("-", "").isalpha():
            parts.append(lowered)
        else:
            break  # the authorship starts
    if subgenus and len(parts) == 1:
        parts.append(f"subg.{subgenus}")
    return parts


INFRAGENERIC_RANKS = {"subg": "subgenus", "sect": "section", "subsect": "subsection", "ser": "series", "subser": "subseries"}


def implied_rank(parts):
    """The rank a name's own parts imply: a binomial is a species, "Genus sect. Epithet" a section; else unknown."""
    if len(parts) == 2 and "." in parts[1]:
        return INFRAGENERIC_RANKS.get(parts[1].split(".")[0])
    return "species" if len(parts) == 2 else None


def rank_above(rank, other):
    """True when both ranks are known and `rank` is strictly higher than `other`."""
    return rank in RANK_ORDER and other in RANK_ORDER and RANK_ORDER.index(rank) < RANK_ORDER.index(other)


def _edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _gender_variant(left, right):
    if left == right:
        return True
    for endings in _GENDER_ENDINGS:
        for ending in endings:
            if left.endswith(ending) and any(right == left[:-len(ending)] + other for other in endings if other != ending):
                return True
    return False


def _classification_agrees(usage, hints, uninomial):
    """The usage sits where the source's own classification says: kingdom always, class and family where hinted."""
    hints, classification = hints or {}, (usage or {}).get("classification") or {}
    if not hints.get("kingdom") or str(classification.get("kingdom") or "").casefold() != str(hints["kingdom"]).casefold():
        return False
    hinted = [rank for rank in ("class", "family") if hints.get(rank)]
    if uninomial and not hinted:
        return False  # "Calanus" -> "Cajanus": a plant genus one letter away
    return all(str(classification.get(rank) or "").casefold() == str(hints[rank]).casefold() for rank in hinted)


def _is_spelling(mine, theirs, match_type, asserted_rank, rank, usage, hints):
    """COL's spelling of the same name in the same place: a close genus spelling and/or a gender ending, same rank."""
    if match_type not in {"VARIANT", "FUZZY"} or len(mine) != len(theirs):
        return False
    expected = asserted_rank or ("species" if len(mine) == 2 else "genus" if len(mine) == 1 and (hints or {}).get("family") else None)
    if not expected or rank != expected:
        return False
    if not all(_gender_variant(left, right) for left, right in zip(mine[1:], theirs[1:])):
        return False
    if _edit_distance(mine[0], theirs[0]) > (1 if len(mine[0]) <= 5 else 2):
        return False
    return _classification_agrees(usage, hints, uninomial=len(mine) == 1)


# Infraspecific rank markers and their usual variants; "ssp." is "subsp.", "fo."/"forma" is "f.".
_MARKER_FORMS = {"subsp.": "subsp.", "ssp.": "subsp.", "var.": "var.", "subvar.": "subvar.", "f.": "f.", "fo.": "f.",
                 "forma": "f.", "subf.": "subf.", "nothosubsp.": "nothosubsp.", "nothovar.": "nothovar."}


def _explicit_markers(name):
    """The infraspecific rank markers written in a name, normalised ("ssp." is "subsp."); authorship words are ignored."""
    tokens = str(name or "").split()
    return [_MARKER_FORMS[token.casefold()] for index, token in enumerate(tokens)
            if token.casefold() in _MARKER_FORMS and index + 1 < len(tokens) and tokens[index + 1][:1].islower()]


def name_change(asserted, usage, match_type=None, asserted_rank=None, hints=None):
    """How accepting `usage` would change the asserted name; None when it is the same name.

    The same name (ignoring markers, authorship and case) is never a change, whatever the ranks say: "Larus sp."
    accepted as the genus Larus keeps the user's assertion. Every kind but "spelling" needs the user's explicit
    confirmation and is never accepted in bulk:
    - coarser: a higher-rank match, fewer name parts or a higher rank ("Calanus" -> the phylum Arthropoda);
    - finer: more name parts than the user asserted;
    - spelling: COL's spelling of the same name in the same place (`_is_spelling`), e.g. Circium -> Cirsium;
    - genus: another genus ("Trientalis europaea" -> Lysimachia, "Calanus" -> the plant genus Cajanus);
    - epithet: another epithet in the same genus ("Parus major" -> "Parus minor").
    """
    usage = usage or {}
    mine, theirs = name_parts(asserted), name_parts(usage.get("scientificName"))
    rank = _rank(usage.get("taxonRank"))
    if not mine or not theirs:
        return None
    asserted_rank = asserted_rank or implied_rank(mine)
    change = {"from": asserted_rank, "to": rank, "confirm": True}
    # The same parts with another explicit rank marker ("subsp. juncea" and "var. juncea") are another name.
    my_markers, their_markers = _explicit_markers(asserted), _explicit_markers(usage.get("scientificName"))
    if mine == theirs and my_markers != their_markers:
        mine_text = " ".join(my_markers) or "name without a rank marker"
        return {**change, "kind": "marker", "text": f"writes your {mine_text} as {' '.join(their_markers) or 'a name without one'}"}
    if mine == theirs:
        # Only an infrageneric name carries its rank in its parts; any other same name is the user's assertion.
        if any("." in part for part in mine) and asserted_rank and rank and asserted_rank != rank:
            return {**change, "kind": "coarser" if rank_above(rank, asserted_rank) else "finer",
                    "text": f"replaces your {asserted_rank} with the {rank} of the same name"}
        return None
    match_type = str(match_type or "").upper()
    if (match_type == "HIGHERRANK" or len(theirs) < len(mine)
            or rank_above(rank, asserted_rank) or (len(mine) > 1 and rank_above(rank, "species"))):
        label = rank or "higher taxon"
        article = "an" if label[:1] in "aeiou" else "a"
        return {**change, "kind": "coarser", "text": f"replaces your {asserted_rank or 'name'} with {article} {label}"}
    if len(theirs) > len(mine):
        label = rank or "lower taxon"
        article = "an" if label[:1] in "aeiou" else "a"
        return {**change, "kind": "finer", "text": f"narrows your {asserted_rank or 'name'} to {article} {label}"}
    if _is_spelling(mine, theirs, match_type, asserted_rank, rank, usage, hints):
        return {**change, "kind": "spelling", "confirm": False, "text": f"corrects the spelling to {usage['scientificName']}"}
    if mine[0] != theirs[0]:
        genus = usage["scientificName"].split()[0]
        return {**change, "kind": "genus", "text": f"replaces your {'genus' if len(mine) > 1 else 'name'} with {genus}"}
    return {**change, "kind": "epithet", "text": f"replaces your name with {usage['scientificName']}"}


def coarser_replacement(asserted, usage, match_type=None, asserted_rank=None, hints=None):
    """The change accepting `usage` would make when it needs the user's explicit confirmation; None otherwise."""
    change = name_change(asserted, usage, match_type, asserted_rank, hints)
    return change if change and change["confirm"] else None


# Standard abbreviations too short for prefix matching that name one author unambiguously.
_AUTHOR_ABBREVIATIONS = {
    "l.": "linnaeus", "dc.": "candolle", "lam.": "lamarck", "fabr.": "fabricius",
    "mill.": "miller", "hook.": "hooker", "willd.": "willdenow", "pers.": "persoon",
}
_FILIUS = {"f.", "fil.", "filius", "jr.", "jun.", "fils"}
_INITIAL = re.compile(r"[A-Z]\.")


def _author(piece):
    """(initials, surname, filius) of one author: "C. L. Koch" -> (("c", "l"), "koch", False); None when empty.

    Initials may run into the surname ("L.Koch"). An initial joined to a hyphenated surname is the abbreviated first
    part of a compound surname: "O.P.-Cambridge" is O. P[ickard]-Cambridge. "L.f." is Linnaeus filius.
    """
    tokens = re.findall(r"[^\s.]+\.?", piece)
    filius = False
    while tokens and tokens[-1].casefold() in _FILIUS:
        tokens.pop()
        filius = True
    if not tokens:
        return None
    surname, rest = tokens[-1], tokens[:-1]
    if surname.startswith("-") and rest and _INITIAL.fullmatch(rest[-1]):
        surname = rest.pop() + surname
    initials = tuple(token[0].casefold() for token in rest if _INITIAL.fullmatch(token))
    return initials, surname.strip("-").casefold(), filius


def _author_keys(value):
    """(authors, "et al." used) of an authorship, each author as `_author` reads it.

    Years (including square-bracketed years) and parentheses are dropped. For "A ex B" only B, the publishing author,
    counts; for "A in B" only A.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = re.sub(r"\[?(\d{4}[a-z]?)\]?", " ", text)
    text = re.split(r"\bex\b", text)[-1]
    # "A in B": A is the author, B's work only published it ("Fitzinger in Bonaparte").
    text = re.sub(r"\bin\b[^(),]*", " ", text)
    et_al = bool(re.search(r"\bet\s+al\b", text, re.IGNORECASE))
    text = re.sub(r"\bet\s+al\b\.?", " ", text, flags=re.IGNORECASE)
    authors = [_author(piece) for piece in re.split(r"[(),&;:]|\bet\b|\band\b", text)]
    return [author for author in authors if author], et_al


def _surname_part(left, right, compound):
    """Compare a surname or compound part, allowing curated aliases and long surname abbreviations.

    Outside compounds, prefix matching needs a four-letter stem. Inside compounds a one-letter part may be abbreviated.
    """
    left, right = _AUTHOR_ABBREVIATIONS.get(left, left), _AUTHOR_ABBREVIATIONS.get(right, right)
    if left == right:
        return True
    for short, full in ((left, right), (right, left)):
        stem = short.rstrip(".")
        # Within a compound a lone letter is an abbreviation even without its full stop ("F.O.P-Cambridge").
        abbreviated = short.endswith(".") or (compound and len(stem) == 1)
        if abbreviated and not full.endswith(".") and len(full) > len(stem) and full.startswith(stem) and (len(stem) >= 4 or (compound and stem)):
            return True
    return False


def _same_surname(left, right):
    left_parts, right_parts = left.split("-"), right.split("-")
    if len(left_parts) == 1 and len(right_parts) == 1:
        return _surname_part(left, right, compound=False)
    return (len(left_parts) == len(right_parts) and any(a == b for a, b in zip(left_parts, right_parts))
            and all(_surname_part(a, b, compound=True) for a, b in zip(left_parts, right_parts)))


def _forms(author):
    """An author as written, and for "O.P.-Cambridge" also as "O.P.Cambridge" (the abbreviated part as an initial)."""
    initials, surname, filius = author
    compound = re.fullmatch(r"([a-z])\.-(.+)", surname)
    return [author] + ([(initials + (compound.group(1),), compound.group(2), filius)] if compound else [])


def _same_author(left, right, both_dated):
    return any(_same_form(a, b, both_dated) for a in _forms(left) for b in _forms(right))


def _same_form(left, right, both_dated):
    (left_initials, left_surname, left_filius), (right_initials, right_surname, right_filius) = left, right
    if left_filius != right_filius or not _same_surname(left_surname, right_surname):
        return False
    if left_initials and right_initials:
        return left_initials == right_initials  # "J.E. Gray" is not "G.R. Gray"
    if left_initials or right_initials:
        # Initials on one side only ("A.Gray" and "Gray" are different botanists): the same full surname and year only.
        return both_dated and left_surname == right_surname and not left_surname.endswith(".")
    return True


def authorships_agree(left, right):
    """Two authorships name the same authors.

    Years must be equal when both give one. Brackets around a year are ignored. Each author's surname must match: exactly,
    by one of the curated abbreviations ("L." for Linnaeus, "Lam." for Lamarck), or as an abbreviation with a stem of
    at least 4 letters ("Lamour." and "Lamouroux"). Initials given on
    both sides must be equal; initials on one side only are accepted for the same full surname with the same year.
    Punctuation, spacing and parentheses do not matter; square brackets around years are ignored. So
    "O.P.-Cambridge" agrees with "O. Pickard-Cambridge" and "L.Koch" with "L. Koch", while "L." and "Lam.",
    "J.E. Gray" and "G.R. Gray", "A.Gray" and "Gray", "Blackwall" and
    "Seo, 2017" disagree.
    """
    left_years, right_years = re.findall(r"\d{4}", str(left or "")), re.findall(r"\d{4}", str(right or ""))
    if left_years and right_years and left_years != right_years:
        return False
    both_dated = bool(left_years and right_years)
    (left_authors, left_et_al), (right_authors, right_et_al) = _author_keys(left), _author_keys(right)
    if not left_authors or not right_authors:
        return not left_authors and not right_authors
    if left_et_al or right_et_al:
        return _same_author(left_authors[0], right_authors[0], both_dated)
    return len(left_authors) == len(right_authors) and all(
        _same_author(a, b, both_dated) for a, b in zip(left_authors, right_authors))


def _remaining(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TaxonServiceError("the time budget for this matching run was used up")
    return remaining


def _get_json(method, url, deadline=None, **kwargs):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = _remaining(deadline)
        timeout = REQUEST_TIMEOUT if remaining is None else min(REQUEST_TIMEOUT, remaining)
        try:
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                raise TaxonServiceError(f"{url} unreachable: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == MAX_ATTEMPTS:
                raise TaxonServiceError(f"{url} returned HTTP {response.status_code}")
            retry_after = response.headers.get("Retry-After", "")
            time.sleep(min(int(retry_after), 30) if retry_after.isdigit() else 2 ** attempt)
            continue
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise TaxonServiceError(f"{url} returned HTTP {response.status_code}: {response.text[:300]}")
        return response.json()
    return None


def _rank(value):
    return str(value or "").lower() or None


def _name_without_authorship(usage):
    """The usage's name as COL writes it, rank marker ("subsp.", "var.", "f.") and hybrid sign included.

    GBIF's canonicalName drops the marker ("Betula pubescens czerepanovii"), which names a different
    combination in botany, so the full name minus its trailing authorship is used. When the authorship is
    not a plain suffix (an autonym, a nomenclatural note) the canonical name is the safe fallback. Without an
    authorship, trailing words the canonical name does not have (an author left in a hybrid formula) are dropped.
    A parenthesised subgenus is kept only for a subgenus itself: "Acartia (Acartiura) longiremis" is published as
    "Acartia longiremis", as GBIF's canonical name has it and as users write it.
    """
    name = " ".join(str(usage.get("name") or "").split())
    authorship = " ".join(str(usage.get("authorship") or "").split())
    canonical = usage.get("canonicalName")
    if name and authorship and name.endswith(" " + authorship):
        name = name[:-len(authorship)].strip()
    elif name and not authorship:
        if canonical:
            words, known = name.split(), {word.casefold() for word in canonical.split()}
            while len(words) > 1 and words[-1].casefold() not in known | _NAME_MARKERS:
                words.pop()
            name = " ".join(words)
    else:
        name = canonical or name
    if name and _rank(usage.get("rank")) != "subgenus":
        name = re.sub(r"^(\S+) \([A-Z][a-z-]+\)(?= )", r"\1", name)
    return name or None


def _usage(usage, classification=None):
    if not usage:
        return None
    ranks = {}
    for item in classification or []:
        rank = _rank(item.get("rank"))
        if rank in CLASSIFICATION_RANKS and item.get("name"):
            ranks[rank] = item["name"]
    return {
        "id": usage.get("key") or usage.get("id"),
        "scientificName": _name_without_authorship(usage),
        "scientificNameAuthorship": usage.get("authorship") or None,
        "label": usage.get("name") or usage.get("label"),
        "taxonRank": _rank(usage.get("rank")),
        "status": _rank(usage.get("status")),
        "classification": ranks,
    }


def summarize_match(payload):
    """Reduce a GBIF v2 match response to what review and write-back need."""
    payload = payload or {}
    diagnostics = payload.get("diagnostics") or {}
    usage = _usage(payload.get("usage"), payload.get("classification"))
    match_type = str(diagnostics.get("matchType") or "NONE").upper()
    accepted = None
    if payload.get("synonym") and payload.get("acceptedUsage"):
        accepted = _usage(payload["acceptedUsage"])
    alternatives = []
    for alternative in diagnostics.get("alternatives") or []:
        alt_usage = _usage(alternative.get("usage"), alternative.get("classification"))
        if not alt_usage:
            continue
        alt_diagnostics = alternative.get("diagnostics") or {}
        alternatives.append({
            **alt_usage,
            "matchType": str(alt_diagnostics.get("matchType") or "").upper() or None,
            "confidence": alt_diagnostics.get("confidence"),
        })
    alternatives, exact_dropped = bounded_alternatives(alternatives)
    return {
        "matchType": match_type,
        "status": MATCH_STATUS.get(match_type, "ambiguous"),
        "confidence": diagnostics.get("confidence"),
        "note": diagnostics.get("note"),
        "usage": usage,
        "acceptedUsage": accepted,
        "alternatives": alternatives,
        "exactAlternativesDropped": exact_dropped,
        "issues": [str(issue) for issue in diagnostics.get("issues") or []],
        "matchedId": ({"id": (diagnostics.get("matchedID") or {}).get("id"),
                       "scientificName": (diagnostics.get("matchedID") or {}).get("scientificName"),
                       "datasetTitle": (diagnostics.get("matchedID") or {}).get("datasetTitle")}
                      if diagnostics.get("matchedID") else None),
    }


def bounded_alternatives(alternatives):
    """(alternatives kept in their order, whether exact ones were dropped): every exact one up to MAX_EXACT_ALTERNATIVES,
    other ones up to MAX_ALTERNATIVES."""
    kept, exact, other, dropped = [], 0, 0, False
    for alternative in alternatives:
        if alternative.get("matchType") == "EXACT":
            if exact >= MAX_EXACT_ALTERNATIVES:
                dropped = True
                continue
            exact += 1
        else:
            if other >= MAX_ALTERNATIVES:
                continue
            other += 1
        kept.append(alternative)
    return kept, dropped


def _query_params(query):
    params = {"scientificName": query["scientificName"]}
    for rank in HINT_RANKS:
        if query.get(rank):
            params[rank] = query[rank]
    for identifier in ("scientificNameID", "taxonID"):
        if query.get(identifier):
            params[identifier] = query[identifier]
    return params


def match_col(queries, deadline=None, verbose_exact=False):
    """Match many names against COL XR through GBIF; returns one summary per query, in order.

    Names go through the batch endpoint first. The batch response omits alternatives, and it has
    been seen to miss names a single request suggests, so anything that is not an exact or variant
    match is retried individually with verbose output.
    """
    checklist_key = col_checklist_key()
    unique = {}
    for query in queries:
        unique.setdefault(json.dumps(_query_params(query), sort_keys=True), _query_params(query))
    keys = list(unique)
    results = {}
    for start in range(0, len(keys), BATCH_SIZE):
        chunk = keys[start:start + BATCH_SIZE]
        payload = _get_json(
            "POST",
            GBIF_MATCH_URL,
            deadline=deadline,
            params={"checklistKey": checklist_key},
            json=[unique[key] for key in chunk],
        ) or []
        if len(payload) != len(chunk):
            raise TaxonServiceError(
                f"GBIF batch matcher returned {len(payload)} results for {len(chunk)} names."
            )
        for key, item in zip(chunk, payload):
            results[key] = summarize_match(item)
    # With verbose_exact, exact names are fetched again for their alternatives: the batch response leaves out the
    # homonyms an automatic decision must see.
    retry = [key for key, summary in results.items() if summary["status"] not in {"exact", "variant"}
             or (verbose_exact and summary["status"] == "exact")]

    def verbose_match(key):
        return _get_json(
            "GET",
            GBIF_MATCH_URL,
            deadline=deadline,
            params={**unique[key], "checklistKey": checklist_key, "verbose": "true"},
        )

    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
        for key, single in zip(retry, pool.map(verbose_match, retry)):
            if single is None:
                continue
            if results[key]["status"] == "exact":
                # The batch's exact match stands (the verbose answer can differ); its homonyms are added, and so is the
                # verbose pick when it is another usage.
                verbose = summarize_match(single)
                alternatives = list(verbose["alternatives"])
                if verbose["usage"] and str(verbose["usage"].get("id")) != str((results[key]["usage"] or {}).get("id")):
                    alternatives.insert(0, {**verbose["usage"], "matchType": verbose["matchType"], "confidence": verbose["confidence"]})
                kept, dropped = bounded_alternatives(alternatives)
                results[key] = {**results[key], "alternatives": kept,
                                "exactAlternativesDropped": dropped or verbose.get("exactAlternativesDropped", False)}
            else:
                results[key] = summarize_match(single)
    return [
        _without_hint_echo(results[json.dumps(_query_params(query), sort_keys=True)], query)
        for query in queries
    ]


def _without_hint_echo(summary, query):
    """A higher-rank match that is only the classification hint we sent is no match at all."""
    usage = summary.get("usage") or {}
    rank = usage.get("taxonRank")
    if (
        summary.get("status") == "higher_rank"
        and rank in HINT_RANKS
        and str(query.get(rank) or "").lower() == str(usage.get("scientificName") or "").lower()
    ):
        return {**summary, "status": "none", "usage": None, "hintOnly": True}
    return summary


def col_release(deadline=None):
    """Describe the COL release GBIF's matcher currently serves, for provenance."""
    metadata = _get_json(
        "GET", GBIF_MATCH_METADATA_URL, deadline=deadline, params={"checklistKey": col_checklist_key()},
    ) or {}
    index = metadata.get("mainIndex") or {}
    return {
        "checklistKey": index.get("datasetKey") or col_checklist_key(),
        "alias": index.get("datasetAlias"),
        "checklistBankDatasetKey": index.get("clbDatasetKey"),
        "created": metadata.get("created"),
    }


def _compact_aid(summary):
    usage = summary.get("usage") or {}
    return {
        "matchType": summary.get("matchType"),
        "status": summary.get("status"),
        "id": usage.get("id"),
        "scientificName": usage.get("scientificName"),
        "scientificNameAuthorship": usage.get("scientificNameAuthorship"),
        "taxonRank": usage.get("taxonRank"),
        "taxonomicStatus": usage.get("status"),
    }


def review_aids(query, deadline=None):
    """Backbone and ChecklistBank XR results for a name GBIF's COL copy could not place exactly."""
    params = _query_params(query)
    backbone = summarize_match(_get_json("GET", GBIF_MATCH_URL, deadline=deadline, params=params))
    clb = _get_json(
        "GET",
        f"{CHECKLISTBANK_API}/dataset/{col_checklistbank_dataset()}/match/nameusage",
        deadline=deadline,
        params={"q": params["scientificName"], **{k: v for k, v in params.items()
                                                    if k not in {"scientificName", "scientificNameID", "taxonID"}}},
    ) or {}
    clb_usage = clb.get("usage") or {}
    clb_type = str(clb.get("type") or "NONE").upper()
    return {
        "gbifBackbone": _compact_aid(backbone),
        "checklistBankXR": {
            "matchType": clb_type,
            "status": MATCH_STATUS.get(clb_type, "ambiguous"),
            "id": clb_usage.get("id"),
            "scientificName": clb_usage.get("name"),
            "scientificNameAuthorship": clb_usage.get("authorship"),
            "taxonRank": _rank(clb_usage.get("rank")),
            "taxonomicStatus": _rank(clb_usage.get("status")),
        },
    }


def search_col(text, limit=10):
    """Name suggestions from COL XR for a reviewer's manual choice; ids are COL usage ids."""
    payload = _get_json(
        "GET",
        f"{CHECKLISTBANK_API}/dataset/{col_checklistbank_dataset()}/nameusage/suggest",
        deadline=time.monotonic() + INTERACTIVE_BUDGET_SECONDS,
        params={"q": text, "limit": limit},
    ) or []
    return [
        {
            "id": item.get("usageId"),
            "label": item.get("match"),
            "taxonRank": _rank(item.get("rank")),
            "status": _rank(item.get("status")),
            "context": item.get("context"),
            "suggestion": item.get("suggestion"),
        }
        for item in payload
        if item.get("usageId")
    ]


def resolve_col_usage(usage_id):
    """Fetch one COL XR usage chosen by a reviewer, with its classification."""
    dataset = col_checklistbank_dataset()
    deadline = time.monotonic() + INTERACTIVE_BUDGET_SECONDS
    usage = _get_json("GET", f"{CHECKLISTBANK_API}/dataset/{dataset}/nameusage/{usage_id}", deadline=deadline)
    if not usage:
        raise TaxonServiceError(f"COL usage {usage_id} was not found in ChecklistBank {dataset}.")
    name = usage.get("name") or {}
    status = _rank(usage.get("status"))
    classification_id = usage_id
    if status and status != "accepted" and (usage.get("accepted") or {}).get("id"):
        classification_id = usage["accepted"]["id"]
    classification = _get_json(
        "GET", f"{CHECKLISTBANK_API}/dataset/{dataset}/taxon/{classification_id}/classification",
        deadline=deadline,
    ) or []
    resolved = _usage(
        {
            "id": usage.get("id"),
            "name": usage.get("label") or name.get("scientificName"),
            "canonicalName": name.get("scientificName"),
            "authorship": name.get("authorship"),
            "rank": name.get("rank"),
            "status": status,
        },
        classification,
    )
    if resolved["taxonRank"] in CLASSIFICATION_RANKS:
        resolved["classification"][resolved["taxonRank"]] = resolved["scientificName"]
    return resolved


# --- Dataset-level workflow -------------------------------------------------------------------

# Qualifiers that express uncertainty rather than rank; "sp." is carried by the genus rank.
UNCERTAIN_QUALIFIERS = ("cf.", "aff.", "nr.")


class MatchScopeError(ValueError):
    pass


def _clean_label(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = " ".join(str(value).split())
    return text or None


def _row_keys(df, verbatim_column, context_column=None):
    """(label, context_key) for every table row, positionally; label is None when blank."""
    if verbatim_column not in df.columns:
        raise KeyError(f"Column '{verbatim_column}' is not in the table.")
    if context_column and context_column not in df.columns:
        raise KeyError(f"Context column '{context_column}' is not in the table.")
    labels = [_clean_label(value) for value in df[verbatim_column].tolist()]
    if context_column:
        contexts = [(_clean_label(value) or "")[:200] for value in df[context_column].tolist()]
    else:
        contexts = [""] * len(labels)
    return list(zip(labels, contexts))


def distinct_labels(df, verbatim_column, context_column=None):
    """Return {(label, context_key): record_count} for the populated labels in a table."""
    counts = {}
    for label, context in _row_keys(df, verbatim_column, context_column):
        if label:
            counts[(label, context)] = counts.get((label, context), 0) + 1
    return counts


def default_query(label):
    name, qualifier = split_qualifier(label)
    return {"scientificName": name}, qualifier


def _scope(dataset, source_table, context_column):
    from api.models import TaxonNameMatch

    return TaxonNameMatch.objects.filter(
        dataset=dataset, source_table=source_table, context_column=context_column or "",
    )


def _check_single_context_configuration(dataset, source_table, context_column):
    from api.models import TaxonNameMatch

    other = (
        TaxonNameMatch.objects.filter(dataset=dataset, source_table=source_table, record_count__gt=0)
        .exclude(context_column=context_column or "")
        .values_list("context_column", flat=True)
        .first()
    )
    if other is not None:
        raise MatchScopeError(
            f"Labels in `{source_table}` were matched "
            + (f"with context column `{other}`" if other else "without a context column")
            + "; use the same configuration."
        )


def record_matches(dataset, source_table, df, verbatim_column, context_column=None, overrides=None,
                   hints=None, budget_seconds=RUN_BUDGET_SECONDS):
    """Create or refresh TaxonNameMatch rows for one table and match what needs matching.

    ``overrides`` maps (label, context_key) to {"query": {...}, "note": str} for labels whose
    matchable name differs from the label (vernacular, abbreviated genus, typo). A new
    interpretation of a reviewed label reopens it for review. Matching stops at the time budget;
    unmatched rows are matched by the next call. Returns (rows for the current labels, finished).
    """
    from django.utils import timezone
    from api.models import TaxonNameMatch

    context_column = context_column or ""
    _check_single_context_configuration(dataset, source_table, context_column)
    overrides = overrides or {}
    hints = {rank: value for rank, value in (hints or {}).items() if rank in HINT_RANKS and value}
    counts = distinct_labels(df, verbatim_column, context_column)
    existing = {(row.verbatim_label, row.context_key): row for row in _scope(dataset, source_table, context_column)}

    to_match = []
    current = []
    for key, count in counts.items():
        label, context_key = key
        row = existing.get(key) or TaxonNameMatch(
            dataset=dataset, source_table=source_table, context_column=context_column,
            verbatim_label=label, context_key=context_key,
        )
        row.record_count = count
        # A decision was made on labels read from one column; the same text elsewhere is new evidence.
        column_changed = row.pk is not None and row.verbatim_column != verbatim_column
        row.verbatim_column = verbatim_column
        row.identification_qualifier = default_query(label)[1] or ""
        if key in overrides:
            query, note = {**hints, **overrides[key]["query"]}, overrides[key].get("note") or ""
        elif row.query:
            query, note = row.query, row.preprocessing_note
        else:
            query, note = {**hints, **default_query(label)[0]}, ""
        if row.decision != TaxonNameMatch.Decision.PENDING and (column_changed or query != row.decided_query):
            # The reviewer decided on a different interpretation or column; ask again.
            _reset_decision(row)
        changed = query != row.query or not row.matched_at
        row.query, row.preprocessing_note = query, note
        if changed:
            row.matched_at = None
            to_match.append(row)
        row.save()
        current.append(row)

    stale = [row.id for key, row in existing.items() if key not in counts]
    if stale:
        TaxonNameMatch.objects.filter(id__in=stale).update(record_count=0)

    deadline = time.monotonic() + budget_seconds
    # Rows matched by an earlier call whose review aids did not finish.
    aid_rows = [row for row in current if row.matched_at and _aids_pending(row)]
    saved = 0
    try:
        release = col_release(deadline) if to_match else {}
        # Small chunks are saved as they finish, so a run cut short by the budget still progresses.
        for start in range(0, len(to_match), PROGRESS_CHUNK):
            chunk = to_match[start:start + PROGRESS_CHUNK]
            summaries = match_col([row.query for row in chunk], deadline=deadline)
            now = timezone.now()
            for row, summary in zip(chunk, summaries):
                row.match = summary
                row.col_release = release
                row.matched_at = now
                row.review_aids = {} if summary["status"] in {"exact", "variant"} else {"pending": True}
                row.save(update_fields=["match", "col_release", "matched_at", "review_aids", "updated_at"])
                saved += 1
                if _aids_pending(row):
                    aid_rows.append(row)
        saved += _fill_review_aids(aid_rows, deadline)
    except TaxonServiceError:
        if not saved:
            raise
        return current, False
    return current, True


def _aids_pending(row):
    return bool((row.review_aids or {}).get("pending"))


def _fill_review_aids(rows, deadline):
    """Fetch review aids concurrently, saving each row as soon as its aids arrive."""
    saved = 0
    pool = ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS)
    try:
        futures = {pool.submit(review_aids, row.query, deadline): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            row.review_aids = future.result()
            row.save(update_fields=["review_aids", "updated_at"])
            saved += 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return saved


def matching_complete(rows):
    """True when every current label has a match and its review aids."""
    return all(row.matched_at and not _aids_pending(row) for row in rows if row.record_count)


def is_preprocessed(row):
    """True when the matched name came from an interpretation rather than the label itself."""
    return row.query.get("scientificName") != default_query(row.verbatim_label)[0]["scientificName"]


def usage_for_decision(row, usage_id=None):
    """The usage a reviewer accepts: the suggestion, one of its alternatives, or a searched id."""
    suggestion = (row.match or {}).get("usage")
    if usage_id is None or (suggestion and suggestion.get("id") == usage_id):
        if not suggestion:
            raise ValueError("There is no suggested COL name to accept for this label.")
        return {**suggestion, "source": "gbif_col"}
    for alternative in (row.match or {}).get("alternatives") or []:
        if alternative.get("id") == usage_id:
            return {
                key: alternative.get(key)
                for key in ("id", "scientificName", "scientificNameAuthorship", "label",
                            "taxonRank", "status", "classification", "matchType")
            } | {"source": "gbif_col"}
    return {**resolve_col_usage(usage_id), "source": "checklistbank_xr"}


def row_replacement(row, usage, match_type):
    """The confirmation-needing change accepting `usage` would make to the row's matched name; None otherwise."""
    query = row.query or {}
    return coarser_replacement(query.get("scientificName"), usage, match_type,
                               hints={rank: query[rank] for rank in HINT_RANKS if query.get(rank)})


def choice_replacements(row):
    """For the review: what accepting the suggestion or each alternative would replace (None when nothing)."""
    match = row.match or {}
    return {
        "suggestion": row_replacement(row, match["usage"], match.get("matchType")) if match.get("usage") else None,
        "alternatives": {str(alternative.get("id")): row_replacement(row, alternative, alternative.get("matchType"))
                         for alternative in match.get("alternatives") or []},
    }


def _reset_decision(row):
    from api.models import TaxonNameMatch

    row.decision = TaxonNameMatch.Decision.PENDING
    row.decided_usage = {}
    row.decided_query = {}
    row.decided_by = None
    row.decided_at = None


def decide(row, decision, user=None, usage_id=None, name=None, confirm_coarser=False):
    """Record one reviewer decision. ``name`` carries the reviewer's name for NOT_IN_COL.

    Accepting a COL name that is coarser than or different from the matched name (see `name_change`) needs
    ``confirm_coarser``.
    """
    from django.utils import timezone
    from api.models import TaxonNameMatch

    Decision = TaxonNameMatch.Decision
    if decision == Decision.PENDING:
        _reset_decision(row)
        row.save(update_fields=["decision", "decided_usage", "decided_query", "decided_by", "decided_at", "updated_at"])
        return row
    if not row.matched_at:
        raise ValueError("This label has not been matched yet.")
    if decision == Decision.ACCEPTED:
        usage = usage_for_decision(row, usage_id)
        suggestion = (row.match or {}).get("usage") or {}
        match_type = (row.match or {}).get("matchType") if usage.get("id") == suggestion.get("id") else usage.get("matchType")
        replaces = row_replacement(row, usage, match_type)
        if replaces and confirm_coarser is not True:
            raise ValueError(f'"{usage.get("scientificName")}" {replaces["text"]} for "{row.verbatim_label}". '
                             "Confirm that replacement explicitly, or keep the original.")
        usage.pop("matchType", None)
        if replaces:
            usage["replaces"] = replaces["text"]
    elif decision == Decision.NOT_IN_COL:
        name = name or {}
        scientific_name = " ".join(str(name.get("scientificName") or "").split())
        if not scientific_name:
            scientific_name = row.query.get("scientificName") or ""
        if not scientific_name:
            raise ValueError("A scientific name is required when the name is not in COL.")
        # The higher-rank placement COL did find is still a valid classification for the name.
        suggestion = (row.match or {}).get("usage") or {}
        classification = dict(suggestion.get("classification") or {}) if (
            (row.match or {}).get("status") == "higher_rank"
        ) else {}
        usage = {
            "id": None,
            "scientificName": scientific_name,
            "scientificNameAuthorship": name.get("scientificNameAuthorship") or None,
            "taxonRank": _rank(name.get("taxonRank")),
            "classification": classification,
            "source": "reviewer",
        }
    elif decision == Decision.KEEP_ORIGINAL:
        usage = {}
    else:
        raise ValueError(f"Unknown decision '{decision}'.")
    row.decision = decision
    row.decided_usage = usage
    row.decided_query = row.query
    row.decided_by = user
    row.decided_at = timezone.now()
    row.save(update_fields=["decision", "decided_usage", "decided_query", "decided_by", "decided_at", "updated_at"])
    return row


def bulk_acceptable(row):
    """Exact matches of names written in the label, without any identification qualifier.

    A variant or higher-rank match is a different name; an interpreted name (translation, expanded
    abbreviation, typo fix) and a qualified label ("sp.", "cf.") each need a reviewer's own look. An
    "exact" match whose name is coarser than or differs from the queried name (hints can steer the
    matcher there) is never accepted in bulk either.
    """
    from api.models import TaxonNameMatch

    match = row.match or {}
    return (
        row.decision == TaxonNameMatch.Decision.PENDING
        and row.record_count > 0
        and row.matched_at is not None  # a changed interpretation awaiting its new match
        and match.get("matchType") == "EXACT"
        and bool(match.get("usage"))
        and not row.identification_qualifier
        and not is_preprocessed(row)
        and not row_replacement(row, match["usage"], match.get("matchType"))
    )


def accept_exact_matches(dataset, user=None, source_table=None, context_column=None):
    from api.models import TaxonNameMatch

    rows = TaxonNameMatch.objects.filter(dataset=dataset, decision=TaxonNameMatch.Decision.PENDING)
    if source_table is not None:
        rows = rows.filter(source_table=source_table, context_column=context_column or "")
    return [decide(row, TaxonNameMatch.Decision.ACCEPTED, user=user) for row in rows if bulk_acceptable(row)]


def _is_blank(value):
    return _clean_label(value) is None


def apply_decisions(dataset, table, verbatim_column, context_column=None):
    """Write reviewed names onto a table's rows; unreviewed and kept labels are left unchanged.

    The reviewed name, authorship and rank replace the current values (only with values the
    decision has). Higher classification only fills blank cells, so supplied taxonomy is never
    overwritten. verbatim_column is only read. Returns a summary dict.
    """
    from django.utils import timezone
    from api.dwc_dp_specs import get_table_spec, normalize_resource_name
    from api.models import TaxonNameMatch

    source_table = normalize_resource_name(table.title)
    context_column = context_column or ""
    _check_single_context_configuration(dataset, source_table, context_column)
    df = table.df.copy()
    keys = _row_keys(df, verbatim_column, context_column)
    try:
        schema_fields = set(get_table_spec(source_table).fields)
    except KeyError:
        schema_fields = None  # a working table: any Darwin Core term may be written

    def writable(column):
        return column in df.columns or schema_fields is None or column in schema_fields

    positions = {}
    for position, key in enumerate(keys):
        if key[0]:
            positions.setdefault(key, []).append(position)

    written = {"rows_updated": 0, "labels_applied": 0, "pending_labels": 0, "pending_rows": 0,
               "kept_labels": 0, "unmatched_labels": 0, "skipped_columns": set(), "qualifiers_not_written": 0}
    rows = {(row.verbatim_label, row.context_key): row for row in _scope(dataset, source_table, context_column)}
    applied = []
    for key, row_positions in positions.items():
        row = rows.get(key)
        if row is None:
            written["unmatched_labels"] += 1
            continue
        if row.decision == TaxonNameMatch.Decision.PENDING:
            written["pending_labels"] += 1
            written["pending_rows"] += len(row_positions)
            continue
        if row.decision == TaxonNameMatch.Decision.KEEP_ORIGINAL:
            written["kept_labels"] += 1
            continue
        usage = row.decided_usage or {}
        replace = {
            column: usage.get(column)
            for column in ("scientificName", "scientificNameAuthorship", "taxonRank")
            if usage.get(column)
        }
        fill = dict(usage.get("classification") or {})
        if row.identification_qualifier:
            fill["identificationQualifier"] = row.identification_qualifier
            if not writable("identificationQualifier"):
                written["qualifiers_not_written"] += 1
        for column, value in [*replace.items(), *fill.items()]:
            if not writable(column):
                written["skipped_columns"].add(column)
                continue
            if column not in df.columns:
                df[column] = None
            if df[column].dtype != object:
                df[column] = df[column].astype(object)
            column_index = df.columns.get_loc(column)
            for position in row_positions:
                if column in replace or _is_blank(df.iat[position, column_index]):
                    df.iat[position, column_index] = value
        written["rows_updated"] += len(row_positions)
        written["labels_applied"] += 1
        applied.append(row.id)

    if applied:
        table.df = df
        table.save()
        TaxonNameMatch.objects.filter(id__in=applied).update(applied_at=timezone.now())
    written["skipped_columns"] = sorted(written["skipped_columns"])
    return written


class ReviewApplyError(ValueError):
    pass


def current_table(dataset, source_table):
    """The dataset's one current table for a resource title; the agent may have replaced it."""
    from api.dwc_dp_specs import normalize_resource_name
    from api.models import Table

    tables = [
        table for table in Table.objects.filter(dataset=dataset).defer("df").order_by("id")
        if normalize_resource_name(table.title) == source_table
    ]
    if len(tables) != 1:
        raise ReviewApplyError(
            f"expected one `{source_table}` table but found {len(tables)}"
        )
    return Table.objects.get(id=tables[0].id)


def apply_review(dataset, source_table, context_column=""):
    """Apply a finished review to the table it was matched on. Returns (table, apply summary)."""
    from api.models import TaxonNameMatch

    rows = list(TaxonNameMatch.objects.filter(
        dataset=dataset, source_table=source_table, context_column=context_column or "", record_count__gt=0,
    ))
    columns = {row.verbatim_column for row in rows}
    if len(columns) != 1 or "" in columns:
        raise ReviewApplyError(f"the reviewed labels for `{source_table}` do not record one source column")
    column = columns.pop()
    table = current_table(dataset, source_table)
    try:
        # Check before writing anything: every reviewed name must still be in the table.
        present = set(_row_keys(table.df, column, context_column))
        missing = [
            row.verbatim_label for row in rows
            if row.decision in {TaxonNameMatch.Decision.ACCEPTED, TaxonNameMatch.Decision.NOT_IN_COL}
            and (row.verbatim_label, row.context_key) not in present
        ]
        if missing:
            raise ReviewApplyError(
                f"{len(missing)} reviewed labels, e.g. {missing[0]!r}, are no longer in column `{column}` "
                f"of `{source_table}`"
            )
        return table, apply_decisions(dataset, table, column, context_column)
    except (KeyError, MatchScopeError) as exc:
        raise ReviewApplyError(str(exc).strip("'\"")) from exc


def describe_apply_result(result, table):
    """Plain sentences describing what apply_decisions did, for the agent."""
    lines = [
        f"Applied reviewed names for {result['labels_applied']:,} labels to {result['rows_updated']:,} rows "
        f"of table {table.id} (`{table.title}`). {result['kept_labels']:,} labels were kept unchanged by the user.",
    ]
    if result["pending_labels"]:
        lines.append(
            f"{result['pending_labels']:,} labels ({result['pending_rows']:,} rows) are still pending "
            "review and were left unchanged."
        )
    if result["unmatched_labels"]:
        lines.append(
            f"{result['unmatched_labels']} labels in this table have never been matched; "
            "call MatchTaxonNames for this table first."
        )
    if result["qualifiers_not_written"]:
        lines.append(
            f"{result['qualifiers_not_written']} labels have an identification qualifier (e.g. sp., cf.) "
            "that this table cannot hold; it remains in the verbatim identification."
        )
    ranks = [column for column in result["skipped_columns"] if column in CLASSIFICATION_RANKS]
    others = [
        column for column in result["skipped_columns"]
        if column not in ranks and column != "identificationQualifier"
    ]
    if ranks:
        lines.append(
            "Higher classification was not written: this table's DwC-DP schema has no columns for it, "
            "and GBIF derives it from the name. This is expected."
        )
    if others:
        lines.append("Not written because the table's DwC-DP schema has no such column: " + ", ".join(others))
    return " ".join(lines)


def _plural(count, word):
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def review_summary(rows):
    """The user's closing message for a review, written from the saved decisions."""
    from api.models import TaxonNameMatch

    Decision = TaxonNameMatch.Decision
    counts, records = {}, {}
    for row in rows:
        counts[row.decision] = counts.get(row.decision, 0) + 1
        records[row.decision] = records.get(row.decision, 0) + row.record_count
    parts = [
        f"{_plural(counts[decision], 'name')} {outcome}"
        for decision, outcome in (
            (Decision.ACCEPTED, "accepted"),
            (Decision.NOT_IN_COL, "marked as correct but not in COL"),
            (Decision.KEEP_ORIGINAL, "kept unchanged"),
        )
        if counts.get(decision)
    ]
    message = "I have finished reviewing the taxon names" + (f": {', '.join(parts)}." if parts else ".")
    pending = counts.get(Decision.PENDING, 0)
    if pending:
        message += (
            f" {_plural(pending, 'name')} ({_plural(records[Decision.PENDING], 'record')}) "
            f"{'is' if pending == 1 else 'are'} left unreviewed."
        )
    return message


def open_review(dataset, lock=False):
    """(agent, scope) when the dataset's current agent is paused waiting for a taxon review."""
    from api.models import Agent, Message

    agents = Agent.objects.filter(dataset=dataset, completed_at__isnull=True).order_by("id")
    if lock:
        agents = agents.select_for_update()
    agent = agents.last()
    if not agent or agent.busy_thinking:
        return None
    last_message = agent.message_set.last()
    if not last_message or last_message.role != Message.Role.ASSISTANT:
        return None
    scope = (last_message.openai_obj or {}).get("taxon_review")
    if not isinstance(scope, dict):
        return None
    return agent, {"source_table": scope.get("source_table") or "", "context_column": scope.get("context_column") or ""}
