import json
from unittest.mock import patch

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase

from api.agent_tools import ReconcileSourceCoverage
from api.models import Agent, Dataset, Message, Table, Task, UserFile
from api.source_coverage import (
    OPEN_ITEMS_PREFIX,
    normalize_value,
    reconcile,
    render_report,
)


def insektmobilen_like(rows=600, events=24):
    """A small stand-in for dataset 525: occurrence-level source, event-level package."""
    source = pd.DataFrame({
        "occurrenceID": [f"IM18_{i % events:03d}:{i:040x}" for i in range(rows)],
        "rightsHolder": ["Naturhistorisk Museum Aarhus"] * rows,
        "eventID": [f"IM18_{i % events:03d}" for i in range(rows)],
        "parentEventID": [f"P{(i % events) // 2}" for i in range(rows)],
        "eventDate": [f"2018-06-{10 + i % events % 5:02d} 12:30:00/2018-06-{10 + i % events % 5:02d} 12:55:00" for i in range(rows)],
        "organismQuantity": [float(1 + i % 97) for i in range(rows)],
        "occurrenceStatus": ["detected"] * rows,
        "blank": [np.nan] * rows,
        "orphan": [f"X{i % 7}" for i in range(rows)],
    })
    events_df = source.drop_duplicates("eventID")
    package = {
        "occurrence": pd.DataFrame({
            "occurrence_pk": "occurrence:" + source.occurrenceID,
            "occurrenceID": source.occurrenceID,
            "event_fk": source.eventID,
            "organismQuantity": source.organismQuantity.astype(int).astype(str),
            "occurrenceStatus": "present",
        }),
        "event": pd.DataFrame({
            "event_pk": events_df.eventID.values,
            "eventID": events_df.eventID.values,
            "parentEvent_fk": ("parent:" + events_df.parentEventID).values,
            "eventDate": events_df.eventDate.str.replace(r"(\d) (\d)", r"\1T\2", regex=True).values,
        }),
    }
    return source, package


class NormalizationTests(SimpleTestCase):
    def test_normalize_value(self):
        self.assertIsNone(normalize_value(" "))
        self.assertIsNone(normalize_value(np.nan))
        self.assertIsNone(normalize_value("NaN"))
        self.assertEqual(normalize_value("12.0"), "12")
        self.assertEqual(normalize_value(12), "12")
        self.assertEqual(normalize_value(" Animalia  "), "animalia")
        self.assertEqual(
            normalize_value("2018-06-10T12:30:00/2018-06-10T12:55:00"),
            normalize_value("2018-06-10 12:30:00/2018-06-10 12:55:00"),
        )


class ReconcileTests(SimpleTestCase):
    def setUp(self):
        self.source, self.package = insektmobilen_like()
        results = reconcile([("upload occ.txt", self.source)], self.package, "Rights: Naturhistorisk Museum Aarhus")
        self.result = results[0]
        self.columns = {column.name: column for column in self.result.columns}

    def test_statuses(self):
        status = {name: column.status for name, column in self.columns.items()}
        self.assertEqual(status["occurrenceID"], "mapped")
        self.assertEqual(status["eventID"], "mapped")
        self.assertEqual(status["eventDate"], "mapped")  # T separator normalised
        self.assertEqual(status["organismQuantity"], "mapped")  # 12.0 vs 12
        self.assertEqual(status["parentEventID"], "mapped")  # embedded in parent:<id>
        self.assertTrue(self.columns["parentEventID"].via_tokens)
        self.assertEqual(status["rightsHolder"], "metadata")
        self.assertEqual(status["occurrenceStatus"], "not_found")  # detected -> present needs a disposition
        self.assertEqual(status["orphan"], "not_found")
        self.assertEqual(status["blank"], "blank")

    def test_rows_and_counts(self):
        self.assertEqual(self.result.identifier[:2], ("occurrenceID", "occurrence.occurrenceID"))
        self.assertEqual(self.result.identifier[2:], (600, 600))
        event_id = self.columns["eventID"]
        self.assertEqual(event_id.populated, 600)
        self.assertIn(("event.eventID", 1.0, 24), event_id.targets)

    def test_report_lists_open_items_only_for_undecided_columns(self):
        report = render_report([self.result], self.package, state="s1")
        self.assertTrue(report.startswith("SOURCE COVERAGE CHECK"))
        open_line = next(line for line in report.splitlines() if line.startswith(OPEN_ITEMS_PREFIX))
        self.assertIn("occurrenceStatus", open_line)
        self.assertIn("orphan", open_line)
        self.assertNotIn("occurrenceID", open_line)
        self.assertIn("eventID -> event.eventID (600->24 values)", report)
        self.assertIn("Rows: unique `occurrenceID` -> occurrence.occurrenceID: 600/600 values found.", report)

    def test_token_matching_is_limited_to_identifier_like_columns(self):
        source = pd.DataFrame({"remarks": ["present", "absent"]})
        package = {
            "occurrence": pd.DataFrame({
                "occurrenceRemarks": ["status:present", "status:absent"],
            }),
        }
        result = reconcile([("source", source)], package)[0]
        self.assertEqual(result.columns[0].status, "not_found")

    def test_metadata_matching_does_not_accept_substrings_inside_words(self):
        source = pd.DataFrame({"countryCode": ["no", "no"]})
        package = {"occurrence": pd.DataFrame({"occurrence_pk": ["1", "2"]})}
        result = reconcile([("source", source)], package, "Notes about another country")[0]
        self.assertEqual(result.columns[0].status, "not_found")

    def test_row_identifier_prefers_identifier_named_column(self):
        source = pd.DataFrame({
            "sequence": ["AAA", "CCC"],
            "occurrenceID": ["o1", "o2"],
        })
        package = {
            "occurrence": pd.DataFrame({"occurrenceID": ["o1", "o2"]}),
            "nucleotide-sequence": pd.DataFrame({"sequence": ["AAA", "CCC"]}),
        }
        result = reconcile([("source", source)], package)[0]
        self.assertEqual(result.identifier[:2], ("occurrenceID", "occurrence.occurrenceID"))


class ReconcileSourceCoverageToolTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        Task.objects.create(name="Data maintenance", text="Maintain", order=2)
        self.dataset = Dataset.objects.create(title="Insektmobilen", description="Rights: Naturhistorisk Museum Aarhus")
        self.source, package = insektmobilen_like()
        self.upload = UserFile.objects.create(
            dataset=self.dataset,
            file="user_files/occ.txt",
            source_manifest=UserFile.build_source_manifest({"occ.txt": self.source}),
        )
        # The working copy was replaced in place by a package resource, as in 525.
        for title, df in package.items():
            Table.objects.create(dataset=self.dataset, title=title, df=df)
        self.agent = Agent.create_with_system_message(
            dataset=self.dataset, task=self.task, tables=list(Table.objects.filter(dataset=self.dataset))
        )

    def run_tool(self, call_id):
        with patch.object(
            UserFile,
            "extract_data",
            return_value=(UserFile.FileType.TABULAR, {"occ.txt": self.source.copy()}),
        ):
            result = ReconcileSourceCoverage(agent_id=self.agent.id).run()
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "ReconcileSourceCoverage", "arguments": json.dumps({"agent_id": self.agent.id}),
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={"role": "tool", "tool_call_id": call_id, "content": result})
        return result

    def test_tool_reconciles_original_upload_and_skips_repeat(self):
        first = self.run_tool("c1")
        self.assertTrue(first.startswith("SOURCE COVERAGE CHECK"))
        self.assertIn("== upload occ.txt (600 rows, 9 columns)", first)
        self.assertIn("Found only in dataset metadata (1): rightsHolder", first)

        second = self.run_tool("c2")
        self.assertTrue(second.startswith("Not re-run"))
        self.assertIn("occurrenceStatus", second)

        state = self.agent.current_state_update()
        self.assertIn("Latest ReconcileSourceCoverage open items", state)
        self.assertIn("upload occ.txt: orphan", state)

        # A table change makes a fresh check worthwhile again.
        occurrence = Table.objects.get(dataset=self.dataset, title="occurrence")
        df = occurrence.df.copy()
        df["occurrenceRemarks"] = "x"
        occurrence.df = df
        occurrence.save()
        self.assertNotIn("Latest ReconcileSourceCoverage open items", self.agent.current_state_update())
        self.assertTrue(self.run_tool("c3").startswith("SOURCE COVERAGE CHECK"))

        # Source and metadata changes also invalidate a cached reconciliation,
        # even when the package tables themselves are untouched.
        self.upload.source_manifest = {"tables": [{"name": "changed"}]}
        self.upload.save(update_fields=["source_manifest"])
        self.assertTrue(self.run_tool("c4").startswith("SOURCE COVERAGE CHECK"))

        self.dataset.title = "Changed title"
        self.dataset.save(update_fields=["title"])
        self.assertTrue(self.run_tool("c5").startswith("SOURCE COVERAGE CHECK"))

    def test_identical_working_table_is_not_double_counted(self):
        Table.objects.create(dataset=self.dataset, title="occ.txt", df=self.source.copy())
        result = self.run_tool("c1")
        self.assertNotIn("working table", result)

    def test_tool_is_available_and_read_only_in_transformation(self):
        from api.schema_ledger import READ_ONLY_TOOLS

        self.assertIn(ReconcileSourceCoverage, self.task.functions)
        self.assertIn("ReconcileSourceCoverage", READ_ONLY_TOOLS)
