import io
import json
import tarfile
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from api.conversion_jobs import process_next_conversion
from api.dwca_conversion import build_plan, convert, validate_decisions
from api.dwca_import import DWC, ImportFailure, read_inputs, source_zip
from api.dwca_taxon import taxonomy_tables
from api.dwc_dp_specs import DWC_DP_PROFILE_URL, create_dwc_dp_archive, validate_dwc_dp_archive
from api.models import CustomUser, Dataset


def decisions(plan):
    return {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}


def manifest_archive(extensions=None, taxon_ids=('local:one', 'local:two')):
    core_terms = ['taxonID', 'scientificName', 'scientificNameAuthorship', 'parentNameUsageID', 'acceptedNameUsageID']
    meta = '<archive xmlns="http://rs.tdwg.org/dwc/text/"><core rowType="' + DWC + 'Taxon" fieldsTerminatedBy="," ignoreHeaderLines="1">'
    meta += '<files><location>names.txt</location></files><id index="0"/>'
    meta += ''.join(f'<field index="{n + 1}" term="{DWC + term}"/>' for n, term in enumerate(core_terms)) + '</core>'
    files = {'names.txt': ('key,taxonID,scientificName,scientificNameAuthorship,parentNameUsageID,acceptedNameUsageID\n'
                          f'join-a,{taxon_ids[0]},Apus apus,Linnaeus,,\n'
                          f'join-b,{taxon_ids[1]},Hirundo apus,Linnaeus,,{taxon_ids[0]}\n').encode()}
    for name, row_type, terms, rows in extensions or []:
        meta += f'<extension rowType="{row_type}" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>{name}</location></files><coreid index="0"/>'
        meta += ''.join(f'<field index="{n + 1}" term="{term}"/>' for n, term in enumerate(terms)) + '</extension>'
        files[name] = ('join,' + ','.join(terms) + '\n' + ''.join(','.join(row) + '\n' for row in rows)).encode()
    files['meta.xml'] = (meta + '</archive>').encode()
    return files


def packaged(source, choices=None):
    plan = build_plan(source)
    frames, report = convert(source, plan, choices or decisions(plan))
    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / 'converted.tar.gz'
    create_dwc_dp_archive(path, frames, 'Taxonomy conversion', 'Source checklist', include_eml=False,
        additional_tables=taxonomy_tables(source, report),
        additional_files=[('source-originals.zip', source_zip(source.files)), ('conversion-report.json', json.dumps(report).encode())],
        declare_additional_resources=True)
    return folder, path, frames, report


class TaxonConversionTests(SimpleTestCase):
    def test_nested_occurrences_keep_automatic_choices_and_original_hierarchy_findings(self):
        source = read_inputs(manifest_archive([
            ('records.csv', DWC + 'Occurrence', [DWC + term for term in
             ('occurrenceID', 'occurrenceStatus', 'eventID', 'parentEventID', 'eventDate')]
             + ['http://example.org/privateNote'],
             [['join-a', 'occ-1', 'present', 'parent', '', '2020', 'source note'],
              ['join-b', 'occ-2', 'present', 'child', 'parent', '2022', 'NA']]),
        ]).items())
        plan = build_plan(source)
        prefix = 'taxon-occurrence:1:'
        automatic = {choice['id']: choice['default'] for choice in plan['automatic_choices']}
        self.assertEqual(automatic[prefix + 'event-grain'], 'by_id')
        self.assertTrue(plan['taxonomy']['scientific_hierarchies']['1']['has_findings'])
        self.assertTrue(any(column['id'] == prefix + 'column:0:5' and column.get('unmapped') == 'no-target' for column in plan['columns']))
        choices = decisions(plan)
        frames, report = convert(source, plan, choices)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(report['decisions'], choices)
        self.assertNotIn(prefix + 'event-grain', report['decisions'])
        self.assertEqual(report['effective_decisions'][prefix + 'event-grain'], 'by_id')
        hierarchy = report['taxonomy']['event_hierarchies']['1']
        self.assertEqual(hierarchy['linked_events'], 1)
        self.assertEqual(hierarchy['source_values'][0]['source_table_index'], 1)
        self.assertEqual(hierarchy['scientific_consistency']['counts']['contradiction'], 1)
        self.assertFalse(hierarchy['scientific_consistency']['review_required'])
        inner_report = report['taxonomy']['occurrence_reports']['1']['report']
        self.assertEqual(inner_report['effective_decisions']['event-grain'], 'by_id')
        self.assertNotIn('event-grain', inner_report['decisions'])
        parent = next(column for column in plan['columns'] if column['id'].startswith(prefix)
                      and column['term'] == DWC + 'parentEventID')
        choices.update({prefix + 'event-grain': 'per_row', parent['id']: 'preserve'})
        self.assertEqual(validate_decisions(plan, choices), [])
        _, overridden = convert(source, plan, choices)
        self.assertEqual(overridden['taxonomy']['event_hierarchies']['1']['linked_events'], 0)
        self.assertTrue(overridden['taxonomy']['event_hierarchies']['1']['scientific_consistency']['has_findings'])
        with self.assertRaisesMessage(ImportFailure, 'Unsupported mapping decision'):
            validate_decisions(plan, {**choices, prefix + 'event-grain': 'invent'})

    def test_standalone_taxonomy_retains_names_synonymy_and_declares_generic_profile(self):
        source = read_inputs(manifest_archive().items())
        folder, path, frames, report = packaged(source)
        self.addCleanup(folder.cleanup)
        self.assertEqual(frames, {})
        self.assertEqual(report['output_format'], 'taxonomy-data-package')
        tables = taxonomy_tables(source, report)
        table = tables['taxonomy-taxon']
        self.assertEqual(table['dataframe'][DWC + 'scientificName'].tolist(), ['Apus apus', 'Hirundo apus'])
        self.assertEqual(table['dataframe'][DWC + 'acceptedNameUsageID'].tolist(), ['', 'local:one'])
        self.assertEqual(table['dataframe']['archive_join_id'].tolist(), ['join-a', 'join-b'])
        self.assertEqual(table['schema']['foreignKeys'][1]['reference']['fields'], DWC + 'taxonID')
        self.assertTrue(validate_dwc_dp_archive(path, require_eml=False, allow_generic=True)['valid'])
        self.assertFalse(validate_dwc_dp_archive(path, require_eml=False)['valid'])
        with tarfile.open(path) as archive:
            descriptor = json.load(archive.extractfile('datapackage.json'))
            self.assertEqual(descriptor['profile'], 'data-package')
            self.assertEqual(descriptor['resources'][0]['name'], 'taxonomy-taxon')
            self.assertEqual(archive.extractfile('source-originals.zip').read(), source_zip(source.files))

    def test_taxon_extensions_keep_multiplicity_and_never_invent_occurrences(self):
        source = read_inputs(manifest_archive([
            ('distribution.csv', 'http://rs.gbif.org/terms/1.0/Distribution', [DWC + 'countryCode', DWC + 'occurrenceStatus', DWC + 'eventDate'],
             [['join-a', 'NO', 'present', '2025-01-01'], ['join-a', 'SE', 'present', '2025-01-01']]),
            ('vernacular.csv', 'http://rs.gbif.org/terms/1.0/VernacularName', [DWC + 'vernacularName'],
             [['join-a', 'Swift'], ['join-a', 'Tårnseiler']]),
            ('types.csv', 'http://rs.gbif.org/terms/1.0/TypesAndSpecimen', [DWC + 'catalogNumber', DWC + 'eventDate'],
             [['join-b', 'Type:123', '1800-01-01']]),
        ]).items())
        folder, path, frames, report = packaged(source); self.addCleanup(folder.cleanup)
        self.assertEqual(frames, {})
        self.assertEqual(report['resources']['taxonomy-extension-1'], 2)
        self.assertEqual(len(report['row_crosswalk']), 7)
        self.assertTrue(validate_dwc_dp_archive(path, require_eml=False, allow_generic=True)['valid'])
        for table in taxonomy_tables(source).values():
            self.assertEqual(table['schema']['missingValues'], [''])

    def test_loose_taxon_layout_joins_by_taxon_id_and_is_order_independent(self):
        inputs = [('taxon.csv', b'taxonID,scientificName,parentNameUsageID\nt1,Apus apus,\nt2,Hirundo apus,t1\n'),
                  ('vernacularname.csv', b'taxonID,vernacularName\nt1,Swift\nt1,Tarnseiler\n')]
        source = read_inputs(inputs); reversed_source = read_inputs(reversed(inputs))
        self.assertEqual(build_plan(source), build_plan(reversed_source))
        plan = build_plan(source); frames, report = convert(source, plan, decisions(plan))
        self.assertEqual(frames, {})
        self.assertEqual(taxonomy_tables(source)['taxonomy-extension-1']['dataframe']['archive_taxon_id'].tolist(), ['t1', 't1'])
        self.assertTrue(report['validation']['valid'])

    def test_archive_join_ids_and_dangling_extension_links_are_checked(self):
        with self.assertRaisesMessage(ImportFailure, 'nonempty and unique'):
            read_inputs([('taxon.csv', b'taxonID,scientificName\nt1,Apus apus\nt1,Hirundo apus\n')])
        with self.assertRaisesMessage(ImportFailure, 'absent from the core'):
            read_inputs([('taxon.csv', b'taxonID,scientificName\nt1,Apus apus\n'),
                         ('distribution.csv', b'taxonID,countryCode\nmissing,NO\n')])

    def test_external_or_ambiguous_name_usage_links_are_retained_without_false_foreign_keys(self):
        files = manifest_archive(taxon_ids=('same', 'same'))
        source = read_inputs(files.items()); plan = build_plan(source)
        self.assertEqual(plan['taxonomy']['link_checks']['duplicate_identifiers'], 1)
        self.assertEqual(plan['taxonomy']['link_checks']['relationships'][1]['ambiguous_local_matches'], 1)
        self.assertNotIn('foreignKeys', taxonomy_tables(source)['taxonomy-taxon']['schema'])
        folder, path, _, _ = packaged(source); self.addCleanup(folder.cleanup)
        self.assertTrue(validate_dwc_dp_archive(path, require_eml=False, allow_generic=True)['valid'])
        external = read_inputs([('taxon.csv', b'taxonID,scientificName,parentNameUsageID\nt1,Apus apus,https://external.example/taxon\n')])
        self.assertNotIn('foreignKeys', taxonomy_tables(external)['taxonomy-taxon']['schema'])
        self.assertEqual(build_plan(external)['taxonomy']['link_checks']['relationships'][0]['external_or_unresolved'], 1)

    def test_actual_occurrences_inherit_classification_and_keep_local_taxon_ids_in_taxonomy(self):
        source = read_inputs(manifest_archive([
            ('records.csv', DWC + 'Occurrence', [DWC + 'occurrenceID', DWC + 'scientificName', DWC + 'occurrenceStatus'],
             [['join-a', 'occ-1', '', 'present'], ['join-a', 'occ-2', 'Apus pallidus', 'present']]),
        ]).items())
        folder, path, frames, report = packaged(source); self.addCleanup(folder.cleanup)
        self.assertEqual(report['output_format'], 'dwc-dp')
        self.assertEqual(frames['occurrence']['scientificName'].tolist(), ['Apus apus', 'Apus pallidus'])
        self.assertNotIn('taxonID', frames['occurrence'])
        self.assertEqual(frames['occurrence']['scientificNameAuthorship'].tolist(), ['Linnaeus', ''])
        self.assertEqual(len(report['taxonomy']['classification_conflicts']), 1)
        self.assertEqual(len(report['taxonomy_occurrence_links']), 2)
        occurrence_traces = [trace for trace in report['row_crosswalk'] if trace['target_table'] == 'occurrence']
        self.assertEqual(occurrence_traces[0]['file'], 'records.csv')
        self.assertEqual(occurrence_traces[0]['linked_taxon_file'], 'names.txt')
        self.assertEqual(occurrence_traces[0]['archive_join_id'], 'join-a')
        self.assertEqual(len([trace for trace in report['row_crosswalk'] if trace['target_table'] == 'taxonomy-occurrence-links']), 2)
        self.assertTrue(validate_dwc_dp_archive(path, require_eml=False)['valid'])
        with tarfile.open(path) as archive:
            self.assertEqual(json.load(archive.extractfile('datapackage.json'))['profile'], DWC_DP_PROFILE_URL)

    def test_occurrence_extraction_can_be_declined_without_losing_source_rows(self):
        source = read_inputs(manifest_archive([
            ('records.csv', DWC + 'Occurrence', [DWC + 'occurrenceID'], [['join-a', 'occ-1']]),
        ]).items())
        plan = build_plan(source)
        frames, report = convert(source, plan, {'taxonomy-package': 'confirm', 'table:1': 'preserve'})
        self.assertEqual(frames, {})
        self.assertEqual(report['resources']['taxonomy-extension-1'], 1)

    def test_external_taxon_ids_require_review_and_are_copied_only_after_approval(self):
        source = read_inputs(manifest_archive([
            ('records.csv', DWC + 'Occurrence', [DWC + 'occurrenceID', DWC + 'occurrenceStatus'], [['join-a', 'occ-1', 'present']]),
        ], taxon_ids=('https://example.org/taxon/1', 'https://example.org/taxon/2')).items())
        plan = build_plan(source)
        issue = next(issue for issue in plan['issues'] if issue['title'].startswith('taxonID:'))
        self.assertGreater(len(issue['options']), 1)
        frames, _ = convert(source, plan, decisions(plan))
        self.assertEqual(frames['occurrence'].iloc[0]['taxonID'], 'https://example.org/taxon/1')
        choices = decisions(plan); choices[issue['id']] = 'preserve'
        frames, _ = convert(source, plan, choices)
        self.assertNotIn('taxonID', frames['occurrence'])

    def test_unknown_columns_and_unlinked_loose_tables_keep_all_values(self):
        source = read_inputs([('taxon.csv', b'scientificName,privateNote\nApus apus, NA \n'),
                              ('notes.csv', b'foo,bar\nx,NA\n')])
        folder, path, frames, report = packaged(source); self.addCleanup(folder.cleanup)
        self.assertEqual(frames, {})
        tables = taxonomy_tables(source)
        self.assertEqual(tables['taxonomy-taxon']['dataframe']['header:privateNote'].iloc[0], ' NA ')
        self.assertEqual(tables['taxonomy-extension-1']['dataframe']['header:bar'].iloc[0], 'NA')
        self.assertNotIn('foreignKeys', tables['taxonomy-extension-1']['schema'])
        self.assertTrue(validate_dwc_dp_archive(path, require_eml=False, allow_generic=True)['valid'])

    def test_serialized_additional_table_foreign_keys_are_validated(self):
        source = read_inputs(manifest_archive([
            ('vernacular.csv', 'http://rs.gbif.org/terms/1.0/VernacularName', [DWC + 'vernacularName'], [['join-a', 'Swift']]),
        ]).items())
        folder, path, _, _ = packaged(source); self.addCleanup(folder.cleanup)
        with tarfile.open(path) as archive:
            contents = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
        contents['taxonomy-extension-1.csv'] = contents['taxonomy-extension-1.csv'].replace(b'join-a', b'nonexistent')
        with tarfile.open(path, 'w:gz') as archive:
            for name, content in contents.items():
                info = tarfile.TarInfo(name); info.size = len(content); archive.addfile(info, io.BytesIO(content))
        result = validate_dwc_dp_archive(path, require_eml=False, allow_generic=True)
        self.assertFalse(result['valid'])
        self.assertTrue(any('foreign key' in error.lower() for error in result['errors']), result)

    def test_multiple_occurrence_extensions_keep_distinct_keys_and_global_row_lineage(self):
        source = read_inputs(manifest_archive([
            ('first.csv', DWC + 'Occurrence', [DWC + 'occurrenceID', DWC + 'occurrenceStatus'], [['join-a', 'occ-1', 'present']]),
            ('second.csv', DWC + 'Occurrence', [DWC + 'occurrenceID', DWC + 'occurrenceStatus'], [['join-a', 'occ-2', 'present']]),
        ]).items())
        plan = build_plan(source); frames, report = convert(source, plan, decisions(plan))
        repeated, repeated_report = convert(source, plan, decisions(plan))
        self.assertTrue(frames['occurrence'].equals(repeated['occurrence']))
        self.assertEqual(report, repeated_report)
        self.assertEqual(frames['occurrence']['occurrence_pk'].nunique(), 2)
        traces = [trace for trace in report['row_crosswalk'] if trace['target_table'] == 'occurrence']
        self.assertEqual([trace['target_row'] for trace in traces], [1, 2])
        self.assertEqual([trace['file'] for trace in traces], ['first.csv', 'second.csv'])
        self.assertTrue(report['validation']['valid'])

    def test_additional_taxonomy_tables_cannot_override_canonical_tables(self):
        source = read_inputs(manifest_archive().items())
        table = taxonomy_tables(source)['taxonomy-taxon']
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesMessage(ValueError, 'non-reserved name'):
            create_dwc_dp_archive(Path(folder) / 'invalid.tar.gz', {}, 'Invalid', 'Invalid', include_eml=False,
                                  additional_tables={'event': table})


class TaxonAPITests(TransactionTestCase):
    def test_taxon_zip_queue_export_and_named_download_preserve_the_archive(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': folder}},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        }):
            client = APIClient(); client.force_authenticate(CustomUser.objects.create_user(username='taxon-owner'))
            original = source_zip(manifest_archive())
            response = client.post('/api/datasets/', {'workflow_type': 'dwca_conversion',
                'files': [SimpleUploadedFile('checklist.zip', original)]}, format='multipart')
            self.assertEqual(response.status_code, 201, response.data)
            dataset = Dataset.objects.get(pk=response.data['id'])
            process_next_conversion(); conversion = dataset.conversion
            response = client.post(f'/api/datasets/{dataset.pk}/conversion/', {
                'plan_id': conversion.plan['id'], 'decisions': decisions(conversion.plan)}, format='json')
            self.assertEqual(response.status_code, 202, response.data)
            process_next_conversion(); conversion.refresh_from_db()
            self.assertEqual(conversion.status, 'complete', conversion.error)
            self.assertTrue(dataset.package_ready)
            self.assertEqual(dataset.table_set.get(title='taxonomy-taxon').row_count, 2)
            with conversion.output_file.open('rb') as stream, tarfile.open(fileobj=stream) as archive:
                self.assertEqual(archive.extractfile('uploaded-archive.zip').read(), original)
            response = client.get(f'/api/datasets/{dataset.pk}/conversion-download/')
            self.assertEqual(response.status_code, 200)
            self.assertIn('taxonomy-data-package-', response['Content-Disposition'])
            response.close()
