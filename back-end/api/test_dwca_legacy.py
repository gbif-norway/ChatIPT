from django.test import SimpleTestCase

from api.dwc_dp_specs import TABLE_SPECS
from api.dwca_import import DWC, REGISTRY, ImportFailure
from api.dwca_legacy import (BMDE, BMDE_ROW_TYPE, GROUPS, LEGACY_DERIVED_TERMS, LEGACY_FAMILIES, NBN_DATE, NBN_ROW_TYPE,
                             NXF, TIMES, UTM, emit_legacy_records, legacy_row_review, legacy_targets)


def group(n, type_="", value="", unit="", accuracy="", by="", method=""):
    names = ("MeasurementType", "MeasurementValue", "MeasurementUnit", "MeasurementAccuracy", "MeasurementDeterminedBy", "MeasurementMethod")
    return {BMDE + f"{name}{n}": text for name, text in zip(names, (type_, value, unit, accuracy, by, method))}


def nbn_date(code, start, end):
    return dict(zip(NBN_DATE, (code, start, end)))


def registered(row_type):
    return {iri for iris in REGISTRY["row_types"][row_type].values() for iri in iris}


class LegacyTargetTests(SimpleTestCase):
    def test_families_use_exact_sandbox_row_types(self):
        self.assertEqual(LEGACY_FAMILIES, {"http://www.birdscanada.org/bmde/Observation": "bmde",
                                           "http://rs.nbn.org.uk/dwc/nxf/0.1/terms/nxfOccurrence": "nbn"})
        self.assertEqual(len(registered(BMDE_ROW_TYPE)), 195)
        self.assertEqual(len(registered(NBN_ROW_TYPE)), 8)

    def test_every_target_is_registered_schema_valid_non_exact_and_reviewed(self):
        for row_type in LEGACY_FAMILIES:
            for term in registered(row_type):
                targets, reason = legacy_targets(row_type, term, ["1"])
                for target in targets:
                    table, field = target.split(".", 1)
                    spec = next(item for item in TABLE_SPECS[table].schema["fields"] if item["name"] == field)
                    self.assertNotEqual(spec.get("dcterms:isVersionOf"), term, target)
                    self.assertTrue(reason, term)
        for row_type, term in LEGACY_DERIVED_TERMS:
            self.assertIn(term, registered(row_type))

    def test_measurement_groups_are_twelve_separate_derived_groups(self):
        self.assertEqual(len(GROUPS), 12)
        self.assertEqual(legacy_targets(BMDE_ROW_TYPE, BMDE + "MeasurementType12", ["mass"])[0], ["occurrence-assertion.verbatimAssertionType"])
        self.assertIn((BMDE_ROW_TYPE, BMDE + "MeasurementMethod7"), LEGACY_DERIVED_TERMS)

    def test_no_basename_or_cross_family_matching(self):
        for row_type, term in ((BMDE_ROW_TYPE, DWC + "measurementType"), (BMDE_ROW_TYPE, "http://example.org/MeasurementType1"),
                               (BMDE_ROW_TYPE, NXF + "gridReference"), (NBN_ROW_TYPE, BMDE + "CoordinatesScope"),
                               (DWC + "Occurrence", NXF + "eventDateStart"), (NBN_ROW_TYPE, DWC + "eventDate")):
            self.assertEqual(legacy_targets(row_type, term, ["x"]), ([], None))

    def test_preserved_terms_explain_why(self):
        for row_type, term in ((NBN_ROW_TYPE, NXF + "gridReferencePrecision"), (NBN_ROW_TYPE, NXF + "sensitiveOccurrence"),
                               (BMDE_ROW_TYPE, BMDE + "MultiScientificName3"), (BMDE_ROW_TYPE, BMDE + "SpecimenDecimalLatitude"),
                               (BMDE_ROW_TYPE, BMDE + "SurveyAreaShape"), (BMDE_ROW_TYPE, DWC + "individualCount"),
                               (BMDE_ROW_TYPE, BMDE + "RecordPermissions")):
            targets, reason = legacy_targets(row_type, term, ["10000"])
            self.assertEqual(targets, [])
            self.assertTrue(reason)
        self.assertEqual(legacy_targets(BMDE_ROW_TYPE, BMDE + "DurationInHours", ["0.05"]), ([], None))

    def test_invalid_hours_and_unknown_date_codes_are_flagged_in_planning(self):
        self.assertEqual(legacy_targets(BMDE_ROW_TYPE, TIMES[0], ["6.5", "noon"])[0], [])
        self.assertEqual(legacy_targets(BMDE_ROW_TYPE, TIMES[0], ["6.5", "12."])[0], ["event.eventTime"])
        self.assertIn("Codes present here", legacy_targets(NBN_ROW_TYPE, NBN_DATE[0], ["Y", "U"])[1])
        self.assertNotIn("Codes present here", legacy_targets(NBN_ROW_TYPE, NBN_DATE[0], ["Y", "DD"])[1])


class BmdeEmitTests(SimpleTestCase):
    def test_multiple_measurement_groups_stay_separate_and_identical_groups_are_not_merged(self):
        source = {**group(1, "wing chord", "128", "mm"), **group(2, "mass", "77", "g", by="J. Doe"),
                  **group(3, "mass", "77", "g", by="J. Doe"), **group(4)}
        rows = emit_legacy_records("bmde", source, {}, "occurrence", "occ-key", "row-key")
        self.assertEqual(rows, [
            ("occurrence-assertion", {"occurrence_fk": "occ-key", "verbatimAssertionType": "wing chord", "assertionValue": "128", "assertionUnit": "mm"}),
            ("occurrence-assertion", {"occurrence_fk": "occ-key", "verbatimAssertionType": "mass", "assertionValue": "77", "assertionUnit": "g", "assertionBy": "J. Doe"}),
            ("occurrence-assertion", {"occurrence_fk": "occ-key", "verbatimAssertionType": "mass", "assertionValue": "77", "assertionUnit": "g", "assertionBy": "J. Doe"}),
        ])
        for name, row in rows:
            fields = {item["name"] for item in TABLE_SPECS[name].schema["fields"]}
            self.assertLessEqual(row.keys(), fields)

    def test_incomplete_or_partially_approved_groups_fail(self):
        for source in (group(1, "", "128", "mm"), group(1, "wing chord", ""), {BMDE + "MeasurementType1": "wing chord", BMDE + "MeasurementValue1": "128"}):
            with self.assertRaises(ImportFailure):
                emit_legacy_records("bmde", source, {}, "occurrence", "occ-key", "row-key")

    def test_measurements_require_an_occurrence_subject(self):
        for subject in ("event", "material", "protocol"):
            with self.assertRaises(ImportFailure):
                emit_legacy_records("bmde", group(1, "mass", "7"), {}, subject, "key", "row-key")
        with self.assertRaises(ImportFailure):
            emit_legacy_records("bmde", group(1, "mass", "7"), {}, "occurrence", "", "row-key")

    def test_event_context_returns_one_patch_for_the_supplied_event(self):
        source = {**dict(zip(UTM, ("17T", "630084", "4833438"))), **dict(zip(TIMES, ("6.5", "7.25"))), **group(1)}
        mapped = {"event": {"siteNumber": "Stop 3", "georeferenceRemarks": "Route (starting point)"}}
        self.assertEqual(emit_legacy_records("bmde", source, mapped, "event", "event-key", "row-key"), [
            ("event", {"event_pk": "event-key", "siteNumber": "Stop 3", "georeferenceRemarks": "Route (starting point)",
                       "verbatimCoordinates": "17T 630084 4833438", "verbatimCoordinateSystem": "UTM", "eventTime": "06:30/07:15"})])
        self.assertEqual(emit_legacy_records("bmde", dict(zip(TIMES, ("13.", ""))), {}, "event", "event-key", "row-key"),
                         [("event", {"event_pk": "event-key", "eventTime": "13:00"})])
        self.assertEqual(emit_legacy_records("bmde", {}, {"event": {"siteNumber": ""}}, "event", "event-key", "row-key"), [])

    def test_invalid_event_values_are_rejected(self):
        for source in (dict(zip(TIMES, ("8", "7.5"))), dict(zip(TIMES, ("", "7.5"))), dict(zip(TIMES, ("24", ""))),
                       dict(zip(TIMES, ("noon", ""))), dict(zip(UTM, ("17T", "630084", ""))), {UTM[0]: "17T"}):
            with self.assertRaises(ImportFailure):
                emit_legacy_records("bmde", source, {}, "event", "event-key", "row-key")

    def test_subminute_time_is_never_rounded(self):
        self.assertEqual(legacy_targets(BMDE_ROW_TYPE, TIMES[0], ['6.123'])[0], [])
        with self.assertRaisesMessage(ImportFailure, 'rather than round'):
            emit_legacy_records('bmde', dict(zip(TIMES, ('6.123', ''))), {}, 'event', 'event-key', 'row-key')

    def test_event_values_are_not_attached_to_occurrence_subjects(self):
        for source, mapped in ((dict(zip(TIMES, ("6.5", ""))), {}), ({}, {"event": {"siteNumber": "Stop 3"}})):
            with self.assertRaises(ImportFailure):
                emit_legacy_records("bmde", source, mapped, "occurrence", "occ-key", "row-key")

    def test_unapproved_terms_and_targets_fail(self):
        for source, mapped in (({DWC + "individualCount": "3"}, {}), ({BMDE + "RecordPermissions": "5"}, {}),
                               ({}, {"occurrence": {"organismQuantity": "3"}}), ({}, {"event": {"eventDate": "2020"}}),
                               ({}, {"event": {"verbatimCoordinates": "SD4261"}})):
            with self.assertRaises(ImportFailure):
                emit_legacy_records("bmde", source, mapped, "event", "event-key", "row-key")
        with self.assertRaises(ImportFailure):
            emit_legacy_records("eol-media", {}, {}, "event", "event-key", "row-key")

    def test_row_review_flags_restrictions_deletes_absence_and_presence_counts(self):
        self.assertIsNone(legacy_row_review(BMDE_ROW_TYPE, {BMDE + "RecordPermissions": "5", DWC + "individualCount": "3",
                                                            BMDE + "ObservationDescriptor": "TotalCount", **group(1, "mass", "7")}))
        for source, text in (({BMDE + "RecordPermissions": "3"}, "RecordPermissions"), ({BMDE + "LastModifiedAction": "DELETE"}, "DELETE"),
                             ({BMDE + "NoObservations": "NoObs"}, "absence"), ({BMDE + "RecordReviewStatus": "pending review"}, "pending"),
                             ({DWC + "individualCount": "1", BMDE + "ObservationDescriptor": "Presence/Absence"}, "presence"),
                             (group(2, "", "7"), "group 2")):
            self.assertIn(text, legacy_row_review(BMDE_ROW_TYPE, source))


class NbnEmitTests(SimpleTestCase):
    def emit(self, source, mapped=None):
        return emit_legacy_records("nbn", source, mapped or {}, "event", "event-key", "row-key")

    def test_valid_vague_dates_patch_the_supplied_event(self):
        for args, expected in ((("D", "2015-06-03", "2015-06-03"), "2015-06-03"), (("DD", "2015-06-03", "2015-06-09"), "2015-06-03/2015-06-09"),
                               (("O", "2016-02-01", "2016-02-29"), "2016-02-01/2016-02-29"), (("OO", "2016-02-01", "2016-04-30"), "2016-02-01/2016-04-30"),
                               (("Y", "1950-01-01", "1950-12-31"), "1950-01-01/1950-12-31"), (("YY", "1950-01-01", "1952-12-31"), "1950-01-01/1952-12-31")):
            self.assertEqual(self.emit(nbn_date(*args)), [("event", {"event_pk": "event-key", "eventDate": expected})])

    def test_unknown_codes_are_withheld_not_extrapolated(self):
        for args in (("U", "", ""), ("-Y", "", "1950-12-31"), ("C", "1901-01-01", "2000-12-31"), ("y", "1950-01-01", "1950-12-31")):
            self.assertEqual(self.emit(nbn_date(*args)), [])
            self.assertIn("not converted", legacy_row_review(NBN_ROW_TYPE, {**nbn_date(*args), NXF + "sensitiveOccurrence": "false"}))

    def test_invalid_or_incompatible_endpoints_are_rejected(self):
        for args in (("D", "2015-06-03", "2015-06-04"), ("DD", "2015-06-09", "2015-06-03"), ("Y", "1950-01-01", "1950-12-30"),
                     ("O", "2015-02-01", "2015-02-27"), ("D", "1950-02-30", "1950-02-30"), ("D", "03/06/2015", "03/06/2015"),
                     ("YY", "1950-06-01", "1952-12-31"), ("DD", "2015-06-03", ""), ("", "2015-06-03", "2015-06-03")):
            with self.assertRaises(ImportFailure):
                self.emit(nbn_date(*args))
            self.assertTrue(legacy_row_review(NBN_ROW_TYPE, {**nbn_date(*args), NXF + "sensitiveOccurrence": "false"}))
        with self.assertRaises(ImportFailure):
            self.emit({NBN_DATE[0]: "D", NBN_DATE[1]: "2015-06-03"})

    def test_grid_reference_is_verbatim_and_precision_never_becomes_uncertainty(self):
        mapped = {"event": {"verbatimCoordinates": "SD4261", "verbatimCoordinateSystem": "BNG", "locationID": "GA0003391006892"}}
        self.assertEqual(self.emit({}, mapped), [("event", {"event_pk": "event-key", **mapped["event"]})])
        self.assertEqual(legacy_targets(NBN_ROW_TYPE, NXF + "gridReferencePrecision", ["10000"])[0], [])
        for bad in ({"event": {"verbatimCoordinateSystem": "BNG"}}, {"event": {"coordinateUncertaintyInMeters": "10000"}},
                    {"event": {"dataGeneralizations": "10 km"}}, {"occurrence": {"informationWithheld": "x"}}):
            with self.assertRaises(ImportFailure):
                self.emit({}, bad)

    def test_nbn_values_only_patch_an_event_subject(self):
        for subject in ("occurrence", "material", "protocol"):
            with self.assertRaises(ImportFailure):
                emit_legacy_records("nbn", nbn_date("D", "2015-06-03", "2015-06-03"), {}, subject, "key", "row-key")
        with self.assertRaises(ImportFailure):
            emit_legacy_records("nbn", {}, {}, "event", " ", "row-key")

    def test_sensitive_rows_need_review(self):
        valid = nbn_date("D", "2015-06-03", "2015-06-03")
        self.assertIsNone(legacy_row_review(NBN_ROW_TYPE, {**valid, NXF + "sensitiveOccurrence": "false"}))
        self.assertIn("sensitive", legacy_row_review(NBN_ROW_TYPE, {**valid, NXF + "sensitiveOccurrence": "true"}))
        self.assertIn("true/false", legacy_row_review(NBN_ROW_TYPE, {**valid, NXF + "sensitiveOccurrence": ""}))
        self.assertIsNone(legacy_row_review(DWC + "Occurrence", {NXF + "sensitiveOccurrence": "true"}))
