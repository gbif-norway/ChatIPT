"""Carry external taxon concepts into a Taxon core without changing its joins."""
import re
from collections import defaultdict

from api import taxon_matching


COL_TAXON_URL = re.compile(
    rf'{re.escape(taxon_matching.CHECKLISTBANK_API)}/dataset/[1-9][0-9]*/taxon/[a-zA-Z0-9][a-zA-Z0-9._~-]*'
)


def _text(value):
    return '' if taxon_matching._is_blank(value) else str(value).strip()


def fill_concept_ids(core, resources):
    """Fill blanks where an explicit row key and the written name identify one concept."""
    identifications = resources.get('identification')
    if identifications is None or not {'scientificName', 'taxonID'} <= set(identifications):
        return 0
    columns = {str(column).casefold(): column for column in core}
    key_column = columns.get('taxonid', columns.get('id'))
    if key_column is None or 'scientificname' not in columns:
        return 0

    organisms = resources.get('organism')
    organism_ids = set() if organisms is None or 'organism_pk' not in organisms else {
        _text(value) for value in organisms['organism_pk']
    }
    identification_ids = {_text(value) for value in identifications.get('identification_pk', ())}
    overlapping_ids = organism_ids & identification_ids
    by_key = defaultdict(set)
    identification_columns = {'identification_pk', 'organism_fk', 'scientificName', 'taxonRank',
                              'scientificNameAuthorship', 'taxonID', 'isAcceptedIdentification',
                              'taxonFormula', 'identificationQualifier'}
    for row in identifications.loc[:, identifications.columns.isin(identification_columns)].to_dict('records'):
        name = _text(row['scientificName'])
        rank = _text(row.get('taxonRank')).casefold()
        author = _text(row.get('scientificNameAuthorship'))
        reference = _text(row['taxonID'])
        if (_text(row.get('isAcceptedIdentification')).casefold() in {'false', '0', 'no'}
                or _text(row.get('identificationQualifier'))
                or _text(row.get('taxonFormula')).casefold() not in {'', 'a'}):
            reference = ''
        keys = {_text(row.get('identification_pk'))}
        organism = _text(row.get('organism_fk'))
        if organism in organism_ids:
            keys.add(organism)
        for key in keys - {''}:
            by_key[key, name].add((rank, author, reference))

    concept_column = columns.get('taxonconceptid', 'taxonConceptID')
    concepts = core[concept_column].tolist() if concept_column in core else [None] * len(core)
    filled = 0
    source_columns = [key_column, columns['scientificname']]
    source_columns += [columns[field] for field in ('taxonrank', 'scientificnameauthorship') if field in columns]
    for index, row in enumerate(core[source_columns].to_dict('records')):
        if not taxon_matching._is_blank(concepts[index]):
            continue
        key, name = _text(row[key_column]), _text(row[columns['scientificname']])
        if not key or not name or key in overlapping_ids:
            continue
        rank = _text(row.get(columns.get('taxonrank'))).casefold()
        author = _text(row.get(columns.get('scientificnameauthorship')))
        candidates = {url for r, a, url in by_key.get((key, name), ())
                      if (not rank or not r or rank == r) and (not author or not a or author == a)}
        if len(candidates) == 1:
            url = next(iter(candidates))
            if COL_TAXON_URL.fullmatch(url):
                concepts[index] = url
                filled += 1
    if filled:
        core[concept_column] = concepts
    return filled
