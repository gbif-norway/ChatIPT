import datetime
import json
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase, override_settings

from api.agent_tools import GetDwcDpTableInfo, SetWorkingPlan
from api.dwc_dp_specs import get_table_spec
from api.helpers.openai_helpers import (
    CompatAssistantMessage,
    CompatFunctionCall,
    CompatToolCall,
)
from api.models import Agent, Dataset, Message, Table, Task
from api.schema_ledger import (
    LEVEL_FIELDS,
    LEVEL_KEYS,
    lookup_is_covered,
    render_field_details,
    render_table_manifest,
    schema_lookups,
    turns_since_progress,
)

# The resources dataset 524 (Insektmobilen eDNA) kept re-fetching.
RUN_524_TABLES = [
    "event",
    "occurrence",
    "material",
    "identification",
    "nucleotide-sequence",
    "nucleotide-analysis",
    "molecular-protocol",
    "material-assertion",
]


def lookup_turn(call_prefix, tables, include_fields=True):
    return CompatAssistantMessage(tool_calls=[
        CompatToolCall(
            id=f"{call_prefix}-{table}",
            function=CompatFunctionCall(
                name="GetDwcDpTableInfo",
                arguments=json.dumps({
                    "table_name": table,
                    "include_fields": include_fields,
                    "max_fields": 40,
                }),
            ),
        )
        for table in tables
    ])


class CompactSchemaRenderingTests(SimpleTestCase):
    def test_manifest_lists_every_field_compactly(self):
        for table in ["event", "occurrence", "material", "molecular-protocol"]:
            spec = get_table_spec(table)
            manifest = render_table_manifest(spec)
            for field in spec.fields:
                self.assertIn(field, manifest, f"{table}.{field} missing")
            self.assertNotIn("more fields not shown", manifest)
            # Small enough to survive OPENAI_COMPACT_TOOL_CHARS-sized history.
            self.assertLess(len(manifest), 3000, table)

        event = render_table_manifest(get_table_spec("event"))
        self.assertIn("event_pk*!", event)
        self.assertIn("eventCategory*", event)
        self.assertIn("decimalLatitude:number[-90..90]", event)
        self.assertIn("parentEvent_fk -> event.event_pk", event)

    def test_keys_only_manifest_omits_fields(self):
        manifest = render_table_manifest(get_table_spec("occurrence"), include_fields=False)
        self.assertIn("occurrence_pk", manifest)
        self.assertIn("event_fk -> event.event_pk", manifest)
        self.assertNotIn("Fields (", manifest)

    def test_field_details_only_for_requested_fields(self):
        details = render_field_details(get_table_spec("event"), ["eventDate", "notAField"])
        self.assertIn("eventDate", details)
        self.assertIn("Guidance:", details)
        self.assertIn("Not fields of `event`: notAField.", details)
        self.assertNotIn("decimalLatitude", details)


class LedgerDerivationTests(SimpleTestCase):
    def history(self, *calls):
        objs = []
        for index, (name, args, result) in enumerate(calls):
            call_id = f"call-{index}"
            objs.append({"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]})
            objs.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return objs

    def test_successful_lookups_are_recorded_with_coverage_level(self):
        objs = self.history(
            ("GetDwcDpTableInfo", {"table_name": "event", "include_fields": False}, "event — Event"),
            ("GetDwcDpTableInfo", {"table_name": "event"}, "event — Event"),
            ("GetDwcDpTableInfo", {"table_name": "occurrence", "include_fields": False}, "occurrence"),
            ("GetDwcDpTableInfo", {"table_name": "nope"}, "Unknown DwC-DP table 'nope'."),
        )
        ledger = schema_lookups(objs, {"event", "occurrence"})
        self.assertEqual(ledger, {"event": LEVEL_FIELDS, "occurrence": LEVEL_KEYS})

        self.assertTrue(lookup_is_covered(ledger, "event", True, None))
        self.assertTrue(lookup_is_covered(ledger, "event", False, None))
        self.assertTrue(lookup_is_covered(ledger, "occurrence", False, None))
        self.assertFalse(lookup_is_covered(ledger, "occurrence", True, None))
        self.assertFalse(lookup_is_covered(ledger, "event", True, ["eventDate"]))
        self.assertFalse(lookup_is_covered(ledger, "material", True, None))

    def test_turns_since_progress(self):
        t0 = datetime.datetime(2026, 9, 24, 10, 0)
        minute = datetime.timedelta(minutes=1)
        entries = [(t0, {"role": "user", "content": "go"})]
        clock = t0
        for index in range(5):
            clock += minute
            entries.append((clock, {"role": "assistant", "tool_calls": [{
                "id": f"read-{index}", "function": {"name": "GetDwcDpTableInfo", "arguments": "{}"},
            }]}))
            entries.append((clock, {"role": "tool", "tool_call_id": f"read-{index}", "content": "x"}))
        self.assertEqual(turns_since_progress(entries), 5)

        clock += minute
        entries.append((clock, {"role": "assistant", "tool_calls": [{
            "id": "notes", "function": {"name": "SetStructureNotes", "arguments": "{}"},
        }]}))
        entries.append((clock + datetime.timedelta(seconds=1), {"role": "tool", "tool_call_id": "notes", "content": "ok"}))
        self.assertEqual(turns_since_progress(entries), 0)

        # A later table revision (a Python write) also resets the count.
        self.assertEqual(turns_since_progress(entries[:11], progress_floor=t0 + 4 * minute), 1)


class GetDwcDpTableInfoTests(TestCase):
    def test_lookup_is_compact_complete_and_ignores_legacy_max_fields(self):
        output = GetDwcDpTableInfo(table_name="event", max_fields=40).run()
        for field in get_table_spec("event").fields:
            self.assertIn(field, output)
        self.assertLess(len(output), 3000)

    def test_field_details(self):
        output = GetDwcDpTableInfo(table_name="event", field_details=["eventDate"]).run()
        self.assertIn("Examples:", output)
        self.assertIn("eventDate", output)


class SchemaLedgerAgentTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(name="Data transformation", text="Transform", order=1)
        Task.objects.create(name="Data maintenance", text="Maintain", order=2)
        self.dataset = Dataset.objects.create(title="Insektmobilen", description="eDNA")
        self.source = Table.objects.create(
            dataset=self.dataset,
            title="occurrences",
            df=pd.DataFrame({"occurrenceID": ["o1"], "eventID": ["e1"]}),
        )
        self.agent = Agent.create_with_system_message(
            dataset=self.dataset, task=self.task, tables=[self.source]
        )
        Message.objects.create(agent=self.agent, openai_obj={"role": "user", "content": "Go ahead."})

    @patch("api.models.create_response_message")
    def test_run_524_pattern_fetches_each_schema_once(self, create_response_message_mock):
        # Turn 1 fetches all schemas; turns 2 and 3 repeat the same lookups, as run 524 did.
        create_response_message_mock.side_effect = [
            lookup_turn("first", RUN_524_TABLES),
            lookup_turn("second", RUN_524_TABLES),
            lookup_turn("third", RUN_524_TABLES, include_fields=False),
        ]
        for _ in range(3):
            self.agent.next_message()

        results = {
            message.openai_obj["tool_call_id"]: message.openai_obj["content"]
            for message in self.agent.message_set.all()
            if message.openai_obj.get("role") == "tool"
        }
        for table in RUN_524_TABLES:
            self.assertTrue(results[f"first-{table}"].startswith(f"{table} — "), table)
            self.assertTrue(results[f"second-{table}"].startswith("Not re-run"), table)
            self.assertTrue(results[f"third-{table}"].startswith("Not re-run"), table)

        # Every field of every looked-up schema is in the per-turn state, which is
        # not subject to history compaction.
        state = self.agent.current_state_update()
        self.assertIn("DWC-DP SCHEMA LEDGER", state)
        for table in RUN_524_TABLES:
            for field in get_table_spec(table).fields:
                self.assertIn(field, state, f"{table}.{field} missing from ledger")
        self.assertLess(len(self.agent.schema_ledger_text()), 16000)

    def test_python_schema_file_reads_are_refused(self):
        result = self.agent.run_function(SimpleNamespace(
            name="Python",
            arguments=json.dumps({
                "code": "import json; print(json.load(open('api/templates/dwc-dp/table-schemas/event.json')))"
            }),
        ))
        self.assertIn("do not read DwC-DP schema files", result)

    def test_working_plan_is_shown_in_state(self):
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "plan-1", "type": "function", "function": {
                "name": "SetWorkingPlan",
                "arguments": json.dumps({"plan": "Graph: event -> material -> occurrence. Done: event."}),
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "tool", "tool_call_id": "plan-1", "content": SetWorkingPlan(plan="x").run(),
        })
        state = self.agent.current_state_update()
        self.assertIn("Working plan", state)
        self.assertIn("Graph: event -> material -> occurrence. Done: event.", state)

    def add_read_only_turns(self, count):
        for index in range(count):
            call_id = f"read-{self.agent.message_set.count()}-{index}"
            Message.objects.create(agent=self.agent, openai_obj={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": "Python", "arguments": json.dumps({"code": "print(1)"}),
                }}],
            })
            Message.objects.create(agent=self.agent, openai_obj={
                "role": "tool", "tool_call_id": call_id, "content": "1",
            })

    @override_settings(AGENT_NO_PROGRESS_WARN_TURNS=3, AGENT_NO_PROGRESS_STOP_TURNS=5)
    def test_no_progress_warns_then_pauses_without_calling_the_model(self):
        self.add_read_only_turns(3)
        self.assertIn("NO PROGRESS", self.agent.current_state_update())

        self.add_read_only_turns(2)
        with patch("api.models.create_response_message") as create_response_message_mock:
            messages = self.agent.next_message()
            create_response_message_mock.assert_not_called()

        self.assertTrue(messages[-1].openai_obj.get("no_progress_pause"))
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.busy_thinking)
        self.assertIsNone(self.agent.next_message())  # waits for the user

        Message.objects.create(agent=self.agent, openai_obj={"role": "user", "content": "continue"})
        self.assertEqual(self.agent.turns_without_progress(), 0)

    @override_settings(AGENT_NO_PROGRESS_WARN_TURNS=3, AGENT_NO_PROGRESS_STOP_TURNS=5)
    def test_table_write_counts_as_progress(self):
        self.add_read_only_turns(4)
        Table.objects.create(
            dataset=self.dataset, title="event", df=pd.DataFrame({"event_pk": ["e1"]})
        )
        self.assertEqual(self.agent.turns_without_progress(), 0)

    @override_settings(AGENT_NO_PROGRESS_STOP_TURNS=2)
    def test_unguarded_tasks_are_not_paused(self):
        task = Task.objects.create(name="Data structure exploration", text="Explore", order=0)
        self.agent.task = task
        self.agent.save()
        self.add_read_only_turns(3)
        with patch("api.models.create_response_message") as create_response_message_mock:
            create_response_message_mock.return_value = CompatAssistantMessage(content="Done.")
            self.agent.next_message()
            self.assertTrue(create_response_message_mock.called)

    @override_settings(
        OPENAI_TOOL_HISTORY_TURNS=2,
        OPENAI_FULL_TOOL_HISTORY_TURNS=1,
        AGENT_NO_PROGRESS_STOP_TURNS=0,
    )
    def test_dropped_tool_turns_keep_assistant_text(self):
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant",
            "content": "Thanks. I'll build an illustrative package and label the uncertain parts.",
            "tool_calls": [{"id": "old", "type": "function", "function": {
                "name": "Python", "arguments": json.dumps({"code": "print(1)"}),
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={"role": "tool", "tool_call_id": "old", "content": "1"})
        self.add_read_only_turns(3)

        bounded = [message.openai_obj for message in self.agent.messages_for_model()]
        self.assertNotIn("old", [obj.get("tool_call_id") for obj in bounded])
        kept_text = [
            obj for obj in bounded
            if obj.get("role") == "assistant" and "illustrative package" in (obj.get("content") or "")
        ]
        self.assertEqual(len(kept_text), 1)
        self.assertNotIn("tool_calls", kept_text[0])
