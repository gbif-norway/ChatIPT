import pandas as pd
from django.test import TestCase

from api.models import Agent, Dataset, Table, Task
from api.relational_candidates import (
    find_relational_candidates,
    render_relational_candidate_report,
)


class RelationalCandidateTests(TestCase):
    def make_table(self, dataset, title, data):
        return Table.objects.create(dataset=dataset, title=title, df=pd.DataFrame(data))

    def test_reused_ordered_agents_and_identifiers_are_strong(self):
        dataset = Dataset.objects.create()
        table = self.make_table(dataset, "source", {
            "recordedBy": ["A. One | B. Two", "A. One", "A. One"],
            "recordedByID": ["orcid:1 | orcid:2", "orcid:1", "orcid:1"],
            "scientificName": ["Species a", "Species b", "Species c"],
        })

        candidates = find_relational_candidates([table])
        agent = next(item for item in candidates if item["family"] == "Agent / Agent Role")

        self.assertEqual(agent["strength"], "strong")
        self.assertIn("supplied agent identifiers", agent["rationale"])
        self.assertIn("ordered multi-agent values", agent["rationale"])

    def test_identification_is_strong_with_determiner_or_higher_classification(self):
        dataset = Dataset.objects.create()
        table = self.make_table(dataset, "source", {
            "scientificName": ["Species a", "Species b"],
            "identifiedBy": ["A. Expert", "B. Expert"],
            "family": ["Family a", "Family b"],
            "genus": ["Genus a", "Genus b"],
        })

        candidates = find_relational_candidates([table])
        identification = next(item for item in candidates if item["family"] == "Identification")

        self.assertEqual(identification["strength"], "strong")
        self.assertIn("determination-specific fields", identification["rationale"])
        self.assertIn("supplied higher classification", identification["rationale"])

    def test_one_unstructured_name_is_possible_not_strong(self):
        dataset = Dataset.objects.create()
        table = self.make_table(dataset, "source", {
            "recordedBy": ["A. Observer"],
            "scientificName": ["Species a"],
        })

        candidates = find_relational_candidates([table])
        agent = next(item for item in candidates if item["family"] == "Agent / Agent Role")

        self.assertEqual(agent["strength"], "possible")

    def test_simple_occurrences_have_no_relational_candidates(self):
        dataset = Dataset.objects.create()
        table = self.make_table(dataset, "occurrence", {
            "occurrence_pk": ["1", "2"],
            "occurrenceID": ["occ-1", "occ-2"],
            "scientificName": ["Species a", "Species b"],
            "eventDate": ["2025-01-01", "2025-01-02"],
            "locality": ["North", "South"],
        })

        self.assertEqual(find_relational_candidates([table]), [])
        self.assertIn(
            "No strong or possible dedicated-resource candidates",
            render_relational_candidate_report([table]),
        )

    def test_report_shows_when_candidate_resources_already_exist(self):
        dataset = Dataset.objects.create()
        source = self.make_table(dataset, "source", {
            "identifiedBy": ["A. Expert", "A. Expert", "A. Expert"],
        })
        agent_table = self.make_table(dataset, "agent", {
            "agent_pk": ["agent-1"],
            "preferredAgentName": ["A. Expert"],
        })
        role = self.make_table(dataset, "identification-agent-role", {
            "identification_fk": ["identification-1"],
            "agent_fk": ["agent-1"],
        })

        report = render_relational_candidate_report([source, agent_table, role])

        self.assertIn("current related resources: `agent`, `identification-agent-role`", report)

    def test_dedicated_resources_do_not_create_their_own_candidates(self):
        dataset = Dataset.objects.create()
        identification = self.make_table(dataset, "identification", {
            "identification_pk": ["identification-1"],
            "identificationID": ["source-identification-1"],
            "kingdom": ["Animalia"],
            "family": ["Corvidae"],
        })
        agent = self.make_table(dataset, "agent", {
            "agent_pk": ["agent-1"],
            "agentID": ["https://orcid.org/0000-0000-0000-0001"],
            "preferredAgentName": ["A. Expert"],
        })

        self.assertEqual(find_relational_candidates([identification, agent]), [])

    def test_candidate_report_is_injected_only_for_refinement_agent(self):
        dataset = Dataset.objects.create()
        self.make_table(dataset, "source", {
            "recordedBy": ["A. One", "A. One", "A. One"],
        })
        refinement = Task.objects.create(name="Data validation and refinement", text="Review", order=1)
        transformation = Task.objects.create(name="Data transformation", text="Transform", order=2)
        refinement_agent = Agent.objects.create(dataset=dataset, task=refinement)
        transformation_agent = Agent.objects.create(dataset=dataset, task=transformation)

        refinement_state = refinement_agent.current_state_update()
        transformation_state = transformation_agent.current_state_update()

        self.assertIn("RELATIONAL MODELLING CANDIDATES — REVIEWER ATTENTION", refinement_state)
        self.assertIn("Agent / Agent Role — STRONG evidence", refinement_state)
        self.assertNotIn("RELATIONAL MODELLING CANDIDATES", transformation_state)
