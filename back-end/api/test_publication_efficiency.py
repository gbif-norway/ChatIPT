"""Checks for the publication-cost shortcuts that must preserve review quality."""

import csv
import io
import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from django.test import SimpleTestCase, TestCase
from openai import OpenAI

import pandas as pd

from api.agent_tools import ExportDwcDp, InspectPublicationArtifacts, PublishToGBIF, SetEML, UploadDwCA
from api.dwc_specs import DarwinCoreCoreType, DarwinCoreExtensionType
from api.helpers.openai_helpers import create_response_message, query_responses_api
from api.helpers.publish import (
    DwcaPreflightError,
    EmlExportError,
    inspect_dwca_archive,
    make_eml,
    normalize_temporal_scope,
    upload_dwca,
    validate_dwca_archive,
)
from api.models import Agent, CustomUser, Dataset, Table, Task


META = '''<archive xmlns="http://rs.tdwg.org/dwc/text/">
  <core encoding="UTF-8" fieldsTerminatedBy="\\t" ignoreHeaderLines="1"
        rowType="http://rs.tdwg.org/dwc/terms/Occurrence">
    <files><location>occurrence.txt</location></files>
    <id index="0"/>
    <field index="1" term="http://rs.tdwg.org/dwc/terms/basisOfRecord"/>
    <field index="2" term="http://rs.tdwg.org/dwc/terms/occurrenceStatus"/>
  </core>
</archive>'''
EML = '''<eml><dataset><title>Example</title>
  <creator><individualName><givenName>Ada</givenName><surName>Lovelace</surName></individualName></creator>
  <metadataProvider><individualName><givenName>Ada</givenName><surName>Lovelace</surName></individualName></metadataProvider>
  <contact><individualName><givenName>Ada</givenName><surName>Lovelace</surName></individualName></contact>
  <coverage><temporalCoverage><rangeOfDates><beginDate><calendarDate>2018-06-02</calendarDate></beginDate>
  <endDate><calendarDate>2018-06-30</calendarDate></endDate></rangeOfDates></temporalCoverage></coverage>
</dataset></eml>'''


def archive_at(path, rows="occ-1\tHumanObservation\tpresent\n", meta=META, eml=EML):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("meta.xml", meta)
        archive.writestr("eml.xml", eml)
        archive.writestr("occurrence.txt", "occurrenceID\tbasisOfRecord\toccurrenceStatus\n" + rows)


class PublicationEfficiencyTests(SimpleTestCase):
    def test_null_license_uses_default_in_eml(self):
        eml = make_eml("Showcase data", "Description", eml_extra={"license": None})
        self.assertIn("http://creativecommons.org/licenses/by/4.0/legalcode", eml)

    @patch("api.helpers.openai_helpers.query_responses_api")
    def test_history_breakpoint_precedes_changing_state(self, query_mock):
        query_mock.return_value = SimpleNamespace(id="resp-1", status="completed", output=[])
        messages = [
            SimpleNamespace(openai_obj={"role": "system", "content": "Fixed opening"}),
            SimpleNamespace(openai_obj={"role": "assistant", "tool_calls": [{
                "id": "call-1", "function": {"name": "Python", "arguments": "{}"},
            }]}),
            SimpleNamespace(openai_obj={
                "role": "tool", "tool_call_id": "call-1", "content": "Stable result",
            }),
        ]

        create_response_message(
            messages, [], model="gpt-6-sol",
            additional_input_items=[{"role": "system", "content": "Changing state"}],
        )

        items = query_mock.call_args.args[0]["input"]
        self.assertEqual(items[2]["output"], [{
            "type": "input_text", "text": "Stable result",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        }])
        self.assertEqual(items[-1], {"role": "system", "content": "Changing state"})

    def test_sdk_serializes_tool_result_cache_breakpoint(self):
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={
                "id": "cache-tool-test", "created_at": 1, "model": "gpt-6-sol",
                "object": "response", "output": [], "status": "completed",
                "service_tier": "flex", "parallel_tool_calls": True,
                "tool_choice": "auto", "tools": [],
            })

        client = OpenAI(
            api_key="test",
            http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        )
        with patch("api.helpers.openai_helpers.OpenAI", return_value=client):
            query_responses_api({
                "model": "gpt-6-sol", "service_tier": "flex",
                "input": [{
                    "type": "function_call_output", "call_id": "call-1",
                    "output": [{
                        "type": "input_text", "text": "Stable result",
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }],
                }],
                "prompt_cache_options": {"mode": "explicit"},
            })

        self.assertEqual(
            requests[0]["input"][0]["output"][0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
    def test_unparseable_temporal_scope_fails_before_export(self):
        with self.assertRaisesRegex(EmlExportError, "temporal_scope could not be exported"):
            make_eml("Example", "Description", eml_extra={
                "temporal_scope": "2–30 June 2018 (sampling dates)",
            })

    @patch("api.helpers.publish.discord_bot.send_discord_message")
    @patch("api.helpers.publish.make_eml")
    def test_dwca_export_reports_unexpected_eml_value_error(self, make_eml_mock, alert_mock):
        make_eml_mock.side_effect = ValueError("unexpected renderer failure")

        with self.assertRaisesRegex(RuntimeError, "unexpected renderer failure"):
            upload_dwca(
                pd.DataFrame([{"occurrenceID": "occ-1"}]),
                "Example", "Description", core_type=DarwinCoreCoreType.OCCURRENCE,
            )

        alert_mock.assert_called_once()

    def test_common_temporal_scopes_normalize_to_calendar_dates(self):
        cases = {
            "1990-2020": ("range", "1990", "2020"),
            "1990 - 2020": ("range", "1990", "2020"),
            "2–30 June 2018": ("range", "2018-06-02", "2018-06-30"),
            "June 2018": ("range", "2018-06-01", "2018-06-30"),
            "2018-06": ("range", "2018-06-01", "2018-06-30"),
            "2020-02": ("range", "2020-02-01", "2020-02-29"),
            "1837-05/1984-07-28": ("range", "1837-05-01", "1984-07-28"),
            "2018-06/2018-08": ("range", "2018-06-01", "2018-08-31"),
            "June 2018 to August 2018": ("range", "2018-06-01", "2018-08-31"),
            "2018-06-02/2018-06-30": ("range", "2018-06-02", "2018-06-30"),
            "2018-06-02": ("single", "2018-06-02"),
            "2020/1990": None,
            "2020-1990": None,
            "2018-06-30/2018-06-02": None,
            "2018-06/2018-05": None,
            "2018/2018-06-02": ("range", "2018", "2018-06-02"),
            "2018-06-02/2018-06-02": ("range", "2018-06-02", "2018-06-02"),
            "Summer 2018": None,
            "Sampled 2018-06-30 and 2018-06-02": ("range", "2018-06-02", "2018-06-30"),
            "Sampled 2018-06-02 and 2018-13-45": None,
            "Collected on 2018-06-02 in the field": None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_temporal_scope(value), expected)

    @patch("api.helpers.publish.upload_file")
    @patch("api.helpers.publish.Minio")
    def test_dwca_export_maps_detection_status_to_present_absent(self, minio_mock, upload_mock):
        captured = {}

        def capture_archive(client, bucket, object_name, local_path):
            with zipfile.ZipFile(local_path) as archive:
                captured["occurrence"] = next(
                    archive.read(name).decode()
                    for name in archive.namelist()
                    if "occurrence" in name
                )

        upload_mock.side_effect = capture_archive
        event = pd.DataFrame([{"eventID": "ev-1", "eventDate": "2018-06-02"}])
        occurrence = pd.DataFrame([
            {"_coreid": "ev-1", "occurrenceID": "occ-1", "basisOfRecord": "MaterialSample",
             "scientificName": "Apus apus", "occurrenceStatus": "detected"},
            {"_coreid": "ev-1", "occurrenceID": "occ-2", "basisOfRecord": "MaterialSample",
             "scientificName": "Apus apus", "occurrenceStatus": "Not detected"},
            {"_coreid": "ev-1", "occurrenceID": "occ-3", "basisOfRecord": "MaterialSample",
             "scientificName": "Apus apus", "occurrenceStatus": "notDetected"},
        ])
        with patch.dict(os.environ, {
            "MINIO_URI": "storage.example.org", "MINIO_ACCESS_KEY": "key",
            "MINIO_SECRET_KEY": "secret", "MINIO_BUCKET": "bucket",
            "MINIO_BUCKET_FOLDER": "packages",
        }):
            upload_dwca(
                event, "Test dataset", "Test description",
                core_type=DarwinCoreCoreType.EVENT,
                extensions=[(occurrence, DarwinCoreExtensionType.OCCURRENCE, "_coreid")],
            )

        self.assertIn("present", captured["occurrence"])
        self.assertIn("absent", captured["occurrence"])
        self.assertNotIn("detected", captured["occurrence"])
        self.assertEqual(occurrence.loc[0, "occurrenceStatus"], "detected")
        self.assertEqual(occurrence.loc[2, "occurrenceStatus"], "notDetected")
        self.assertEqual(captured["occurrence"].count("absent"), 2)

    @patch("api.helpers.publish.upload_file")
    @patch("api.helpers.publish.Minio")
    def test_dwca_export_writes_comma_tables_ipt_can_import(self, minio_mock, upload_mock):
        # IPT guesses each table's delimiter from its first lines, picking the most frequent
        # candidate, before it reads meta.xml (gbif/ipt#3253). Comma-rich JSON in a tab table
        # made it guess comma and fail, so tables are comma-delimited and must stay readable.
        captured = {}

        def capture_archive(client, bucket, object_name, local_path):
            with zipfile.ZipFile(local_path) as archive:
                captured["meta"] = ET.fromstring(archive.read("meta.xml"))
                captured["files"] = {
                    name: archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".txt")
                }

        upload_mock.side_effect = capture_archive
        dynamic = json.dumps({
            "eventAgentRoles": [{"role": "collector", "agent": "Doe, J."}, {"role": "identifier", "agent": "Roe, K."}],
            "material": 'soil; "wet" | cores',
            "sourceFile": "C:\\field\\cores.xlsx",
        }, separators=(",", ":"))
        event = pd.DataFrame([{"eventID": "ev-1", "eventDate": "2018-06-02", "dynamicProperties": dynamic}])
        occurrence = pd.DataFrame([{
            "_coreid": "ev-1", "occurrenceID": "occ-1", "basisOfRecord": "MaterialSample",
            "scientificName": "Apus apus", "occurrenceStatus": "present", "dynamicProperties": dynamic,
        }])
        with patch.dict(os.environ, {
            "MINIO_URI": "storage.example.org", "MINIO_ACCESS_KEY": "key",
            "MINIO_SECRET_KEY": "secret", "MINIO_BUCKET": "bucket",
            "MINIO_BUCKET_FOLDER": "packages",
        }):
            upload_dwca(
                event, "Test dataset", "Test description",
                core_type=DarwinCoreCoreType.EVENT,
                extensions=[(occurrence, DarwinCoreExtensionType.OCCURRENCE, "_coreid")],
            )

        tables = [element for element in captured["meta"] if element.tag.rsplit("}", 1)[-1] in {"core", "extension"}]
        self.assertEqual(len(tables), 2)
        for table in tables:
            location = table.find("{http://rs.tdwg.org/dwc/text/}files/{http://rs.tdwg.org/dwc/text/}location").text
            with self.subTest(location=location):
                self.assertEqual(table.attrib["fieldsTerminatedBy"], ",")
                self.assertEqual(table.attrib["fieldsEnclosedBy"], '"')
                text = captured["files"][location]
                self.assertTrue(all(text.count(",") > text.count(other) for other in "\t;|"))
                rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=",", quotechar='"', strict=True))
                self.assertEqual(len({len(row) for row in rows}), 1)
                # Readers declare no escape character, so backslashes must be written once.
                self.assertEqual(rows[1][rows[0].index("dynamicProperties")], dynamic)

    def test_occurrence_preflight_catches_missing_and_invalid_vocabulary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            archive_at(path, rows="occ-1\tDNASequence\tdetected\n")
            result = validate_dwca_archive(path)
            self.assertFalse(result["valid"])
            self.assertTrue(any("unsupported basisOfRecord" in error for error in result["errors"]))
            self.assertTrue(any("occurrenceStatus" in warning for warning in result["warnings"]))

            archive_at(path, rows="occ-1\tHUMAN_OBSERVATION\tdetected\n")
            result = validate_dwca_archive(path)
            self.assertTrue(result["valid"])
            self.assertEqual(result["errors"], [])
            self.assertTrue(any("published as presences" in warning for warning in result["warnings"]))

            archive_at(path, rows="occ-1\t\tpresent\n")
            self.assertTrue(any(
                "unsupported basisOfRecord" in error
                for error in validate_dwca_archive(path)["errors"]
            ))

            archive_at(path, meta=META.replace(
                '<field index="1" term="http://rs.tdwg.org/dwc/terms/basisOfRecord"/>',
                '<field index="1" term="http://rs.tdwg.org/dwc/terms/scientificName"/>',
            ))
            self.assertTrue(any(
                "no basisOfRecord field" in error
                for error in validate_dwca_archive(path)["errors"]
            ))

    def test_archive_inspection_summarizes_exported_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            archive_at(path)
            report = inspect_dwca_archive(path)

        self.assertTrue(report["preflight"]["valid"])
        self.assertEqual(report["eml"]["title"], "Example")
        self.assertEqual(report["eml"]["temporal_coverage"][0]["start"], "2018-06-02")
        self.assertEqual(report["tables"][0]["row_count"], 1)
        self.assertEqual(report["tables"][0]["basis_of_record"], {"HumanObservation": 1})

    def test_archive_preflight_rejects_incomplete_exported_eml_person(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            archive_at(path, eml=EML.replace(
                "<surName>Lovelace</surName></individualName>",
                "</individualName><userId></userId>",
            ))
            errors = validate_dwca_archive(path)["errors"]

        self.assertTrue(any("missing a verified surname" in error for error in errors))
        self.assertTrue(any("empty or malformed ORCID" in error for error in errors))


class InspectPublicationArtifactsTests(TestCase):
    @patch("api.agent_tools.Minio")
    def test_tool_uses_current_archive_and_groups_validator_issues(self, minio_class):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "archive.zip"
            archive_at(source)
            minio_class.return_value.fget_object.side_effect = (
                lambda bucket, object_name, destination: shutil.copyfile(source, destination)
            )
            name = "output-2026-09-25-105242-028744.zip"
            url = f"https://storage.example.org/bucket/packages/{name}"
            dataset = Dataset.objects.create(
                title="Example", dwca_url=url,
                dwca_validation={
                    "url": url, "status": "FINISHED", "key": "validator-key",
                    "metrics": {"indexeable": True, "files": [{
                        "fileName": "occurrence.txt",
                        "issues": [{"issue": "COORDINATE_ROUNDED", "count": 1}],
                    }]},
                },
            )
            task = Task.objects.create(name=Task.PREPUBLICATION_QUALITY_TASK, text="Review")
            agent = Agent.objects.create(dataset=dataset, task=task)
            with patch.dict(os.environ, {
                "MINIO_URI": "storage.example.org", "MINIO_BUCKET": "bucket",
                "MINIO_BUCKET_FOLDER": "packages", "MINIO_ACCESS_KEY": "key",
                "MINIO_SECRET_KEY": "secret",
            }):
                report = json.loads(InspectPublicationArtifacts(agent_id=agent.id).run())

        self.assertEqual(report["tables"][0]["row_count"], 1)
        self.assertEqual(report["gbif_validation"]["issues"][0]["codes"], [
            {"code": "COORDINATE_ROUNDED", "count": 1},
        ])
        minio_class.return_value.fget_object.assert_called_once()


class SetEMLTemporalScopeExportTests(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(title="Temporal", eml={})
        task = Task.objects.create(name="Metadata", text="Set metadata", order=1)
        self.agent = Agent.objects.create(dataset=self.dataset, task=task)

    def test_rejects_unexportable_scope_without_saving(self):
        result = SetEML(
            agent_id=self.agent.id, temporal_scope="Summer 2018", methodology="Trapping",
        ).run()
        self.dataset.refresh_from_db()

        self.assertIn("cannot be exported", result)
        self.assertEqual(self.dataset.eml, {})

    def test_accepts_month_scope(self):
        result = SetEML(agent_id=self.agent.id, temporal_scope="June 2018").run()
        self.dataset.refresh_from_db()

        self.assertEqual(result, "EML has been successfully set.")
        self.assertEqual(self.dataset.eml["temporal_scope"], "June 2018")

    def test_explicit_null_keeps_saved_license_and_exports(self):
        self.dataset.eml = {"license": "CC BY 4.0"}
        self.dataset.save(update_fields=["eml"])
        Dataset.objects.filter(pk=self.dataset.pk).update(
            dwc_dp_url="https://example.org/package.zip",
            dwca_url="https://example.org/archive.zip",
            dwc_dp_validation={"valid": True},
            dwca_validation={"valid": True},
        )

        result = SetEML(agent_id=self.agent.id, license=None).run()
        self.dataset.refresh_from_db()
        self.assertEqual(result, "EML has been successfully set.")
        self.assertEqual(self.dataset.eml["license"], "CC BY 4.0")
        self.assertEqual(self.dataset.dwc_dp_url, "https://example.org/package.zip")
        self.assertEqual(self.dataset.dwca_url, "https://example.org/archive.zip")

        Dataset.objects.filter(pk=self.dataset.pk).update(eml={"license": None})
        SetEML(agent_id=self.agent.id, license=None).run()
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.eml["license"], "CC BY 4.0")
        self.assertEqual(self.dataset.dwca_url, "")
        self.assertEqual(self.dataset.dwc_dp_url, "")

        SetEML(agent_id=self.agent.id, license="CC0 1.0").run()
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.eml["license"], "CC0 1.0")

        SetEML(agent_id=self.agent.id, license=None).run()
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.eml["license"], "CC0 1.0")

    def test_rejects_backwards_scope_without_saving_other_fields(self):
        result = SetEML(
            agent_id=self.agent.id, temporal_scope="2020/1990", methodology="Trapping",
        ).run()
        self.dataset.refresh_from_db()

        self.assertIn("cannot be exported", result)
        self.assertEqual(self.dataset.eml, {})

    def test_saved_invalid_scope_warns_without_blocking_other_edits(self):
        self.dataset.eml = {"temporal_scope": "Summer 2018"}
        self.dataset.save(update_fields=["eml"])

        result = SetEML(agent_id=self.agent.id, license="CC0 1.0").run()
        self.dataset.refresh_from_db()
        self.assertIn("EML has been successfully set.", result)
        self.assertIn("saved temporal_scope 'Summer 2018'", result)
        self.assertIn("will block export", result)
        self.assertEqual(self.dataset.eml.get("license"), "CC0 1.0")
        self.assertEqual(self.dataset.eml["temporal_scope"], "Summer 2018")

        result = SetEML(agent_id=self.agent.id, temporal_scope="Autumn 2018").run()
        self.dataset.refresh_from_db()
        self.assertIn("Nothing was saved", result)
        self.assertEqual(self.dataset.eml["temporal_scope"], "Summer 2018")

        result = SetEML(
            agent_id=self.agent.id, temporal_scope="", license="CC0 1.0"
        ).run()
        self.dataset.refresh_from_db()
        self.assertIn("EML has been successfully set.", result)
        self.assertEqual(self.dataset.eml.get("license"), "CC0 1.0")
        self.assertEqual(self.dataset.eml["temporal_scope"], "")

    def test_accepts_documented_year_range(self):
        result = SetEML(agent_id=self.agent.id, temporal_scope="1990-2020").run()
        self.dataset.refresh_from_db()

        self.assertEqual(result, "EML has been successfully set.")
        self.assertEqual(self.dataset.eml["temporal_scope"], "1990-2020")


class PublicationErrorHandlingTests(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(title="Example", description="Description")
        task = Task.objects.create(name="Publication errors", text="Publish")
        self.agent = Agent.objects.create(dataset=self.dataset, task=task)

    @patch("api.agent_tools.discord_bot.send_discord_message")
    @patch("api.agent_tools.upload_dwca")
    def test_dwca_tool_only_suppresses_expected_validation_alerts(self, upload_mock, alert_mock):
        core = Table.objects.create(
            dataset=self.dataset,
            title="occurrence_dwca",
            df=pd.DataFrame([{"occurrenceID": "occ-1"}]),
        )
        tool = UploadDwCA(
            agent_id=self.agent.id,
            core_table_id=core.id,
            core_type=DarwinCoreCoreType.OCCURRENCE,
        )

        upload_mock.side_effect = DwcaPreflightError("correctable archive problem")
        self.assertEqual(tool.run(), "Error: correctable archive problem")
        alert_mock.assert_not_called()

        upload_mock.side_effect = EmlExportError("correctable EML problem")
        self.assertEqual(tool.run(), "Error: correctable EML problem")
        alert_mock.assert_not_called()

        upload_mock.side_effect = ValueError("unexpected writer failure")
        self.assertIn("unexpected writer failure", tool.run())
        alert_mock.assert_called_once()

    @patch("api.agent_tools.discord_bot.send_discord_message")
    @patch("api.agent_tools._dwc_dp_export_fingerprint")
    def test_dwc_dp_tool_only_suppresses_expected_eml_alerts(self, fingerprint_mock, alert_mock):
        fingerprint_mock.side_effect = EmlExportError("correctable EML problem")
        self.assertEqual(
            ExportDwcDp(agent_id=self.agent.id).run(),
            "Error: correctable EML problem",
        )
        alert_mock.assert_not_called()

        fingerprint_mock.side_effect = ValueError("unexpected fingerprint failure")

        result = ExportDwcDp(agent_id=self.agent.id).run()

        self.assertIn("unexpected fingerprint failure", result)
        alert_mock.assert_called_once()


class QualityGateRecoveryTests(TestCase):
    def test_conversion_dataset_does_not_create_quality_gate(self):
        user = CustomUser.objects.create_user(username="conversion-publisher")
        dataset = Dataset.objects.create(
            user=user,
            title="Converted archive",
            workflow_type=Dataset.WorkflowType.DWCA_CONVERSION,
            dwca_url="https://example.org/archive.zip",
            dwca_validation={
                "url": "https://example.org/archive.zip",
                "status": "FINISHED",
                "metrics": {"indexeable": True},
            },
        )
        maintenance = Task.objects.create(name=Task.MAINTENANCE_TASK, text="Maintain", order=5)
        agent = Agent.objects.create(dataset=dataset, task=maintenance)

        result = PublishToGBIF(agent_id=agent.id).run()

        self.assertIn("Conversion datasets", result)
        self.assertFalse(dataset.agent_set.filter(task__name=Task.PREPUBLICATION_QUALITY_TASK).exists())
