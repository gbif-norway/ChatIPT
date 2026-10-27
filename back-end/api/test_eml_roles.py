"""Source metadata roles must not change the publishing account's identity."""
import xml.etree.ElementTree as ET

from django.test import TestCase

from api.agent_tools import SetEML
from api.dwc_dp_specs import validate_eml
from api.helpers.publish import (
    PROVISIONAL_EML_NOTE,
    UNDOCUMENTED_PERSON_POSITION,
    UNKNOWN_CREATOR_POSITION,
    EmlExportError,
    make_eml,
)
from api.models import Agent, CustomUser, Dataset, Task


class EmlSourceRoleTests(TestCase):
    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            username="metadata-uploader",
            first_name="Ada",
            last_name="Example-Owner",
            email="0000-0002-1825-0097@orcid.org",
            orcid_id="0000-0002-1825-0097",
        )
        self.dataset = Dataset.objects.create(
            user=self.owner, title="Source metadata", description="Dataset description",
        )
        task = Task.objects.create(name="Metadata", text="Set metadata", order=1)
        self.agent = Agent.objects.create(dataset=self.dataset, task=task)
        self.people = [
            {"first_name": "Alex", "last_name": "Sample", "email": "alex.sample@example.org"},
            {"first_name": "Jamie Lee", "last_name": "Di Test", "email": "jamie.ditest@example.org"},
        ]

    def render(self):
        self.dataset.refresh_from_db()
        xml = make_eml(
            self.dataset.title, self.dataset.description,
            user=self.owner, eml_extra=self.dataset.eml,
        )
        self.assertEqual(validate_eml(xml.encode("utf-8")), [])
        return ET.fromstring(xml).find("dataset")

    def assert_people(self, dataset, tag, people):
        self.assertEqual(
            [(node.findtext("individualName/surName"), node.findtext("electronicMailAddress"))
             for node in dataset.findall(tag)],
            [(person["last_name"], person["email"]) for person in people],
        )

    def test_workbook_people_export_in_all_roles_without_changing_owner(self):
        SetEML(
            agent_id=self.agent.id, users=self.people,
            contacts=self.people, metadata_providers=self.people,
        ).run()
        dataset = self.render()
        for tag in ("creator", "contact", "metadataProvider"):
            self.assert_people(dataset, tag, self.people)
        self.assertEqual(dataset.findall(".//userId"), [])
        self.owner.refresh_from_db()
        self.assertEqual(
            (self.owner.first_name, self.owner.last_name, self.owner.email, self.owner.orcid_id),
            ("Ada", "Example-Owner", "0000-0002-1825-0097@orcid.org", "0000-0002-1825-0097"),
        )

    def test_roles_are_independent_and_omitted_or_null_values_preserve_them(self):
        SetEML(
            agent_id=self.agent.id, users=[self.people[0]],
            contacts=[self.people[1]], metadata_providers=self.people,
        ).run()
        SetEML(agent_id=self.agent.id).run()
        SetEML(agent_id=self.agent.id, contacts=None, metadata_providers=None).run()
        dataset = self.render()
        self.assert_people(dataset, "creator", [self.people[0]])
        self.assert_people(dataset, "contact", [self.people[1]])
        self.assert_people(dataset, "metadataProvider", self.people)

    def test_empty_lists_restore_each_account_default_independently(self):
        SetEML(
            agent_id=self.agent.id, users=self.people,
            contacts=self.people, metadata_providers=self.people,
        ).run()
        for key, tag, other_tag in (
            ("contacts", "contact", "metadataProvider"),
            ("metadata_providers", "metadataProvider", "contact"),
        ):
            with self.subTest(key=key):
                SetEML(agent_id=self.agent.id, contacts=self.people, metadata_providers=self.people).run()
                SetEML(agent_id=self.agent.id, **{key: []}).run()
                dataset = self.render()
                self.assertEqual(len(dataset.findall(tag)), 1)
                self.assertEqual(dataset.findtext(f"{tag}/individualName/surName"), "Example-Owner")
                self.assertIsNone(dataset.find(f"{tag}/electronicMailAddress"))
                self.assert_people(dataset, other_tag, self.people)
                self.assert_people(dataset, "creator", self.people)
                self.assertNotIn(key, self.dataset.eml)

    def test_unknown_creators_can_have_source_contacts_and_metadata_providers(self):
        SetEML(
            agent_id=self.agent.id, creators_unknown=True,
            contacts=self.people, metadata_providers=self.people,
        ).run()
        dataset = self.render()
        self.assertEqual(len(dataset.findall("creator")), 1)
        self.assertEqual(dataset.findtext("creator/positionName"), "Unknown creator")
        for tag in ("contact", "metadataProvider"):
            self.assert_people(dataset, tag, self.people)

    def test_source_contact_without_email_exports_and_validates(self):
        source_contact = {"first_name": "Taylor", "last_name": "Field", "orcid": None}
        SetEML(
            agent_id=self.agent.id,
            contacts=[source_contact],
            metadata_providers=[source_contact],
        ).run()

        dataset = self.render()
        for tag in ("contact", "metadataProvider"):
            person = dataset.find(tag)
            self.assertEqual(person.findtext("individualName/surName"), "Field")
            self.assertIsNone(person.find("electronicMailAddress"))

    def test_placeholder_email_is_saved_and_exported_for_all_roles(self):
        for role, tag in (("users", "creator"), ("contacts", "contact"),
                          ("metadata_providers", "metadataProvider")):
            with self.subTest(role=role):
                SetEML(
                    agent_id=self.agent.id,
                    **{role: [{
                        "first_name": "Taylor", "last_name": "Field",
                        "email": "unknown@example.org",
                    }]},
                ).run()
                dataset = self.render()
                self.assertEqual(self.dataset.eml[role][0]["email"], "unknown@example.org")
                self.assertEqual(dataset.findtext(f"{tag}/electronicMailAddress"), "unknown@example.org")

    def test_saved_contact_email_fills_first_source_contact_without_email(self):
        SetEML(
            agent_id=self.agent.id,
            contacts=[{"first_name": "Taylor", "last_name": "Field"}],
        ).run()
        self.dataset.refresh_from_db()
        self.dataset.eml["contact_email"] = "taylor.field@example.org"
        self.dataset.save(update_fields=["eml"])

        dataset = self.render()

        self.assertEqual(dataset.findtext("contact/electronicMailAddress"), "taylor.field@example.org")

    def test_source_orcids_are_normalized_and_missing_surnames_block_export(self):
        for key in ("contacts", "metadata_providers"):
            with self.subTest(key=key):
                person = {**self.people[0], "orcid": "https://orcid.org/0000-0002-1825-0097"}
                SetEML(agent_id=self.agent.id, **{key: [person]}).run()
                self.dataset.refresh_from_db()
                self.assertEqual(self.dataset.eml[key][0]["orcid"], "0000-0002-1825-0097")
                self.render()
                result = SetEML(agent_id=self.agent.id, **{key: [{**person, "last_name": "[unknown]"}]}).run()
                self.assertIn(f"{key}: Alex", result)
                self.dataset.refresh_from_db()
                with self.assertRaises(EmlExportError):
                    make_eml("Title", "Description", user=self.owner, eml_extra=self.dataset.eml)
                SetEML(agent_id=self.agent.id, **{key: []}).run()

    def test_changing_source_roles_invalidates_existing_archives(self):
        SetEML(agent_id=self.agent.id).run()
        for key in ("contacts", "metadata_providers"):
            with self.subTest(key=key):
                Dataset.objects.filter(pk=self.dataset.pk).update(
                    dwc_dp_url="https://example.org/package.tar.gz", dwc_dp_validation={"valid": True},
                    dwca_url="https://example.org/archive.zip", dwca_validation={"valid": True},
                )
                SetEML(agent_id=self.agent.id, **{key: self.people}).run()
                self.dataset.refresh_from_db()
                self.assertEqual(self.dataset.dwc_dp_url, "")
                self.assertIsNone(self.dataset.dwc_dp_validation)
                self.assertEqual(self.dataset.dwca_url, "")
                self.assertIsNone(self.dataset.dwca_validation)


class ProvisionalEmlTests(TestCase):
    """Dataset 615: the validation-only archive named "Test User" and granted CC BY 4.0."""

    def render(self, user=None, eml_extra=None):
        xml = make_eml("Title", "Description", user=user, eml_extra=eml_extra, provisional=True)
        self.assertEqual(validate_eml(xml.encode("utf-8")), [])
        return xml, ET.fromstring(xml).find("dataset")

    def test_names_nobody_and_grants_no_licence_by_default(self):
        xml, dataset = self.render()

        self.assertNotIn("<surName>User</surName>", xml)
        self.assertNotIn("creativecommons", xml)
        self.assertIsNone(dataset.find("intellectualRights"))
        self.assertEqual(dataset.findtext("creator/positionName"), UNKNOWN_CREATOR_POSITION)
        for tag in ("metadataProvider", "contact"):
            self.assertEqual(dataset.findtext(f"{tag}/positionName"), UNDOCUMENTED_PERSON_POSITION)
        self.assertEqual(dataset.findtext("additionalInfo/para"), PROVISIONAL_EML_NOTE)

    def test_account_holder_without_a_surname_is_not_named(self):
        user = CustomUser.objects.create_user(username="orcid-only", first_name="Artem")

        _xml, dataset = self.render(user=user)

        self.assertEqual(dataset.findtext("contact/positionName"), UNDOCUMENTED_PERSON_POSITION)
        self.assertIsNone(dataset.find("contact/individualName"))

    def test_saved_people_and_licence_are_kept(self):
        owner = CustomUser.objects.create_user(username="owner", first_name="Ada", last_name="Owner")
        creator = {"first_name": "Alex", "last_name": "Sample", "email": "alex.sample@example.org"}

        _xml, dataset = self.render(user=owner, eml_extra={"users": [creator], "license": "CC0 1.0"})

        self.assertEqual(dataset.findtext("creator/individualName/surName"), "Sample")
        self.assertEqual(dataset.findtext("contact/individualName/surName"), "Owner")
        self.assertEqual(
            dataset.find("intellectualRights/para/ulink").get("url"),
            "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
        )
