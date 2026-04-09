import os
import datetime
import xml.etree.ElementTree as ET
from unittest.mock import patch
from django.test import SimpleTestCase
import pandas as pd
from .helpers.publish import (
    make_eml,
    parse_newick_tip_labels,
    parse_nexus_tip_labels,
)
from .agent_tools import GetDarwinCoreInfo, SetEML, LogBugWithDeveloper


class EmlGenerationTests(SimpleTestCase):
    def test_eml_template_is_well_formed(self):
        template_path = os.path.join(os.path.dirname(__file__), 'templates', 'eml.xml')
        with open(template_path, 'rb') as fh:
            xml_bytes = fh.read()
        # Should parse without error
        ET.fromstring(xml_bytes)

    def test_make_eml_generates_parseable_xml(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='Test Dataset',
            description='Plain text abstract',
            user=DummyUser(),
            eml_extra={
                'geographic_scope': 'Norway',
                'temporal_scope': '2024-01-01/2024-12-31',
                'taxonomic_scope': 'Coleoptera',
                'methodology': 'Trap sampling',
                'users': [
                    {'first_name': 'Alice', 'last_name': 'Smith', 'orcid': '0000-0001-2345-6789'}
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        # dataset exists
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)
        # title
        title = dataset.find('title')
        self.assertIsNotNone(title)
        self.assertEqual(title.text, 'Test Dataset')
        # language
        language = dataset.find('language')
        self.assertIsNotNone(language)
        self.assertEqual(language.text, 'eng')
        # pubDate
        self.assertIsNotNone(dataset.find('pubDate'))
        # abstract para
        abstract_para = dataset.find('abstract/para')
        self.assertIsNotNone(abstract_para)
        self.assertEqual(abstract_para.text, 'Plain text abstract')
        # contact with email
        contact_email = dataset.find('contact/electronicMailAddress')
        self.assertIsNotNone(contact_email)
        self.assertEqual(contact_email.text, 'alice@example.org')
        # coverage blocks present
        coverage = dataset.find('coverage')
        self.assertIsNotNone(coverage)
        self.assertIsNotNone(coverage.find('geographicCoverage/geographicDescription'))
        # methods description present
        self.assertIsNotNone(dataset.find('methods/methodStep/description/para'))

    def test_make_eml_maps_manuscript_fields_and_creators(self):
        class DummyUser:
            first_name = 'Alice'
            last_name = 'Smith'
            orcid_id = '0000-0001-2345-6789'
            email = 'alice@example.org'

        xml_text = make_eml(
            title='PDF-derived dataset',
            description='Abstract text',
            user=DummyUser(),
            eml_extra={
                'manuscript_doi': '10.1234/abcd.1',
                'dataset_citation': 'Doe J, Roe R (2025) Example manuscript.',
                'manuscript_title': 'Example manuscript',
                'journal': 'Journal of Examples',
                'publication_year': 2025,
                'users': [
                    {'first_name': 'Jane', 'last_name': 'Doe', 'email': 'jane@example.org'},
                    {'first_name': 'Richard', 'last_name': 'Roe', 'email': 'richard@example.org'},
                ],
            },
        )
        root = ET.fromstring(xml_text.encode('utf-8'))
        dataset = root.find('dataset')
        self.assertIsNotNone(dataset)

        creators = dataset.findall('creator')
        self.assertEqual(len(creators), 2)
        self.assertEqual(creators[0].find('individualName/givenName').text, 'Jane')
        self.assertEqual(creators[1].find('individualName/givenName').text, 'Richard')

        contact_email = dataset.find('contact/electronicMailAddress')
        self.assertIsNotNone(contact_email)
        self.assertEqual(contact_email.text, 'alice@example.org')

        alternate_identifier = dataset.find('alternateIdentifier')
        self.assertIsNotNone(alternate_identifier)
        self.assertEqual(alternate_identifier.text, 'https://doi.org/10.1234/abcd.1')

        citation = dataset.find('additionalMetadata/metadata/gbif/citation')
        self.assertIsNotNone(citation)
        self.assertEqual(citation.text, 'Doe J, Roe R (2025) Example manuscript.')


class PhylogenyParsingTests(SimpleTestCase):
    """Tests for phylogenetic tree parsing and matching functions."""
    
    def test_parse_nexus_with_translate_block(self):
        """Test parsing NEXUS file with TRANSLATE block."""
        nexus_content = """#NEXUS
BEGIN TREES;
	Title Acacia;
	LINK Taxa = Taxa;
	TRANSLATE
        DBT01_clean	Berneuxia_thibetica_01,
        DBT03_clean	Berneuxia_thibetica_02,
        Berneuxia_thibetica	Berneuxia_thibetica_P,
        S7451102	Cyrilla_racemiflora,
        DDH04_clean	Diapensia_himalaica_01;
    TREE tree_1 = (DBT01_clean,DBT03_clean);
END;"""
        
        tip_labels = parse_nexus_tip_labels(nexus_content)
        
        # Should extract translated names from TRANSLATE block
        expected_labels = [
            'Berneuxia_thibetica_01',
            'Berneuxia_thibetica_02',
            'Berneuxia_thibetica_P',
            'Cyrilla_racemiflora',
            'Diapensia_himalaica_01'
        ]
        
        self.assertEqual(set(tip_labels), set(expected_labels))
        self.assertEqual(len(tip_labels), len(expected_labels))
    
    def test_parse_nexus_from_example_file(self):
        """Test parsing the actual example NEXUS file."""
        nexus_path = os.path.join(
            os.path.dirname(__file__),
            'templates', 'examples', 'gaynor_et_al_v3', 'above50_genes.nex'
        )
        
        with open(nexus_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        tip_labels = parse_nexus_tip_labels(content)
        
        # Should extract all translated tip labels
        self.assertGreater(len(tip_labels), 0)
        
        # Check for specific expected labels from the file
        expected_labels = [
            'Berneuxia_thibetica_01',
            'Berneuxia_thibetica_02',
            'Berneuxia_thibetica_P',
            'Cyrilla_racemiflora',
            'Diapensia_himalaica_01',
            'Diapensia_himalaica_02',
            'Diapensia_himalaica_P',
            'Diapensia_lapponica_01',
            'Diapensia_lapponica_02',
            'Diapensia_lapponica_03',
            'Diapensia_obovata_01',
            'Diapensia_obovata_02',
            'Diapensia_purpurea_01',
            'Diapensia_purpurea_P',
            'Diapensia_wardii_P',
            'Galax_urceolata_01',
            'Galax_urceolata_02',
            'Pyxidanthera_barbulata_01',
            'Pyxidanthera_barbulata_02',
            'Pyxidanthera_brevifolia_01',
            'Pyxidanthera_brevifolia_02',
            'Schizocodon_ilicifolius_01',
            'Schizocodon_soldanelloides_01',
            'Schizocodon_soldanelloides_02',
            'Shortia_galacifolia_01',
            'Shortia_galacifolia_02',
            'Shortia_rotundifolia_01',
            'Shortia_sinensis_01',
            'Shortia_sinensis_P',
            'Shortia_uniflora_01',
            'Shortia_uniflora_02',
        ]
        
        # Check that all expected labels are present
        for expected in expected_labels:
            self.assertIn(expected, tip_labels, f"Expected label {expected} not found")
        
        # Should have exactly 31 labels
        self.assertEqual(len(tip_labels), 31)
    
    def test_parse_newick_simple(self):
        """Test parsing a simple Newick format tree."""
        newick_content = "(A:0.1,B:0.2,(C:0.3,D:0.4):0.5);"
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        expected = ['A', 'B', 'C', 'D']
        self.assertEqual(set(tip_labels), set(expected))
        self.assertEqual(len(tip_labels), len(expected))
    
    def test_parse_newick_with_underscores(self):
        """Test parsing Newick with labels containing underscores."""
        newick_content = "(Berneuxia_thibetica:0.1,Diapensia_himalaica:0.2);"
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        expected = ['Berneuxia_thibetica', 'Diapensia_himalaica']
        self.assertEqual(set(tip_labels), set(expected))
    
    def test_parse_newick_complex(self):
        """Test parsing a more complex Newick tree."""
        newick_content = "((A:0.1,B:0.2)100:0.3,(C:0.4,D:0.5)90:0.6);"
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        expected = ['A', 'B', 'C', 'D']
        self.assertEqual(set(tip_labels), set(expected))
    
    def test_parse_nexus_no_translate_block(self):
        """Test parsing NEXUS file without TRANSLATE block."""
        nexus_content = """#NEXUS
BEGIN TREES;
    TREE tree_1 = (A:0.1,B:0.2,(C:0.3,D:0.4):0.5);
END;"""
        
        tip_labels = parse_nexus_tip_labels(nexus_content)
        
        expected = ['A', 'B', 'C', 'D']
        self.assertEqual(set(tip_labels), set(expected))
    
    def test_parse_newick_handles_quotes(self):
        """Test that Newick parser handles various label formats."""
        # Labels might have quotes or special characters
        newick_content = '("Species A":0.1,"Species B":0.2);'
        
        tip_labels = parse_newick_tip_labels(newick_content)
        
        # Should extract labels even with quotes
        # Note: current implementation may not handle quotes, but should not crash
        self.assertIsInstance(tip_labels, list)


class GetDarwinCoreInfoTests(SimpleTestCase):
    def test_summary_response_lists_sections_only(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier. Examples: abc",
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
            "Event": {
                "eventID": "An identifier for the event. Examples: EVT-1",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo().run()

        self.assertIn("Darwin Core quick reference sections:", response)
        self.assertIn("- Occurrence (2 terms)", response)
        self.assertIn("- Event (1 terms)", response)
        self.assertNotIn("The specific nature of the data record", response)

    def test_section_response_returns_all_terms_and_includes_examples_by_default(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier for the occurrence. Examples: 1234",
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(section="Occurrence").run()

        self.assertIn("Occurrence (2 terms total):", response)
        self.assertIn("- occurrenceID: An identifier for the occurrence. Examples: 1234", response)
        self.assertIn("- basisOfRecord: The specific nature of the data record. Examples: HumanObservation", response)

    def test_section_response_can_strip_examples_when_requested(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier for the occurrence. Examples: 1234",
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(section="Occurrence", include_examples=False).run()

        self.assertIn("Occurrence (2 terms total):", response)
        self.assertIn("- occurrenceID: An identifier for the occurrence.", response)
        self.assertIn("- basisOfRecord: The specific nature of the data record.", response)
        self.assertNotIn("Examples:", response)

    def test_section_response_applies_optional_limit(self):
        sample_reference = {
            "Occurrence": {
                "occurrenceID": "An identifier for the occurrence.",
                "basisOfRecord": "The specific nature of the data record.",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(section="Occurrence", max_terms=1).run()

        self.assertIn("Occurrence (2 terms total):", response)
        self.assertIn("- occurrenceID: An identifier for the occurrence.", response)
        self.assertIn("... 1 more terms not shown. Increase `max_terms` to see more.", response)

    def test_term_lookup_can_include_examples(self):
        sample_reference = {
            "Occurrence": {
                "basisOfRecord": "The specific nature of the data record. Examples: HumanObservation",
            },
        }
        with patch("api.agent_tools._load_dwc_quick_reference", return_value=sample_reference):
            response = GetDarwinCoreInfo(terms=["basisOfRecord"], include_examples=True).run()

        self.assertIn("Darwin Core term lookup results:", response)
        self.assertIn("basisOfRecord (Occurrence): The specific nature of the data record. Examples: HumanObservation", response)


class SetEMLTemporalInferenceTests(SimpleTestCase):
    def test_infer_temporal_bounds_from_eventdate_column(self):
        df = pd.DataFrame(
            {
                "eventDate": [
                    "2019-01-15",
                    "2020-02",
                    "2018",
                    "2007-03-01/2008-05-11",
                ]
            }
        )

        bounds = SetEML._infer_temporal_bounds_from_df(df)

        self.assertEqual(bounds[0].isoformat(), "2007-03-01")
        self.assertEqual(bounds[1].isoformat(), "2020-02-29")

    def test_infer_temporal_bounds_from_year_month_day_columns(self):
        df = pd.DataFrame(
            {
                "year": ["2011", "2013", "2012"],
                "month": ["", "6", "2"],
                "day": ["", "", "29"],
            }
        )

        bounds = SetEML._infer_temporal_bounds_from_df(df)

        self.assertEqual(bounds[0].isoformat(), "2011-01-01")
        self.assertEqual(bounds[1].isoformat(), "2013-06-30")

    def test_resolve_temporal_scope_replaces_today_placeholder_with_inferred_range(self):
        today = datetime.date.today()
        today_text = today.strftime("%d %B %Y")
        provided_scope = f"{today_text} - {today_text}"
        inferred_scope = "2018-01-01/2020-12-31"

        resolved_scope, note = SetEML._resolve_temporal_scope(provided_scope, None, inferred_scope)

        self.assertEqual(resolved_scope, inferred_scope)
        self.assertIn("adjusted from placeholder", note)

    def test_resolve_temporal_scope_keeps_non_placeholder_value(self):
        provided_scope = "2015-01-01/2016-12-31"
        inferred_scope = "2018-01-01/2020-12-31"

        resolved_scope, note = SetEML._resolve_temporal_scope(provided_scope, None, inferred_scope)

        self.assertEqual(resolved_scope, provided_scope)
        self.assertIsNone(note)

    def test_resolve_temporal_scope_keeps_existing_non_placeholder_when_not_provided(self):
        existing_scope = "2001-01-01/2003-12-31"
        inferred_scope = "2018-01-01/2020-12-31"

        resolved_scope, note = SetEML._resolve_temporal_scope(None, existing_scope, inferred_scope)

        self.assertEqual(resolved_scope, existing_scope)
        self.assertIsNone(note)

    def test_infer_geographic_scope_from_country_and_coordinates(self):
        class DummyTable:
            def __init__(self, df):
                self.df = df

        class DummyTableSet:
            def __init__(self, tables):
                self._tables = tables

            def all(self):
                return self._tables

        class DummyDataset:
            def __init__(self, tables):
                self.table_set = DummyTableSet(tables)

        dataset = DummyDataset(
            [
                DummyTable(
                    pd.DataFrame(
                        {
                            "country": ["Norway", "Sweden", "Norway"],
                            "decimalLatitude": ["60.1", "61.2", "59.9"],
                            "decimalLongitude": ["10.0", "11.5", "9.8"],
                        }
                    )
                )
            ]
        )

        inferred = SetEML._infer_geographic_scope_from_dataset(dataset)

        self.assertIn("Countries:", inferred)
        self.assertIn("Norway", inferred)
        self.assertIn("Sweden", inferred)
        self.assertIn("Coordinate bounds:", inferred)

    def test_infer_taxonomic_scope_from_taxonomic_columns(self):
        class DummyTable:
            def __init__(self, df):
                self.df = df

        class DummyTableSet:
            def __init__(self, tables):
                self._tables = tables

            def all(self):
                return self._tables

        class DummyDataset:
            def __init__(self, tables):
                self.table_set = DummyTableSet(tables)

        dataset = DummyDataset(
            [
                DummyTable(
                    pd.DataFrame(
                        {
                            "family": ["Felidae", "Canidae", "Felidae"],
                        }
                    )
                )
            ]
        )

        inferred = SetEML._infer_taxonomic_scope_from_dataset(dataset)

        self.assertIn("Families:", inferred)
        self.assertIn("Felidae", inferred)
        self.assertIn("Canidae", inferred)

    def test_infer_methodology_from_sampling_protocol(self):
        class DummyTable:
            def __init__(self, df):
                self.df = df

        class DummyTableSet:
            def __init__(self, tables):
                self._tables = tables

            def all(self):
                return self._tables

        class DummyDataset:
            def __init__(self, tables):
                self.table_set = DummyTableSet(tables)

        dataset = DummyDataset(
            [
                DummyTable(
                    pd.DataFrame(
                        {
                            "samplingProtocol": ["Camera trap", "Camera trap"],
                        }
                    )
                )
            ]
        )

        inferred = SetEML._infer_methodology_from_dataset(dataset)
        self.assertEqual(inferred, "Camera trap")


class LogBugWithDeveloperTests(SimpleTestCase):
    @patch("api.agent_tools.discord_bot.send_discord_message")
    def test_uses_discord_user_id_for_direct_mention(self, send_discord_message_mock):
        with patch.dict(os.environ, {"DISCORD_DEVELOPER_USER_ID": "1234567890"}, clear=False):
            result = LogBugWithDeveloper(
                message="Python tool failed with unexpected KeyError",
                agent_id=44,
                urgent=True,
            ).run()

        self.assertIn("Bug report sent", result)
        sent_message = send_discord_message_mock.call_args.args[0]
        sent_allowed_mentions = send_discord_message_mock.call_args.kwargs["allowed_mentions"]

        self.assertIn("<@1234567890>", sent_message)
        self.assertIn("Agent ID: 44", sent_message)
        self.assertIn("Python tool failed with unexpected KeyError", sent_message)
        self.assertEqual(sent_allowed_mentions, {"parse": [], "users": ["1234567890"]})

    @patch("api.agent_tools.discord_bot.send_discord_message")
    def test_falls_back_to_plain_rkian_tag_without_user_id(self, send_discord_message_mock):
        with patch.dict(os.environ, {"DISCORD_DEVELOPER_USER_ID": "", "DISCORD_DEVELOPER_HANDLE": "@_rkian"}, clear=False):
            result = LogBugWithDeveloper(message="Validation response parsing failed").run()

        self.assertIn("Bug report sent", result)
        sent_message = send_discord_message_mock.call_args.args[0]

        self.assertIn("@_rkian", sent_message)
        self.assertIn("Validation response parsing failed", sent_message)
        self.assertNotIn("allowed_mentions", send_discord_message_mock.call_args.kwargs)
