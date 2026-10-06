"""Report-only checks for source values that may be placeholders or malformed."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from api.dwca_import import DWC
from api.publication_validation import _ISO_ALPHA_2_COUNTRY_CODES


_ZERO_PROFILE_TERMS = (
    'minimumElevationInMeters', 'maximumElevationInMeters',
    'minimumDepthInMeters', 'maximumDepthInMeters',
)
ASSERTION_IRI_FIELDS = ('.assertionTypeIRI', '.assertionValueIRI', '.assertionUnitIRI')
# A scheme followed by a nonempty, space-free remainder: http(s)://…, urn:…, and similar.
_ABSOLUTE_IRI = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:[^\s]+')


def semantic_target_rejection(target: str, value: str) -> str | None:
    """Reject only values whose source text contradicts a target field's meaning."""
    if not value:
        return None
    if target.endswith('.countryCode') and value.strip() not in _ISO_ALPHA_2_COUNTRY_CODES:
        return 'countryCode is not an exact ISO 3166-1 alpha-2, XZ, or ZZ code; original retained.'
    if target.endswith('.dateIdentified') and value.strip() == '0-0-0':
        return 'dateIdentified is the 0-0-0 placeholder, not a calendar date; original retained.'
    if target.endswith(ASSERTION_IRI_FIELDS) and not _ABSOLUTE_IRI.fullmatch(value.strip()):
        return f"{target.rsplit('.', 1)[1]} needs an absolute IRI; this value is not one, so the original is retained."
    return None


def _is_zero(value: str) -> bool:
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return False
    return number.is_finite() and number == 0


def is_age_like_remark(value: str) -> bool:
    return bool(re.match(r'^(?:\d+\s+)?(?:ad|subad|juv|fad|adult|subadult|juvenile)\b', value.strip(), re.I))


def audit_semantic_values(archive, *, example_limit: int = 5) -> dict:
    """Summarize suspicious source values without changing or withholding them.

    Findings are grouped by source DwC term. Counts cover all source rows;
    examples are bounded and retain table name, one-based row number, and the
    exact source value. The caller can attach this object to a conversion report.
    """
    if example_limit < 0:
        raise ValueError('example_limit must be nonnegative')

    specs = {
        DWC + 'countryCode': (
            'country_code_not_iso_alpha2',
            'Values must be exact uppercase ISO 3166-1 alpha-2 codes, XZ, or ZZ.',
            lambda value: value not in _ISO_ALPHA_2_COUNTRY_CODES,
        ),
        DWC + 'dateIdentified': (
            'placeholder_identification_date',
            'The value 0-0-0 is commonly a placeholder; verify against the source before treating it as a date.',
            lambda value: value == '0-0-0',
        ),
        DWC + 'eventRemarks': (
            'age_like_event_remarks',
            'These remarks resemble organism life stages. Review each distinct value before routing it to occurrence.lifeStage or occurrence.occurrenceRemarks.',
            is_age_like_remark,
        ),
        **{
            DWC + term: (
                'zero_elevation_or_depth',
                'Zero can be a valid elevation or depth. Review in context; this audit does not reject or change it.',
                _is_zero,
            )
            for term in _ZERO_PROFILE_TERMS
        },
    }
    findings = {}
    for table in archive.tables:
        for column, term in enumerate(table.terms):
            if term not in specs:
                continue
            finding_id, guidance, predicate = specs[term]
            finding = findings.setdefault(term, {
                'id': finding_id,
                'source_term': term,
                'count': 0,
                'examples': [],
                'guidance': guidance,
                'action': 'audit-only; source value retained unchanged (target mapping may withhold invalid values)',
            })
            for row_number, row in enumerate(table.rows, start=1):
                value = row[column]
                if not value or not predicate(value.strip()):
                    continue
                finding['count'] += 1
                if len(finding['examples']) < example_limit:
                    finding['examples'].append({
                        'source_table': table.name,
                        'source_row': row_number,
                        'value': value,
                    })

    return {
        'version': 1,
        'policy': 'The audit preserves source values. Invalid country codes can be routed as reviewed labels or withheld from mapped fields; the exact 0-0-0 identification-date placeholder is withheld. Age-like event remarks require value review; zeros in elevation and depth require contextual review.',
        'findings': [findings[term] for term in specs if term in findings and findings[term]['count']],
    }
