"""Match verbatim taxon labels against Catalogue of Life XR.

GBIF.org interprets occurrences against COL XR by default, so the primary suggestion comes from
GBIF's v2 matcher with the COL checklistKey: the same engine and taxonomy copy GBIF indexes with.
When that finds nothing or only a higher rank, ChecklistBank's newer XR release and the legacy
GBIF Backbone are consulted as review aids only; they may not yet be reflected on GBIF.org.

A match suggests a name. It never asserts that the identification is correct, so suggestions are
stored for review. The shared review accepts safe exact matches and uncertain stems automatically;
interpretations and taxonomic replacements require the reviewer's choice.
"""

import json
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import pandas as pd
import requests
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


GBIF_MATCH_URL = "https://api.gbif.org/v2/species/match"
GBIF_PARSER_URL = "https://api.gbif.org/v1/parser/name"
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
NAME_RULES_VERSION = 3

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


def explicit_markers(name):
    """The meaning-bearing markers written in a name: infraspecific rank markers, normalised ("ssp." is "subsp."), a
    hybrid sign ("Rosa × canina", "×Agropogon", "Rosa x canina") and "agg."; authorship words are ignored."""
    tokens = str(name or "").split()
    markers = [_MARKER_FORMS[token.casefold()] for index, token in enumerate(tokens)
               if token.casefold() in _MARKER_FORMS and index + 1 < len(tokens) and tokens[index + 1][:1].islower()]
    if "×" in str(name or "") or any(token == "x" and index + 1 < len(tokens) and tokens[index + 1][:1].islower()
                                     for index, token in enumerate(tokens[1:], 1)):
        markers.append("×")
    if any(token.casefold() == "agg." for token in tokens):
        markers.append("agg.")
    return markers


def name_change(asserted, usage, match_type=None, asserted_rank=None, hints=None):
    """How accepting `usage` would change the asserted name; None when it is the same name.

    Missing infraspecific markers, authorship and case do not change a name; explicit marker changes do. "Larus sp."
    accepted as the genus Larus keeps the user's assertion. Every kind but "spelling" needs explicit confirmation:
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
    my_markers, their_markers = explicit_markers(asserted), explicit_markers(usage.get("scientificName"))
    rank_markers = {"subsp.", "var.", "subvar.", "f.", "subf.", "nothosubsp.", "nothovar."}
    my_rank = next((marker for marker in my_markers if marker in rank_markers), None)
    their_rank = next((marker for marker in their_markers if marker in rank_markers), None)
    special_differs = (("×" in my_markers) != ("×" in their_markers)
                       or ("agg." in my_markers) != ("agg." in their_markers))
    rank_differs = bool(my_rank and their_rank and my_rank != their_rank) or bool(my_rank and not their_rank)
    if mine == theirs and (special_differs or rank_differs):
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


INFRASPECIFIC_RANKS = {"subspecies", "variety", "subvariety", "form", "subform"}
# Rank markers inside a name; any other word after the genus in an unparsed label may be an authorship.
RANK_MARKER_WORDS = {"subsp.", "ssp.", "var.", "subvar.", "f.", "fo.", "forma", "subf.", "agg.", "nothosubsp.", "nothovar.", "×", "x"}


def rank_change(asserted, supplied_rank, col_rank):
    """What COL's rank does to the rank the source supplies for its own name ("makes your genus an order"); else None.

    Compared for a uninomial supplied at genus or above, and for a trinomial supplied at a specific infraspecific rank.
    """
    if supplied_rank not in RANK_ORDER or col_rank not in RANK_ORDER or supplied_rank == col_rank:
        return None
    parts = len(name_parts(asserted))
    uninomial = parts == 1 and RANK_ORDER.index(supplied_rank) <= RANK_ORDER.index("genus")
    trinomial = parts > 2 and supplied_rank in INFRASPECIFIC_RANKS and col_rank in INFRASPECIFIC_RANKS
    article = "an" if col_rank[:1] in "aeiou" else "a"
    return f"makes your {supplied_rank} {article} {col_rank}" if uninomial or trinomial else None


def unread_trailing_text(label):
    """An unparsed label has words after the first that are not plain epithets or rank markers ("Aus bus Smith, 1900",
    "Larus argentatus s."): they may be an authorship nobody can compare."""
    words = split_qualifier(label)[0].split()[1:]
    return any(not (word.casefold() in RANK_MARKER_WORDS or (word.islower() and word.replace("-", "").isalpha())) for word in words)


def col_authorship_agrees(supplied, theirs, unreadable=False):
    """COL's authorship (theirs) names the authors of every supplied authorship, or COL has none, so writing it replaces
    nothing of the user's. An unreadable supplied authorship (too many to compare, or text after a name the parser could
    not read) never agrees."""
    if unreadable:
        return False
    return not " ".join(str(theirs or "").split()) or all(authorships_agree(value, theirs) for value in supplied)


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


def summarize_match(payload, name=None):
    """Reduce a GBIF v2 match response to what review and write-back need; `name` is the queried name, whose same-name
    alternatives (possible homonyms) are all kept."""
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
    alternatives, exact_dropped = bounded_alternatives(alternatives, name)
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


def bounded_alternatives(alternatives, name=None):
    """(alternatives kept in their order, whether possible homonyms were dropped): every exact one, and every one with the
    queried name's parts whatever its match type, up to MAX_EXACT_ALTERNATIVES; other ones up to MAX_ALTERNATIVES."""
    kept, exact, other, dropped = [], 0, 0, False
    parts = name_parts(name) if name else None
    for alternative in alternatives:
        if alternative.get("matchType") == "EXACT" or (parts and name_parts(alternative.get("scientificName")) == parts):
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
            results[key] = summarize_match(item, unique[key]["scientificName"])
    # With verbose_exact, exact names are fetched again for their alternatives: the batch response leaves out the
    # homonyms an automatic decision must see.
    retry = [key for key, summary in results.items() if summary["status"] not in {"exact", "variant"}
             or (verbose_exact and summary["status"] in {"exact", "variant"})]

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
            if results[key]["status"] in {"exact", "variant"}:
                # The batch's exact match stands (the verbose answer can differ); its homonyms are added, and so is the
                # verbose pick when it is another usage.
                verbose = summarize_match(single, unique[key]["scientificName"])
                alternatives = list(verbose["alternatives"])
                if verbose["usage"] and str(verbose["usage"].get("id")) != str((results[key]["usage"] or {}).get("id")):
                    alternatives.insert(0, {**verbose["usage"], "matchType": verbose["matchType"], "confidence": verbose["confidence"]})
                kept, dropped = bounded_alternatives(alternatives, unique[key]["scientificName"])
                results[key] = {**results[key], "alternatives": kept,
                                "exactAlternativesDropped": dropped or verbose.get("exactAlternativesDropped", False)}
            else:
                results[key] = summarize_match(single, unique[key]["scientificName"])
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
    release_key = usage.get("datasetKey")
    classification = _get_json(
        "GET", f"{CHECKLISTBANK_API}/dataset/{release_key or dataset}/taxon/{classification_id}/classification",
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
    if release_key:
        resolved["checklistBankDatasetKey"] = release_key
    return resolved


# GBIF's parser reports a rank marker rather than a rank; only these are mapped to a DwC taxonRank.
RANK_MARKERS = {"sp.": "species", "subsp.": "subspecies", "var.": "variety", "f.": "form",
                "subvar.": "subvariety", "subf.": "subform", "agg.": "species aggregate"}


def summarize_parse(label, item):
    """What review needs from one GBIF name-parser result."""
    item = item or {}
    with_marker = item.get("canonicalNameWithMarker") or item.get("canonicalName") or ""
    complete = item.get("canonicalNameComplete") or ""
    usable = bool(item.get("parsed")) and not item.get("parsedPartially") and item.get("type") == "SCIENTIFIC" and bool(with_marker)
    authorship = None
    if with_marker and complete.startswith(with_marker + " "):
        authorship = complete[len(with_marker):].strip() or None  # the full authorship, year and brackets included
    elif item.get("authorship") and complete != with_marker:
        authorship = item["authorship"]
    rank = str(item.get("rank") or "").lower().replace("_", " ") or RANK_MARKERS.get(item.get("rankMarker"))
    if rank in {"unranked", "infraspecific name", "infrasubspecific name", "informal"}:
        rank = None
    return {"type": item.get("type"), "usable": usable, "canonical": with_marker or None, "authorship": authorship,
            "rank": rank or None,
            # The parts rebuild the supplied text exactly, so splitting loses and renames nothing.
            "lossless": usable and complete == " ".join(str(label or "").split()), "splits": usable and bool(authorship)}


def parse_names(labels, deadline=None):
    """One summary per label from GBIF's name parser (batch POST)."""
    if not labels:
        return []
    payload = _get_json("POST", GBIF_PARSER_URL, deadline=deadline, json=list(labels)) or []
    if not isinstance(payload, list) or len(payload) != len(labels):
        raise TaxonServiceError(f"GBIF name parser returned {len(payload) if isinstance(payload, list) else 'an invalid response'} "
                                f"results for {len(labels)} names.")
    return [summarize_parse(label, item) for label, item in zip(labels, payload)]


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


def supplied_context(df, verbatim_column, context_column=None):
    """Collect the same rank, lineage, identifiers and qualifier evidence in either workflow."""
    from api.name_review import collect_records

    keys = _row_keys(df, verbatim_column, context_column)
    columns = [field for field in df.columns if field != 'scientificName']
    values = []
    for (label, context), values_in_row in zip(keys, df[columns].itertuples(index=False, name=None)):
        if not label:
            continue
        values.append([_clean_label(value) or '' for value in values_in_row]
                      + [label, json.dumps([label, context])])
    state = collect_records([('source', columns + ['scientificName', 'reviewKey'], values)], max_labels=len(values))
    return {tuple(json.loads(record['key'])): {
                **{key: value for key, value in record.items() if key not in {'key', 'label', 'rows', 'tables'}},
                'authorships': record.get('source_authorships') or [],
                'truncated': bool(record.get('authorships_truncated')),
                'rank': record.get('source_rank'),
            } for record in state['labels']}


def default_query(label):
    name, qualifier = split_qualifier(label)
    return {"scientificName": name}, qualifier


MAX_CORRECTED_NAME_CHARS = 200


def corrected_name(text):
    """A name the reviewer typed, whitespace-normalised. Both reviews publish it as scientificName exactly as typed."""
    name = " ".join(str(text or "").split())
    if not name:
        raise ValueError("Type the corrected name.")
    if len(name) > MAX_CORRECTED_NAME_CHARS:
        raise ValueError(f"A corrected name can be at most {MAX_CORRECTED_NAME_CHARS} characters.")
    return name

# --- What a name decision writes (both reviews) ------------------------------------------------

NAME_FIELDS = ("scientificName", "scientificNameAuthorship", "taxonRank")


def own_name_kind(parsed, qualified):
    """What "Keep original" writes: the parser's split ("parsed") when it rebuilds the label exactly and nothing
    qualifies the name ("sp.", "cf."), else the label as written ("keep")."""
    parsed = parsed or {}
    return "parsed" if not qualified and parsed.get("usable") and parsed.get("lossless") else "keep"


def written_fields(kind, decided, keeps_name=False, label_authorship=None):
    """What a name decision writes to each of NAME_FIELDS, for both reviews and their previews.

    Each field maps to (how, value): "set" writes value ("" empties the cell); "yours" keeps the row's own value;
    "yours_or" keeps the row's own value and writes value where the row has none; "yours_or_label" does so with the
    row's own label as written.
    - keep: the label fills an empty scientificName; authorship and rank stay the row's own.
    - parsed: the parser's name, and its rank when it has one; the row's own authorship, else the parsed one.
    - corrected: the typed name; authorship and rank stay the row's own.
    - empty: no name, authorship or rank.
    - any COL name: COL's name, rank and authorship. When it is the user's own name (keeps_name), the row's authorship
      (else the label's) and rank stand where COL has none; another taxon gets none of them.
    """
    decided = decided or {}
    name, authorship, rank = (decided.get(field) or "" for field in NAME_FIELDS)
    yours = ("yours", None)
    if kind == "keep":
        return {"scientificName": ("yours_or_label", None), "scientificNameAuthorship": yours, "taxonRank": yours}
    if kind == "empty":
        return dict.fromkeys(NAME_FIELDS, ("set", ""))
    if kind == "corrected":
        return {"scientificName": ("set", name), "scientificNameAuthorship": yours, "taxonRank": yours}
    if kind == "parsed":
        return {"scientificName": ("set", name), "scientificNameAuthorship": ("yours_or", authorship),
                "taxonRank": ("set", rank) if rank else yours}
    return {"scientificName": ("set", name),
            "scientificNameAuthorship": ("set", authorship) if authorship
            else ("yours_or", label_authorship or "") if keeps_name else ("set", ""),
            "taxonRank": ("set", rank) if rank else yours if keeps_name else ("set", "")}


def row_value(outcome, own, label):
    """One row's value for one field of `written_fields`, given the row's own value and label."""
    how, value = outcome
    if how == "set":
        return value
    if how == "yours" or not _is_blank(own):
        return own
    return label if how == "yours_or_label" else (value or own)


def preview_items(fields, label):
    """`written_fields` as the reviews show them: [{field, how, value}], how being "set", "empty" or "yours" (the row's
    own value, or value where it has none)."""
    items = []
    for field, (how, value) in fields.items():
        if how == "set":
            items.append({"field": field, "how": "set" if value else "empty", "value": value or None})
        else:
            fill = label if how == "yours_or_label" else value
            items.append({"field": field, "how": "yours", "value": fill or None})
    return items


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
    interpretation of a reviewed label reopens it for review. Changing the context column retires
    the old grouping and starts a fresh review without changing source data. Matching stops at the
    time budget; unmatched rows are matched by the next call. Returns (current rows, finished).
    """
    from django.utils import timezone
    from api.models import Agent, Dataset, TaxonNameMatch

    context_column = context_column or ""
    overrides = overrides or {}
    hints = {rank: value for rank, value in (hints or {}).items() if rank in HINT_RANKS and value}
    counts = distinct_labels(df, verbatim_column, context_column)
    supplied = supplied_context(df, verbatim_column, context_column)
    # The review endpoints take this same lock. Retire the old grouping and prepare
    # the new rows atomically, then release the lock before calling taxon services.
    with transaction.atomic():
        Dataset.objects.select_for_update().get(pk=dataset.pk)
        other = TaxonNameMatch.objects.filter(
            dataset=dataset, source_table=source_table, record_count__gt=0,
        ).exclude(context_column=context_column)
        context_changed = other.exists()
        if context_changed:
            reviewing = Agent.objects.filter(
                dataset=dataset, completed_at__isnull=True,
                open_taxon_review__source_table=source_table,
            ).first()
            if reviewing:
                raise MatchScopeError(open_review_error(reviewing, "change the taxon matching context"))
            # Keep the old suggestions and decisions for provenance, outside the active review.
            other.update(record_count=0)
        existing = {(row.verbatim_label, row.context_key): row for row in _scope(dataset, source_table, context_column)}

        to_match = []
        current = []
        for key, count in counts.items():
            label, context_key = key
            row = existing.get(key) or TaxonNameMatch(
                dataset=dataset, source_table=source_table, context_column=context_column,
                verbatim_label=label, context_key=context_key,
            )
            if context_changed:
                # A returning configuration is a fresh review, not permission to revive a
                # broad interpretation or decisions from an earlier grouping.
                _reset_decision(row)
                row.query = {}
                row.preprocessing_note = ""
                row.review_meta = {}
                row.match = {}
                row.parsed = {}
                row.review_aids = {}
                row.col_release = {}
                row.matched_at = None
                row.applied_at = None
            row.record_count = count
            # A decision was made on labels read from one column; the same text elsewhere is new evidence.
            column_changed = row.pk is not None and row.verbatim_column != verbatim_column
            row.verbatim_column = verbatim_column
            row.identification_qualifier = default_query(label)[1] or ""
            context = supplied.get(key, {})
            previous_hints = set((row.supplied or {}).get('hints') or {})
            supplied_changed = bool(row.supplied) and context != row.supplied
            row.supplied = context
            if key in overrides:
                query, note = {**hints, **overrides[key]["query"]}, overrides[key].get("note") or ""
            elif row.query:
                query = {**{field: value for field, value in row.query.items() if field not in previous_hints},
                         **hints, **context.get('hints', {})}
                note = row.preprocessing_note
            else:
                query, note = {**hints, **context.get("hints", {}), **default_query(label)[0]}, ""
            if row.decision != TaxonNameMatch.Decision.PENDING and (column_changed or supplied_changed or query != row.decided_query):
                # The reviewer decided on a different interpretation or column; ask again.
                _reset_decision(row)
                row.review_meta = {}
            changed = query != row.query or supplied_changed or not row.matched_at
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
            items = [{'label': str(row.id), 'query': row.query, 'qualifier': row.identification_qualifier,
                      'ids': (row.supplied or {}).get('source_ids') or {}, 'parse_label': row.verbatim_label} for row in chunk]
            results, error = check_name_chunk(items, deadline)
            if error and not any(item.get('match') for item in results.values()):
                raise error
            now = timezone.now()
            for row in chunk:
                result = results.get(str(row.id), {})
                summary = result.get('match')
                if not summary:
                    continue
                row.match = summary
                row.parsed = result.get('parsed', {})
                row.col_release = release
                row.matched_at = now
                row.review_aids = {} if summary["status"] in {"exact", "variant"} else {"pending": True}
                row.save(update_fields=["match", "parsed", "col_release", "matched_at", "review_aids", "updated_at"])
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


def choice_check(row, usage, match_type):
    from api import name_review, taxon_review

    source = taxon_review.record(row)
    found = name_review.change(source, usage, match_type)
    return {"replaces": found if found and found['confirm'] else None,
            "changeKind": found['kind'] if found else None,
            "authorship_differs": bool(usage.get('scientificNameAuthorship')) and not name_review.authorship_agrees(source, usage),
            "rank_change": name_review._rank_change(source, usage)}


def needs_confirmation(check):
    return bool(check["replaces"] or check["authorship_differs"] or check["rank_change"])


def choice_checks(row):
    """For the review: `choice_check` of the suggestion and of each alternative."""
    match = row.match or {}
    return {
        "suggestion": choice_check(row, match["usage"], match.get("matchType")) if match.get("usage") else None,
        "alternatives": {str(alternative.get("id")): choice_check(row, alternative, alternative.get("matchType"))
                         for alternative in match.get("alternatives") or []},
    }


def _reset_decision(row):
    from api.models import TaxonNameMatch

    row.decision = TaxonNameMatch.Decision.PENDING
    row.decided_usage = {}
    row.decided_query = {}
    row.decided_by = None
    row.decided_at = None


def decide(row, decision, user=None, usage_id=None, scientific_name=None, confirm_coarser=False):
    """Validate a chat choice using the same snapshots and confirmation rules as conversion."""
    from api import name_review, taxon_review
    from api.models import TaxonNameMatch

    if decision == TaxonNameMatch.Decision.PENDING:
        review = taxon_review.state([row])
        name_review.set_decisions(review, {str(row.id): None})
    else:
        if not row.matched_at:
            raise ValueError("This label has not been matched yet.")
        kinds = {'accepted': 'col', 'keep_original': 'mine', 'corrected': 'corrected', 'empty': 'empty'}
        if decision not in kinds:
            raise ValueError(f"Unknown decision '{decision}'.")
        kind = kinds[decision]
        if kind == 'col' and usage_id is not None:
            suggested = (row.match or {}).get('usage') or {}
            if str(suggested.get('id')) != str(usage_id):
                kind = 'alternative'
        review = taxon_review.state([row])
        name_review.set_decisions(review, {str(row.id): {'decision': kind, 'usage_id': usage_id,
            'scientificName': scientific_name, 'confirm_coarser': confirm_coarser}})
    taxon_review.save_state([row], review, user)
    return row


def decided_kind(row):
    """The `written_fields` kind of a row's decision; None while it is pending."""
    from api.models import TaxonNameMatch

    Decision = TaxonNameMatch.Decision
    if row.decision == Decision.KEEP_ORIGINAL:
        return (row.decided_usage or {}).get("kind") or "keep"
    return {Decision.ACCEPTED: "col", Decision.CORRECTED: "corrected", Decision.EMPTY: "empty"}.get(row.decision)


def row_fields(row):
    from api import name_review, taxon_review
    decision = taxon_review.effective_snapshot(row)
    return name_review.decision_fields(taxon_review.record(row), decision, _schema_fields(row.source_table)) if decision else {}


def _schema_fields(source_table):
    """The columns a DwC-DP resource may hold; None for a working table, which may hold any Darwin Core term."""
    from api.dwc_dp_specs import get_table_spec

    try:
        return set(get_table_spec(source_table).fields)
    except KeyError:
        return None


def decision_preview(row):
    from api import name_review, taxon_review
    return name_review.decision_preview(taxon_review.record(row), taxon_review.effective_snapshot(row), _schema_fields(row.source_table))


def bulk_acceptable(row):
    """The legacy exact-match endpoint accepts only the shared policy's safe COL defaults."""
    from api import name_review, taxon_review
    from api.models import TaxonNameMatch

    if row.decision != TaxonNameMatch.Decision.PENDING or not row.record_count or not row.matched_at:
        return False
    record = taxon_review.record(row)
    classification = name_review.classify(record)
    return classification['kind'] == 'auto' and name_review.row_default(record, classification) == 'col'


def accept_exact_matches(dataset, user=None, source_table=None, context_column=None):
    from api.models import TaxonNameMatch

    rows = TaxonNameMatch.objects.filter(dataset=dataset, decision=TaxonNameMatch.Decision.PENDING)
    if source_table is not None:
        rows = rows.filter(source_table=source_table, context_column=context_column or "")
    return [decide(row, TaxonNameMatch.Decision.ACCEPTED, user=user) for row in rows if bulk_acceptable(row)]


def _is_blank(value):
    return _clean_label(value) is None


def _same_cell(current, new):
    """Blank cells (None, NaN, pd.NA, "") are the same; a blank is never compared with ==, which pd.NA cannot answer."""
    if _is_blank(current) or _is_blank(new):
        return _is_blank(current) and _is_blank(new)
    return current == new


def _review_row_keys(df, spec):
    if spec and 'taxonID' in spec.fields and len(spec.primary_key) == 1 and spec.primary_key[0] in df:
        return df[spec.primary_key[0]].astype('string').fillna('').str.strip()
    return None


def review_source_frame(table):
    """Exclude IDs previously filled by the review from evidence for a new match."""
    from api.dwc_dp_specs import get_table_spec, normalize_resource_name

    df = table.df
    if not table.reviewed_taxon_ids or 'taxonID' not in df:
        return df
    try:
        spec = get_table_spec(normalize_resource_name(table.title))
    except KeyError:
        return df
    row_ids = _review_row_keys(df, spec)
    if row_ids is None:
        return df
    result = df.copy()
    result['taxonID'] = [
        None if key and _same_cell(value, table.reviewed_taxon_ids.get(key)) else value
        for key, value in zip(row_ids, df['taxonID'])
    ]
    return result


@transaction.atomic
def apply_decisions(dataset, table, verbatim_column, context_column=None):
    """Write reviewed names onto a table's rows; unreviewed names are left unchanged.

    Name, authorship and rank follow `written_fields`, the rules the conversion's review applies too. An accepted
    COL name also fills blank higher-classification cells and retains uncertainty in taxonFormula or
    identificationQualifier where supported by the schema. Reviewed taxon links are tracked by row key so
    later decisions can replace them while preserving supplied IDs. verbatim_column is only read.
    """
    from django.utils import timezone
    from api import name_review, taxon_review
    from api.dwc_dp_specs import get_table_spec, normalize_resource_name
    from api.models import Dataset, Table, TaxonNameMatch

    Dataset.objects.select_for_update().only('id').get(pk=dataset.pk)
    table.refresh_from_db(from_queryset=Table.objects.select_for_update())

    source_table = normalize_resource_name(table.title)
    context_column = context_column or ""
    _check_single_context_configuration(dataset, source_table, context_column)
    df = table.df.copy()
    keys = _row_keys(df, verbatim_column, context_column)
    try:
        spec = get_table_spec(source_table)
        schema_fields = set(spec.fields)
    except KeyError:
        spec = None
        schema_fields = None  # a working table: any Darwin Core term may be written

    row_keys = _review_row_keys(df, spec)
    row_ids = None
    generated_ids = dict(table.reviewed_taxon_ids)
    if row_keys is not None:
        row_ids = row_keys.mask(row_keys.duplicated(keep=False), '').tolist()
        current_keys = set(row_keys)
        generated_ids = {key: value for key, value in generated_ids.items() if key in current_keys}
    taxon_ids = df['taxonID'].astype(object).tolist() if 'taxonID' in df else [None] * len(df)
    taxon_ids_changed = False

    def writable(column):
        return column in df.columns or schema_fields is None or column in schema_fields

    positions = {}
    for position, key in enumerate(keys):
        if key[0]:
            positions.setdefault(key, []).append(position)

    written = {"rows_updated": 0, "labels_applied": 0, "pending_labels": 0, "pending_rows": 0,
               "kept_labels": 0, "unmatched_labels": 0, "skipped_columns": set(), "qualifiers_not_written": 0,
               "taxon_ids_written": 0, "taxon_ids_cleared": 0}
    rows = {
        (row.verbatim_label, row.context_key): row
        for row in _scope(dataset, source_table, context_column).filter(record_count__gt=0)
    }
    applied = []
    applied_id_keys = set()
    for key, row_positions in positions.items():
        row = rows.get(key)
        if row is None:
            written["unmatched_labels"] += 1
            continue
        if row.decision == TaxonNameMatch.Decision.PENDING:
            written["pending_labels"] += 1
            written["pending_rows"] += len(row_positions)
            continue
        written["kept_labels" if row.decision == TaxonNameMatch.Decision.KEEP_ORIGINAL else "labels_applied"] += 1
        source, decision = taxon_review.record(row), taxon_review.effective_snapshot(row)
        outcomes = name_review.decision_fields(source, decision, schema_fields)
        outcomes.pop('taxonID', None)
        if schema_fields is not None:
            excluded = set(name_review.decision_fields(source, decision)) - set(outcomes)
            if 'taxonFormula' in outcomes:
                excluded.discard('identificationQualifier')
            written['skipped_columns'].update(excluded)
            if 'identificationQualifier' in excluded:
                written['qualifiers_not_written'] += 1
        changed = set()
        for column, outcome in outcomes.items():
            how, value = outcome
            if how == "yours" or (how == "yours_or" and not value) or (how == "set" and not value and column not in df.columns):
                continue  # nothing to write
            if not writable(column):
                written["skipped_columns"].add(column)
                continue
            if column not in df.columns:
                df[column] = None
            if df[column].dtype != object:
                df[column] = df[column].astype(object)
            column_index = df.columns.get_loc(column)
            for position in row_positions:
                current = df.iat[position, column_index]
                actual = outcome
                if column in {'taxonFormula', 'identificationQualifier'}:
                    qualifier = df.iloc[position].get('identificationQualifier')
                    actual = name_review.decision_fields(source, decision, schema_fields, qualifier).get(column, ('yours', None))
                new = row_value(actual, current, str(df.iloc[position][verbatim_column]))
                if _is_blank(new):
                    new = None
                if not _same_cell(current, new):
                    df.iat[position, column_index] = new
                    changed.add(position)
        if row_ids is not None:
            for position in row_positions:
                row_id = row_ids[position]
                if not row_id:
                    continue
                applied_id_keys.add(row_id)
                current = taxon_ids[position]
                previous = generated_ids.pop(row_id, None)
                if not _is_blank(current) and not _same_cell(current, previous):
                    continue
                record = df.iloc[position]
                qualifier = record.get('identificationQualifier')
                if name_review.normal(record.get('taxonFormula')).casefold() not in {'', 'a'}:
                    qualifier = record['taxonFormula']
                new = (name_review.reviewed_taxon_id(source, decision, qualifier)
                       if _same_cell(record.get('scientificName'), decision.get('scientificName')) else None)
                if new:
                    generated_ids[row_id] = new
                    written["taxon_ids_written"] += 1
                if not _same_cell(current, new):
                    taxon_ids[position] = new
                    taxon_ids_changed = True
                    changed.add(position)
        written["rows_updated"] += len(changed)
        applied.append(row.id)

    # A reopened review can finish with some labels pending, or without a match
    # for a row that used to have one. Remove only links still owned by the review.
    for position, row_id in enumerate(row_ids or ()):
        if not row_id or row_id in applied_id_keys:
            continue
        previous = generated_ids.pop(row_id, None)
        if previous and _same_cell(taxon_ids[position], previous):
            taxon_ids[position] = None
            taxon_ids_changed = True
            written["taxon_ids_cleared"] += 1

    if applied or taxon_ids_changed:
        if taxon_ids_changed:
            df['taxonID'] = taxon_ids
        table.df = df
        table.reviewed_taxon_ids = generated_ids
        table.save(update_fields=['df', 'reviewed_taxon_ids'])
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
            if row.decision != TaxonNameMatch.Decision.PENDING
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
        f"of table {table.id} (`{table.title}`). {result['kept_labels']:,} labels keep their original name, "
        "split into name and authorship where the GBIF name parser could do so exactly.",
    ]
    if result.get("taxon_ids_written"):
        lines.append(f"{result['taxon_ids_written']:,} rows have reviewed Catalogue of Life links in taxonID.")
    if result.get("taxon_ids_cleared"):
        lines.append(f"Removed {result['taxon_ids_cleared']:,} earlier review links without a current decision.")
    if result["pending_labels"]:
        lines.append(
            f"{result['pending_labels']:,} labels ({result['pending_rows']:,} rows) are still pending "
            "review and kept their current names."
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
            (Decision.CORRECTED, "corrected"),
            (Decision.KEEP_ORIGINAL, "kept as the original"),
            (Decision.EMPTY, "left without a published name"),
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
    """(agent, scope) when the dataset's current agent has a taxon review open and is waiting for the user.

    A review opens when the agent pauses with RequestTaxonReview and stays open through chat replies until the user
    presses Done or Skip. While the agent is answering a reply, the review can be read but not changed or closed.
    """
    from api.models import Agent, Message

    agents = Agent.objects.filter(dataset=dataset, completed_at__isnull=True).order_by(
        'task__order', 'task_id', 'created_at', 'id',
    )
    if lock:
        agents = agents.select_for_update(of=('self',))  # not the joined Task row other datasets share
    agent = agents.first()
    scope = agent.open_taxon_review if agent else None
    if not scope or not isinstance(scope, dict) or agent.busy_thinking:
        return None
    last_message = agent.message_set.last()
    if not last_message or last_message.role != Message.Role.ASSISTANT or (last_message.openai_obj or {}).get("tool_calls"):
        return None
    return agent, {"source_table": scope.get("source_table") or "", "context_column": scope.get("context_column") or ""}


def undecided_names(agent):
    """(undecided, total) names in the taxon review `agent` has open."""
    from api.models import TaxonNameMatch

    scope = agent.open_taxon_review or {}
    rows = TaxonNameMatch.objects.filter(
        dataset=agent.dataset_id, record_count__gt=0,
        source_table=scope.get("source_table") or "", context_column=scope.get("context_column") or "",
    )
    return rows.filter(decision=TaxonNameMatch.Decision.PENDING).count(), rows.count()


def open_review_error(agent, action):
    """The refusal for an agent `action` (e.g. "publish") while its taxon review is open; None otherwise."""
    from api.schema_ledger import OPEN_TAXON_REVIEW_REFUSAL

    if agent.completed_at or not agent.open_taxon_review:
        return None
    undecided, total = undecided_names(agent)
    return (
        f"{OPEN_TAXON_REVIEW_REFUSAL} ({undecided} of {total} names undecided), so you cannot {action} yet. Only "
        "the user closes it, with Done (ChatIPT applies their decisions) or Skip (the names stay as they are) in the "
        "review. End your turn so they can finish it, and answer anything they ask in the chat meanwhile."
    )


def compact_usage(usage):
    if not usage:
        return None
    compact = {key: usage.get(key) for key in ('id', 'scientificName', 'scientificNameAuthorship', 'taxonRank', 'status')}
    # Enough classification to tell same-name homonyms apart ("Calanus" the copepod or the grasshopper).
    context = {rank: (usage.get('classification') or {}).get(rank) for rank in CLASSIFICATION_RANKS}
    if any(context.values()):
        compact['classification'] = {rank: value for rank, value in context.items() if value}
    return compact


def compact_match(summary):
    return {'matchType': summary.get('matchType'), 'status': summary.get('status'), 'confidence': summary.get('confidence'),
            'usage': compact_usage(summary.get('usage')), 'acceptedUsage': compact_usage(summary.get('acceptedUsage')),
            'alternatives': [{**compact_usage(alternative), 'matchType': alternative.get('matchType')}
                             for alternative in summary.get('alternatives') or []],
            # More exact usages than were kept: their homonyms cannot all be checked.
            'exactAlternativesDropped': bool(summary.get('exactAlternativesDropped')),
            'hintOnly': bool(summary.get('hintOnly')), 'issues': list(summary.get('issues') or []),
            'matchedId': summary.get('matchedId'),
            **{key: summary[key] for key in ('idCheck', 'disambiguatedBy', 'nameMatch') if key in summary}}


def _exact_same_name(summary, stem):
    usage = summary.get('usage') or {}
    return (summary.get('matchType') == 'EXACT' and bool(usage) and not summary.get('hintOnly')
            and name_parts(usage.get('scientificName')) == name_parts(stem))


def check_name_chunk(items, deadline, matcher=None, parser=None):
    """Run matching, ID disambiguation and parsing for one names-job chunk without database access."""
    matcher, parser = matcher or match_col, parser or parse_names
    results = defaultdict(dict)
    error = None
    match_items = [item for item in items if not item.get('match')]
    step1 = {}
    try:
        summaries = matcher([item['query'] for item in match_items], deadline=deadline, verbose_exact=True) if match_items else []
        step1 = {item['label']: compact_match(summary) for item, summary in zip(match_items, summaries)}
        id_items = [item for item in match_items if item.get('ids')
                    and not _exact_same_name(step1[item['label']], split_qualifier(item.get('parse_label', item['label']))[0])]
        step2_queries = [{**item['query'], **item['ids']} for item in id_items]
        step2 = matcher(step2_queries, deadline=deadline, verbose_exact=True) if step2_queries else []
        for item, summary in zip(id_items, step2):
            first = step1[item['label']]
            second = compact_match(summary)
            usage = second.get('usage')
            stem = split_qualifier(item.get('parse_label', item['label']))[0]
            same_name = bool(usage and name_parts(usage.get('scientificName')) == name_parts(stem))
            safe_promotion = (summary.get('matchType') == 'EXACT' and usage and same_name
                              and name_change(stem, usage, 'EXACT') is None
                              and not {'TAXON_ID_NOT_FOUND', 'SCIENTIFIC_NAME_AND_ID_INCONSISTENT'}.intersection(summary.get('issues') or []))
            if safe_promotion:
                promoted = dict(second)
                promoted['disambiguatedBy'] = sorted(item['ids'])
                promoted['nameMatch'] = {key: first.get(key) for key in ('matchType', 'status', 'usage')}
                step1[item['label']] = promoted
            else:
                issues = list(summary.get('issues') or [])
                outcome = ('not_found' if 'TAXON_ID_NOT_FOUND' in issues else
                           'elsewhere' if ('SCIENTIFIC_NAME_AND_ID_INCONSISTENT' in issues
                                           or (summary.get('matchType') == 'EXACT' and usage and not same_name)) else 'no_help')
                first['idCheck'] = {'fields': item['ids'], 'outcome': outcome, 'matchType': summary.get('matchType'),
                                    'usage': compact_usage(usage), 'issues': issues, 'matchedId': second.get('matchedId')}
        for label, summary in step1.items():
            results[label]['match'] = summary
    except TaxonServiceError as exc:
        # The first and ID matches are a unit: the next run retries them together.
        error = exc
    need_parse = [item for item in items if not item.get('parse')]
    plain = [item for item in need_parse if not item.get('qualifier')]
    for item in need_parse:
        if item.get('qualifier'):
            results[item['label']]['parsed'] = {'type': None, 'usable': False, 'canonical': None, 'authorship': None,
                                                'rank': None, 'lossless': False, 'splits': False, 'reason': 'qualifier'}
    try:
        for item, parsed in zip(plain, parser([item.get('parse_label', item['label']) for item in plain], deadline=deadline)):
            results[item['label']]['parsed'] = parsed
    except TaxonServiceError as exc:
        error = error or exc
    return dict(results), error
