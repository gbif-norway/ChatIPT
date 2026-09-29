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
    @patch("api.quality_report.agent_tools.submit_gbif_validation", return_value="k1")
    @patch("api.helpers.publish.upload_file")
    @patch("api.helpers.publish.Minio")
    def test_provisional_archive_drops_dwc_dp_keys_instead_of_failing_preflight(
        self, _minio, _upload, _submit, _delete
    ):
        from api.quality_report import start_refinement_report

        start_refinement_report(self.agent)

        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.provisional_dwca_validation.get("key"), "k1")
