from django.test import SimpleTestCase

from api.dwca_import import DWC, read_inputs
from api.dwca_semantic_audit import audit_semantic_values, semantic_target_rejection


class SemanticValueAuditTests(SimpleTestCase):
    def test_reports_country_date_and_zero_profile_values_without_mutating_source(self):
        source = (
            b'occurrenceID,countryCode,dateIdentified,minimumElevationInMeters,maximumElevationInMeters,minimumDepthInMeters,maximumDepthInMeters\n'
            b'a,Norway,0-0-0,0,0,0,0\n'
            b'b,NO,2020-01-01,-1,2,0.0,10\n'
            b'c,no, 0-0-0 ,,,1e1,-0\n'
            b'd,XZ,0-0-1,1,,,-\n'
        )
        archive = read_inputs([('occurrence.csv', source)])
        before = [list(row) for row in archive.tables[0].rows]

        report = audit_semantic_values(archive)
        findings = {item['source_term']: item for item in report['findings']}

        country = findings[DWC + 'countryCode']
        self.assertEqual(country['count'], 2)
        self.assertEqual([sample['value'] for sample in country['examples']], ['Norway', 'no'])
        self.assertEqual(findings[DWC + 'dateIdentified']['count'], 2)
        self.assertEqual(findings[DWC + 'minimumElevationInMeters']['count'], 1)
        self.assertEqual(findings[DWC + 'maximumElevationInMeters']['count'], 1)
        self.assertEqual(findings[DWC + 'minimumDepthInMeters']['count'], 2)
        self.assertEqual(findings[DWC + 'maximumDepthInMeters']['count'], 2)
        self.assertTrue(all(item['action'].startswith('audit-only') for item in report['findings']))
        self.assertIn('valid elevation or depth', findings[DWC + 'minimumDepthInMeters']['guidance'])
        self.assertEqual(archive.tables[0].rows, before)

    def test_examples_are_bounded_and_negative_limit_is_rejected(self):
        archive = read_inputs([('occurrence.csv', b'occurrenceID,countryCode\na,Norway\nb,Sweden\nc,Denmark\n')])
        finding = audit_semantic_values(archive, example_limit=1)['findings'][0]
        self.assertEqual(finding['count'], 3)
        self.assertEqual(len(finding['examples']), 1)
        with self.assertRaises(ValueError):
            audit_semantic_values(archive, example_limit=-1)

    def test_age_like_event_remarks_are_flagged_without_reinterpreting_them(self):
        archive = read_inputs([('occurrence.csv',
            b'occurrenceID,eventRemarks\na,ad\nb,juv.\nc,adult + egg\nd,sampled by hand\n')])
        finding = next(item for item in audit_semantic_values(archive)['findings']
                       if item['id'] == 'age_like_event_remarks')
        self.assertEqual(finding['count'], 3)
        self.assertEqual([example['value'] for example in finding['examples']], ['ad', 'juv.', 'adult + egg'])

    def test_assertion_iri_fields_accept_only_absolute_iris(self):
        for value in ('http://vocab.nerc.ac.uk/collection/P01/current/MSHSIZE1/', 'https://vocab.nerc.ac.uk/collection/P06/current/XXXX/',
                      'urn:lsid:marinespecies.org:taxname:103259'):
            self.assertIsNone(semantic_target_rejection('event-assertion.assertionTypeIRI', value))
        for value in ('NA', 'micrometers', 'P01 MSHSIZE1', 'vocab.nerc.ac.uk/collection/S10/current/S102/'):
            self.assertIn('absolute IRI', semantic_target_rejection('occurrence-assertion.assertionValueIRI', value))
        self.assertIsNone(semantic_target_rejection('occurrence-assertion.assertionValue', 'NA'))
