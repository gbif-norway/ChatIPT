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


DWC = 'http://rs.tdwg.org/dwc/terms/'


def two_extensions(first, second):
    extension = ('<extension rowType="' + DWC + 'Occurrence" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>{name}</location></files>'
                 '<coreid index="0"/><field index="1" term="' + DWC + 'occurrenceID"/><field index="2" term="' + DWC + 'locality"/>'
                 '<field term="' + DWC + 'occurrenceStatus" default="present"/></extension>')
    meta = ('<archive xmlns="http://rs.tdwg.org/dwc/text/"><core rowType="' + DWC + 'Event" fieldsTerminatedBy="," ignoreHeaderLines="1">'
            '<files><location>event.csv</location></files><id index="0"/><field index="0" term="' + DWC + 'eventID"/>'
            '<field term="' + DWC + 'eventCategory" default="survey"/></core>'
            + extension.format(name='birds.csv') + extension.format(name='plants.csv') + '</archive>')
    return read_inputs([('meta.xml', meta.encode()), ('event.csv', b'eventID\ne1\n'),
                        ('birds.csv', first), ('plants.csv', second)])


class SeveralExtensionTests(SimpleTestCase):
    def test_two_extensions_copying_different_details_onto_one_event_need_a_choice(self):
        source = two_extensions(b'event,occ,locality\ne1,b1,Lake\n', b'event,occ,locality\ne1,p1,Hill\n')
        plan = build_plan(source)
        both = {'occurrence-events:1': 'patch', 'occurrence-events:2': 'patch'}
        self.assertFalse(option_status(plan, both)['occurrence-events:1']['patch']['available'])
        self.assertTrue(option_status(plan, {**both, 'occurrence-events:2': 'per-row'})['occurrence-events:1']['patch']['available'])
        frames, _ = convert(source, plan, {**answers(plan), **both, 'occurrence-events:2': 'per-row'})
        self.assertEqual(frames['event'].set_index('eventID').loc['e1', 'locality'], 'Lake')

    def test_agreeing_extensions_both_copy(self):
        source = two_extensions(b'event,occ,locality\ne1,b1,Lake\n', b'event,occ,locality\ne1,p1,Lake\n')
        plan = build_plan(source)
        self.assertEqual({entry(plan, f'occurrence-events:{t}')['default'] for t in (1, 2)}, {'patch'})


class CrossExtensionDateTests(SimpleTestCase):
    def test_a_year_from_one_extension_must_fit_a_date_from_another(self):
        def extension(name, term):
            return ('<extension rowType="' + DWC + 'Occurrence" fieldsTerminatedBy="," ignoreHeaderLines="1"><files><location>' + name
                    + '</location></files><coreid index="0"/><field index="1" term="' + DWC + 'occurrenceID"/><field index="2" term="'
                    + DWC + term + '"/><field term="' + DWC + 'occurrenceStatus" default="present"/></extension>')
        meta = ('<archive xmlns="http://rs.tdwg.org/dwc/text/"><core rowType="' + DWC + 'Event" fieldsTerminatedBy="," ignoreHeaderLines="1">'
                '<files><location>event.csv</location></files><id index="0"/><field index="0" term="' + DWC + 'eventID"/>'
                '<field term="' + DWC + 'eventCategory" default="survey"/></core>'
                + extension('years.csv', 'year') + extension('dates.csv', 'eventDate') + '</archive>')
        source = read_inputs([('meta.xml', meta.encode()), ('event.csv', b'eventID\ne1\n'),
                              ('years.csv', b'event,occ,year\ne1,a,2020\n'), ('dates.csv', b'event,occ,date\ne1,b,2021-01-01\n')])
        plan = build_plan(source)
        both = {'occurrence-events:1': 'patch', 'occurrence-events:2': 'patch'}
        status = option_status(plan, both)
        self.assertFalse(status['occurrence-events:1']['patch']['available'])
        self.assertFalse(status['occurrence-events:2']['patch']['available'])
        self.assertTrue(option_status(plan, {**both, 'occurrence-events:2': 'per-row'})['occurrence-events:1']['patch']['available'])


class ExtensionIndividualCountTests(SimpleTestCase):
    """individualCount on Occurrence extensions of Event cores (production datasets 566 and 567)."""

    def test_counts_become_individual_quantities_unless_the_row_supplies_its_own_quantity(self):
        events = (b'eventID,eventCategory,eventDate\n'
                  b'Nord-1985-02-11-3-0.1,survey,1985-02-11\n1f0290ea-9b2e-11e8-91c9-005056a2b019,survey,2018-08-06\n')
        source = archive(
            b'eventID,basisOfRecord,occurrenceID,individualCount,organismQuantity,organismQuantityType,occurrenceStatus,scientificName\n'
            b'Nord-1985-02-11-3-0.1,MaterialSample,Nord-1985-02-11-3-0.1-3,14,,,present,Themisto abyssorum\n'
            b'Nord-1985-02-11-3-0.1,MaterialSample,Nord-1985-02-11-3-0.1-4,0,,,absent,Themisto libellula\n'
            b'1f0290ea-9b2e-11e8-91c9-005056a2b019,Occurrence,f4702da8-6b3b-4d4c-b282-cb2de823bbf3,1,,,present,Aglantha digitale\n'
            b'1f0290ea-9b2e-11e8-91c9-005056a2b019,Occurrence,8665290b-0a6b-482c-b711-457750f159f6,2,0.899256315,ind/m3,present,Calanus finmarchicus\n',
            events)
        plan = build_plan(source)
        frames, report = convert(source, plan, answers(plan))
        rows = frames['occurrence'].set_index('occurrenceID')
        self.assertEqual((rows.loc['Nord-1985-02-11-3-0.1-3', 'organismQuantity'], rows.loc['Nord-1985-02-11-3-0.1-3', 'organismQuantityType']),
                         ('14', 'individuals'))
        # Zero stays a quantity and never decides status; the supplied absence is kept.
        self.assertEqual(rows.loc['Nord-1985-02-11-3-0.1-4', 'organismQuantity'], '0')
        self.assertEqual(rows.loc['Nord-1985-02-11-3-0.1-4', 'occurrenceStatus'], 'absent')
        self.assertEqual(rows.loc['f4702da8-6b3b-4d4c-b282-cb2de823bbf3', 'organismQuantity'], '1')
        # An explicit density takes precedence; the count stays in the originals.
        self.assertEqual((rows.loc['8665290b-0a6b-482c-b711-457750f159f6', 'organismQuantity'],
                          rows.loc['8665290b-0a6b-482c-b711-457750f159f6', 'organismQuantityType']), ('0.899256315', 'ind/m3'))
        count = disposition(report, 'individualCount')
        self.assertEqual((count['disposition'], count['mapped_rows'], count['retained_only_rows']), ('derived', 3, 1))
        self.assertEqual(count['retained_reasons'], {'explicit_quantity_present': 1, 'invalid_nonnegative_integer': 0})
        self.assertEqual(count['derived_value_examples'][0]['target_row'], 1)
        self.assertTrue(report['validation']['valid'])

    def test_a_retained_extension_is_not_counted_as_derived(self):
        source = archive(b'eventID,occurrenceID,individualCount,occurrenceStatus\ne1,o1,3,present\ne2,o2,5,present\n')
        plan = build_plan(source)
        frames, report = convert(source, plan, {**answers(plan), 'table:1': 'preserve'})
        self.assertNotIn('occurrence', frames)
        count = disposition(report, 'individualCount')
        self.assertEqual((count['disposition'], count['target']), ('retained-unmapped', 'preserve'))


class LargeEmlTests(SimpleTestCase):
    def test_metadata_is_read_from_eml_larger_than_the_evidence_limit(self):
        from api.conversion_evidence import EML_MAX_BYTES, publication_metadata
        padding = '<keywordSet>' + '<keyword>forest</keyword>' * (EML_MAX_BYTES // 20) + '</keywordSet>'
        eml = f'<eml><dataset><title>Big</title><abstract><para>Short.</para></abstract>{padding}</dataset></eml>'.encode()
        self.assertGreater(len(eml), EML_MAX_BYTES)
        source = read_inputs([('occurrence.csv', b'occurrenceID,occurrenceStatus\no1,present\n'), ('eml.xml', eml)])
        self.assertEqual(publication_metadata(source, 5000)['title'], 'Big')
