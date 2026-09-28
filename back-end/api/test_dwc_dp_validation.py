"""Checks ported from GBIF's dwc-dp-analyser, plus Frictionless validate() on the package."""

import tarfile
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase

from api.dwc_dp_specs import (
    DwcDpArchiveValidationError,
    _frictionless_data_errors,
    build_datapackage_descriptor,
    create_dwc_dp_archive,
    validate_datapackage_descriptor,
    validate_dwc_dp_archive,
    validate_dwc_dp_resources,
    validate_eml,
)
from api.helpers.publish import make_eml


def _resources():
    return {
        "event": pd.DataFrame([
            {
                "event_pk": "event-1",
                "eventID": "source-event-1",
                "eventCategory": "occurrence",
                "eventDate": "2025-04-26",
                "year": "2025",
            },
        ]),
        "occurrence": pd.DataFrame([
            {
                "occurrence_pk": "occ-1",
                "occurrenceID": "source-occ-1",
                "event_fk": "event-1",
                "scientificName": "Apus apus",
                "occurrenceStatus": "present",
            },
        ]),
    }


def _resource(descriptor, name):
    return next(resource for resource in descriptor["resources"] if resource["name"] == name)


def _field(resource, name):
    return next(field for field in resource["schema"]["fields"] if field["name"] == name)


class DwcDpProfileValidationTests(SimpleTestCase):
    def test_generated_descriptor_matches_profile(self):
        descriptor = build_datapackage_descriptor(_resources())
        self.assertEqual(validate_datapackage_descriptor(descriptor), [])

    def test_reports_violations_of_the_referenced_data_package_schema_offline(self):
        descriptor = build_datapackage_descriptor(_resources())
        descriptor["resources"][0]["name"] = "Event"  # Data Package v1 names are lowercase

        with patch("urllib.request.urlopen", side_effect=AssertionError("network access")):
            errors = validate_datapackage_descriptor(descriptor)

        self.assertTrue(
            any("does not match the DwC-DP profile at '/resources/0/name'" in error for error in errors),
            errors,
        )


class DwcDpCanonicalSchemaTests(SimpleTestCase):
    def setUp(self):
        self.descriptor = build_datapackage_descriptor(_resources())
        self.event = _resource(self.descriptor, "event")
        self.occurrence = _resource(self.descriptor, "occurrence")

    def assertReports(self, fragment):
        errors = validate_datapackage_descriptor(self.descriptor)
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_rejects_duplicate_fields(self):
        self.event["schema"]["fields"].append(deepcopy(_field(self.event, "eventDate")))
        self.assertReports("Resource 'event' declares field 'eventDate' more than once.")

    def test_rejects_missing_required_field(self):
        self.occurrence["schema"]["fields"] = [
            field for field in self.occurrence["schema"]["fields"] if field["name"] != "occurrenceStatus"
        ]
        self.assertReports("Resource 'occurrence' schema is missing required field 'occurrenceStatus'.")

    def test_rejects_field_definitions_that_differ_from_canonical_schema(self):
        _field(self.event, "year")["type"] = "string"
        _field(self.event, "eventDate")["dcterms:isVersionOf"] = "http://example.org/eventDate"
        self.assertReports("Resource 'event' field 'year' declares type 'string'")
        self.assertReports("Resource 'event' field 'eventDate' declares dcterms:isVersionOf")

    def test_rejects_fields_missing_from_canonical_schema(self):
        field = deepcopy(_field(self.event, "eventDate"))
        field["name"] = "myLocalField"
        self.event["schema"]["fields"].append(field)
        self.assertReports("Resource 'event' field 'myLocalField' is not defined by its DwC-DP table schema.")

    def test_rejects_foreign_key_to_absent_resource(self):
        self.descriptor["resources"].remove(self.event)
        self.assertReports(
            "Resource 'occurrence' foreignKeys 'event_fk' references resource 'event', which is not in the package."
        )

    def test_rejects_foreign_key_to_undeclared_target_field(self):
        self.occurrence["schema"]["foreignKeys"][0]["reference"]["fields"] = "missing_pk"
        errors = validate_datapackage_descriptor(self.descriptor)
        self.assertTrue(
            any("references field 'missing_pk', which resource 'event' does not declare" in e for e in errors),
            errors,
        )
        self.assertTrue(
            any("does not match a relationship in its DwC-DP table schema" in e for e in errors),
            errors,
        )


class DwcDpIntegerValueTests(SimpleTestCase):
    def test_rejects_integers_with_decimal_point_or_exponent(self):
        resources = _resources()
        resources["event"]["year"] = ["2025.0"]
        validation = validate_dwc_dp_resources(resources)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("'2025.0'" in error for error in validation["errors"]), validation)

        resources["event"]["year"] = ["2e3"]
        self.assertFalse(validate_dwc_dp_resources(resources)["valid"])

        resources["event"]["year"] = ["+2025"]
        self.assertTrue(validate_dwc_dp_resources(resources)["valid"])

    def test_writes_whole_number_float_columns_as_integers(self):
        resources = _resources()
        resources["event"] = pd.concat(
            [resources["event"], pd.DataFrame([{"event_pk": "event-2", "eventCategory": "occurrence"}])],
            ignore_index=True,
        )
        resources["event"]["year"] = [2025, None]  # pandas upcasts to float64
        self.assertTrue(pd.api.types.is_float_dtype(resources["event"]["year"]))

        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "package.tar.gz"
            create_dwc_dp_archive(archive_path, resources, title="T", description="D")
            with tarfile.open(archive_path, "r:gz") as archive:
                event_csv = archive.extractfile("event.csv").read().decode("utf-8")

        self.assertIn(",2025\n", event_csv)
        self.assertNotIn("2025.0", event_csv)


class EmlValidationTests(SimpleTestCase):
    def test_generated_eml_is_valid(self):
        eml = make_eml("Title", "Description")
        self.assertEqual(validate_eml(eml.encode("utf-8")), [])

    def test_reports_schema_violations(self):
        eml = make_eml("Title", "Description").replace("<language>", "<notAnEmlElement/><language>", 1)
        errors = validate_eml(eml.encode("utf-8"))
        self.assertTrue(any("does not match the EML 2.2.0 schema" in error for error in errors), errors)

    def test_reports_blank_title(self):
        eml = make_eml("Title", "Description").replace(">Title</title>", ">  </title>", 1)
        self.assertIn("eml.xml has no non-empty dataset <title>.", validate_eml(eml.encode("utf-8")))

    def test_rejects_unparseable_or_entity_expanding_xml(self):
        self.assertTrue(validate_eml(b"<eml")[0].startswith("eml.xml cannot be parsed"))
        entity_bomb = (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
            b"<eml><dataset><title>&a;</title></dataset></eml>"
        )
        errors = validate_eml(entity_bomb)
        self.assertNotIn("aaaa", " ".join(errors))


class FrictionlessPackageValidationTests(SimpleTestCase):
    def test_reports_row_level_errors(self):
        descriptor = build_datapackage_descriptor(_resources())
        files = {
            "event.csv": (
                b"event_pk,eventID,eventCategory,eventDate,year\n"
                b"event-1,e1,occurrence,2025-04-26,2025.0\n"
                b"event-1,e2,occurrence,2025-04-26,2025\n"
            ),
            "occurrence.csv": (
                b"occurrence_pk,occurrenceID,event_fk,scientificName,occurrenceStatus\n"
                b"occ-1,o1,event-2,Apus apus,present\n"
            ),
        }

        errors = _frictionless_data_errors(descriptor, files)

        joined = "\n".join(errors)
        self.assertIn('Type error in the cell "2025.0"', joined)
        self.assertIn("violates the primary key", joined)
        self.assertIn("violates the foreign key", joined)

    def test_caps_errors_per_resource(self):
        descriptor = build_datapackage_descriptor(_resources())
        rows = "".join(f"event-{index},e{index},occurrence,,1.5\n" for index in range(25))
        files = {
            "event.csv": ("event_pk,eventID,eventCategory,eventDate,year\n" + rows).encode("utf-8"),
            "occurrence.csv": (
                b"occurrence_pk,occurrenceID,event_fk,scientificName,occurrenceStatus\n"
                b"occ-1,o1,event-1,Apus apus,present\n"
            ),
        }

        errors = _frictionless_data_errors(descriptor, files)

        self.assertEqual(len(errors), 11, errors)
        self.assertEqual(errors[-1], "Resource 'event' has 15 more Frictionless validation errors.")

    def test_archive_runs_frictionless_and_blocks_export_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "package.tar.gz"
            create_dwc_dp_archive(archive_path, _resources(), title="T", description="D")
            with patch("api.dwc_dp_specs._frictionless_data_errors", return_value=["boom"]) as check:
                validation = validate_dwc_dp_archive(archive_path)
            self.assertEqual(validation["errors"], ["boom"])
            self.assertEqual(sorted(check.call_args.args[1]), ["event.csv", "occurrence.csv"])

            with patch("api.dwc_dp_specs._frictionless_data_errors", return_value=["boom"]):
                with self.assertRaises(DwcDpArchiveValidationError):
                    create_dwc_dp_archive(archive_path, _resources(), title="T", description="D")
