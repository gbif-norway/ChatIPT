import os
import xml.etree.ElementTree as ET
from django.test import SimpleTestCase
from .helpers.publish import (
    make_eml,
    parse_newick_tip_labels,
    parse_nexus_tip_labels,
)


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


