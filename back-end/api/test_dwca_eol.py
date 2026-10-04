from django.test import SimpleTestCase

from api.dwc_dp_specs import TABLE_SPECS
from api.dwca_eol import (BIBO, DCT, EOL_FAMILIES, EOL_MEDIA, EOL_REFERENCE, IPTC, LEGACY_SUBTYPE, XMP, emit_eol_records,
                          eol_media_row_review, eol_targets)
from api.dwca_import import DWC, REGISTRY, ImportFailure

EOL = 'http://eol.org/schema/'
USAGE = XMP + 'rights/UsageTerms'
STILL = 'http://purl.org/dc/dcmitype/StillImage'
LICENSE = 'http://creativecommons.org/licenses/by/4.0/'


def registered(row_type):
    return {iri for iris in REGISTRY['row_types'][row_type].values() for iri in iris}


class EolTargetTests(SimpleTestCase):
    def test_families_use_exact_row_type_iris(self):
        self.assertEqual(EOL_FAMILIES, {'http://eol.org/schema/media/Document': 'eol-media',
                                        'http://eol.org/schema/reference/Reference': 'eol-reference'})
        self.assertEqual(len(registered(EOL_MEDIA)), 31)
        self.assertEqual(len(registered(EOL_REFERENCE)), 18)

    def test_targets_exist_and_every_non_exact_target_has_a_review_reason(self):
        samples = (['x'], ['https://example.org/x'])
        for row_type in EOL_FAMILIES:
            for term in registered(row_type):
                for values in samples:
                    targets, reason = eol_targets(row_type, term, values)
                    for target in targets:
                        table, field = target.split('.', 1)
                        self.assertIn(table, {'media', 'usage-policy', 'provenance', 'bibliographic-resource'})
                        spec = next(f for f in TABLE_SPECS[table].schema['fields'] if f['name'] == field)
                        if spec.get('dcterms:isVersionOf') != term:
                            self.assertTrue(reason, (term, target))

    def test_media_exact_terms(self):
        for term, target in ((DCT + 'identifier', 'media.mediaID'), (DCT + 'type', 'media.mediaType'),
                             (DCT + 'title', 'media.title'), ('http://rs.tdwg.org/ac/terms/accessURI', 'media.accessURI'),
                             (XMP + 'CreateDate', 'media.createDate'), (XMP + 'Rating', 'media.rating'),
                             (USAGE, 'usage-policy.usageTerms'), (XMP + 'rights/Owner', 'usage-policy.owner'),
                             (DCT + 'bibliographicCitation', 'provenance.bibliographicCitation')):
            self.assertEqual(eol_targets(EOL_MEDIA, term, ['https://example.org/v']), ([target], None))

    def test_literal_and_iri_columns_route_by_value_shape(self):
        for term, literal, iri in ((DCT + 'language', 'media.language', 'media.languageIRI'),
                                   (DCT + 'format', 'media.format', 'media.formatIRI'),
                                   (DCT + 'rights', 'usage-policy.rights', 'usage-policy.rightsIRI'),
                                   (LEGACY_SUBTYPE, 'media.subtypeLiteral', 'media.subtypeIRI')):
            targets, reason = eol_targets(EOL_MEDIA, term, ['en', 'image/jpeg'])
            self.assertEqual(targets, [literal]); self.assertTrue(reason)
            self.assertEqual(eol_targets(EOL_MEDIA, term, ['http://example.org/a'])[0], [iri])
            targets, reason = eol_targets(EOL_MEDIA, term, ['en', 'http://example.org/a'])
            self.assertEqual(targets, []); self.assertIn('mixes', reason)

    def test_legacy_and_iptc_aliases_need_review(self):
        targets, reason = eol_targets(EOL_MEDIA, LEGACY_SUBTYPE, ['Photograph'])
        self.assertEqual(targets, ['media.subtypeLiteral']); self.assertIn('Legacy', reason)
        targets, reason = eol_targets(EOL_MEDIA, IPTC + 'CVterm', ['http://rs.tdwg.org/ontology/voc/SPMInfoItems#Habitat'])
        self.assertEqual(targets, ['media.subjectCategoryIRI']); self.assertTrue(reason)
        self.assertEqual(eol_targets(EOL_MEDIA, IPTC + 'CVterm', ['Habitat'])[0], [])

    def test_eol_namespace_and_unsupported_terms_are_preserved(self):
        for term in (EOL + 'reference/referenceID', EOL + 'agent/agentID', EOL + 'media/thumbnailURL', DWC + 'taxonID'):
            targets, reason = eol_targets(EOL_MEDIA, term, ['https://example.org/r1'])
            self.assertEqual(targets, []); self.assertTrue(reason)
        for term in (DCT + 'audience', DCT + 'publisher', DCT + 'contributor', DCT + 'spatial', IPTC + 'LocationCreated',
                     'http://www.w3.org/2003/01/geo/wgs84_pos#lat'):
            self.assertEqual(eol_targets(EOL_MEDIA, term, ['59.91'])[0], [])

    def test_arbitrary_basenames_and_other_row_types_never_match(self):
        for term in ('http://example.org/title', EOL + 'media/title', 'http://rs.tdwg.org/ac/terms/subtype',
                     'http://example.org/pages'):
            self.assertEqual(eol_targets(EOL_MEDIA, term, ['x']), ([], None))
            self.assertEqual(eol_targets(EOL_REFERENCE, term, ['x']), ([], None))
        self.assertEqual(eol_targets(DWC + 'Occurrence', DCT + 'title', ['x']), ([], None))

    def test_reference_targets(self):
        for term, field in ((DCT + 'title', 'title'), (BIBO + 'pages', 'pages'), (BIBO + 'volume', 'volume'),
                            (BIBO + 'edition', 'edition')):
            self.assertEqual(eol_targets(EOL_REFERENCE, term, ['2']), ([f'bibliographic-resource.{field}'], None))
        for term, field in ((DCT + 'identifier', 'referenceID'), (EOL + 'reference/publicationType', 'referenceType'),
                            (BIBO + 'authorList', 'author'), (BIBO + 'editorList', 'editor')):
            targets, reason = eol_targets(EOL_REFERENCE, term, ['x'])
            self.assertEqual(targets, [f'bibliographic-resource.{field}']); self.assertTrue(reason)
        targets, reason = eol_targets(EOL_REFERENCE, EOL + 'reference/full_reference', ['Doe J. (2012) Birds.'])
        self.assertEqual(targets, ['bibliographic-resource.bibliographicCitation']); self.assertIn('ignored', reason)
        self.assertEqual(eol_targets(EOL_REFERENCE, DCT + 'publisher', ['Press'])[0], ['bibliographic-resource.publisher'])
        self.assertEqual(eol_targets(EOL_REFERENCE, DCT + 'publisher', ['https://ror.org/01xtthb56'])[0],
                         ['bibliographic-resource.publisherID'])
        self.assertEqual(eol_targets(EOL_REFERENCE, DCT + 'publisher', ['Press', 'https://ror.org/01xtthb56'])[0], [])
        for term in (DCT + 'created', BIBO + 'doi', BIBO + 'uri', BIBO + 'pageStart', BIBO + 'pageEnd'):
            targets, reason = eol_targets(EOL_REFERENCE, term, ['10.1234/x'])
            self.assertEqual(targets, []); self.assertTrue(reason)
        for term in (DCT + 'language', EOL + 'reference/primaryTitle', 'http://schemas.talis.com/2005/address/schema#localityName'):
            self.assertEqual(eol_targets(EOL_REFERENCE, term, ['x'])[0], [])


class EolMediaRowReviewTests(SimpleTestCase):
    row = {DCT + 'identifier': 'eol-media-9001', DCT + 'type': STILL, USAGE: LICENSE}

    def test_complete_media_row_needs_no_row_review(self):
        self.assertIsNone(eol_media_row_review(self.row))

    def test_text_rows_are_preserved(self):
        for kind in ('Text', 'http://purl.org/dc/dcmitype/Text', ' text '):
            self.assertIn('Text item', eol_media_row_review({**self.row, DCT + 'type': kind}))

    def test_missing_source_required_values(self):
        for term in (USAGE, DCT + 'identifier', DCT + 'type'):
            self.assertIn('requires', eol_media_row_review({**self.row, term: ''}))
            self.assertIn('requires', eol_media_row_review({k: v for k, v in self.row.items() if k != term}))

    def test_taxon_page_signals(self):
        self.assertIn('Taxon-page', eol_media_row_review({**self.row, DWC + 'taxonID': 'taxon9876'}))
        self.assertIn('Taxon-page', eol_media_row_review(
            {**self.row, IPTC + 'CVterm': 'http://rs.tdwg.org/ontology/voc/SPMInfoItems#GeneralDescription'}))


class EmitEolReferenceTests(SimpleTestCase):
    source = {DCT + 'identifier': 'ref-3', DCT + 'title': 'Birds of Oslo', BIBO + 'volume': '2',
              BIBO + 'doi': '10.1234/boo.2012', DCT + 'created': '2012-03-08', DCT + 'publisher': 'Oslo Univ. Press'}
    mapped = {'bibliographic-resource': {'referenceID': 'ref-3', 'title': 'Birds of Oslo', 'volume': '2', 'pages': ''}}

    def test_one_resource_and_one_join_per_subject_type(self):
        for subject, fk in (('occurrence', 'occurrence_fk'), ('event', 'event_fk'),
                            ('material', 'materialEntity_fk'), ('protocol', 'protocol_fk')):
            rows = emit_eol_records('eol-reference', self.source, self.mapped, subject, 'subject-key', 'row-key')
            self.assertEqual(rows, [
                ('bibliographic-resource', {'reference_pk': 'row-key', 'referenceID': 'ref-3', 'title': 'Birds of Oslo', 'volume': '2'}),
                (f'{subject}-reference', {'reference_fk': 'row-key', fk: 'subject-key'})])
            schema = TABLE_SPECS[f'{subject}-reference'].schema
            self.assertEqual(set(rows[1][1]), {f['name'] for f in schema['fields']} - {'relationshipType'})

    def test_source_multiplicity_is_kept(self):
        first = emit_eol_records('eol-reference', self.source, self.mapped, 'occurrence', 'occ-1', 'row-1')
        second = emit_eol_records('eol-reference', self.source, self.mapped, 'occurrence', 'occ-2', 'row-2')
        self.assertNotEqual(first[0][1]['reference_pk'], second[0][1]['reference_pk'])
        self.assertEqual(first[0][1]['referenceID'], second[0][1]['referenceID'])

    def test_literal_publisher_and_aliases_copy_only_source_values(self):
        source = {**self.source, EOL + 'reference/full_reference': 'Doe J. (2012) Birds of Oslo.'}
        mapped = {'bibliographic-resource': {'referenceID': 'ref-3', 'publisher': 'Oslo Univ. Press',
                                             'bibliographicCitation': 'Doe J. (2012) Birds of Oslo.'}}
        rows = emit_eol_records('eol-reference', source, mapped, 'event', 'ev', 'row')
        self.assertEqual(rows[0][1]['publisher'], 'Oslo Univ. Press')
        self.assertEqual(rows[0][1]['bibliographicCitation'], 'Doe J. (2012) Birds of Oslo.')

    def test_substitutions_and_unaudited_fields_fail(self):
        for mapped in ({'bibliographic-resource': {'referenceID': '10.1234/boo.2012'}},  # DOI is not referenceID
                       {'bibliographic-resource': {'issued': '2012-03-08'}},  # created is not issued
                       {'bibliographic-resource': {'title': 'Another title'}},
                       {'bibliographic-resource': {'reference_pk': 'x'}},
                       {'bibliographic-resource': {'relationshipType': 'cites'}},
                       {'occurrence-reference': {'relationshipType': 'cites'}},
                       {'media': {'mediaID': 'ref-3'}},
                       {'bibliographic-resource': {'volume': 2}}):
            with self.assertRaises(ImportFailure):
                emit_eol_records('eol-reference', self.source, mapped, 'occurrence', 'occ', 'row')

    def test_required_values_and_subjects(self):
        no_identifier = {k: v for k, v in self.source.items() if k != DCT + 'identifier'}
        cases = ((no_identifier, {'bibliographic-resource': {'title': 'Birds of Oslo'}}, 'occurrence', 'occ', 'row'),
                 (self.source, {'bibliographic-resource': {'referenceID': '', 'title': ' '}}, 'occurrence', 'occ', 'row'),
                 (self.source, {}, 'occurrence', 'occ', 'row'),
                 (self.source, self.mapped, 'taxon', 'tx', 'row'),
                 (self.source, self.mapped, 'occurrence', '', 'row'),
                 (self.source, self.mapped, 'occurrence', 'occ', ''))
        for source, mapped, subject, subject_key, record_key in cases:
            with self.assertRaises(ImportFailure):
                emit_eol_records('eol-reference', source, mapped, subject, subject_key, record_key)

    def test_media_family_and_unknown_families_are_not_emitted_here(self):
        for family in ('eol-media', 'reference', 'media'):
            with self.assertRaises(ImportFailure):
                emit_eol_records(family, self.source, self.mapped, 'occurrence', 'occ', 'row')
