import sys
from io import StringIO
from pydantic import Field, PositiveInt
import re
import pandas as pd
import numpy as np
from api.helpers.openai_helpers import OpenAIBaseModel
from typing import Optional
from api.helpers.publish import upload_dwca, register_dataset_and_endpoint
import datetime
import uuid
import utm
from dateutil.parser import parse, ParserError
from django.template.loader import render_to_string
from django.db.models import Q
from api.helpers import discord_bot
import json


# Allowed Darwin Core terms
DARWIN_CORE_TERMS = {
    # Record-level
    "type", "modified", "language", "references", "institutionID", "collectionID", "institutionCode",
    "collectionCode", "ownerInstitutionCode", "basisOfRecord", "informationWithheld", "dynamicProperties",
    # Occurrence
    "occurrenceID", "catalogNumber", "recordNumber", "recordedBy", "recordedByID", "individualCount",
    "organismQuantity", "organismQuantityType", "sex", "lifeStage", "reproductiveCondition", "caste",
    "behavior", "vitality", "establishmentMeans", "degreeOfEstablishment", "pathway", "georeferenceVerificationStatus",
    "occurrenceStatus", "associatedMedia", "associatedOccurrences", "associatedReferences", "associatedTaxa",
    "otherCatalogNumbers", "occurrenceRemarks",
    # Organism
    "organismID", "organismName", "organismScope", "associatedOrganisms", "previousIdentifications",
    "organismRemarks",
    # MaterialEntity
    "materialEntityID", "preparations", "disposition", "verbatimLabel", "associatedSequences", "materialEntityRemarks",
    # MaterialSample
    "materialSampleID",
    # Event
    "eventID", "parentEventID", "eventType", "fieldNumber", "eventDate", "eventTime", "startDayOfYear", "endDayOfYear",
    "year", "month", "day", "verbatimEventDate", "habitat", "samplingProtocol", "sampleSizeValue", "sampleSizeUnit",
    "samplingEffort", "fieldNotes", "eventRemarks",
    # Location
    "locationID", "higherGeographyID", "higherGeography", "continent", "waterBody", "islandGroup", "island", "country",
    "countryCode", "stateProvince", "county", "municipality", "locality", "verbatimLocality", "minimumElevationInMeters",
    "maximumElevationInMeters", "verbatimElevation", "verticalDatum", "minimumDepthInMeters", "maximumDepthInMeters",
    "verbatimDepth", "minimumDistanceAboveSurfaceInMeters", "maximumDistanceAboveSurfaceInMeters", "locationAccordingTo",
    "locationRemarks", "decimalLatitude", "decimalLongitude", "geodeticDatum", "coordinateUncertaintyInMeters",
    "coordinatePrecision", "pointRadiusSpatialFit", "verbatimCoordinates", "verbatimLatitude", "verbatimLongitude",
    "verbatimCoordinateSystem", "verbatimSRS", "footprintWKT", "footprintSRS", "footprintSpatialFit", "georeferencedBy",
    "georeferencedDate", "georeferenceProtocol", "georeferenceSources", "georeferenceRemarks",
    # GeologicalContext
    "geologicalContextID", "earliestEonOrLowestEonothem", "latestEonOrHighestEonothem", "earliestEraOrLowestErathem",
    "latestEraOrHighestErathem", "earliestPeriodOrLowestSystem", "latestPeriodOrHighestSystem", "earliestEpochOrLowestSeries",
    "latestEpochOrHighestSeries", "earliestAgeOrLowestStage", "latestAgeOrHighestStage", "lowestBiostratigraphicZone",
    "highestBiostratigraphicZone", "lithostratigraphicTerms", "group", "formation", "member", "bed",
    # Identification
    "identificationID", "verbatimIdentification", "identificationQualifier", "typeStatus", "identifiedBy", "identifiedByID",
    "dateIdentified", "identificationReferences", "identificationVerificationStatus", "identificationRemarks",
    # Taxon
    "taxonID", "scientificNameID", "acceptedNameUsageID", "parentNameUsageID", "originalNameUsageID", "nameAccordingToID",
    "namePublishedInID", "taxonConceptID", "scientificName", "acceptedNameUsage", "parentNameUsage", "originalNameUsage",
    "nameAccordingTo", "namePublishedIn", "namePublishedInYear", "higherClassification", "kingdom", "phylum", "class", "order",
    "superfamily", "family", "subfamily", "tribe", "subtribe", "genus", "genericName", "subgenus", "infragenericEpithet",
    "specificEpithet", "infraspecificEpithet", "cultivarEpithet", "taxonRank", "verbatimTaxonRank", "scientificNameAuthorship",
    "vernacularName", "nomenclaturalCode", "taxonomicStatus", "nomenclaturalStatus", "taxonRemarks",
    # MeasurementOrFact
    "measurementID", "parentMeasurementID", "measurementType", "measurementValue", "measurementAccuracy", "measurementUnit",
    "measurementDeterminedBy", "measurementDeterminedDate", "measurementMethod", "measurementRemarks"
}

class BasicValidationForSomeDwCTerms(OpenAIBaseModel):
    """
    A few automatic basic checks for an Agent's tables against the Darwin Core standard.
    Returns a basic validation report.
    """
    agent_id: PositiveInt = Field(...)

    def validate_and_format_event_dates(self, df):
        failed_indices = []

        if "eventDate" in df.columns:
            for idx, date_value in df["eventDate"].items():
                try:
                    if isinstance(date_value, pd.Timestamp):  # Already a datetime object
                        formatted_date = date_value.isoformat()
                    elif isinstance(date_value, str):
                        # Handle date ranges
                        if "/" in str(date_value):
                            start_date, end_date = date_value.split("/")
                            start_date_parsed = parse(start_date).isoformat()
                            end_date_parsed = parse(end_date).isoformat()
                            formatted_date = f"{start_date_parsed}/{end_date_parsed}"
                            df.at[idx, "eventDate"] = formatted_date
                        else:
                            # Parse single date and format as ISO
                            formatted_date = parse(date_value).isoformat()
                            df.at[idx, "eventDate"] = formatted_date
                    else: 
                        failed_indices.append(idx)
                
                except (ParserError, ValueError, TypeError):
                    # If parsing fails, add the index to the failed_indices list
                    failed_indices.append(idx)

        return df, failed_indices
    
    def run(self):
        from api.models import Agent, Table
        agent = Agent.objects.get(id=self.agent_id)
        dataset = agent.dataset
        tables = dataset.table_set.all()
        table_results = {}
        for table in tables:
            table_results[table.id] = {}
            df = table.df
            if all(isinstance(col, int) for col in df.columns):            
                table_results[table.id]['table_errors'] = f'Table {table.id} appears to only have ints as column headers - most probably the column headers are row 1 or you need to make column headers. Fix this and run the validation report again.'
            else:
                standardized_columns = {col.lower(): col for col in df.columns}
                matched_columns = {}
                for term in DARWIN_CORE_TERMS:
                    if term.lower() in standardized_columns:
                        original_col = standardized_columns[term.lower()]
                        matched_columns[term] = original_col
                        if term != original_col:
                            df.rename(columns={original_col: term}, inplace=True)
                table_results[table.id]['unmatched_columns'] = [col for col in df.columns if col not in matched_columns.values()]
                
                validation_errors = {}
                allowed_basis_of_record = {'MaterialEntity', 'PreservedSpecimen', 'FossilSpecimen', 'LivingSpecimen', 'MaterialSample', 'Event', 'HumanObservation', 'MachineObservation', 'Taxon', 'Occurrence', 'MaterialCitation'}
                if 'basisOfRecord' in df.columns:
                    invalid_basis = df[~df['basisOfRecord'].isin(allowed_basis_of_record)]
                    if not invalid_basis.empty:
                        validation_errors['basisOfRecord'] = invalid_basis.index.tolist()
                if 'decimalLatitude' in df.columns:
                    df['decimalLatitude'] = pd.to_numeric(df['decimalLatitude'], errors='coerce')
                    invalid_latitude = df[(df['decimalLatitude'] < -90) | (df['decimalLatitude'] > 90)]
                    if not invalid_latitude.empty:
                        validation_errors['decimalLatitude'] = invalid_latitude.index.tolist()
                if 'decimalLongitude' in df.columns:
                    df['decimalLongitude'] = pd.to_numeric(df['decimalLongitude'], errors='coerce')
                    invalid_longitude = df[(df['decimalLongitude'] < -180) | (df['decimalLongitude'] > 180)]
                    if not invalid_longitude.empty:
                        validation_errors['decimalLongitude'] = invalid_longitude.index.tolist()
                if 'individualCount' in df.columns:
                    df['individualCount'] = pd.to_numeric(df['individualCount'], errors='coerce')
                    invalid_individual_count = df[(df['individualCount'] <= 0) | (df['individualCount'] % 1 != 0)]
                    if not invalid_individual_count.empty:
                        validation_errors['individualCount'] = invalid_individual_count.index.tolist()
                
                corrected_dates_df, event_date_error_indices = self.validate_and_format_event_dates(df)
                validation_errors['eventDate'] = event_date_error_indices
                table_results[table.id]['validation_errors'] = validation_errors

                table.df = corrected_dates_df
                table.save()
                
                general_errors = {}

                if ('organismQuantity' in df.columns and 'organismQuantityType' not in df.columns):
                    general_errors['organismQuantity'] = 'organismQuantity is a column in this Table, but the corresponding required column "organismQuantityType" is missing.'
                elif ('organismQuantityType' in df.columns and 'organismQuantity' not in df.columns):
                    general_errors['organismQuantity'] = 'organismQuantityType is a column in this Table, but the corresponding required column "organismQuantity" is missing.'
                if 'basisOfRecord' not in df.columns:
                    general_errors['basisOfRecord'] = 'basisOfRecord is missing from this Table (this is fine if the core is Taxon or if this Table is a Measurement or Fact extension)'
                if 'scientificName' not in df.columns:
                    general_errors['scientificName'] = 'scientificName is missing from this Table (this is fine if this Table is a Measurement or Fact extension)'
                if 'occurrenceID' not in df.columns:
                    general_errors['occurrenceID'] = 'occurrenceID is missing from this Table and is a required field. If this is a Measurement or Fact table, the occurrenceID column needs to link back to the core occurrence table.'
                if 'id' not in df.columns and 'ID' not in df.columns and 'measurementID' not in df.columns:
                    # It is an occurrence core table
                    if 'occurrenceID' in df.columns:
                        if not df['occurrenceID'].is_unique:
                            general_errors['occurrenceID'] = f'Is this an occurrence core table? If it is, occurrenceID must be unique - use e.g. `df["occurrenceID"] = [str(uuid.uuid4()) for _ in range(len(df))]` to force a unique value for each row. Be careful of any extension tables with linkages using the ID column.'

                table_results[table.id]['general_errors'] = general_errors
        
        print('validation report:')
        print(render_to_string('validation.txt', context={ 'tables': table_results }))
        return render_to_string('validation.txt', context={ 'tables': table_results })


class Python(OpenAIBaseModel):
    """
    Run python code using `exec(code, globals={'Dataset': Dataset, 'Table': Table, 'pd': pd, 'np': np, 'uuid': uuid, 'datetime': datetime, 're': re, 'utm': utm}, {})`. 
    I.e., you have access to an environment with those libraries and a Django database with models `Table` and `Dataset`. You cannot import anything else.
    E.g. code: `table = Table.objects.get(id=df_id); print(table.df.to_string()); dataset = Dataset.objects.get(id=1);` etc
    Notes: - Edit, delete or create new Table objects as required - remember to save changes to the database (e.g. `table.save()`). 
    - Use print() if you want to see output - a string of stdout, truncated to 2000 chars 
    - IMPORTANT NOTE #1: State does not persist - Every time this function is called, the slate is wiped clean and you will not have access to objects created previously.
    - IMPORTANT NOTE #2: If you merge or create a new Table based on old Tables, tidy up after yourself and delete irrelevant/out of date Tables.
    """
    code: str = Field(..., description="String containing valid python code to be executed in `exec()`")

    def run(self):
        code = re.sub(r"^(\s|`)*(?i:python)?\s*", "", self.code)
        code = re.sub(r"(\s|`)*$", "", code)
        old_stdout = sys.stdout
        new_stdout = StringIO()
        sys.stdout = new_stdout
        result = ''
        try:
            from api.models import Dataset, Table

            locals = {}
            globals = { 'Dataset': Dataset, 'Table': Table, 'pd': pd, 'np': np, 'uuid': uuid, 'datetime': datetime, 're': re, 'utm': utm }
            combined_context = globals.copy()
            combined_context.update(locals)
            exec(code, combined_context, combined_context)  # See https://github.com/python/cpython/issues/86084
            stdout_value = new_stdout.getvalue()
            
            if stdout_value:
                result = stdout_value
            else:
                result = f"Executed successfully without errors."
        except Exception as e:
            result = repr(e)
        finally:
            sys.stdout = old_stdout
        return str(result)[:3000]


class RollBack(OpenAIBaseModel):
    """
    USE WITH EXTREME CAUTION! RESETS TABLES COMPLETELY to the original dataframes loaded into pandas from the Excel sheet uploaded by the user. 
    ALL CHANGES WILL BE UNDONE. Use as a last resort if data columns have been accidentally deleted or lost.
    Returns: 
     - the IDs of the new, reloaded Tables (note the old Tables will be deleted)
     - a list of all Python code snippets which have been run on the old deleted Tables up till now, and the results given after running them. NOTE: code may not always have executed fully due to errors, so check the results as well. 
    """
    agent_id: PositiveInt = Field(...)

    def run(self):
        from api.models import Agent, Dataset, Table, Message
        agent = Agent.objects.get(id=self.agent_id)
        agent.dataset.table_set.all().delete()
        dfs = Dataset.get_dfs_from_user_file(agent.dataset.file, agent.dataset.file.name)
        tables = []
        for sheet_name, df in dfs.items():
            if not df.empty:
                tables.append(Table.objects.create(dataset=agent.dataset, title=sheet_name, df=df))

        # Get all code run 
        code_snippets = []
        agents = Agent.objects.filter(dataset_id=agent.dataset.id)
        function_messages = Message.objects.filter(
            agent__in=agents,
            openai_obj__tool_calls__contains=[{'function': {'name': 'Python'}}]
        )
        for msg in function_messages:
            if msg:
                for tool_call in msg.openai_obj['tool_calls']:
                    if tool_call['function']['name'] == 'Python':
                        result = Message.objects.filter(agent__in=agents, openai_obj__tool_call_id=tool_call['id']).first()
                        snippet = {
                            'code_run': tool_call['function']['arguments'],
                            'results': result.openai_obj['content']
                        }
                        code_snippets.append(snippet)
        
        discord_bot.send_discord_message(f"Dataset tables rolled back for Dataset id {agent.dataset.id}.")
        return json.dumps({'new_table_ids': [t.id for t in tables], 'code_snippets': code_snippets})


class SetBasicMetadata(OpenAIBaseModel):
    """Sets the title and description (Metadata) for a Dataset via an Agent, returns a success message or an error message"""
    agent_id: PositiveInt = Field(...)
    title: str = Field(..., description="A short but descriptive title for the dataset as a whole")
    description: str = Field(..., description="A longer description of what the dataset contains, including any important information about why the data was gathered (e.g. for a study) as well as how it was gathered.")
    user_language: str = Field('English', description="Note down if the user wants to speak in a particular language. Default is English.") 
    structure_notes: Optional[str] = Field(None, description="Optional - Use to note any significant data structural problems or oddities.") 
    suitable_for_publication_on_gbif: Optional[bool] = Field(default=True, description="An OPTIONAL boolean field, set to false if the data is deemed unsuitable for publication on GBIF.")

    def run(self):
        try:
            from api.models import Agent
            agent = Agent.objects.get(id=self.agent_id)
            dataset = agent.dataset
            dataset.title = self.title
            dataset.description = self.description
            if self.structure_notes:
                dataset.structure_notes = self.structure_notes
            if self.user_language != 'English':
                dataset.user_language = self.user_language
            if self.suitable_for_publication_on_gbif == False:
                print('Rejecting dataset')
                dataset.rejected_at = datetime.datetime.now()
            dataset.save()
            return 'Basic Metadata has been successfully set.'
        except Exception as e:
            print('There has been an error with SetBasicMetadata')
            return repr(e)[:2000]


class SetAgentTaskToComplete(OpenAIBaseModel):
    """Mark an Agent's task as complete"""
    agent_id: PositiveInt = Field(...)

    def run(self):
        from api.models import Agent
        try:
            agent = Agent.objects.get(id=self.agent_id)
            agent.completed_at = datetime.datetime.now()
            agent.save()
            print('Marking as complete...')
            return f'Task marked as complete for agent id {self.agent_id} .'
        except Exception as e:
            return repr(e)[:2000]


class PublishDwC(OpenAIBaseModel):
    """
    The final step required to publish the user's data to GBIF. 
    Generates a darwin core archive and uploads it to a server, then registers it with the GBIF API. 
    At the moment this only publishes the test GBIF platform, not production.
    Returns the GBIF URL.
    """
    agent_id: PositiveInt = Field(...)

    def run(self):
        from api.models import Agent
        try:
            agent = Agent.objects.get(id=self.agent_id)
            dataset = agent.dataset
            tables = dataset.table_set.all()
            
            core_table = next((table for table in tables if 'scientificName' in table.df.columns), None)
            if not core_table:
                core_table = next((table for table in tables if 'kingdom' in table.df.columns), tables[0])
            if not core_table:
                return 'Validation error: The occurrence core table should contain species names/identifications in a field called scientificName. If species identifications are not known, this can even be as broad as kingdom level, i.e. df["scientificName"] = "Animalia"'

            extension_tables = [table for table in tables if table != core_table]
            mof_table =  extension_tables[0] if extension_tables else None
            if mof_table:
                url = upload_dwca(core_table.df, dataset.title, dataset.description, mof_table.df)
            else:
                url = upload_dwca(core_table.df, dataset.title, dataset.description)
            dataset.dwca_url = url
            gbif_url = register_dataset_and_endpoint(dataset.title, dataset.description, dataset.dwca_url)
            dataset.gbif_url = gbif_url
            dataset.published_at = datetime.datetime.now()
            # import pdb; pdb.set_trace()
            dataset.save()
            return(f'Successfully published. GBIF URL: {gbif_url}, Darwin core archive upload: {url}')
        except Exception as e:
            return repr(e)[:2000]

