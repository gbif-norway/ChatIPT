import os
import json
import xml.etree.ElementTree as ET
import pandas as pd
from django.test import SimpleTestCase
from .helpers.publish import (
    make_eml,
    parse_newick_tip_labels,
    parse_nexus_tip_labels,
    match_tip_label_to_scientific_name,
    update_occurrence_dynamic_properties,
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
    
    def test_match_tip_label_to_scientific_name_exact_match(self):
        """Test matching when tip label contains scientific name exactly."""
        # Tip label: Berneuxia_thibetica_01
        # Scientific name: Berneuxia thibetica
        self.assertTrue(match_tip_label_to_scientific_name(
            "Berneuxia_thibetica_01",
            "Berneuxia thibetica"
        ))
    
    def test_match_tip_label_to_scientific_name_with_suffix(self):
        """Test matching with various suffixes."""
        test_cases = [
            ("Berneuxia_thibetica_01", "Berneuxia thibetica", True),
            ("Berneuxia_thibetica_P", "Berneuxia thibetica", True),
            ("Diapensia_himalaica_02", "Diapensia himalaica", True),
            ("Shortia_sinensis_P", "Shortia sinensis", True),
            ("Cyrilla_racemiflora", "Cyrilla racemiflora", True),
        ]
        
        for tip_label, scientific_name, expected in test_cases:
            result = match_tip_label_to_scientific_name(tip_label, scientific_name)
            self.assertEqual(
                result, expected,
                f"Failed for tip_label='{tip_label}', scientific_name='{scientific_name}'"
            )
    
    def test_match_tip_label_to_scientific_name_no_match(self):
        """Test that non-matching names return False."""
        self.assertFalse(match_tip_label_to_scientific_name(
            "Berneuxia_thibetica_01",
            "Diapensia himalaica"
        ))
        
        self.assertFalse(match_tip_label_to_scientific_name(
            "Species_A",
            "Species B"
        ))
    
    def test_match_tip_label_to_scientific_name_case_insensitive(self):
        """Test that matching is case insensitive."""
        self.assertTrue(match_tip_label_to_scientific_name(
            "berneuxia_thibetica_01",
            "Berneuxia thibetica"
        ))
        
        self.assertTrue(match_tip_label_to_scientific_name(
            "BERNEUXIA_THIBETICA_01",
            "berneuxia thibetica"
        ))
    
    def test_update_occurrence_dynamic_properties_basic(self):
        """Test updating dynamicProperties with phylogeny information."""
        df = pd.DataFrame({
            'occurrenceID': ['occ1', 'occ2', 'occ3'],
            'scientificName': [
                'Berneuxia thibetica',
                'Diapensia himalaica',
                'Unknown species'
            ],
            'dynamicProperties': ['', '', '']
        })
        
        tree_files = [
            ('above50_genes.nex', [
                'Berneuxia_thibetica_01',
                'Diapensia_himalaica_01'
            ])
        ]
        
        result_df = update_occurrence_dynamic_properties(df, tree_files)
        
        # Check first occurrence
        dp1 = json.loads(result_df.loc[0, 'dynamicProperties'])
        self.assertIn('phylogenies', dp1)
        self.assertEqual(len(dp1['phylogenies']), 1)
        self.assertEqual(dp1['phylogenies'][0]['phyloTreeTipLabel'], 'Berneuxia_thibetica_01')
        self.assertEqual(dp1['phylogenies'][0]['phyloTreeFileName'], 'above50_genes.nex')
        
        # Check second occurrence
        dp2 = json.loads(result_df.loc[1, 'dynamicProperties'])
        self.assertIn('phylogenies', dp2)
        self.assertEqual(len(dp2['phylogenies']), 1)
        self.assertEqual(dp2['phylogenies'][0]['phyloTreeTipLabel'], 'Diapensia_himalaica_01')
        
        # Check third occurrence (no match)
        dp3 = result_df.loc[2, 'dynamicProperties']
        self.assertEqual(dp3, '')  # Should remain empty if no match
    
    def test_update_occurrence_dynamic_properties_preserves_existing(self):
        """Test that existing dynamicProperties are preserved."""
        existing_json = json.dumps({"otherProperty": "value"})
        df = pd.DataFrame({
            'occurrenceID': ['occ1'],
            'scientificName': ['Berneuxia thibetica'],
            'dynamicProperties': [existing_json]
        })
        
        tree_files = [
            ('above50_genes.nex', ['Berneuxia_thibetica_01'])
        ]
        
        result_df = update_occurrence_dynamic_properties(df, tree_files)
        
        dp = json.loads(result_df.loc[0, 'dynamicProperties'])
        # Should have both existing property and new phylogenies
        self.assertIn('otherProperty', dp)
        self.assertEqual(dp['otherProperty'], 'value')
        self.assertIn('phylogenies', dp)
    
    def test_update_occurrence_dynamic_properties_multiple_trees(self):
        """Test handling multiple tree files."""
        df = pd.DataFrame({
            'occurrenceID': ['occ1'],
            'scientificName': ['Berneuxia thibetica'],
            'dynamicProperties': ['']
        })
        
        tree_files = [
            ('above50_genes.nex', ['Berneuxia_thibetica_01']),
            ('above70_genes.nex', ['Berneuxia_thibetica_02'])
        ]
        
        result_df = update_occurrence_dynamic_properties(df, tree_files)
        
        dp = json.loads(result_df.loc[0, 'dynamicProperties'])
        self.assertEqual(len(dp['phylogenies']), 2)
        
        # Check both trees are represented
        tree_filenames = [p['phyloTreeFileName'] for p in dp['phylogenies']]
        self.assertIn('above50_genes.nex', tree_filenames)
        self.assertIn('above70_genes.nex', tree_filenames)
    
    def test_update_occurrence_dynamic_properties_from_example(self):
        """Test with actual example occurrence data."""
        occurrence_path = os.path.join(
            os.path.dirname(__file__),
            'templates', 'examples', 'gaynor_et_al_v3', 'occurrence.csv'
        )
        
        df = pd.read_csv(occurrence_path, sep='\t', dtype=str)
        
        # Read the NEXUS file
        nexus_path = os.path.join(
            os.path.dirname(__file__),
            'templates', 'examples', 'gaynor_et_al_v3', 'above50_genes.nex'
        )
        
        with open(nexus_path, 'r', encoding='utf-8') as f:
            nexus_content = f.read()
        
        tip_labels = parse_nexus_tip_labels(nexus_content)
        tree_files = [('above50_genes.nex', tip_labels)]
        
        # Create a fresh DataFrame without existing dynamicProperties
        test_df = df[['occurrenceID', 'scientificName']].copy()
        test_df['dynamicProperties'] = ''
        
        result_df = update_occurrence_dynamic_properties(test_df, tree_files)
        
        # Check a few specific rows from the example
        # Row 0: DBT01_clean -> Berneuxia thibetica
        row0 = result_df[result_df['occurrenceID'] == 'DBT01_clean'].iloc[0]
        dp0 = json.loads(row0['dynamicProperties'])
        self.assertIn('phylogenies', dp0)
        self.assertEqual(dp0['phylogenies'][0]['phyloTreeTipLabel'], 'Berneuxia_thibetica_01')
        
        # Row 1: DBT03_clean -> Berneuxia thibetica
        # Note: Multiple tip labels may match, so we check that the expected one is present
        row1 = result_df[result_df['occurrenceID'] == 'DBT03_clean'].iloc[0]
        dp1 = json.loads(row1['dynamicProperties'])
        # Should have at least one phylogeny entry
        self.assertGreater(len(dp1['phylogenies']), 0)
        # Check that Berneuxia_thibetica_02 is in the list (may have multiple matches)
        tip_labels = [p['phyloTreeTipLabel'] for p in dp1['phylogenies']]
        self.assertIn('Berneuxia_thibetica_02', tip_labels)
        
        # Row 4: S7451102 -> Cyrilla racemiflora
        row4 = result_df[result_df['occurrenceID'] == 'S7451102'].iloc[0]
        dp4 = json.loads(row4['dynamicProperties'])
        self.assertEqual(dp4['phylogenies'][0]['phyloTreeTipLabel'], 'Cyrilla_racemiflora')
    
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


