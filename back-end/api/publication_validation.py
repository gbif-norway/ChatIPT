import calendar
import datetime
import math
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

import pandas as pd


_COORDINATE_PAIR_RE = re.compile(
    r"(?<![\d.])"
    r"[+-]?(?:90(?:\.0+)?|(?:[0-8]?\d)(?:\.\d{3,}))"
    r"\s*[,;/]\s*"
    r"[+-]?(?:180(?:\.0+)?|(?:1[0-7]\d|(?:\d?\d))(?:\.\d{3,}))"
    r"(?![\d.])"
)
_PLACEHOLDER_URL_RE = re.compile(
    r"^(?:url[_ -]?\d+|placeholder(?:[_ -]?\d+)?|example(?:[_ -]?\d+)?)$",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"^(\d{4})$")
_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_FULL_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_ALPHA_2_COUNTRY_CODES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
    BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
    CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
    GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
    IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
    LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
    MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
    PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
    ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
    UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
    """.split()
) | {"XZ", "ZZ"}
_INTERACTION_RELATIONSHIPS = {
    "eats",
    "eaten by",
    "feeds on",
    "host of",
    "hosted by",
    "parasite of",
    "parasitoid of",
    "pollinates",
    "pollinated by",
    "preys on",
    "preyed upon by",
}


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _row_positions(mask: pd.Series) -> list[int]:
    return [
        position
        for position, matched in enumerate(mask.fillna(False).tolist(), start=1)
        if bool(matched)
    ]


def _append_bdq_warning(
    warnings: list[str],
    test_id: str,
    resource_name: str,
    row_positions: list[int],
    message: str,
) -> None:
    if not row_positions:
        return
    preview = ", ".join(map(str, row_positions[:10]))
    suffix = f" and {len(row_positions) - 10} more" if len(row_positions) > 10 else ""
    warnings.append(
        f"BDQ-inspired {test_id}: resource '{resource_name}' {message} "
        f"in row(s) {preview}{suffix}."
    )


def _as_integer(value: Any) -> int | None:
    if _is_blank(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _parse_date_token(
    value: Any,
) -> tuple[datetime.date, datetime.date, str] | None:
    if _is_blank(value):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime.datetime):
        parsed = value.date()
        return parsed, parsed, "day"
    if isinstance(value, datetime.date):
        return value, value, "day"
    if not isinstance(value, str):
        return None

    text = value.strip()
    full_date = _FULL_DATE_RE.fullmatch(text)
    if full_date:
        try:
            parsed = datetime.date(*map(int, full_date.groups()))
        except ValueError:
            return None
        return parsed, parsed, "day"

    year_month = _YEAR_MONTH_RE.fullmatch(text)
    if year_month:
        year, month = map(int, year_month.groups())
        try:
            start = datetime.date(year, month, 1)
            end = datetime.date(year, month, calendar.monthrange(year, month)[1])
        except ValueError:
            return None
        return start, end, "month"

    year_match = _YEAR_RE.fullmatch(text)
    if year_match:
        year = int(year_match.group(1))
        try:
            return datetime.date(year, 1, 1), datetime.date(year, 12, 31), "year"
        except ValueError:
            return None

    try:
        parsed_datetime = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = parsed_datetime.date()
    return parsed, parsed, "day"


def _parse_dwc_date_interval(
    value: Any,
) -> tuple[datetime.date, datetime.date, str, str] | None:
    if isinstance(value, str) and value.strip().count("/") == 1:
        start_text, end_text = value.strip().split("/", 1)
        start = _parse_date_token(start_text)
        end = _parse_date_token(end_text)
        if start is None or end is None or start[0] > end[1]:
            return None
        return start[0], end[1], start[2], end[2]

    parsed = _parse_date_token(value)
    if parsed is None:
        return None
    return parsed[0], parsed[1], parsed[2], parsed[2]


def _calendar_day_is_invalid(row: Mapping[str, Any]) -> bool:
    day = _as_integer(row.get("day"))
    if day is None or day < 1 or day > 31:
        return False
    if day <= 28:
        return False

    month = _as_integer(row.get("month"))
    if month is None or not 1 <= month <= 12:
        return False
    if month == 2 and day == 29:
        year = _as_integer(row.get("year"))
        return year is not None and day > calendar.monthrange(year, month)[1]

    maximum_day = 29 if month == 2 else calendar.monthrange(2000, month)[1]
    return day > maximum_day


def _event_date_inconsistent_fields(row: Mapping[str, Any]) -> list[str]:
    interval = _parse_dwc_date_interval(row.get("eventDate"))
    if interval is None:
        return []
    start, end, start_precision, end_precision = interval
    inconsistent: list[str] = []

    year = _as_integer(row.get("year"))
    if year is not None and (start.year != end.year or start.year != year):
        inconsistent.append("year")

    month = _as_integer(row.get("month"))
    if month is not None and (
        start_precision == "year"
        or end_precision == "year"
        or (start.year, start.month) != (end.year, end.month)
        or start.month != month
    ):
        inconsistent.append("month")

    day = _as_integer(row.get("day"))
    if day is not None and (
        start_precision != "day"
        or end_precision != "day"
        or start != end
        or start.day != day
    ):
        inconsistent.append("day")

    start_day = _as_integer(row.get("startDayOfYear"))
    if start_day is not None and (
        start_precision != "day" or start.timetuple().tm_yday != start_day
    ):
        inconsistent.append("startDayOfYear")

    end_day = _as_integer(row.get("endDayOfYear"))
    if end_day is not None and (
        end_precision != "day" or end.timetuple().tm_yday != end_day
    ):
        inconsistent.append("endDayOfYear")

    return inconsistent


def bdq_inspired_dwc_warnings(resources: Mapping[str, pd.DataFrame]) -> list[str]:
    """Return fast, deterministic cross-field checks inspired by selected BDQ tests."""
    warnings: list[str] = []

    for resource_name, df in resources.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        column_counts = Counter(df.columns)
        available_columns = {
            column for column, count in column_counts.items() if count == 1
        }

        if {"decimalLatitude", "decimalLongitude"} <= available_columns:
            latitude = pd.to_numeric(df["decimalLatitude"], errors="coerce")
            longitude = pd.to_numeric(df["decimalLongitude"], errors="coerce")
            zero_coordinates = latitude.eq(0) & longitude.eq(0)
            _append_bdq_warning(
                warnings,
                "VALIDATION_COORDINATES_NOTZERO",
                resource_name,
                _row_positions(zero_coordinates),
                "has decimalLatitude and decimalLongitude both equal to zero",
            )

            if "geodeticDatum" not in available_columns:
                missing_datum = latitude.notna() & longitude.notna()
            else:
                missing_datum = (
                    latitude.notna()
                    & longitude.notna()
                    & df["geodeticDatum"].map(_is_blank)
                )
            _append_bdq_warning(
                warnings,
                "VALIDATION_GEODETICDATUM_NOTEMPTY",
                resource_name,
                _row_positions(missing_datum),
                "has decimal coordinates without a geodeticDatum",
            )

        for minimum_field, maximum_field, test_id, label in (
            (
                "minimumDepthInMeters",
                "maximumDepthInMeters",
                "VALIDATION_MINDEPTH_LESSTHAN_MAXDEPTH",
                "has minimumDepthInMeters greater than maximumDepthInMeters",
            ),
            (
                "minimumElevationInMeters",
                "maximumElevationInMeters",
                "VALIDATION_MINELEVATION_LESSTHAN_MAXELEVATION",
                "has minimumElevationInMeters greater than maximumElevationInMeters",
            ),
        ):
            if {minimum_field, maximum_field} <= available_columns:
                minimum = pd.to_numeric(df[minimum_field], errors="coerce")
                maximum = pd.to_numeric(df[maximum_field], errors="coerce")
                _append_bdq_warning(
                    warnings,
                    test_id,
                    resource_name,
                    _row_positions(minimum.notna() & maximum.notna() & minimum.gt(maximum)),
                    label,
                )

        if "countryCode" in available_columns:
            invalid_country_code = df["countryCode"].map(
                lambda value: (
                    not _is_blank(value)
                    and (
                        not isinstance(value, str)
                        or value not in _ISO_ALPHA_2_COUNTRY_CODES
                    )
                )
            )
            _append_bdq_warning(
                warnings,
                "VALIDATION_COUNTRYCODE_STANDARD",
                resource_name,
                _row_positions(invalid_country_code),
                "has a countryCode that is not an exact ISO 3166-1 alpha-2 code, XZ, or ZZ",
            )

        if "occurrenceStatus" in available_columns:
            invalid_occurrence_status = df["occurrenceStatus"].map(
                lambda value: (
                    not _is_blank(value)
                    and (
                        not isinstance(value, str)
                        or value not in {"present", "absent"}
                    )
                )
            )
            _append_bdq_warning(
                warnings,
                "VALIDATION_OCCURRENCESTATUS_STANDARD",
                resource_name,
                _row_positions(invalid_occurrence_status),
                "has occurrenceStatus outside the exact lowercase values 'present' and 'absent'",
            )

        component_fields = (
            "year",
            "month",
            "day",
            "startDayOfYear",
            "endDayOfYear",
        )
        date_fields = [
            field
            for field in ("eventDate", *component_fields)
            if field in available_columns
        ]
        check_calendar_day = "day" in available_columns
        check_event_consistency = (
            "eventDate" in available_columns
            and bool(set(component_fields).intersection(available_columns))
        )
        if check_calendar_day or check_event_consistency:
            normalized_dates = df[date_fields].astype("string").fillna("")
            invalid_calendar_combinations: set[tuple[str, ...]] = set()
            inconsistent_combinations: set[tuple[str, ...]] = set()
            inconsistent_names: set[str] = set()
            for values in normalized_dates.drop_duplicates().itertuples(
                index=False,
                name=None,
            ):
                row = dict(zip(date_fields, values))
                if check_calendar_day and _calendar_day_is_invalid(row):
                    invalid_calendar_combinations.add(values)
                if check_event_consistency:
                    fields = _event_date_inconsistent_fields(row)
                    if fields:
                        inconsistent_combinations.add(values)
                        inconsistent_names.update(fields)

            invalid_calendar_days: list[int] = []
            inconsistent_rows: list[int] = []
            for position, values in enumerate(
                normalized_dates.itertuples(index=False, name=None),
                start=1,
            ):
                if values in invalid_calendar_combinations:
                    invalid_calendar_days.append(position)
                if values in inconsistent_combinations:
                    inconsistent_rows.append(position)

        if check_calendar_day:
            _append_bdq_warning(
                warnings,
                "VALIDATION_DAY_INRANGE",
                resource_name,
                invalid_calendar_days,
                "has a day that is impossible for the supplied month and year",
            )

        if check_event_consistency:
            field_summary = ", ".join(sorted(inconsistent_names))
            _append_bdq_warning(
                warnings,
                "VALIDATION_EVENT_CONSISTENT",
                resource_name,
                inconsistent_rows,
                f"has eventDate inconsistent with supplied component field(s): {field_summary}",
            )

    return warnings


def utf8_serialization_errors(resources: Mapping[str, pd.DataFrame]) -> list[str]:
    """Return cell-level errors that would make a strict UTF-8 export unsafe."""
    errors: list[str] = []
    for resource_name, df in resources.items():
        for column_index, column in enumerate(df.columns):
            if isinstance(column, str):
                try:
                    column.encode("utf-8", "strict")
                except UnicodeEncodeError as exc:
                    errors.append(
                        f"Resource '{resource_name}' column {column_index} cannot be encoded as UTF-8: {exc}."
                    )
                if "\x00" in column:
                    errors.append(
                        f"Resource '{resource_name}' column {column_index} contains a NUL character."
                    )

        for column_index, column in enumerate(df.columns):
            series = df.iloc[:, column_index]
            for row_position, value in enumerate(series, start=1):
                if not isinstance(value, str):
                    continue
                try:
                    value.encode("utf-8", "strict")
                except UnicodeEncodeError as exc:
                    errors.append(
                        f"Resource '{resource_name}', row {row_position}, field '{column}' "
                        f"cannot be encoded as UTF-8: {exc}."
                    )
                if "\x00" in value:
                    errors.append(
                        f"Resource '{resource_name}', row {row_position}, field '{column}' "
                        "contains a NUL character."
                    )
                if len(errors) >= 100:
                    errors.append("UTF-8 validation stopped after 100 errors.")
                    return errors
    return errors


def validate_publication_safety(resources: Mapping[str, pd.DataFrame]) -> list[str]:
    """Return focused, warning-only checks for accidental publication mistakes."""
    warnings: list[str] = []
    for resource_name, df in resources.items():
        for field_name in ("dataGeneralizations", "informationWithheld"):
            if field_name not in df.columns:
                continue
            matches = []
            for row_position, value in enumerate(df[field_name], start=1):
                if isinstance(value, str) and _COORDINATE_PAIR_RE.search(value):
                    matches.append(row_position)
            if matches:
                preview = ", ".join(map(str, matches[:10]))
                suffix = f" and {len(matches) - 10} more" if len(matches) > 10 else ""
                warnings.append(
                    f"Publication safety: resource '{resource_name}' field '{field_name}' contains "
                    f"precise coordinate pairs in row(s) {preview}{suffix}. Check that generalized or "
                    "withheld coordinates are not being disclosed in free text."
                )

        for column in df.columns:
            column_text = str(column)
            if not column_text.lower().endswith(("url", "uri")):
                continue
            placeholder_rows = [
                row_position
                for row_position, value in enumerate(df[column], start=1)
                if isinstance(value, str) and _PLACEHOLDER_URL_RE.fullmatch(value.strip())
            ]
            if placeholder_rows:
                preview = ", ".join(map(str, placeholder_rows[:10]))
                suffix = f" and {len(placeholder_rows) - 10} more" if len(placeholder_rows) > 10 else ""
                warnings.append(
                    f"Publication safety: resource '{resource_name}' field '{column_text}' contains "
                    f"placeholder URL values in row(s) {preview}{suffix}."
                )
    return warnings


def semantic_dwc_dp_warnings(resources: Mapping[str, pd.DataFrame]) -> list[str]:
    """Return modelling observations for the agent to resolve using its judgement."""
    warnings = bdq_inspired_dwc_warnings(resources)

    for resource_name, df in resources.items():
        if resource_name.endswith("assertion") and "assertionValue" in df.columns:
            blank_count = sum(_is_blank(value) for value in df["assertionValue"])
            if blank_count:
                warnings.append(
                    f"Semantic review: resource '{resource_name}' contains {blank_count} assertion "
                    "row(s) with a blank assertionValue. Consider omitting them and documenting the "
                    "unused source field once."
                )

    relationship = resources.get("resource-relationship")
    if (
        isinstance(relationship, pd.DataFrame)
        and "relationshipType" in relationship.columns
        and "organism-interaction" not in resources
    ):
        interaction_count = sum(
            isinstance(value, str) and value.strip().casefold() in _INTERACTION_RELATIONSHIPS
            for value in relationship["relationshipType"]
        )
        if interaction_count:
            warnings.append(
                f"Semantic review: {interaction_count} biological interaction relationship(s) are "
                "represented in 'resource-relationship'. Consider the dedicated "
                "'organism-interaction' resource."
            )

    return warnings


def accounting_semantic_warnings(accounting: Any) -> list[str]:
    """Summarize explicit source omissions without preventing export."""
    if not isinstance(accounting, dict):
        return []
    sources = (accounting.get("declaration") or {}).get("sources") or []
    omitted_rows = sum(int(source.get("omitted_rows") or 0) for source in sources)
    omitted_destinations = [
        destination
        for source in sources
        for column in (source.get("columns") or [])
        for destination in (column.get("destinations") or [])
        if destination.get("kind") == "omitted"
    ]
    omitted_columns = len(omitted_destinations)
    omitted_values = sum(
        int(destination.get("source_values") or 0)
        for destination in omitted_destinations
    )
    warnings = []
    if any((omitted_values, omitted_columns, omitted_rows)):
        warnings.append(
            "Semantic review: source accounting records explicit omissions "
            f"({omitted_rows} row(s), {omitted_columns} column(s), {omitted_values} populated value(s)). "
            "Confirm that these omissions are intentional or user-requested before publication."
        )

    higher_taxonomy_fields = {
        "kingdom", "subkingdom", "phylum", "subphylum", "class", "subclass",
        "order", "suborder", "superfamily", "family", "subfamily", "tribe",
        "subtribe", "genus", "subgenus", "specificEpithet", "infraspecificEpithet",
    }
    missing_taxonomy = []
    for source in sources:
        for column in source.get("columns") or []:
            source_name = str(column.get("source_column") or "").strip()
            source_term = source_name.rsplit(".", 1)[-1].strip()
            canonical_name = next(
                (field for field in higher_taxonomy_fields if field.casefold() == source_term.casefold()),
                None,
            )
            if not canonical_name:
                continue
            populated_values = int(column.get("source_populated_values") or 0)
            if populated_values <= 0:
                continue
            preserved = any(
                destination.get("kind") == "resource"
                and destination.get("target_table") in {"identification", "identification-taxon"}
                and destination.get("target_field") == canonical_name
                and int(destination.get("source_values") or 0) > 0
                for destination in column.get("destinations") or []
            )
            if not preserved:
                missing_taxonomy.append(
                    f"{source.get('source_table_title', 'source')}.{source_name} "
                    f"({populated_values} populated value(s))"
                )
    if missing_taxonomy:
        warnings.append(
            "Higher-taxonomy preservation: populated source classification fields are not routed "
            "to identification or identification-taxon: " + "; ".join(missing_taxonomy[:10]) + ". "
            "Create a linked Identification resource and preserve these fields, or verify that the "
            "accounting route is accurate before publication."
        )
    return warnings
