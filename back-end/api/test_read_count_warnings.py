from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yaml
from django.test import SimpleTestCase, TestCase

from api import agent_tools, quality_report
from api.dwc_dp_specs import validate_dwc_dp_resources
from api.models import Dataset, Table
from api.publication_validation import dna_read_warnings


EXAMPLES = Path(__file__).resolve().parent / 'templates' / 'examples'
NON_DETECTIONS = 'non-detection(s) linked to nucleotide analyses'


def otu_package(keep_zero_cells):
    """Melt the example ZOTU table (with its SINTAX taxonomy) into a metabarcoding DwC-DP."""
    matrix = pd.read_csv(EXAMPLES / 'ZOTU  samples.txt', sep='\t').set_index('#OTU ID')
    taxonomy = pd.read_csv(EXAMPLES / 'results_MinION (2).txt', sep='\t', header=None,
                           names=['otu', 'assignment', 'strand', 'cutoff'])
    assignments = taxonomy.set_index(taxonomy['otu'].str.split(';').str[0])['assignment']
    cells = matrix.rename_axis(columns='sample').stack().rename('reads').reset_index()
    cells.columns = ['otu', 'sample', 'reads']
    if not keep_zero_cells:
        cells = cells[cells['reads'] > 0]
    cells = cells.reset_index(drop=True)
    key = cells['sample'] + ':' + cells['otu']
    samples = sorted(cells['sample'].unique())
    otus = sorted(cells['otu'].unique())
    return {
        'event': pd.DataFrame({'event_pk': samples}),
        'material': pd.DataFrame({'materialEntity_pk': samples, 'collectionEvent_fk': samples}),
        'molecular-protocol': pd.DataFrame({'molecularProtocol_pk': ['p'], 'target_gene': ['12S rRNA']}),
        'nucleotide-sequence': pd.DataFrame({'nucleotideSequence_pk': otus, 'sequence': ['ACGT'] * len(otus)}),
        'nucleotide-analysis': pd.DataFrame({
            'nucleotideAnalysis_pk': key, 'event_fk': cells['sample'], 'materialEntity_fk': cells['sample'],
            'nucleotideSequence_fk': cells['otu'], 'molecularProtocol_fk': 'p', 'readCount': cells['reads'],
        }),
        'occurrence': pd.DataFrame({
            'occurrence_pk': key, 'event_fk': cells['sample'],
            'occurrenceStatus': cells['reads'].map(lambda reads: 'absent' if reads == 0 else 'present'),
            'organismQuantity': cells['reads'].astype(str), 'organismQuantityType': 'DNA sequence reads',
        }),
        'identification': pd.DataFrame({
            'identification_pk': key, 'occurrence_fk': key, 'nucleotideAnalysis_fk': key,
            'verbatimIdentification': cells['otu'].map(assignments),
        }),
    }


def linked_non_detections(*rows):
    """Occurrences (pk, status, quantity type) each identified from a nucleotide analysis."""
    return {
        'occurrence': pd.DataFrame([
            {'occurrence_pk': key, 'occurrenceStatus': status, 'organismQuantityType': quantity_type}
            for key, status, quantity_type in rows
        ]),
        'identification': pd.DataFrame([
            {'identification_pk': f'i-{key}', 'occurrence_fk': key, 'nucleotideAnalysis_fk': f'a-{key}'}
            for key, _, _ in rows
        ]),
    }


class DnaReadWarningTests(SimpleTestCase):
    def test_zero_cells_of_the_example_otu_table_are_flagged(self):
        warnings = dna_read_warnings(otu_package(keep_zero_cells=True))

        self.assertEqual(len(warnings), 3, warnings)
        self.assertIn("'nucleotide-analysis' has 105577 analysis row(s) with readCount 0", warnings[0])
        self.assertIn('and 105567 more', warnings[0])
        self.assertIn('For metabarcoding OTU/ASV table cells', warnings[0])
        self.assertIn("'occurrence' has 105577 occurrence(s) with organismQuantity 0", warnings[1])
        self.assertIn(f'105577 {NON_DETECTIONS}', warnings[2])

    def test_detections_only_package_has_no_read_warnings(self):
        package = otu_package(keep_zero_cells=False)

        self.assertEqual(len(package['occurrence']), 14363)
        self.assertEqual(dna_read_warnings(package), [])

    def test_validation_surfaces_the_warnings(self):
        validation = validate_dwc_dp_resources(otu_package(keep_zero_cells=True))

        self.assertTrue(any(NON_DETECTIONS in warning for warning in validation['warnings']))

    def test_targeted_assay_non_detection_is_reviewed_but_still_valid(self):
        package = linked_non_detections(('o1', 'notDetected', 'ddPCR droplets'))
        package['occurrence'][['event_fk', 'organismQuantity']] = ['e1', '0']
        package.update({
            'event': pd.DataFrame([{'event_pk': 'e1', 'eventCategory': 'sampling'}]),
            'material': pd.DataFrame([{'materialEntity_pk': 'm1', 'collectionEvent_fk': 'e1'}]),
            'molecular-protocol': pd.DataFrame([{'molecularProtocol_pk': 'p', 'assayType': 'ddPCR'}]),
            'nucleotide-sequence': pd.DataFrame([{'nucleotideSequence_pk': 's1', 'sequence': 'ACGT'}]),
            'nucleotide-analysis': pd.DataFrame([{
                'nucleotideAnalysis_pk': 'a-o1', 'event_fk': 'e1', 'materialEntity_fk': 'm1',
                'nucleotideSequence_fk': 's1', 'molecularProtocol_fk': 'p',
            }]),
        })

        warnings = dna_read_warnings(package)
        validation = validate_dwc_dp_resources(package)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f'1 {NON_DETECTIONS}', warnings[0])
        self.assertIn("Keep a targeted assay's reported non-detections", warnings[0])
        self.assertTrue(validation['valid'], validation['errors'])

    def test_statuses_are_normalised_and_each_occurrence_counted_once(self):
        package = linked_non_detections(
            ('o1', ' Absent ', ''), ('o2', 'NOTDETECTED', ''), ('o3', 'present', ''), ('o4', 'absent', ''),
        )
        package['identification'] = pd.concat([
            package['identification'],
            pd.DataFrame([{'identification_pk': 'i-o1-again', 'occurrence_fk': 'o1', 'nucleotideAnalysis_fk': 'a-x'}]),
        ])
        package['identification'].loc[package['identification']['occurrence_fk'] == 'o4', 'nucleotideAnalysis_fk'] = ''

        warnings = dna_read_warnings(package)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f'2 {NON_DETECTIONS} or counted in \'DNA sequence reads\' in row(s) 1, 2.', warnings[0])

    def test_unlinked_read_typed_non_detection_is_flagged(self):
        warnings = dna_read_warnings({'occurrence': pd.DataFrame([
            {'occurrence_pk': 'o1', 'occurrenceStatus': 'absent', 'organismQuantityType': ' dna Sequence READS '},
            {'occurrence_pk': 'o2', 'occurrenceStatus': 'absent', 'organismQuantityType': 'individuals'},
        ])})

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f'1 {NON_DETECTIONS}', warnings[0])
        self.assertIn('in row(s) 1.', warnings[0])

    def test_unlinked_survey_absences_and_non_numeric_counts_are_not_flagged(self):
        resources = {
            'nucleotide-analysis': pd.DataFrame({'nucleotideAnalysis_pk': ['a1', 'a2', 'a3'], 'readCount': ['', None, 'n/a']}),
            'occurrence': pd.DataFrame([
                {'occurrence_pk': 'o1', 'occurrenceStatus': 'absent', 'organismQuantity': '0',
                 'organismQuantityType': 'ddPCR droplets'},
                {'occurrence_pk': 'o2', 'occurrenceStatus': 'absent', 'organismQuantity': '0',
                 'organismQuantityType': 'individuals'},
                {'occurrence_pk': 'o3', 'occurrenceStatus': 'present', 'organismQuantity': '',
                 'organismQuantityType': 'DNA sequence reads'},
            ]),
        }

        self.assertEqual(dna_read_warnings(resources), [])


class CachedValidationTests(TestCase):
    def test_cached_validation_from_before_the_read_checks_is_recomputed(self):
        dataset = Dataset.objects.create(title='OTU table')
        for name, df in otu_package(keep_zero_cells=True).items():
            Table.objects.create(dataset=dataset, title=name, df=df.head(200))
        with patch.object(agent_tools, '_FINGERPRINT_CACHE_VERSION', 2):
            stale = agent_tools._dwc_dp_resource_fingerprint(dataset, None)
        dataset.dwc_dp_validation = {'valid': True, 'errors': [], 'warnings': [], 'resource_fingerprint': stale}
        dataset.save(update_fields=['dwc_dp_validation'])

        section = quality_report._dwc_dp_section(dataset)

        self.assertIn(NON_DETECTIONS, section)
        self.assertNotEqual(Dataset.objects.get(id=dataset.id).dwc_dp_validation['resource_fingerprint'], stale)


class OtuTablePromptTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'tasks.yaml').read_text(encoding='utf-8'))
        cls.task_text = {item['fields']['name']: item['fields']['text'] for item in fixture}

    def test_transformation_keeps_only_detected_cells_and_classifier_evidence(self):
        text = self.task_text['Data transformation']

        for phrase in (
            'only for each OTU in each sample with at least one read',
            'A zero cell is neither a detection nor an absence',
            "a targeted assay's result, such as qPCR or ddPCR",
            'before selective removal of non-target OTUs',
            'confirm with the user before omitting them',
            'never merge different markers or primers',
            '`derivedFromMaterialEntityID`',
            'never use `size` as a sample\'s reads',
            "use the classifier's own cutoff assignment when supplied",
            'never choose one yourself',
            'keep the full assignment string as `verbatimIdentification`',
        ):
            self.assertIn(phrase, text)

    def test_validation_and_publication_check_read_based_records(self):
        validation = self.task_text['Data validation and refinement']
        publication = self.task_text['Publication package preparation']

        self.assertIn('no zero-read OTU table cell became a detection or a non-detection', validation)
        self.assertIn('`organismQuantityType` "DNA sequence reads" for read counts', validation)
        self.assertIn('`sampleSizeUnit` "DNA sequence reads"', publication)
        self.assertIn('`basisOfRecord` `MaterialSample`', publication)
        self.assertIn('Never project metabarcoding detections or zero-read cells as absences', publication)
