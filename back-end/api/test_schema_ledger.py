import datetime
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase, override_settings

from api.agent_tools import (
    DwcTermReference,
    GetDwcDpTableInfo,
    SetWorkingPlan,
    _dwc_dp_resource_fingerprint,
)
from api.dwc_dp_specs import get_table_spec
from api.helpers.openai_helpers import (
    CompatAssistantMessage,
    CompatFunctionCall,
    CompatToolCall,
)
from api.models import Agent, Dataset, Message, OpenAIUsage, Table, Task
from api.schema_ledger import (
    LEVEL_FIELDS,
    LEVEL_KEYS,
    dwc_lookups,
    lookup_is_covered,
    plan_dwc_lookup,
    render_dwc_ledger,
    render_field_details,
    render_table_manifest,
    result_is_progress,
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


class AgentFixtureMixin:
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


class SchemaLedgerAgentTests(AgentFixtureMixin, TestCase):
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

        # Every looked-up field survives history compaction in the per-turn ledger.
        context = self.agent.lookup_ledger_text()
        self.assertIn("DWC-DP SCHEMA LEDGER", context)
        for table in RUN_524_TABLES:
            for field in get_table_spec(table).fields:
                self.assertIn(field, context, f"{table}.{field} missing from ledger")
        self.assertLess(len(context), 16000)

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
        # Turns are dropped a whole batch at a time: with batches of two, the
        # first turn goes once the third batch opens (turn five).
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant",
            "content": "Thanks. I'll build an illustrative package and label the uncertain parts.",
            "tool_calls": [{"id": "old", "type": "function", "function": {
                "name": "Python", "arguments": json.dumps({"code": "print(1)"}),
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={"role": "tool", "tool_call_id": "old", "content": "1"})
        self.add_read_only_turns(4)

        bounded = [message.openai_obj for message in self.agent.messages_for_model()]
        self.assertNotIn("old", [obj.get("tool_call_id") for obj in bounded])
        kept_text = [
            obj for obj in bounded
            if obj.get("role") == "assistant" and "illustrative package" in (obj.get("content") or "")
        ]
        self.assertEqual(len(kept_text), 1)
        self.assertNotIn("tool_calls", kept_text[0])


class VerificationLoopTests(AgentFixtureMixin, TestCase):
    """Dataset 525: after a valid package, the agent alternated read-only Python
    checks with no-op ValidateDwcDp calls, which used to reset the limit."""

    def add_validation_turn(self):
        call_id = f"validate-{self.agent.message_set.count()}"
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "ValidateDwcDp", "arguments": json.dumps({"agent_id": self.agent.id}),
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"valid": True, "unchanged_since_last_validation": True}),
        })

    @override_settings(AGENT_NO_PROGRESS_WARN_TURNS=8, AGENT_NO_PROGRESS_STOP_TURNS=15)
    def test_validation_does_not_reset_the_no_progress_count(self):
        for _ in range(2):
            self.add_read_only_turns(7)
            self.add_validation_turn()
        self.assertEqual(self.agent.turns_without_progress(), 16)

        state = self.agent.current_state_update()
        warning = next(line for line in state.splitlines() if line.startswith("NO PROGRESS"))
        self.assertNotIn("ValidateDwcDp", warning)
        self.assertIn("final source coverage report", warning)

        with patch("api.models.create_response_message") as create_response_message_mock:
            messages = self.agent.next_message()
            create_response_message_mock.assert_not_called()
        self.assertTrue(messages[-1].openai_obj.get("no_progress_pause"))

    def test_structure_notes_count_as_progress(self):
        self.add_read_only_turns(10)
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "notes", "type": "function", "function": {
                "name": "SetStructureNotes", "arguments": "{}",
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "tool", "tool_call_id": "notes", "content": "ok",
        })
        self.assertEqual(self.agent.turns_without_progress(), 0)


class DatasetCostLimitTests(AgentFixtureMixin, TestCase):
    def add_cost(self, amount, response_id="cost-1"):
        return OpenAIUsage.objects.create(
            dataset=self.dataset,
            agent=self.agent,
            task_name=self.task.name,
            response_id=response_id,
            estimated_cost_usd=Decimal(amount),
        )

    @override_settings(OPENAI_DATASET_COST_LIMIT_USD=Decimal("2.20"))
    @patch("api.models.create_response_message")
    def test_dataset_at_limit_stops_before_another_model_call(self, create_response_message_mock):
        self.add_cost("2.20")

        messages = self.agent.next_message()

        create_response_message_mock.assert_not_called()
        self.assertTrue(messages[-1].openai_obj.get("cost_limit_pause"))
        self.assertIn("automated processing limit", messages[-1].openai_obj["content"])
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.busy_thinking)

    @override_settings(OPENAI_DATASET_COST_LIMIT_USD=Decimal("2.20"))
    @patch("api.models.create_response_message")
    def test_user_cannot_bypass_dataset_limit_with_continue(self, create_response_message_mock):
        self.add_cost("2.21")
        self.agent.next_message()
        Message.objects.create(
            agent=self.agent,
            openai_obj={"role": "user", "content": "continue"},
        )

        messages = self.agent.next_message()

        create_response_message_mock.assert_not_called()
        self.assertTrue(messages[-1].openai_obj.get("cost_limit_pause"))

    @override_settings(OPENAI_DATASET_COST_LIMIT_USD=Decimal("2.20"))
    @patch("api.models.create_response_message")
    def test_limit_prevents_automatic_no_tool_retry(self, create_response_message_mock):
        def first_call(*_args, **_kwargs):
            self.add_cost("2.21")
            return CompatAssistantMessage(content="I did not choose a tool.")

        create_response_message_mock.side_effect = first_call

        messages = self.agent.next_message()

        self.assertEqual(create_response_message_mock.call_count, 1)
        self.assertTrue(messages[-1].openai_obj.get("cost_limit_pause"))

    @override_settings(OPENAI_DATASET_COST_LIMIT_USD=Decimal("0"))
    @patch("api.models.create_response_message")
    def test_zero_disables_dataset_limit(self, create_response_message_mock):
        self.add_cost("20.00")
        create_response_message_mock.return_value = CompatAssistantMessage(
            content="Done.",
            tool_calls=[CompatToolCall(
                id="complete",
                function=CompatFunctionCall(
                    name="SetAgentTaskToComplete",
                    arguments=json.dumps({"agent_id": self.agent.id}),
                ),
            )],
        )

        self.agent.next_message()

        create_response_message_mock.assert_called_once()


def tool_turn(call_id, name, args, result):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]},
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


class ArtifactProgressTests(SimpleTestCase):
    """Dataset 526: a no-op ExportDwcDp between term lookups reset the limit."""

    def entries(self, *turns):
        t0 = datetime.datetime(2026, 9, 24, 14, 37)
        entries = [(t0, {"role": "user", "content": "go"})]
        for index, (name, result) in enumerate(turns, start=1):
            clock = t0 + datetime.timedelta(seconds=index)
            for obj in tool_turn(f"call-{index}", name, {}, result):
                entries.append((clock, obj))
        return entries

    def test_only_artifact_changes_count_as_progress(self):
        lookup = ("GetDwCExtensionInfo", "Projection guidance: ...")
        cases = {
            "DwC-DP successfully created and uploaded: https://x/dp.tar.gz": 0,
            "Nothing has changed since the last successful export (same tables, mapping, and metadata), so it was not re-exported.": 3,
            "DwC-DP validation failed; package was not exported:\n{}": 3,
        }
        for export_result, expected in cases.items():
            entries = self.entries(lookup, ("ExportDwcDp", export_result))
            self.assertEqual(turns_since_progress(entries + self.entries(lookup)[1:]), expected, export_result)

        rejected = json.dumps({"status": "correction_required"})
        self.assertEqual(turns_since_progress(self.entries(lookup, ("UploadDwCA", rejected), lookup)), 3)
        uploaded = "DwCA successfully created and uploaded: https://x/dwca.zip"
        self.assertEqual(turns_since_progress(self.entries(lookup, ("UploadDwCA", uploaded))), 0)

    def test_result_is_progress(self):
        self.assertTrue(result_is_progress("SetStructureNotes", "ok"))
        self.assertFalse(result_is_progress("SetStructureNotes", "ERROR CALLING FUNCTION: bad"))
        self.assertFalse(result_is_progress("GetDarwinCoreInfo", "Darwin Core term lookup results:"))


class DwcTermLedgerTests(SimpleTestCase):
    def setUp(self):
        self.ref = DwcTermReference()

    def lookups(self, *calls):
        objs = []
        for index, (name, args, result) in enumerate(calls):
            objs += tool_turn(f"call-{index}", name, args, result)
        return dwc_lookups(objs, self.ref)

    def test_repeat_extension_terms_run_only_what_is_new(self):
        lookups = self.lookups(
            ("GetDwCExtensionInfo", {"extension": "dna_derived_data", "terms": ["DNA_sequence", "seq_meth"]}, "Projection guidance:"),
        )
        args, prefix = plan_dwc_lookup(
            lookups, "GetDwCExtensionInfo",
            {"extension": "DNA Derived Data", "terms": ["dna_sequence", "sop"]}, self.ref,
        )
        self.assertEqual(args["terms"], ["sop"])
        self.assertIn("not repeated: dna_sequence", prefix)

        args, prefix = plan_dwc_lookup(
            lookups, "GetDwCExtensionInfo", {"extension": "dna_derived_data", "terms": ["seq_meth"]}, self.ref,
        )
        self.assertIsNone(args)
        self.assertTrue(prefix.startswith("Not re-run"))

        args, prefix = plan_dwc_lookup(lookups, "GetDwCExtensionInfo", {"extension": "dna_derived_data"}, self.ref)
        self.assertIsNone(args)
        self.assertIn("Registered `dna_derived_data` terms:", prefix)

        # An extension not looked up yet runs normally.
        args, prefix = plan_dwc_lookup(lookups, "GetDwCExtensionInfo", {"extension": "occurrence"}, self.ref)
        self.assertEqual((args, prefix), ({"extension": "occurrence"}, ""))

    def test_catalogue_sections_and_terms(self):
        lookups = self.lookups(
            ("GetDwCExtensionInfo", {}, "Darwin Core extension projection catalogue:"),
            ("GetDarwinCoreInfo", {"section": "Event", "max_terms": 5}, "Event (24 terms total):"),
            ("GetDarwinCoreInfo", {"terms": ["basisOfRecord", "notATerm"]}, "Darwin Core term lookup results:"),
        )
        self.assertIsNone(plan_dwc_lookup(lookups, "GetDwCExtensionInfo", {}, self.ref)[0])
        self.assertIsNone(plan_dwc_lookup(lookups, "GetDarwinCoreInfo", {"section": "event", "max_terms": 3}, self.ref)[0])
        # A longer listing than was shown still runs.
        self.assertIsNotNone(plan_dwc_lookup(lookups, "GetDarwinCoreInfo", {"section": "Event"}, self.ref)[0])
        self.assertIsNone(plan_dwc_lookup(lookups, "GetDarwinCoreInfo", {"terms": ["basis_of_record"]}, self.ref)[0])
        # Unknown terms were never recorded, so they are passed through.
        self.assertEqual(
            plan_dwc_lookup(lookups, "GetDarwinCoreInfo", {"terms": ["notATerm"]}, self.ref)[0]["terms"],
            ["notATerm"],
        )
        # Terms listed by the section lookup are covered too.
        event_terms = self.ref.dwc_section("Event")[1][:5]
        self.assertIsNone(plan_dwc_lookup(lookups, "GetDarwinCoreInfo", {"terms": list(event_terms)}, self.ref)[0])

        ledger = render_dwc_ledger(lookups, self.ref)
        self.assertIn("DWC-A TERM LEDGER", ledger)
        self.assertIn("occurrence (event, taxon)", ledger)
        self.assertIn("Event (first 5 terms)", ledger)
        self.assertIn("- basisOfRecord (", ledger)

    def test_failed_lookups_are_not_recorded(self):
        lookups = self.lookups(
            ("GetDwCExtensionInfo", {"extension": "nope"}, "Extension 'nope' not recognised."),
            ("GetDarwinCoreInfo", {"terms": ["basisOfRecord"]}, "ERROR CALLING FUNCTION: boom"),
        )
        self.assertFalse(lookups)


# The lookups agent 2228 (dataset 526) kept repeating after its DwC-DP export.
RUN_526_LOOKUP_TURNS = [
    [("GetDwCExtensionInfo", {"extension": ext}) for ext in [
        "occurrence", "dna_derived_data", "extended_measurement_or_fact",
        "measurement_or_fact", "identification", "humboldt_ecological_inventory",
    ]],
    [
        ("GetDwCExtensionInfo", {"extension": "occurrence", "terms": ["occurrenceID", "eventID", "occurrenceStatus", "scientificName", "taxonRank"]}),
        ("GetDwCExtensionInfo", {"extension": "dna_derived_data", "terms": ["DNA_sequence", "seq_meth", "sop", "pcr_primer_forward"]}),
        ("GetDarwinCoreInfo", {"terms": ["eventID", "parentEventID", "eventDate", "decimalLatitude"]}),
    ],
    [("Python", {"code": "print(1)"})],
    [
        ("GetDwCExtensionInfo", {"extension": "dna_derived_data", "terms": ["DNA_sequence", "seq_meth", "sop"]}),
        ("GetDwCExtensionInfo", {"extension": "occurrence", "terms": ["occurrenceID", "scientificName"]}),
    ],
    [("SetWorkingPlan", {"plan": "Event core; occurrence + dna_derived_data extensions."})],
    [("ValidateDwcDp", {}), ("GetDwCExtensionInfo", {}), ("GetDarwinCoreInfo", {"section": "Event", "max_terms": 50})],
    [("GetDwCExtensionInfo", {"extension": "occurrence", "terms": ["taxonRank", "occurrenceStatus"]})],
    [("GetDwCExtensionInfo", {"extension": "dna_derived_data"})],
    [("GetDarwinCoreInfo", {"terms": ["eventID", "basisOfRecord"]}), ("GetDwCExtensionInfo", {"extension": "dna_derived_data", "terms": ["sop"]})],
    [("Python", {"code": "print(2)"})],
    [("ExportDwcDp", {})],
    [("ValidateDwcDp", {}), ("GetDwCExtensionInfo", {}), ("GetDarwinCoreInfo", {"section": "Event", "max_terms": 80})],
    [("GetDwCExtensionInfo", {"extension": "occurrence", "terms": ["occurrenceID", "occurrenceStatus"]})],
    [("GetDwCExtensionInfo", {"extension": "dna_derived_data", "terms": ["seq_meth"]})],
    [("GetDwCExtensionInfo", {"extension": "measurement_or_fact"})],
]


class PackagePreparationLoopTests(AgentFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.agent.task = Task.objects.create(name=Task.PACKAGE_PREPARATION_TASK, text="Package", order=3)
        self.agent.save()
        self.event = Table.objects.create(
            dataset=self.dataset, title="event", df=pd.DataFrame({"eventID": ["e1"]})
        )

    def mark_exported(self):
        self.dataset.dwc_dp_url = "https://x/dp.tar.gz"
        self.dataset.dwc_dp_validation = {
            "valid": True,
            "resource_fingerprint": _dwc_dp_resource_fingerprint(self.dataset, None),
            "export_fingerprint": "export",
        }
        self.dataset.save()

    def replay(self, turns):
        """Run each turn's tool calls through Agent.run_function, as next_message does."""
        results = {}
        for turn_index, calls in enumerate(turns):
            tool_calls = [{
                "id": f"t{turn_index}-{call_index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            } for call_index, (name, args) in enumerate(calls)]
            Message.objects.create(agent=self.agent, openai_obj={
                "role": "assistant", "content": "", "tool_calls": tool_calls,
            })
            for tool_call, (name, args) in zip(tool_calls, calls):
                if name in {"ExportDwcDp", "ValidateDwcDp"}:
                    result = (
                        "Nothing has changed since the last successful export (same tables, mapping, "
                        "and metadata), so it was not re-exported."
                        if name == "ExportDwcDp"
                        else json.dumps({"valid": True, "unchanged_since_last_validation": True})
                    )
                elif name == "Python":
                    result = "1"
                else:
                    result = self.agent.run_function(SimpleNamespace(
                        name=name, arguments=tool_call["function"]["arguments"],
                    ))
                results[tool_call["id"]] = (name, args, result)
                Message.objects.create(agent=self.agent, openai_obj={
                    "role": "tool", "tool_call_id": tool_call["id"], "content": result,
                })
        return results

    @override_settings(AGENT_NO_PROGRESS_WARN_TURNS=8, AGENT_NO_PROGRESS_STOP_TURNS=15)
    def test_run_526_pattern_is_deduplicated_nudged_and_paused(self):
        self.mark_exported()
        results = self.replay(RUN_526_LOOKUP_TURNS)

        repeats = [
            result for name, _args, result in results.values()
            if name in {"GetDarwinCoreInfo", "GetDwCExtensionInfo"} and result.startswith("Not re-run")
        ]
        self.assertGreaterEqual(len(repeats), 10)
        # The one partly-new request ran only for its new term.
        partial = results["t8-0"][2]
        self.assertTrue(partial.startswith("Already in the DWC-A TERM LEDGER, not repeated: eventID"))
        self.assertIn("- basisOfRecord (", partial.split("\n\n", 1)[1])

        context = self.agent.lookup_ledger_text()
        self.assertIn("DWC-A TERM LEDGER", context)
        self.assertIn("Extension `dna_derived_data`", context)
        self.assertIn("- DNA_sequence:", context)
        self.assertLess(len(context), 16000)
        state = self.agent.current_state_update()
        self.assertIn("- DNA_sequence:", state)
        self.assertEqual(self.agent.turns_without_progress(), 15)
        warning = state[state.index("NO PROGRESS"):]
        self.assertIn("already validated and exported", warning)
        self.assertIn("call UploadDwCA", warning)

        with patch("api.models.create_response_message") as create_response_message_mock:
            messages = self.agent.next_message()
            create_response_message_mock.assert_not_called()
        self.assertTrue(messages[-1].openai_obj.get("no_progress_pause"))

    @override_settings(AGENT_NO_PROGRESS_WARN_TURNS=2, AGENT_NO_PROGRESS_STOP_TURNS=0)
    def test_nudge_asks_for_export_when_tables_changed_since(self):
        self.mark_exported()
        self.event.df = pd.DataFrame({"eventID": ["e1", "e2"]})
        self.event.save()
        self.add_read_only_turns(3)
        warning = self.agent.current_state_update().split("NO PROGRESS", 1)[1]
        self.assertIn("not exported yet", warning)
        self.assertIn("call ExportDwcDp", warning)


class PromptCacheStabilityTests(AgentFixtureMixin, TestCase):
    """Dataset 527: alternate calls were cached only up to the system prompt,
    because history compaction rewrote every older turn every second call."""

    def rendered(self):
        return [json.dumps(message.openai_obj, sort_keys=True) for message in self.agent.messages_for_model()]

    def add_large_turn(self, index):
        call_id = f"big-{index}"
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "Python", "arguments": json.dumps({"code": f"print({index})"}),
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "tool", "tool_call_id": call_id, "content": f"{index}:" + "x" * 6000,
        })

    @override_settings(OPENAI_TOOL_HISTORY_TURNS=8, OPENAI_FULL_TOOL_HISTORY_TURNS=2, OPENAI_COMPACT_TOOL_CHARS=1000)
    def test_each_call_only_rewrites_the_newest_turns(self):
        previous = None
        full_rewrites = 0
        for index in range(26):
            self.add_large_turn(index)
            current = self.rendered()
            if previous is not None:
                shared = 0
                while shared < min(len(previous), len(current)) and previous[shared] == current[shared]:
                    shared += 1
                # Messages are system + user + 2 per turn. Outside a batch drop,
                # everything before the turn leaving the full window is reused.
                if shared <= 2:
                    full_rewrites += 1
                else:
                    self.assertGreaterEqual(shared, len(previous) - 4, index)
            previous = current
        # 26 turns in batches of 8: drops when turns 17 and 25 open new batches.
        self.assertEqual(full_rewrites, 2)

    @override_settings(OPENAI_FULL_TOOL_HISTORY_TURNS=2)
    def test_full_window_and_compaction(self):
        for index in range(5):
            self.add_large_turn(index)
        outputs = [obj["content"] for obj in (m.openai_obj for m in self.agent.messages_for_model()) if obj.get("role") == "tool"]
        self.assertEqual(len(outputs), 5)
        self.assertTrue(all("older tool output compacted" in output for output in outputs[:3]))
        self.assertTrue(all(len(output) > 6000 for output in outputs[3:]))

    @patch("api.models.create_response_message")
    def test_mutable_lookup_and_notes_follow_frozen_prompt(self, create_response_message_mock):
        opening = self.agent.message_set.first().openai_obj["content"]
        self.dataset.structure_notes = "Event hierarchy: 71 parents, 124 samples."
        self.dataset.save()
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "assistant", "content": "", "tool_calls": [{"id": "lk", "type": "function", "function": {
                "name": "GetDwcDpTableInfo", "arguments": json.dumps({"table_name": "event"}),
            }}],
        })
        Message.objects.create(agent=self.agent, openai_obj={
            "role": "tool", "tool_call_id": "lk", "content": GetDwcDpTableInfo(table_name="event").run(),
        })
        create_response_message_mock.return_value = CompatAssistantMessage(content="Done.")
        self.agent.next_message()

        model_messages = create_response_message_mock.call_args_list[0].args[0]
        self.assertEqual(model_messages[0].openai_obj["content"], opening)
        self.assertEqual(model_messages[1].openai_obj["role"], "user")
        state = create_response_message_mock.call_args_list[0].kwargs["additional_input_items"][0]["content"]
        self.assertIn("DWC-DP SCHEMA LEDGER", state)
        self.assertIn("Event hierarchy: 71 parents", state)

    def test_source_change_appends_update_without_rewriting_opening(self):
        opening = self.agent.message_set.first().openai_obj["content"]

        self.dataset.notify_active_agent_of_source_change()

        self.assertEqual(self.agent.message_set.first().openai_obj["content"], opening)
        update = self.agent.message_set.last().openai_obj
        self.assertEqual(update["role"], "system")
        self.assertIn("SOURCE FILE UPDATE", update["content"])
        self.assertIn("No source files are currently attached", update["content"])

        self.dataset.notify_active_agent_of_source_change()
        included_updates = [
            message for message in self.agent.messages_for_model()
            if (message.openai_obj or {}).get('source_update')
        ]
        self.assertEqual(len(included_updates), 1)


class ConcurrentTurnTests(AgentFixtureMixin, TestCase):
    """Dataset 527: two overlapping refresh polls sent the same request twice."""

    @patch("api.models.create_response_message")
    def test_turn_already_claimed_elsewhere_is_not_sent_again(self, create_response_message_mock):
        stale = Agent.objects.get(id=self.agent.id)
        Agent.objects.filter(id=self.agent.id).update(busy_thinking=True)
        result = stale.next_message()
        create_response_message_mock.assert_not_called()
        self.assertEqual(result.openai_obj["role"], "user")
        self.assertTrue(Agent.objects.get(id=self.agent.id).busy_thinking)

    @patch("api.models.create_response_message")
    def test_turn_finished_elsewhere_is_not_sent_again(self, create_response_message_mock):
        stale = Agent.objects.get(id=self.agent.id)

        def other_request_answers():
            # Runs between next_message's first read and its claim.
            Message.objects.create(agent=self.agent, openai_obj={"role": "assistant", "content": "Done."})
            return False

        with patch.object(Agent, "_dataset_cost_limit_reached", side_effect=other_request_answers):
            self.assertIsNone(stale.next_message())
        create_response_message_mock.assert_not_called()
        self.assertFalse(Agent.objects.get(id=self.agent.id).busy_thinking)
