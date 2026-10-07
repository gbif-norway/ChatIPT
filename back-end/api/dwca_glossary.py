"""Plain-language glossary for archive conversion choices.

All wording that explains Data Package tables and fields to people lives in this
file, so it can be reviewed and edited in one place. It is written for the
pinned DwC-DP snapshot below (test_dwca_glossary checks the revision and that
every target a column question can offer has an explanation).

Each target explanation has:
- label: the option as a person would say it ("Who collected the specimen");
- gloss: what the value means there, in one sentence;
- consequence: where it is stored and what that implies;
- choose_when: when this is usually the right choice.
The official definition is added from the pinned schema by describe().
"""
from __future__ import annotations

import re

SCHEMA_REVISION = '76898192fd298c2aa170a7059e1bdadf3ee2a828'

# The kinds of record in a Data Package, in one line each.
TABLES = {
    'event': {'label': 'event record',
              'gloss': 'The visit or sampling: when, where and by whom. Everything found during that visit shares it.'},
    'occurrence': {'label': 'observation record',
                   'gloss': 'An organism (or group of organisms) observed or collected at a place and time.'},
    'identification': {'label': 'identification record',
                       'gloss': 'Who decided what the organism is, and when. One observation can have several identifications over time.'},
    'material': {'label': 'specimen record',
                 'gloss': 'A physical specimen or sample kept in a collection, such as a pinned insect, a herbarium sheet, '
                          'a tissue or a soil sample.'},
    'media': {'label': 'media record', 'gloss': 'A photo, sound recording, video or other media file.'},
    'agent': {'label': 'person or organization',
              'gloss': 'A person or organization, listed once and linked to every record that mentions them.'},
    'provenance': {'label': 'provenance record', 'gloss': 'Where a media file comes from and who made it.'},
    'usage-policy': {'label': 'usage policy', 'gloss': 'The rights and licence that apply to a media file.'},
}

PRESERVE = {
    'label': 'Keep only in my original files',
    'gloss': 'The column is not copied into the Data Package tables.',
    'consequence': 'Nothing is lost: your download keeps every original file, including this column.',
    'choose_when': 'Choose this if none of the other options describes the column.',
}

# Said under every option list whose options copy values: no option changes the text.
COPY_NOTE = 'Every option copies the values exactly as written.'

# Plain names for fields, used in labels and summaries. Fields not listed are named from their
# technical name ("organismQuantityType" -> "organism quantity type").
FIELD_LABELS = {
    'recordedBy': 'recorded by', 'recordedByID': 'recorded by (identifier)',
    'collectedBy': 'collected by', 'collectedByID': 'collected by (identifier)',
    'eventConductedBy': 'fieldwork carried out by', 'eventConductedByID': 'fieldwork carried out by (identifier)',
    'scientificName': 'scientific name', 'scientificNameAuthorship': 'name author',
    'scientificNameID': 'scientific name identifier', 'taxonID': 'taxon identifier', 'taxonRank': 'taxon rank',
    'vernacularName': 'common name', 'verbatimIdentification': 'identification as written',
    'identifiedBy': 'identified by', 'identifiedByID': 'identified by (identifier)', 'dateIdentified': 'date identified',
    'identificationReferences': 'identification references', 'identificationRemarks': 'identification remarks',
    'identificationVerificationStatus': 'identification verification status',
    'externalClassificationSource': 'name source',
    'typeStatus': 'type status', 'typeDesignationType': 'type designation',
    'occurrenceReferences': 'references', 'eventReferences': 'references', 'materialReferences': 'references',
    'dataGeneralizations': 'data generalizations', 'informationWithheld': 'information withheld',
    'feedbackURL': 'feedback link', 'modified': 'date the record was last changed',
    'catalogNumber': 'catalogue number', 'otherCatalogNumbers': 'other catalogue numbers',
    'institutionCode': 'institution code', 'institutionID': 'institution identifier',
    'ownerInstitutionCode': 'owner institution code', 'collectionCode': 'collection code',
    'collectionID': 'collection identifier', 'collectorNumber': 'collector number',
    'preparations': 'preparations', 'disposition': 'where the specimen is now',
    'associatedSequences': 'linked DNA sequences', 'digitalSpecimenID': 'digital specimen identifier',
    'discipline': 'discipline', 'materialEntityID': 'specimen identifier',
    'materialEntityCategory': 'specimen category', 'materialEntityType': 'specimen type',
    'materialEntityRemarks': 'specimen remarks', 'objectQuantity': 'number of objects',
    'objectQuantityType': 'kind of objects counted', 'typeOfType': 'kind of type', 'verbatimLabel': 'label text',
    'derivedFromMaterialEntityID': 'taken from specimen', 'isPartOfMaterialEntityID': 'part of specimen',
}

# Targets that need their own explanation: the options of agent-role, type-status and
# specimen-identity questions.
TARGETS = {
    'occurrence.recordedBy': {
        'label': 'Who saw or recorded the organism',
        'gloss': 'The people who observed or recorded this organism.',
        'consequence': 'Stored on the observation record.',
        'choose_when': 'Best for sightings, surveys and other records without a kept specimen.',
        'decided': 'saved as who saw or recorded the organism, on each observation record',
    },
    'material.collectedBy': {
        'label': 'Who collected the specimen',
        'gloss': 'The people who collected the physical specimen or sample.',
        'consequence': 'Stored on the specimen record, next to its catalogue number and collector number.',
        'choose_when': 'Best for museum and herbarium specimens and other kept samples.',
        'decided': 'saved as who collected the specimen, on each specimen record',
    },
    'event.eventConductedBy': {
        'label': 'Who carried out the fieldwork',
        'gloss': 'The people who carried out the visit or sampling.',
        'consequence': 'Stored on the event record, so it applies to everything recorded during that visit.',
        'choose_when': 'Best when the same people did the whole survey or sampling visit.',
        'decided': 'saved as who carried out the fieldwork, on each event record',
    },
    'occurrence.recordedByID': {
        'label': 'Identifier of who saw or recorded the organism',
        'gloss': 'An identifier, such as an ORCID, for the people who observed or recorded this organism.',
        'consequence': 'Stored on the observation record, next to the names.',
        'choose_when': 'Follows where the names in recordedBy go.',
        'decided': 'saved next to the recorders\' names, on each observation record',
    },
    'material.collectedByID': {
        'label': 'Identifier of who collected the specimen',
        'gloss': 'An identifier, such as an ORCID, for the people who collected the specimen.',
        'consequence': 'Stored on the specimen record, next to the collectors\' names.',
        'choose_when': 'Follows where the names in recordedBy go.',
        'decided': 'saved next to the collectors\' names, on each specimen record',
    },
    'event.eventConductedByID': {
        'label': 'Identifier of who carried out the fieldwork',
        'gloss': 'An identifier, such as an ORCID, for the people who carried out the visit or sampling.',
        'consequence': 'Stored on the event record, next to the names.',
        'choose_when': 'Follows where the names in recordedBy go.',
        'decided': 'saved next to the names, on each event record',
    },
    'material.typeStatus': {
        'label': 'Type status of the specimen',
        'gloss': 'Whether this physical specimen is a type specimen for a scientific name, such as a holotype or paratype, and for which name.',
        'consequence': 'Stored on the specimen record.',
        'choose_when': 'Best when specimen records are created: a type status describes a specimen.',
        'decided': 'saved as the type status of each specimen record',
    },
    'identification.typeStatus': {
        'label': 'Type status, with the identification',
        'gloss': 'Whether the organism is a type specimen for a scientific name, such as a holotype or paratype, recorded together with its identification.',
        'consequence': 'Stored on the identification record linked to the observation.',
        'choose_when': 'Use when no specimen records are created, so there is no specimen record to hold it.',
        'decided': 'saved with the identification of each observation',
    },
    'material.typeDesignationType': {
        'label': 'Kind of type designation, on the specimen record',
        'gloss': 'How the type was designated (for example original designation or later designation).',
        'consequence': 'Stored on the specimen record, with its type status.',
        'choose_when': 'Best when specimen records are created.',
        'decided': 'saved on each specimen record, with its type status',
    },
    'identification.typeDesignationType': {
        'label': 'Kind of type designation, with the identification',
        'gloss': 'How the type was designated (for example original designation or later designation).',
        'consequence': 'Stored on the identification record, with its type status.',
        'choose_when': 'Use when no specimen records are created.',
        'decided': 'saved with the identification of each observation',
    },
    'material.materialEntityID': {
        'label': 'The specimen\'s own identifier',
        'gloss': 'An identifier for this specimen or sample itself.',
        'consequence': 'Becomes the identifier of the specimen record.',
        'choose_when': 'Usual choice: the column names the specimen on this row.',
        'decided': 'saved as the identifier of each specimen record',
    },
    'material.derivedFromMaterialEntityID': {
        'label': 'Identifier of the specimen this one was taken from',
        'gloss': 'The specimen this one was taken from, for example the whole animal a tissue sample came from.',
        'consequence': 'Stored on the specimen record as a link to the original specimen.',
        'choose_when': 'Choose when each row is a subsample and the column names its source specimen.',
        'decided': 'saved as the specimen each one was taken from',
    },
    'material.isPartOfMaterialEntityID': {
        'label': 'Identifier of the larger specimen this one belongs to',
        'gloss': 'The larger specimen this one is part of, for example one sheet of a specimen mounted on several sheets.',
        'consequence': 'Stored on the specimen record as a link to the larger specimen.',
        'choose_when': 'Choose when each row is one part of a specimen kept in several parts.',
        'decided': 'saved as the larger specimen each one belongs to',
    },
}

# Fields that exist on several tables with the same meaning; the table says what the value is about.
FAMILIES = {
    'agent-role': {
        'fields': {'recordedBy', 'recordedByID', 'collectedBy', 'collectedByID', 'eventConductedBy', 'eventConductedByID'},
        'question': 'Who are the people in {column}?',
        'reason': ('The names in this column could be the people who collected a specimen, who saw or recorded the '
                   'organism, or who carried out the fieldwork. Choose what they did.'),
        'summary': 'People',
    },
    'identification': {
        'fields': {'scientificName', 'scientificNameAuthorship', 'scientificNameID', 'taxonID', 'taxonRank',
                   'vernacularName', 'verbatimIdentification', 'identifiedBy', 'identifiedByID', 'dateIdentified',
                   'identificationReferences', 'identificationRemarks', 'identificationVerificationStatus',
                   'externalClassificationSource'},
        'question': 'Where should {column} go?',
        'reason': ('This column is part of the identification: what the organism is, and who decided. It can stay on the '
                   'observation record, become a separate identification record, or go on the specimen record.'),
        'summary': 'Identification details',
        'tables': {
            'occurrence': {
                'label': '{field}, on the observation record',
                'gloss': 'Kept with the observation as its current identification, the way most archives store it.',
                'consequence': 'GBIF reads it as the identification of the observation.',
                'choose_when': 'Usual choice when each row has one identification.',
            },
            'identification': {
                'label': '{field}, on a separate identification record',
                'gloss': 'Stored as a separate identification linked to the observation. An observation can have '
                         'several identifications over time.',
                'consequence': 'An identification record is created for each row that has a value.',
                'choose_when': 'Choose when you keep identification history, or this describes a re-identification.',
            },
            'material': {
                'label': '{field}, on the specimen record',
                'gloss': 'Stored with the physical specimen, as the identification that belongs to the specimen itself '
                         '(for example the name on its label).',
                'consequence': 'Used only when specimen records are created; otherwise kept in your original files.',
                'choose_when': 'Choose when the identification describes the specimen rather than the observation.',
            },
        },
    },
    'record-metadata': {
        'fields': {'occurrenceReferences', 'eventReferences', 'materialReferences', 'dataGeneralizations',
                   'informationWithheld', 'feedbackURL', 'modified'},
        'question': 'What does {column} describe?',
        'reason': ('This detail can describe the observation, the event that several records share, or the specimen. '
                   'Choose the record it is about.'),
        'summary': 'Record details',
        'tables': {
            'occurrence': {
                'label': '{field}, about the observation record',
                'gloss': 'Describes the observation.',
                'consequence': 'Stored on the observation record.',
                'choose_when': 'Usual choice when the value is about the observation on this row.',
            },
            'event': {
                'label': '{field}, about the event',
                'gloss': 'Describes the visit or sampling event.',
                'consequence': 'Stored on the event record, so it applies to everything recorded during that visit.',
                'choose_when': 'Choose when the value is the same for everything from one visit.',
            },
            'identification': {
                'label': '{field}, about the identification',
                'gloss': 'Describes the identification.',
                'consequence': 'Stored on the identification record.',
                'choose_when': 'Choose when the value is about who identified the organism, or how.',
            },
            'material': {
                'label': '{field}, about the specimen record',
                'gloss': 'Describes the physical specimen or its record in the collection.',
                'consequence': 'Used only when specimen records are created; otherwise kept in your original files.',
                'choose_when': 'Choose when the value is about the kept specimen.',
            },
        },
    },
    'type-status': {
        'fields': {'typeStatus', 'typeDesignationType'},
        'question': 'Where should {column} go?',
        'reason': ('Type status says whether a specimen is a type specimen for a scientific name, such as a holotype or '
                   'paratype. It can be stored on '
                   'the specimen record or with the identification.'),
        'summary': 'Type status',
    },
    'specimen-identity': {
        'fields': {'materialEntityID', 'derivedFromMaterialEntityID', 'isPartOfMaterialEntityID'},
        'question': 'What does {column} identify?',
        'reason': ('This identifier can name the specimen on each row, the specimen it was taken from, or a larger '
                   'specimen it belongs to.'),
        'summary': 'Specimen identifiers',
    },
    'specimen-details': {
        'fields': {'catalogNumber', 'otherCatalogNumbers', 'institutionCode', 'institutionID', 'ownerInstitutionCode',
                   'collectionCode', 'collectionID', 'collectorNumber', 'preparations', 'disposition',
                   'associatedSequences', 'digitalSpecimenID', 'discipline', 'materialEntityCategory',
                   'materialEntityType', 'materialEntityRemarks', 'objectQuantity', 'objectQuantityType', 'typeOfType',
                   'verbatimLabel'},
        'question': 'Where should {column} go?',
        'reason': 'This detail belongs to a physical specimen or sample, so it is stored on the specimen record.',
        'summary': 'Specimen details',
    },
}
GENERIC_QUESTION = 'Where does {column} belong?'
GENERIC_REASON = ('This column fits more than one kind of record. Choose the record it describes, or keep it only in '
                  'your original files.')

# Lines for the "What we decided for you" summary of columns that follow the specimen answer.
SPECIMEN_DETAILS_STORED = '{count} specimen details stored on the specimen records'
SPECIMEN_DETAILS_STORED_WHY = 'Why: specimen records are created for these rows, and these details describe the specimen.'
SPECIMEN_DETAILS_KEPT = '{count} specimen details kept in your original files'
SPECIMEN_DETAILS_KEPT_WHY = 'Why: no specimen records are created for these rows, so there is no record to hold them.'
SPECIMEN_DETAILS_PENDING = '{count} specimen details follow your answer about specimen records'
SPECIMEN_DETAILS_PENDING_WHY = ('If specimen records are created, these details are stored on them; if not, they stay in your '
                                'original files.')


def field_label(field):
    """A field's plain name: 'catalogNumber' -> 'catalogue number'."""
    if field in FIELD_LABELS:
        return FIELD_LABELS[field]
    words = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', field).replace('_', ' ').lower()
    return re.sub(r'\bid\b', 'identifier', words)


def family_of(field):
    """The family of a target field name (or source term name), or None."""
    return next((name for name, family in FAMILIES.items() if field in family['fields']), None)


def _capitalised(text):
    return text[:1].upper() + text[1:]


def explain(target):
    """{'label', 'gloss', 'consequence', 'choose_when', 'technical'} for a 'table.field' target, or None.

    A target listed in TARGETS uses its own text; a field of a family with per-table text
    uses that; otherwise only a label is built from the field and table names.
    """
    if target == 'preserve':
        return dict(PRESERVE)
    if '.' not in target:
        return None
    table, field = target.split('.', 1)
    technical = f'{table} · {field}'
    if target in TARGETS:
        return {key: value for key, value in TARGETS[target].items() if key != 'decided'} | {'technical': technical}
    family = FAMILIES.get(family_of(field) or '', {})
    text = family.get('tables', {}).get(table)
    if text:
        return {'label': _capitalised(text['label'].format(field=field_label(field))), 'gloss': text['gloss'],
                'consequence': text['consequence'], 'choose_when': text['choose_when'], 'technical': technical}
    record = TABLES.get(table, {}).get('label', table.replace('-', ' '))
    return {'label': _capitalised(f'{field_label(field)}, on the {record}'), 'technical': technical}


def decided(target):
    """How an automatic choice of this target reads after 'Values … ', or a plain fallback."""
    if target in TARGETS:
        return TARGETS[target]['decided']
    if '.' not in target:
        return 'kept in your original files' if target == 'preserve' else target
    table, field = target.split('.', 1)
    record = TABLES.get(table, {}).get('label', table.replace('-', ' '))
    return f'saved as {field_label(field)}, on each {record}'


def question(field, column):
    """(title, reason) for a column question about a field of this family; column is the source column name."""
    family = FAMILIES.get(family_of(field) or '')
    if family is None:
        return GENERIC_QUESTION.format(column=column), GENERIC_REASON
    return family['question'].format(column=column), family['reason']


def is_explained(target):
    """True when the target has a gloss, not just a built label (used by the coverage test)."""
    found = explain(target)
    return bool(found and found.get('gloss'))
