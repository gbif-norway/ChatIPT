import datetime
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from api.helpers.openai_helpers import CompatAssistantMessage
from api.models import Agent, Dataset, Message, Task
from api.quality_report import prepare_gate_report, render_gbif_validation


FINISHED_VALIDATION = {
    "status": "FINISHED",
    "key": "k1",
    "url": "https://example.org/current.zip",
    "metrics": {
        "indexeable": True,
        "files": [{
            "fileName": "occurrence.txt",
            "count": 10,
            "indexedCount": 10,
            "issues": [
                {"issue": "COUNTRY_DERIVED_FROM_COORDINATES", "count": 10, "samples": []},
                {
                    "issue": "COUNTRY_COORDINATE_MISMATCH", "count": 2,
                    "issueCategory": "OCC_INTERPRETATION_BASED",
                    "samples": [{"recordId": "mat-1", "relatedData": {
                        "dwc:decimalLatitude": "0.277", "dwc:country": "Norway",
                    }}],
                },
            ],
        }],
    },
}


class RenderGbifValidationTests(SimpleTestCase):
    def test_actionable_issues_show_samples_and_notes_are_summarised(self):
        text = render_gbif_validation(FINISHED_VALIDATION)

        self.assertIn("indexable: yes", text)
        self.assertIn("COUNTRY_COORDINATE_MISMATCH [OCC_INTERPRETATION_BASED]: 2 row(s)", text)
        self.assertIn("record mat-1: decimalLatitude='0.277', country='Norway'", text)
        self.assertIn("Informational only: COUNTRY_DERIVED_FROM_COORDINATES (10)", text)


@patch("api.quality_report.agent_tools.inspect_publication_artifacts", return_value={"preflight": {"valid": True}})
class PrepareGateReportTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(name=Task.PREPUBLICATION_QUALITY_TASK, text="Check", order=1)
        self.dataset = Dataset.objects.create(
            title="Gate", description="Test", dwca_url="https://example.org/current.zip",
        )
        self.agent = Agent.create_with_system_message(dataset=self.dataset, task=self.task, tables=[])

    def report_messages(self):
        return Message.objects.filter(agent=self.agent, openai_obj__quality_report=True)

    @patch("api.quality_report.agent_tools.submit_gbif_validation", return_value="new-key")
    def test_unvalidated_archive_is_submitted_and_waits(self, submit_mock, _inspect):
        self.assertFalse(prepare_gate_report(self.agent))

        submit_mock.assert_called_once_with("https://example.org/current.zip")
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.dwca_validation["key"], "new-key")
        self.assertFalse(self.report_messages().exists())

    @patch("api.quality_report.agent_tools._fetch_gbif_validation", return_value={"status": "RUNNING"})
    def test_running_validation_keeps_waiting(self, _fetch, _inspect):
        self.dataset.dwca_validation = {
            "url": self.dataset.dwca_url, "key": "k1", "status": "RUNNING",
            "submitted_at": timezone.now().isoformat(),
        }
        self.dataset.save(update_fields=["dwca_validation"])

        self.assertFalse(prepare_gate_report(self.agent))
        self.assertFalse(self.report_messages().exists())

    @patch("api.quality_report.agent_tools.submit_gbif_validation", return_value="k2")
    @patch("api.quality_report.agent_tools.get_gbif_validation", return_value={"status": "ABORTED"})
    def test_aborted_validation_is_resubmitted_once(self, _get, submit_mock, _inspect):
        self.dataset.dwca_validation = {
            "url": self.dataset.dwca_url, "key": "k1", "status": "RUNNING",
            "submitted_at": timezone.now().isoformat(),
        }
        self.dataset.save(update_fields=["dwca_validation"])

        self.assertFalse(prepare_gate_report(self.agent))

        submit_mock.assert_called_once_with("https://example.org/current.zip")
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.dwca_validation["key"], "k2")
        self.assertEqual(self.dataset.dwca_validation["aborted_keys"], ["k1"])
        self.assertFalse(self.report_messages().exists())

    @patch("api.quality_report.agent_tools.submit_gbif_validation")
    @patch("api.quality_report.agent_tools.get_gbif_validation", return_value={"status": "ABORTED"})
    def test_repeated_abort_is_reported_instead_of_resubmitted(self, _get, submit_mock, _inspect):
        self.dataset.dwca_validation = {
            "url": self.dataset.dwca_url, "key": "k2", "status": "RUNNING",
            "submitted_at": timezone.now().isoformat(), "aborted_keys": ["k1"],
        }
        self.dataset.save(update_fields=["dwca_validation"])

        self.assertTrue(prepare_gate_report(self.agent))

        submit_mock.assert_not_called()
        content = self.report_messages().get().openai_obj["content"]
        self.assertIn("GBIF aborted validating the current DwC-A twice", content)
        self.assertIn("k1, k2", content)

    @patch("api.quality_report.agent_tools.get_gbif_validation", return_value={"status": "QUEUED"})
    def test_timed_out_resubmission_explains_the_replaced_key(self, _get, _inspect):
        self.dataset.dwca_validation = {
            "url": self.dataset.dwca_url, "key": "k2", "status": "RUNNING", "aborted_keys": ["k1"],
            "submitted_at": (timezone.now() - datetime.timedelta(hours=4)).isoformat(),
        }
        self.dataset.save(update_fields=["dwca_validation"])

        self.assertTrue(prepare_gate_report(self.agent))

        content = self.report_messages().get().openai_obj["content"]
        self.assertIn("Call ValidateDwCA with validation_key k2 to keep waiting.", content)
        self.assertIn("GBIF aborted the earlier validation of this archive (k1)", content)

    def test_finished_validation_creates_hidden_report(self, _inspect):
        self.dataset.dwca_validation = FINISHED_VALIDATION
        self.dataset.save(update_fields=["dwca_validation"])

        self.assertTrue(prepare_gate_report(self.agent))

        report = self.report_messages().get()
        self.assertEqual(report.openai_obj["role"], "system")
        content = report.openai_obj["content"]
        self.assertIn("QUALITY REPORT", content)
        self.assertIn("COUNTRY_COORDINATE_MISMATCH", content)
        self.assertIn("SOURCE COVERAGE RECONCILIATION", content)

    @patch("api.models.create_response_message")
    @patch("api.quality_report.agent_tools._fetch_gbif_validation", return_value={"status": "RUNNING"})
    def test_gate_makes_no_model_call_until_report_is_ready(self, _fetch, model_mock, _inspect):
        self.dataset.dwca_validation = {
            "url": self.dataset.dwca_url, "key": "k1", "status": "RUNNING",
            "submitted_at": timezone.now().isoformat(),
        }
        self.dataset.save(update_fields=["dwca_validation"])

        self.agent.next_message()

        model_mock.assert_not_called()

    @patch("api.models.create_response_message")
    def test_gate_first_turn_sees_the_report(self, model_mock, _inspect):
        self.dataset.dwca_validation = FINISHED_VALIDATION
        self.dataset.save(update_fields=["dwca_validation"])
        model_mock.return_value = CompatAssistantMessage(content="Reviewing.")

        self.agent.next_message()

        self.assertTrue(model_mock.called)
        sent = [message.openai_obj.get("content", "") for message in model_mock.call_args_list[0].args[0]]
        self.assertTrue(any(content.startswith("QUALITY REPORT") for content in sent))
        self.assertEqual(self.report_messages().count(), 1)


class ProvisionalCoreTests(TestCase):
    def test_full_date_keeps_precision_without_hiding_source_conflicts(self):
        import pandas as pd
        from api.models import Table
        from api.quality_report import provisional_core

        dataset = Dataset.objects.create(title="Dates", description="Test")
        events = pd.DataFrame([
            {"event_pk": "matching", "eventDate": "2018-06-30", "year": 2018},
            {"event_pk": "conflicting", "eventDate": "2018-06-30", "year": 2019},
            {"event_pk": "invalid", "eventDate": "2018-02-30", "year": 2018},
            {"event_pk": "partial", "eventDate": "2018-06", "year": 2018},
            {"event_pk": "interval", "eventDate": "2018-06-01/2018-06-30", "year": 2018},
            {"event_pk": "components", "eventDate": "2018-06-30", "year": 2018, "month": 7},
            {"event_pk": "unparseable-year", "eventDate": "2018-06-30", "year": "unknown"},
            {"event_pk": "year-only", "year": 2018},
        ])
        table = Table.objects.create(dataset=dataset, title="event", df=events)
        Table.objects.create(dataset=dataset, title="occurrence", df=pd.DataFrame([
            {"occurrence_pk": f"o-{key}", "event_fk": key, "occurrenceStatus": "notDetected"}
            for key in events["event_pk"]
        ]))

        core = provisional_core(dataset)

        self.assertEqual(core["year"].tolist(), ['', 2019, 2018, 2018, 2018, 2018, 'unknown', 2018])
        self.assertEqual(core["occurrenceStatus"].tolist(), ['notDetected'] * len(events))
        table.refresh_from_db()
        pd.testing.assert_frame_equal(table.df, events)

    def test_material_records_take_event_and_accepted_identification(self):
        import pandas as pd
        from api.models import Table
        from api.quality_report import provisional_core

        dataset = Dataset.objects.create(title="Material", description="Test")
        Table.objects.create(dataset=dataset, title="event", df=pd.DataFrame([
            {"event_pk": "e1", "eventDate": "1958-05", "decimalLatitude": "60.277"},
        ]))
        Table.objects.create(dataset=dataset, title="material", df=pd.DataFrame([
            {"materialEntity_pk": "m1", "collectionEvent_fk": "e1", "catalogNumber": "Z1", "scientificName": ""},
            {"materialEntity_pk": "m2", "collectionEvent_fk": "e1", "catalogNumber": "Z2", "scientificName": "Own name"},
        ]))
        Table.objects.create(dataset=dataset, title="identification", df=pd.DataFrame([
            {"identification_pk": "i1", "materialEntity_fk": "m1", "scientificName": "Old name", "isAcceptedIdentification": "false"},
            {"identification_pk": "i2", "materialEntity_fk": "m1", "scientificName": "Current name", "isAcceptedIdentification": "true"},
            {"identification_pk": "i3", "materialEntity_fk": "m2", "scientificName": "Other name", "isAcceptedIdentification": "true"},
        ]))

        core = provisional_core(dataset)

        self.assertEqual(core["occurrenceID"].tolist(), ["m1", "m2"])
        self.assertEqual(core["scientificName"].tolist(), ["Current name", "Own name"])
        self.assertEqual(core["eventDate"].tolist(), ["1958-05", "1958-05"])
        self.assertEqual(core["basisOfRecord"].tolist(), ["MaterialEntity", "MaterialEntity"])

    def test_collectors_on_linked_specimens_fill_recorded_by(self):
        # A converted specimen archive (prod 568) stores collectors on material.collectedBy. GBIF reads them
        # through one unambiguous evidenceForOccurrenceID link, then from ordered collector agent roles.
        import pandas as pd
        from api.models import Table
        from api.quality_report import provisional_core

        dataset = Dataset.objects.create(title="Specimens", description="Test")
        Table.objects.create(dataset=dataset, title="occurrence", df=pd.DataFrame([
            {"occurrence_pk": "o1", "occurrenceID": "urn:1", "recordedBy": ""},
            {"occurrence_pk": "o2", "occurrenceID": "urn:2", "recordedBy": "Own, Name"},
            {"occurrence_pk": "o3", "occurrenceID": "urn:3", "recordedBy": ""},
            {"occurrence_pk": "o4", "occurrenceID": "urn:4", "recordedBy": ""},
            {"occurrence_pk": "o5", "occurrenceID": "urn:5", "recordedBy": ""},
            # An identifier without names: the specimen's collector would not match it, so nothing is filled.
            {"occurrence_pk": "o6", "occurrenceID": "urn:6", "recordedBy": "", "recordedByID": "https://orcid.org/A"},
        ]))
        Table.objects.create(dataset=dataset, title="material", df=pd.DataFrame([
            {"materialEntity_pk": "m1", "evidenceForOccurrenceID": "urn:1", "collectedBy": "Hagen, Yngvar", "collectedByID": "https://orcid.org/0000-0001"},
            # The occurrence names its own recorder: the specimen's collector and identifier are not mixed in.
            {"materialEntity_pk": "m2", "evidenceForOccurrenceID": "urn:2", "collectedBy": "Collett, Robert", "collectedByID": "https://orcid.org/0000-0002"},
            # Two materials name one occurrence: ambiguous, never joined.
            {"materialEntity_pk": "m3a", "evidenceForOccurrenceID": "urn:3", "collectedBy": "A", "collectedByID": ""},
            {"materialEntity_pk": "m3b", "evidenceForOccurrenceID": "urn:3", "collectedBy": "B", "collectedByID": ""},
            {"materialEntity_pk": "m4", "evidenceForOccurrenceID": "urn:4", "collectedBy": "", "collectedByID": ""},
            {"materialEntity_pk": "m6", "evidenceForOccurrenceID": "urn:6", "collectedBy": "Somebody, Else", "collectedByID": ""},
        ]))
        Table.objects.create(dataset=dataset, title="agent", df=pd.DataFrame([
            {"agent_pk": "a1", "preferredAgentName": "Kjernslie, O.L."},
            {"agent_pk": "a2", "preferredAgentName": "Second, Person"},
        ]))
        Table.objects.create(dataset=dataset, title="material-agent-role", df=pd.DataFrame([
            {"materialEntity_fk": "m4", "agent_fk": "a2", "agentRole": "collectedBy", "agentRoleOrder": "2"},
            {"materialEntity_fk": "m4", "agent_fk": "a1", "agentRole": "collectedBy", "agentRoleOrder": "1"},
            {"materialEntity_fk": "m4", "agent_fk": "a2", "agentRole": "identifiedBy", "agentRoleOrder": "3"},
        ]))

        core = provisional_core(dataset)

        self.assertEqual(core["recordedBy"].fillna("").tolist(),
                         ["Hagen, Yngvar", "Own, Name", "", "Kjernslie, O.L. | Second, Person", "", ""])
        self.assertEqual(core["recordedByID"].fillna("").tolist(), ["https://orcid.org/0000-0001", "", "", "", "", "https://orcid.org/A"])
        self.assertEqual(core["occurrenceID"].tolist(), ["o1", "o2", "o3", "o4", "o5", "o6"])

    def test_no_record_resources_means_no_projection(self):
        import pandas as pd
        from api.models import Table
        from api.quality_report import provisional_core

        dataset = Dataset.objects.create(title="Events", description="Test")
        Table.objects.create(dataset=dataset, title="event", df=pd.DataFrame([{"event_pk": "e1"}]))

        self.assertIsNone(provisional_core(dataset))


@patch("api.quality_report.agent_tools.inspect_publication_artifacts", return_value={})
class GateReportClaimTests(TestCase):
    @patch("api.quality_report.agent_tools.submit_gbif_validation")
    def test_no_submission_when_another_caller_holds_the_agent(self, submit_mock, _inspect):
        task = Task.objects.create(name=Task.PREPUBLICATION_QUALITY_TASK, text="Check", order=1)
        dataset = Dataset.objects.create(title="Gate", description="Test", dwca_url="https://example.org/a.zip")
        agent = Agent.create_with_system_message(dataset=dataset, task=task, tables=[])
        Agent.objects.filter(pk=agent.pk).update(busy_thinking=True)

        agent.next_message()

        submit_mock.assert_not_called()
        self.assertFalse(Message.objects.filter(agent=agent, openai_obj__quality_report=True).exists())


class RefinementReportTests(TestCase):
    def setUp(self):
        import pandas as pd
        from api.models import Table

        task = Task.objects.create(name="Data validation and refinement", text="Validate", order=1)
        self.dataset = Dataset.objects.create(title="Refine", description="Test")
        Table.objects.create(dataset=self.dataset, title="occurrence", df=pd.DataFrame([{"occurrence_pk": "o1"}]))
        Table.objects.create(dataset=self.dataset, title="material", df=pd.DataFrame([{"materialEntity_pk": "m1"}]))
        self.agent = Agent.objects.create(dataset=self.dataset, task=task)

    @patch("api.quality_report._delete_archive")
    @patch("api.quality_report.agent_tools.submit_gbif_validation", side_effect=RuntimeError("GBIF down"))
    @patch("api.helpers.publish.upload_dwca", return_value="https://example.org/provisional.zip")
    def test_failed_submission_still_removes_uploaded_archive(self, _upload, _submit, delete_mock):
        from api.quality_report import start_refinement_report

        report = start_refinement_report(self.agent)

        delete_mock.assert_called_once_with("https://example.org/provisional.zip")
        self.assertIn("Could not build or submit", report)
        self.assertIn("material records were not validated separately", report)

    @patch("api.quality_report._delete_archive")
    @patch("api.quality_report.agent_tools.submit_gbif_validation", return_value="k2")
    @patch("api.quality_report.agent_tools.get_gbif_validation", return_value={"status": "ABORTED"})
    def test_aborted_provisional_validation_is_resubmitted_once_then_reported(
        self, _get, submit_mock, delete_mock
    ):
        import json
        from api.quality_report import _refinement_waiting_payload, refresh_refinement_report

        record = {
            "agent_id": self.agent.id, "url": "https://example.org/provisional.zip", "key": "k1",
            "status": "RUNNING", "submitted_at": timezone.now().isoformat(),
        }
        self.dataset.provisional_dwca_validation = record
        self.dataset.save(update_fields=["provisional_dwca_validation"])

        waiting = json.loads(refresh_refinement_report(self.dataset, json.loads(_refinement_waiting_payload(record))))

        self.assertEqual((waiting["status"], waiting["validation_key"]), ("RUNNING", "k2"))
        submit_mock.assert_called_once_with("https://example.org/provisional.zip")
        delete_mock.assert_not_called()

        report = refresh_refinement_report(self.dataset, waiting)

        self.assertIn("GBIF aborted validating the provisional DwC-A twice", report)
        submit_mock.assert_called_once()
        delete_mock.assert_called_once_with("https://example.org/provisional.zip")

    @patch("api.quality_report._delete_archive")
    @patch("api.quality_report.agent_tools.submit_gbif_validation", return_value="k1")
    @patch("api.helpers.publish.upload_file")
    @patch("api.helpers.publish.Minio")
    def test_provisional_archive_drops_dwc_dp_keys_instead_of_failing_preflight(
        self, _minio, _upload, _submit, _delete
    ):
        import io
        import os
        import zipfile
        import pandas as pd
        from api.models import Table
        from api.quality_report import start_refinement_report

        occurrence = self.dataset.table_set.get(title="occurrence")
        occurrence.df = pd.DataFrame([
            {"occurrence_pk": "o1", "event_fk": "e1", "occurrenceStatus": "detected"},
            {"occurrence_pk": "o2", "event_fk": "e2", "occurrenceStatus": "notDetected"},
        ])
        occurrence.save()
        Table.objects.create(dataset=self.dataset, title="event", df=pd.DataFrame([
            {"event_pk": "e1", "eventDate": "2018-06-30", "year": "2018"},
            {"event_pk": "e2", "eventDate": "2018-06-30", "year": "2019"},
        ]))
        captured = {}

        def capture_archive(_client, _bucket, _name, local_path):
            with zipfile.ZipFile(local_path) as archive:
                captured["core"] = pd.read_csv(
                    io.BytesIO(archive.read("occurrence.txt")), sep="\t", dtype=str, keep_default_na=False,
                )

        _upload.side_effect = capture_archive
        with patch.dict(os.environ, {"MINIO_BUCKET": "bucket", "MINIO_BUCKET_FOLDER": "test"}):
            start_refinement_report(self.agent)

        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.provisional_dwca_validation.get("key"), "k1")
        self.assertEqual(captured["core"]["occurrenceStatus"].tolist(), ["present", "absent"])
        self.assertEqual(captured["core"]["year"].tolist(), ["", "2019"])
        self.assertEqual(captured["core"]["eventDate"].tolist(), ["2018-06-30"] * 2)
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.df["occurrenceStatus"].tolist(), ["detected", "notDetected"])

    @patch("api.quality_report._delete_archive")
    @patch("api.quality_report.agent_tools.submit_gbif_validation", return_value="k1")
    @patch("api.helpers.publish.upload_file")
    @patch("api.helpers.publish.Minio")
    def test_provisional_archive_metadata_states_only_saved_people_and_licence(
        self, _minio, upload_mock, _submit, _delete
    ):
        # Dataset 615: the archive's eml.xml named "Test User" and granted CC BY 4.0.
        import os
        import zipfile
        from api.models import CustomUser
        from api.quality_report import start_refinement_report

        self.dataset.user = CustomUser.objects.create_user(
            username="uploader", first_name="Artem", last_name="Uploader",
        )
        # A scope the final export would reject must not stop the validation.
        self.dataset.eml = {"creators_unknown": True, "temporal_scope": "the sampling season"}
        self.dataset.save()
        captured = {}

        def capture_archive(_client, _bucket, _name, local_path):
            with zipfile.ZipFile(local_path) as archive:
                captured["eml"] = archive.read("eml.xml").decode("utf-8")

        upload_mock.side_effect = capture_archive
        with patch.dict(os.environ, {"MINIO_BUCKET": "bucket", "MINIO_BUCKET_FOLDER": "test"}):
            report = start_refinement_report(self.agent)

        self.assertNotIn("Could not build", report)
        eml = captured["eml"]
        self.assertNotIn("Test User", eml)
        self.assertNotIn("<surName>User</surName>", eml)
        self.assertNotIn("creativecommons", eml)
        self.assertIn("Unknown creator", eml)
        self.assertIn("<surName>Uploader</surName>", eml)
        self.assertIn("Provisional archive for GBIF validation only", eml)
