from django.test import SimpleTestCase

from api.dwca_eml_descriptor import extract_eml_descriptor_metadata
from api.dwca_import import read_inputs
from api.dwca_conversion import build_plan, convert
from api.dwc_dp_specs import build_datapackage_descriptor


class EmlDescriptorMetadataTests(SimpleTestCase):
    def test_extracts_only_compatible_descriptor_properties(self):
        xml = b'''<eml:eml xmlns:eml="eml://ecoinformatics.org/eml-2.1.1">
          <dataset>
            <creator><individualName><givenName>Ada</givenName><surName>Lovelace</surName></individualName>
              <organizationName>Analytical Engine Lab</organizationName><electronicMailAddress>ada@example.org</electronicMailAddress></creator>
            <associatedParty><organizationName>Field Team</organizationName><role>custodian</role></associatedParty>
            <keywordSet><keyword>birds</keyword><keyword>  point counts </keyword><keyword>birds</keyword></keywordSet>
            <intellectualRights>https://creativecommons.org/licenses/by/4.0/</intellectualRights>
            <citation><para>Lovelace, A. (2024). Forest birds.</para></citation>
            <coverage><geographicCoverage><geographicDescription>Nordmarka</geographicDescription></geographicCoverage>
              <temporalCoverage><rangeOfDates><beginDate><calendarDate>2020-01-01</calendarDate></beginDate>
                <endDate><calendarDate>2020-12-31</calendarDate></endDate></rangeOfDates></temporalCoverage>
              <taxonomicCoverage><taxonomicClassification><taxonRankName>Class</taxonRankName><taxonRankValue>Aves</taxonRankValue></taxonomicClassification></taxonomicCoverage>
            </coverage>
          </dataset>
        </eml:eml>'''
        result = extract_eml_descriptor_metadata(xml)
        self.assertEqual(result['descriptor']['licenses'], [{
            'name': 'cc-by-4.0', 'path': 'https://creativecommons.org/licenses/by/4.0/', 'title': 'CC BY 4.0'}])
        self.assertEqual(result['descriptor']['keywords'], ['birds', 'point counts'])
        self.assertEqual(result['descriptor']['contributors'][0], {
            'title': 'Ada Lovelace', 'email': 'ada@example.org', 'organization': 'Analytical Engine Lab', 'role': 'author'})
        self.assertEqual(result['source_metadata']['citation'], 'Lovelace, A. (2024). Forest birds.')
        self.assertEqual(result['source_metadata']['coverage']['temporal'], ['2020-01-01/2020-12-31'])
        self.assertEqual(result['source_metadata']['coverage']['taxonomic'], ['Class: Aves'])
        archive = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus\none,present\n')])
        plan = build_plan(archive)
        choices = {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}
        resources, _ = convert(archive, plan, choices)
        package = build_datapackage_descriptor(resources, descriptor_metadata=result['descriptor'])
        self.assertEqual(package['licenses'], result['descriptor']['licenses'])
        self.assertEqual(package['contributors'], result['descriptor']['contributors'])

    def test_ambiguous_free_text_rights_are_retained_without_license_claim(self):
        result = extract_eml_descriptor_metadata(
            b'<eml><dataset><intellectualRights>Contact the museum before reuse.</intellectualRights></dataset></eml>')
        self.assertNotIn('licenses', result['descriptor'])
        self.assertEqual(result['source_metadata']['intellectual_rights'], 'Contact the museum before reuse.')
        self.assertEqual(result['warnings'][0]['field'], 'intellectualRights')

    def test_explicit_eml_license_link_is_promoted(self):
        result = extract_eml_descriptor_metadata(
            b'<eml><dataset><intellectualRights><para>This work is licensed under '
            b'<ulink url="http://creativecommons.org/licenses/by/4.0/legalcode">CC BY 4.0</ulink>.'
            b'</para></intellectualRights></dataset></eml>')
        self.assertEqual(result['descriptor']['licenses'][0]['path'],
                         'https://creativecommons.org/licenses/by/4.0/')
        self.assertEqual(result['warnings'], [])

    def test_rejects_multiple_datasets_and_does_not_resolve_external_entities(self):
        with self.assertRaisesRegex(ValueError, 'exactly one dataset'):
            extract_eml_descriptor_metadata(b'<eml><dataset/><dataset/></eml>')
        result = extract_eml_descriptor_metadata(
            b'<!DOCTYPE eml [<!ENTITY x SYSTEM "file:///etc/passwd">]><eml><dataset><keyword>&x;</keyword></dataset></eml>')
        self.assertEqual(result['descriptor'], {})
