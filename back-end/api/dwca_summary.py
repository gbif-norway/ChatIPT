"""A short description of the current Darwin Core Archive for the dataset page.

The page shows the archive as a summary next to its download: which core and extensions it has, how
many rows each holds and whether GBIF could index it. Built from what is already stored on the
dataset (`dwca_url`, `dwca_validation`, `dwca_row_types`), so it never downloads the archive.
"""

import re

# GBIF validator states after which no new result will come.
FINISHED_STATUSES = frozenset({'FINISHED', 'SUCCEEDED'})
FAILED_STATUSES = frozenset({'FAILED', 'ABORTED'})
NOT_APPLICABLE_STATUS = 'NOT_APPLICABLE'

# Names people know these tables by; any other rowType is named after its last path segment.
ROW_TYPE_LABELS = {
    'http://rs.tdwg.org/dwc/terms/Occurrence': 'Occurrence',
    'http://rs.tdwg.org/dwc/terms/Event': 'Event',
    'http://rs.tdwg.org/dwc/terms/Taxon': 'Taxon',
    'http://rs.tdwg.org/dwc/terms/MeasurementOrFact': 'Measurement or fact',
    'http://rs.iobis.org/obis/terms/ExtendedMeasurementOrFact': 'Extended measurement or fact',
    'http://rs.gbif.org/terms/1.0/DNADerivedData': 'DNA derived data',
    'http://rs.tdwg.org/ac/terms/Multimedia': 'Audiovisual media',
    'http://rs.gbif.org/terms/1.0/Multimedia': 'Multimedia',
    'http://rs.tdwg.org/eco/terms/Event': 'Humboldt ecological inventory',
    'http://rs.tdwg.org/dwc/terms/Identification': 'Identification',
    'http://rs.tdwg.org/chrono/terms/ChronometricAge': 'Chronometric age',
}


def row_type_label(row_type):
    if not row_type:
        return 'Unknown table'
    if row_type in ROW_TYPE_LABELS:
        return ROW_TYPE_LABELS[row_type]
    name = re.split(r'[/#]', str(row_type).rstrip('/#'))[-1]
    # "ResourceRelationship" -> "Resource relationship", keeping runs of capitals ("DNA") together.
    words = re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+', name)
    if not words:
        return name or 'Unknown table'
    text = ' '.join(word if word.isupper() else word.lower() for word in words)
    return text[0].upper() + text[1:]


def validation_state(validation, dwca_url):
    """'not_checked', 'checking', 'passed', 'not_applicable' or 'failed' for the archive at dwca_url.

    'passed' matches Dataset.has_current_dwca_validation: GBIF finished and could index the archive.
    A result for an earlier archive counts as not checked.
    """
    validation = validation or {}
    if not dwca_url or validation.get('url') != dwca_url or not validation.get('status'):
        return 'not_checked'
    status = validation['status']
    if status == NOT_APPLICABLE_STATUS:
        return 'not_applicable'
    if status in FINISHED_STATUSES:
        return 'passed' if (validation.get('metrics') or {}).get('indexeable') else 'failed'
    if status in FAILED_STATUSES:
        return 'failed'
    return 'checking'


def dwca_summary(dwca_url, validation=None, row_types=None):
    """The archive's tables and GBIF check, or None when there is no archive.

    tables: the core first, then extensions, each {kind, row_type, label, rows}; rows is None when GBIF
    has not counted them. Row counts come from a finished GBIF validation of this archive, the table
    list from meta.xml (`dwca_row_types`) or, failing that, from the files GBIF reported.
    """
    if not dwca_url:
        return None
    validation = validation or {}
    current_validation = validation if validation.get('url') == dwca_url else {}
    files = (current_validation.get('metrics') or {}).get('files') or []
    counts = {}
    for file in files:
        row_type = file.get('rowType')
        if row_type and isinstance(file.get('count'), int):
            counts[row_type] = counts.get(row_type, 0) + file['count']

    row_types = row_types or {}
    if row_types.get('url') == dwca_url and (row_types.get('core') or row_types.get('extensions')):
        declared = [('core', row_types.get('core'))] if row_types.get('core') else []
        declared += [('extension', row_type) for row_type in row_types.get('extensions') or [] if row_type]
    else:
        data_files = [file for file in files if file.get('rowType')]
        declared = sorted(
            (
                (
                    'core' if str(file.get('fileType') or '').upper() == 'CORE' else 'extension',
                    file['rowType'],
                )
                for file in data_files
            ),
            key=lambda entry: entry[0] != 'core',
        )

    tables = []
    seen = set()
    for kind, row_type in declared:
        if row_type in seen:
            continue
        seen.add(row_type)
        tables.append({
            'kind': kind,
            'row_type': row_type,
            'label': row_type_label(row_type),
            'rows': counts.get(row_type),
        })

    return {
        'url': dwca_url,
        'tables': tables,
        'validation': {
            'state': validation_state(validation, dwca_url),
            'message': current_validation.get('message') or '',
        },
    }
