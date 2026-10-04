from django.test import SimpleTestCase

from api.dwca_conversion import build_plan, convert, option_status, validate_decisions
from api.dwca_import import ImportFailure, read_inputs

EVENTS = b'eventID,eventCategory,eventDate\ne1,survey,2020-06-01\ne2,survey,2020-06-02\n'


def archive(occurrences, events=EVENTS):
    return read_inputs([('event.csv', events), ('occurrence.csv', occurrences)])


def entry(plan, identifier):
    return next(item for item in [*plan['issues'], *plan['automatic_choices']] if item['id'] == identifier)


def answers(plan):
    return {item['id']: item['options'][0]['value'] for item in plan['issues']}


def disposition(report, term):
    return next(column for column in report['columns'] if column['source_table'] == 'occurrence.csv' and column['term'].endswith('/' + term))


class OccurrenceEventDetailTests(SimpleTestCase):
    """Event details supplied on Occurrence extension rows (production dataset 543)."""

    def test_agreeing_details_are_copied_onto_the_linked_event_and_reported_accurately(self):
        source = archive(b'eventID,occurrenceID,occurrenceStatus,decimalLatitude,decimalLongitude,year,locality\n'
                         b'e1,o1,present,60.1,10.2,2020,Lake\ne1,o2,present,60.1,10.2,2020,Lake\ne2,o3,present,61,11,2020,Hill\n')
        plan = build_plan(source)
        self.assertEqual(entry(plan, 'occurrence-events:1')['default'], 'patch')
        frames, report = convert(source, plan, answers(plan))
        events = frames['event'].set_index('eventID')
        self.assertEqual(events.loc['e1', 'decimalLatitude'], '60.1')
        self.assertEqual(events.loc['e2', 'locality'], 'Hill')
        self.assertEqual(len(frames['event']), 2)
        self.assertEqual(disposition(report, 'decimalLatitude')['mapped_rows'], 3)
        self.assertEqual(disposition(report, 'decimalLatitude')['disposition'], 'mapped+retained')
        self.assertTrue(report['validation']['valid'])

    def test_disagreeing_details_need_a_choice_and_each_row_can_be_its_own_event(self):
        source = archive(b'eventID,occurrenceID,occurrenceStatus,decimalLatitude,decimalLongitude\n'
                         b'e1,o1,present,60.1,10.2\ne1,o2,present,60.3,10.4\ne2,o3,present,61,11\n')
        plan = build_plan(source)
        question = next(item for item in plan['issues'] if item['id'] == 'occurrence-events:1')
        self.assertEqual(question['authority'], 'ai-reviewable')
        self.assertTrue(next(option for option in question['options'] if option['value'] == 'per-row')['assertion'])
        self.assertIn('differs between occurrences of the same event', question['reason'])
        self.assertFalse(option_status(plan, {'occurrence-events:1': 'patch'})['occurrence-events:1']['patch']['available'])
        with self.assertRaises(ImportFailure):
            validate_decisions(plan, {**answers(plan), 'occurrence-events:1': 'patch'})
        frames, report = convert(source, plan, {**answers(plan), 'occurrence-events:1': 'per-row'})
        events, occurrences = frames['event'], frames['occurrence']
        children = events[events['parentEvent_fk'] != '']
        self.assertEqual(len(children), 3)
        self.assertTrue((children['eventID'] == '').all() if 'eventID' in children else True)
        self.assertEqual(set(occurrences['event_fk']), set(children['event_pk']))
        self.assertEqual(sorted(children['decimalLatitude']), ['60.1', '60.3', '61'])
        self.assertTrue(report['validation']['valid'])

    def test_retained_details_are_reported_as_retained(self):
        source = archive(b'eventID,occurrenceID,occurrenceStatus,decimalLatitude,decimalLongitude\ne1,o1,present,60.1,10.2\ne1,o2,present,60.3,10.4\n')
        plan = build_plan(source)
        frames, report = convert(source, plan, {**answers(plan), 'occurrence-events:1': 'preserve'})
        self.assertNotIn('decimalLatitude', frames['event'])
        self.assertEqual(disposition(report, 'decimalLatitude')['disposition'], 'retained-unmapped')

    def test_year_must_agree_with_the_linked_event_date(self):
        source = archive(b'eventID,occurrenceID,occurrenceStatus,year\ne1,o1,present,2019\ne2,o2,present,2020\n')
        plan = build_plan(source)
        question = next(item for item in plan['issues'] if item['id'] == 'occurrence-events:1')
        self.assertIn('year disagrees with the eventDate for 1 events', question['reason'])

    def test_years_inside_an_event_date_interval_agree(self):
        events = b'eventID,eventCategory,eventDate\ne1,survey,1938/2016\n'
        source = archive(b'eventID,occurrenceID,occurrenceStatus,year\ne1,o1,present,1938\ne1,o2,present,1938\n', events)
        plan = build_plan(source)
        self.assertEqual(entry(plan, 'occurrence-events:1')['default'], 'patch')
        outside = archive(b'eventID,occurrenceID,occurrenceStatus,year\ne1,o1,present,1937\n', events)
        question = next(item for item in build_plan(outside)['issues'] if item['id'] == 'occurrence-events:1')
        self.assertTrue(question['reason'].startswith('occurrence.csv has event details'))
        self.assertIn('year disagrees with the eventDate', question['reason'])

    def test_details_disagreeing_with_the_event_itself_cannot_be_copied(self):
        events = b'eventID,eventCategory,decimalLatitude,decimalLongitude\ne1,survey,59,9\n'
        source = archive(b'eventID,occurrenceID,occurrenceStatus,decimalLatitude,decimalLongitude\ne1,o1,present,60,10\n', events)
        plan = build_plan(source)
        self.assertIn('disagrees with the linked event', next(item for item in plan['issues'] if item['id'] == 'occurrence-events:1')['reason'])


class ScientificNameTests(SimpleTestCase):
    def test_supplied_authorship_is_removed_exactly_and_the_text_kept_verbatim(self):
        source = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus,scientificName,scientificNameAuthorship\n'
                                                 b'o1,present,Betula nana L.,L.\no2,present,Picea abies,(L.) H.Karst.\n')])
        plan = build_plan(source)
        self.assertFalse(any(item['kind'] == 'name-semantics' for item in plan['issues']))
        frames, _ = convert(source, plan, answers(plan))
        occurrences = frames['occurrence'].set_index('occurrenceID')
        self.assertEqual(occurrences.loc['o1', 'scientificName'], 'Betula nana')
        self.assertEqual(occurrences.loc['o1', 'scientificNameAuthorship'], 'L.')
        self.assertEqual(occurrences.loc['o1', 'verbatimIdentification'], 'Betula nana L.')
        self.assertEqual(occurrences.loc['o2', 'scientificName'], 'Picea abies')

    def test_names_left_out_of_scientific_name_stay_in_verbatim_identification(self):
        source = read_inputs([('occurrence.csv', 'occurrenceID,occurrenceStatus,scientificName\n'
                                                 'o1,present,Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti\n'.encode())])
        plan = build_plan(source)
        question = next(item for item in plan['issues'] if item['kind'] == 'name-semantics')
        self.assertIn('verbatimIdentification', next(option for option in question['options'] if option['value'] == 'preserve')['label'])
        frames, report = convert(source, plan, {**answers(plan), question['id']: 'preserve'})
        occurrence = frames['occurrence'].iloc[0]
        self.assertEqual(occurrence['verbatimIdentification'], 'Betula pubescens subsp. czerepanovii (N.I.Orlova) Hämet-Ahti')
        self.assertNotIn('scientificName', frames['occurrence'])
        name = next(column for column in report['columns'] if column['term'].endswith('/scientificName'))
        self.assertEqual((name['disposition'], name['mapped_rows']), ('derived', 1))

    def test_columns_without_a_data_package_field_are_flagged(self):
        plan = build_plan(read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus,basisOfRecord,dynamicProperties\no1,present,HumanObservation,{}\n')]))
        flags = {column['term'].rsplit('/', 1)[-1]: column.get('unmapped') for column in plan['columns']}
        self.assertEqual((flags['basisOfRecord'], flags['dynamicProperties']), ('no-target', 'no-target'))
        self.assertFalse(any(warning['id'].startswith('column:') and 'basisOfRecord' in warning['title'] for warning in plan['warnings']))


class ReviewFindingTests(SimpleTestCase):
    """Fixes from the joint review of the conversion-fixes branch."""

    def test_child_events_keep_a_supplied_category(self):
        source = archive(b'eventID,occurrenceID,occurrenceStatus,eventCategory\ne1,o1,present,survey\n')
        plan = build_plan(source)
        frames, report = convert(source, plan, {**answers(plan), 'occurrence-events:1': 'per-row'})
        child = frames['event'][frames['event']['parentEvent_fk'] != ''].iloc[0]
        self.assertEqual(child['eventCategory'], 'survey')

    def test_blank_verbatim_cells_are_filled_from_the_row_name(self):
        source = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus,scientificName,verbatimIdentification\n'
                                                 b'o1,present,Aus bus,A. bus (field note)\no2,present,Cus dus,\n')])
        plan = build_plan(source)
        frames, _ = convert(source, plan, answers(plan))
        verbatim = frames['occurrence'].set_index('occurrenceID')['verbatimIdentification']
        self.assertEqual((verbatim['o1'], verbatim['o2']), ('A. bus (field note)', 'Cus dus'))

    def test_identification_rows_keep_their_own_name_text(self):
        source = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus,scientificName\no1,present,Aus bus\n'),
                              ('identification.csv', b'occurrenceID,scientificName,identifiedBy\no1,Cus dus,Ann\n')])
        plan = build_plan(source)
        frames, _ = convert(source, plan, answers(plan))
        texts = set(frames['identification']['verbatimIdentification'])
        self.assertIn('Cus dus', texts)
        self.assertEqual(frames['occurrence'].iloc[0]['verbatimIdentification'], 'Aus bus')

    def test_copied_category_must_agree_with_the_missing_category_choice(self):
        events = b'eventID\ne1\ne2\n'
        source = archive(b'eventID,occurrenceID,occurrenceStatus,eventCategory\ne1,o1,present,survey\ne2,o2,present,survey\n', events)
        plan = build_plan(source)
        status = option_status(plan, {'event-category': 'occurrence', 'occurrence-events:1': 'patch'})
        self.assertFalse(status['occurrence-events:1']['patch']['available'])
        self.assertTrue(option_status(plan, {'event-category': 'survey', 'occurrence-events:1': 'patch'})['occurrence-events:1']['patch']['available'])
        frames, _ = convert(source, plan, {**answers(plan), 'event-category': 'survey', 'occurrence-events:1': 'patch'})
        self.assertEqual(set(frames['event']['eventCategory']), {'survey'})


class PublicationMetadataTests(SimpleTestCase):
    def test_long_abstracts_are_not_shortened_to_the_evidence_limit(self):
        from api.conversion_evidence import publication_metadata
        abstract = 'Long abstract sentence. ' * 200
        eml = f'<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0"><dataset><title>Birds</title><abstract><para>{abstract}</para><para>Second.</para></abstract></dataset></eml:eml>'
        source = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus\no1,present\n'), ('eml.xml', eml.encode())])
        metadata = publication_metadata(source, 5000)
        self.assertEqual(metadata['title'], 'Birds')
        self.assertTrue(metadata['description'].endswith('Second.') or len(metadata['description']) == 5000)
        self.assertGreater(len(metadata['description']), 2000)
        self.assertNotIn('…', metadata['description'])
