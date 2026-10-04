import copy

from django.test import SimpleTestCase

from api.dwca_hierarchy import resolve_parents
from api.dwca_humboldt import ECO, ECOIRI
from api.dwca_scientific import DWC, audit_hierarchy, parse_event_date

DATE, LAT, LON, RADIUS, DATUM = (DWC + term for term in ('eventDate', 'decimalLatitude', 'decimalLongitude',
                                                         'coordinateUncertaintyInMeters', 'geodeticDatum'))


def chain(*event_values, surveys=None):
    """Positions 0..n-1 where each node's parent is the next position; returns audit result."""
    nodes = [{'event_id': f'e{i}', 'parents': {f'e{i + 1}' if i + 1 < len(event_values) else ''}, 'rows': [i + 1]}
             for i in range(len(event_values))]
    links = resolve_parents(nodes)['links']
    return audit_hierarchy(nodes, links, dict(enumerate(event_values)), surveys or {})


def only(result, kind, child=0):
    matches = [check for check in result['checks'] if check['kind'] == kind and check['child'] == child]
    assert len(matches) == 1, matches
    return matches[0]


def survey(row, **terms):
    return {'table': 1, 'row': row, 'values': terms}


def place(lat, lon, radius='1000', datum='WGS84', **extra):
    # These fixtures explicitly supply fine coordinate precision. A separate
    # test below exercises coordinates supplied at whole-degree precision.
    lat = lat if '.' in lat else lat + '.000000'
    lon = lon if '.' in lon else lon + '.000000'
    return {LAT: lat, LON: lon, RADIUS: radius, DATUM: datum, **extra}


class TemporalTests(SimpleTestCase):
    def status(self, child, parent):
        return only(chain({DATE: child}, {DATE: parent}), 'temporal')['status']

    def test_partial_precision_is_an_envelope_not_an_exact_interval(self):
        self.assertEqual(self.status('2019', '2019-06'), 'not-demonstrated')
        self.assertEqual(self.status('2019-03-05', '2019'), 'not-demonstrated')
        self.assertEqual(self.status('2019', '2018-06'), 'contradiction')
        self.assertEqual(self.status('2019-03-05', '2019-01-01/2019-12-31'), 'compatible')
        self.assertEqual(self.status('2020-01-01', '2019-01-01/2019-12-31'), 'not-demonstrated')  # time zones unknown
        self.assertEqual(self.status('2020-01-03', '2019-01-01/2019-12-31'), 'contradiction')

    def test_time_zones(self):
        self.assertEqual(self.status('2019-12-31T23:00-05:00', '2019-01-01T00:00Z/2019-12-31T23:59Z'), 'contradiction')
        self.assertEqual(self.status('2019-12-31T23:00-05:00', '2019-01-01/2019-12-31'), 'not-demonstrated')
        self.assertEqual(self.status('2019-06-01T10:00+02:00', '2019-06-01T07:00Z/2019-06-01T09:00Z'), 'compatible')

    def test_supported_and_unsupported_forms(self):
        self.assertIsNotNone(parse_event_date('2007-11-13/15'))
        self.assertEqual(parse_event_date('2007-11-13/15')[2:], parse_event_date('2007-11-15')[2:])
        for value in ('2019-05/2019-01', '2019-02-30', '13/03/2019', '2019-W01', '2019/..', '', '2019-06-01T24:00',
                      '2019-06-01T10:00+15:00', '2019-06-01T10:00+00:99'):
            self.assertIsNone(parse_event_date(value), value)
        self.assertEqual(self.status('spring 2019', '2019'), 'incomparable')
        result = chain({DATE: '2019'}, {})
        self.assertEqual((only(result, 'temporal')['status'], only(result, 'temporal')['ancestor']), ('incomparable', None))

    def test_missing_middle_compares_with_nearest_dated_ancestor_and_invalid_is_not_skipped(self):
        result = chain({DATE: '2020'}, {}, {DATE: '2018'})
        check = only(result, 'temporal')
        self.assertEqual((check['parent'], check['ancestor'], check['status']), (1, 2, 'contradiction'))
        self.assertEqual(check['evidence'], {'child': '2020', 'ancestor': '2018'})
        invalid = only(chain({DATE: '2020'}, {DATE: 'unknown'}, {DATE: '2018'}), 'temporal')
        self.assertEqual((invalid['ancestor'], invalid['status']), (1, 'incomparable'))

    def test_reduced_precision_parent_cannot_hide_remote_ancestor_conflict(self):
        result = chain({DATE: '2018-12'}, {DATE: '2018'}, {DATE: '2018-01-01/2018-06-30'})
        self.assertEqual(only(result, 'temporal')['status'], 'not-demonstrated')
        conflict = only(result, 'temporal-ancestor')
        self.assertEqual((conflict['parent'], conflict['ancestor'], conflict['status']), (1, 2, 'contradiction'))
        self.assertEqual(conflict['evidence']['ancestor'], '2018-01-01/2018-06-30')
        invalid = chain({DATE: '2020'}, {DATE: 'unknown'}, {DATE: '2018'})
        self.assertEqual(only(invalid, 'temporal')['status'], 'incomparable')
        self.assertEqual(only(invalid, 'temporal-ancestor')['status'], 'contradiction')

    def test_deep_chain_is_linear_and_iterative(self):
        size = 20000
        values = [{} for _ in range(size)]
        values[0], values[-1] = {DATE: '2020'}, {DATE: '2018'}
        result = chain(*values)
        self.assertEqual(len([check for check in result['checks'] if check['kind'] == 'temporal']), size - 1)
        self.assertEqual((only(result, 'temporal')['ancestor'], only(result, 'temporal')['status']), (size - 1, 'contradiction'))


class SpatialTests(SimpleTestCase):
    def status(self, child, parent):
        return only(chain(child, parent), 'spatial')

    def test_only_disjoint_qualified_circles_contradict(self):
        self.assertEqual(self.status(place('0', '0'), place('0', '1'))['status'], 'contradiction')  # ~111 km apart
        self.assertEqual(self.status(place('0', '0', '60000'), place('0', '1', '60000'))['status'], 'not-demonstrated')
        # A circle fully inside another still does not demonstrate containment of the actual locations.
        self.assertEqual(self.status(place('0', '0', '10'), place('0', '0', '100000'))['status'], 'not-demonstrated')

    def test_center_outside_parent_radius_is_not_enough(self):
        self.assertEqual(self.status({LAT: '0', LON: '0', DATUM: 'WGS84'}, place('0', '1'))['status'], 'incomparable')
        self.assertEqual(self.status(place('0', '0'), {LAT: '0', LON: '1', DATUM: 'WGS84'})['status'], 'incomparable')

    def test_datum_radius_and_qualifiers(self):
        for child in (place('0', '0', datum=''), place('0', '0', datum='not recorded'), place('0', '0', datum='NAD27'),
                      place('0', '0', radius='0'), place('0', '0', radius='-5'), place('91', '0')):
            self.assertEqual(self.status(child, place('0', '1'))['status'], 'incomparable', child)
        self.assertEqual(self.status(place('0', '0', datum='EPSG:4326'), place('0', '1'))['status'], 'contradiction')
        for extra in ({DWC + 'footprintWKT': 'POLYGON ((0 0, 1 0, 1 1, 0 0))'}, {DWC + 'dataGeneralizations': 'rounded'},
                      {DWC + 'informationWithheld': 'exact location'}):
            check = self.status(place('0', '0', **extra), place('0', '1'))
            self.assertEqual(check['status'], 'incomparable')
            self.assertEqual(check['evidence']['child'][next(iter(extra))], next(iter(extra.values())))
            self.assertTrue(check['evidence']['calculated']['circles_disjoint'])

    def test_antimeridian_and_missing_ancestor(self):
        self.assertEqual(self.status(place('0', '179.999'), place('0', '-179.999'))['status'], 'not-demonstrated')
        self.assertEqual(only(chain(place('0', '0'), {}), 'spatial')['status'], 'incomparable')

    def test_coarse_coordinates_and_supplied_precision_widen_tolerance(self):
        coarse = {LAT: '0', LON: '0', RADIUS: '1000', DATUM: 'WGS84'}
        other = {**coarse, LON: '1'}
        self.assertEqual(self.status(coarse, other)['status'], 'not-demonstrated')
        precise = place('0', '0', **{DWC + 'coordinatePrecision': '1'})
        self.assertEqual(self.status(precise, place('0', '1'))['status'], 'not-demonstrated')
        invalid = place('0', '0', **{DWC + 'coordinatePrecision': 'unknown'})
        self.assertEqual(self.status(invalid, place('0', '1'))['status'], 'incomparable')

    def test_spherical_distance_error_is_not_a_borderline_contradiction(self):
        # Near the equator along a meridian, the mean-radius distance exceeds
        # the WGS84 distance. Leave a margin instead of declaring separation.
        self.assertEqual(self.status(place('0', '0', radius='55300'),
                                     place('1', '0', radius='55300'))['status'], 'not-demonstrated')


class SurveyTests(SimpleTestCase):
    def audit(self, child_rows, parent_rows):
        return chain({}, {}, surveys={0: child_rows, 1: parent_rows})

    def kinds(self, result):
        return {(check['kind'], check['status']) for check in result['checks'] if check['kind'] not in {'temporal', 'spatial'}}

    def test_parent_false_child_true_and_other_differences_are_not_findings(self):
        child = survey(2, **{ECO + 'targetTaxonomicScope': 'Aves', ECO + 'isTaxonomicScopeFullyReported': 'true',
                             ECO + 'hasVouchers': 'true', ECO + 'protocolNames': 'pointCount', ECO + 'siteCount': '9',
                             ECO + 'samplingEffortValue': '500', ECO + 'samplingEffortUnit': 'h'})
        parent = survey(1, **{ECO + 'targetTaxonomicScope': 'Aves', ECO + 'isTaxonomicScopeFullyReported': 'false',
                              ECO + 'hasVouchers': 'false', ECO + 'protocolNames': 'transect', ECO + 'siteCount': '2',
                              ECO + 'samplingEffortValue': '5', ECO + 'samplingEffortUnit': 'h'})
        result = self.audit([child], [parent])
        self.assertEqual(self.kinds(result), set())
        self.assertFalse(result['has_findings'])

    def test_true_to_false_with_same_whole_scope_is_a_signal_otherwise_incomparable(self):
        scope = {ECO + 'targetTaxonomicScope': 'Aves | Mammalia'}
        parent = survey(1, **scope, **{ECO + 'isTaxonomicScopeFullyReported': 'TRUE'})
        same = survey(2, **{ECO + 'targetTaxonomicScope': 'Mammalia | Aves', ECO + 'isTaxonomicScopeFullyReported': 'false'})
        self.assertIn(('completeness', 'conflict-signal'), self.kinds(self.audit([same], [parent])))
        narrower = survey(2, **scope, **{ECO + 'isTaxonomicScopeFullyReported': 'false', ECO + 'targetLifeStageScope': 'adult'})
        self.assertIn(('completeness', 'incomparable'), self.kinds(self.audit([narrower], [parent])))
        tcr = [survey(1, **scope, **{ECO + 'taxonCompletenessReported': 'reportedComplete'}),
               survey(2, **scope, **{ECO + 'taxonCompletenessReported': 'reportedIncomplete'})]
        self.assertIn(('completeness', 'conflict-signal'), self.kinds(self.audit([tcr[1]], [tcr[0]])))

    def test_literal_and_iri_scopes_are_not_equivalent(self):
        parent = survey(1, **{ECO + 'targetTaxonomicScope': 'Aves', ECO + 'isTaxonomicScopeFullyReported': 'true'})
        child = survey(2, **{ECOIRI + 'targetTaxonomicScope': 'https://www.gbif.org/species/212',
                             ECO + 'isTaxonomicScopeFullyReported': 'false'})
        result = self.audit([child], [parent])
        self.assertIn(('completeness', 'incomparable'), self.kinds(result))
        self.assertFalse([check for check in result['checks'] if check['status'] == 'reporting-gap'])
        check = only(result, 'inference-term')
        self.assertEqual((check['status'], check['evidence']['child_counterpart']),
                         ('incomparable', {ECOIRI + 'targetTaxonomicScope': 'https://www.gbif.org/species/212'}))
        flagless = survey(2, **{ECOIRI + 'targetTaxonomicScope': 'https://www.gbif.org/species/212'})
        statuses = {c['evidence']['term']: c['status'] for c in self.audit([flagless], [parent])['checks'] if c['kind'] == 'inference-term'}
        self.assertEqual(statuses[ECO + 'isTaxonomicScopeFullyReported'], 'reporting-gap')

    def test_flags_without_declared_scope_cannot_be_compared_as_same_scope(self):
        result = self.audit([survey(2, **{ECO + 'isTaxonomicScopeFullyReported': 'false'})],
                            [survey(1, **{ECO + 'isTaxonomicScopeFullyReported': 'true'})])
        self.assertEqual(only(result, 'completeness')['status'], 'incomparable')

    def test_exact_inclusion_exclusion_collision_is_signal_only(self):
        parent = survey(1, **{ECO + 'targetTaxonomicScope': 'Aves', ECO + 'excludedTaxonomicScope': 'Larus | Fulmarus'})
        child = survey(2, **{ECO + 'targetTaxonomicScope': 'Larus', ECO + 'excludedTaxonomicScope': 'Fulmarus'})
        check = only(self.audit([child], [parent]), 'scope-collision')
        self.assertEqual((check['status'], check['evidence']['tokens']), ('conflict-signal', ['Larus']))
        self.assertEqual(check['survey_rows'], {'child': [{'table': 1, 'row': 2}], 'ancestor': [{'table': 1, 'row': 1}]})
        other = survey(2, **{ECO + 'targetTaxonomicScope': 'larus', ECO + 'excludedTaxonomicScope': 'Fulmarus'})
        self.assertNotIn('scope-collision', {kind for kind, _ in self.kinds(self.audit([other], [parent]))})

    def test_reporting_gaps_never_fill_values(self):
        parent = survey(1, **{ECO + 'targetHabitatScope': 'oak savannah', ECO + 'taxonCompletenessProtocols': 'expert list'})
        result = self.audit([], [parent])
        gaps = sorted(check['evidence']['term'] for check in result['checks'] if check['status'] == 'reporting-gap')
        self.assertEqual(gaps, [ECO + 'targetHabitatScope', ECO + 'taxonCompletenessProtocols'])
        self.assertTrue(result['has_findings'])

    def test_areas_compare_exact_units_without_sums_or_conversion(self):
        parent = survey(1, **{ECO + 'geospatialScopeAreaValue': '25', ECO + 'geospatialScopeAreaUnit': 'ha',
                              ECO + 'totalAreaSampledValue': '1', ECO + 'totalAreaSampledUnit': 'ha'})
        child = survey(2, **{ECO + 'geospatialScopeAreaValue': '25.4', ECO + 'geospatialScopeAreaUnit': 'ha',
                             ECO + 'totalAreaSampledValue': '0.5', ECO + 'totalAreaSampledUnit': 'ha'})
        kinds = self.kinds(self.audit([child], [parent]))
        self.assertIn(('area:geospatialScopeArea>geospatialScopeArea', 'conflict-signal'), kinds)
        self.assertIn(('area:totalAreaSampled>geospatialScopeArea', 'compatible'), kinds)
        self.assertNotIn('area:totalAreaSampled>totalAreaSampled', {kind for kind, _ in kinds})
        self.assertNotIn('contradiction', {status for _, status in kinds})
        metres = survey(2, **{ECO + 'geospatialScopeAreaValue': '300000', ECO + 'geospatialScopeAreaUnit': 'm²'})
        self.assertIn(('area:geospatialScopeArea>geospatialScopeArea', 'incomparable'), self.kinds(self.audit([metres], [parent])))
        for value in ('-1', '1e9999', 'NaN'):
            invalid = survey(2, **{ECO + 'geospatialScopeAreaValue': value, ECO + 'geospatialScopeAreaUnit': 'ha'})
            self.assertEqual(only(self.audit([invalid], [parent]), 'area:geospatialScopeArea>geospatialScopeArea')['status'], 'incomparable')

    def test_multiple_distinct_rows_are_ambiguous_identical_rows_compare_once(self):
        a = survey(1, **{ECO + 'isTaxonomicScopeFullyReported': 'true'})
        b = survey(3, **{ECO + 'isTaxonomicScopeFullyReported': 'false'})
        result = self.audit([survey(2)], [a, b])
        check = only(result, 'survey-ambiguous')
        self.assertEqual((check['status'], [ref['row'] for ref in check['survey_rows']['ancestor']]), ('incomparable', [1, 3]))
        self.assertEqual(len([c for c in result['checks'] if c['kind'] not in {'temporal', 'spatial'}]), 1)
        self.assertTrue(result['has_findings'])
        reverse = self.audit([survey(2)], [b, a])
        self.assertEqual(only(reverse, 'survey-ambiguous')['survey_rows'], check['survey_rows'])
        twin = survey(3, **{ECO + 'isTaxonomicScopeFullyReported': 'true'})
        gap = only(self.audit([survey(2)], [a, twin]), 'inference-term')
        self.assertEqual([ref['row'] for ref in gap['survey_rows']['ancestor']], [1, 3])

    def test_survey_checks_use_nearest_surveyed_ancestor(self):
        parent = survey(1, **{ECO + 'targetTaxonomicScope': 'Aves'})
        result = chain({}, {}, {}, surveys={2: [parent]})
        self.assertEqual([(c['child'], c['ancestor']) for c in result['checks'] if c['kind'] == 'inference-term'], [(0, 2), (1, 2)])


class ContractTests(SimpleTestCase):
    def test_inputs_are_not_mutated_and_output_is_deterministic(self):
        nodes = [{'event_id': 'c', 'parents': {'p'}, 'rows': [1]}, {'event_id': 'p', 'parents': {''}, 'rows': [2]}]
        links = {0: 1}
        values = {0: {DATE: '2021', **place('0', '0')}, 1: {DATE: '2019', **place('0', '1')}}
        surveys = {1: [survey(1, **{ECO + 'targetTaxonomicScope': 'Aves'})]}
        before = copy.deepcopy((nodes, links, values, surveys))
        first = audit_hierarchy(nodes, links, values, surveys)
        self.assertEqual(before, (nodes, links, values, surveys))
        self.assertEqual(first, audit_hierarchy(*copy.deepcopy(before)))
        self.assertEqual(set(first['counts']), {'contradiction', 'conflict-signal', 'reporting-gap', 'not-demonstrated',
                                                'incomparable', 'compatible'})
        self.assertEqual((first['counts']['contradiction'], first['counts']['reporting-gap']), (2, 1))
        self.assertTrue(first['has_findings'])
        self.assertEqual([check['kind'] for check in first['checks']], ['temporal', 'spatial', 'inference-term'])

    def test_every_link_gets_temporal_and_spatial_outcomes_without_data(self):
        result = chain({}, {}, {})
        self.assertEqual([(c['child'], c['kind'], c['status']) for c in result['checks']],
                         [(0, 'temporal', 'incomparable'), (0, 'spatial', 'incomparable'),
                          (1, 'temporal', 'incomparable'), (1, 'spatial', 'incomparable')])
        self.assertFalse(result['has_findings'])
