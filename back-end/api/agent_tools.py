import sys
from io import StringIO
from pydantic import Field, PositiveInt, BaseModel, EmailStr
import re
import pandas as pd
import numpy as np
from api.helpers.openai_helpers import OpenAIBaseModel
from typing import Optional, List
from api.helpers.publish import upload_dwca, register_dataset_and_endpoint
import datetime
import uuid
import utm
from dateutil.parser import parse, ParserError
from django.template.loader import render_to_string
from django.db.models import Q
from api.helpers import discord_bot
import json
from tenacity import retry, stop_after_attempt, wait_fixed
import os
from requests.auth import HTTPBasicAuth
import requests


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

class EMLUser(BaseModel):
    """Representation of an individual associated with the dataset."""
    first_name: str
    last_name: str
    email: EmailStr
    orcid: Optional[str] = None


class BasicValidationForSomeDwCTerms(OpenAIBaseModel):
    """
    A few automatic basic checks for an Agent's tables against the Darwin Core standard.
    Returns a basic validation report.
    """
    agent_id: PositiveInt = Field(...)

    def validate_and_format_event_dates(self, df):
        from datetime import datetime
        
        failed_indices = []
        future_date_indices = []
        current_date = datetime.now()

        if "eventDate" in df.columns:
            for idx, date_value in df["eventDate"].items():
                try:
                    parsed_date = None
                    formatted_date = None
                    
                    if isinstance(date_value, pd.Timestamp):  # Already a datetime object
                        formatted_date = date_value.isoformat()
                        parsed_date = date_value.to_pydatetime()
                    elif isinstance(date_value, str):
                        # First, try parsing the value directly – this covers most single-date strings
                        try:
                            parsed_date = parse(date_value)
                            formatted_date = parsed_date.isoformat()
                            df.at[idx, "eventDate"] = formatted_date
                        except (ParserError, ValueError):
                            # If direct parsing fails **and** the string contains '/', treat it as a date range
                            if "/" in date_value:
                                try:
                                    start_date, end_date = date_value.split("/", 1)
                                    start_date_parsed = parse(start_date)
                                    end_date_parsed = parse(end_date)
                                    formatted_date = f"{start_date_parsed.isoformat()}/{end_date_parsed.isoformat()}"
                                    df.at[idx, "eventDate"] = formatted_date
                                    # For date ranges, check if the end date is in the future
                                    parsed_date = end_date_parsed
                                except (ParserError, ValueError):
                                    failed_indices.append(idx)
                            else:
                                failed_indices.append(idx)
                    else: 
                        failed_indices.append(idx)
                    
                    # Check if the date is in the future (only if parsing was successful)
                    if parsed_date and parsed_date.date() > current_date.date():
                        future_date_indices.append(idx)
                
                except (ParserError, ValueError, TypeError):
                    # If parsing fails, add the index to the failed_indices list
                    failed_indices.append(idx)

        return df, failed_indices, future_date_indices
    
    def validate_scientific_names(self, df):
        """
        Validate scientific names against GBIF API to detect potential typos.
        
        Args:
            df: DataFrame with scientificName column
            
        Returns:
            str: Validation message describing any issues found, or None if no issues
        """
        import urllib.parse
        import time
        
        # Get unique scientific names and their counts
        name_counts = df['scientificName'].value_counts()
        unique_names = name_counts.index.tolist()
        
        # Remove empty/null names
        unique_names = [name for name in unique_names if pd.notna(name) and str(name).strip()]
        
        if not unique_names:
            return "No valid scientific names found in the scientificName column."
        
        # Determine which names to check based on quantity
        if len(unique_names) <= 50:
            # Check all names if reasonable amount
            names_to_check = unique_names
        else:
            # Check the least common names (most likely to be typos) - bottom 30
            names_to_check = name_counts.tail(30).index.tolist()
        
        fuzzy_matches = []
        unmatched_names = []
        corrected_names = {}
        
        print(f"Validating {len(names_to_check)} scientific names against GBIF API...")
        
        for i, name in enumerate(names_to_check):
            try:
                # Add small delay to be respectful to GBIF API
                if i > 0 and i % 10 == 0:
                    time.sleep(1)
                
                encoded_name = urllib.parse.quote(str(name))
                response = requests.get(
                    f"https://api.gbif.org/v1/species/match?scientificName={encoded_name}",
                    timeout=10
                )
                
                if response.status_code == 200:
                    data = response.json()
                    confidence = data.get('confidence', 0)
                    match_type = data.get('matchType', '')
                    suggested_name = data.get('canonicalName', data.get('scientificName', ''))
                    
                    if match_type == 'FUZZY' and confidence >= 80:
                        # High confidence fuzzy match - likely a typo
                        fuzzy_matches.append({
                            'original': name,
                            'suggested': suggested_name,
                            'confidence': confidence
                        })
                        # Auto-correct high confidence matches
                        if confidence >= 85:
                            corrected_names[name] = suggested_name
                    elif match_type == 'NONE' or confidence < 50:
                        # No match or very low confidence
                        unmatched_names.append(name)
                        
            except Exception as e:
                print(f"Error checking name '{name}': {e}")
                continue
        
        # Apply auto-corrections to the DataFrame
        if corrected_names:
            for original, corrected in corrected_names.items():
                df.loc[df['scientificName'] == original, 'scientificName'] = corrected
        
        # Build validation message
        issues = []
        
        if corrected_names:
            corrections_list = [f"'{orig}' → '{corr}'" for orig, corr in corrected_names.items()]
            issues.append(f"Auto-corrected {len(corrected_names)} scientific names with high confidence matches: {'; '.join(corrections_list[:5])}")
            if len(corrections_list) > 5:
                issues.append(f"... and {len(corrections_list) - 5} more corrections")
        
        if fuzzy_matches and not corrected_names:
            # Only show fuzzy matches that weren't auto-corrected
            remaining_fuzzy = [match for match in fuzzy_matches if match['original'] not in corrected_names]
            if remaining_fuzzy:
                fuzzy_list = [f"'{match['original']}' (suggested: '{match['suggested']}', confidence: {match['confidence']}%)" for match in remaining_fuzzy[:3]]
                issues.append(f"Found {len(remaining_fuzzy)} potential typos with moderate confidence: {'; '.join(fuzzy_list)}")
                if len(remaining_fuzzy) > 3:
                    issues.append(f"... and {len(remaining_fuzzy) - 3} more potential typos")
        
        if unmatched_names:
            unmatched_list = [f"'{name}'" for name in unmatched_names[:5]]
            issues.append(f"Could not match {len(unmatched_names)} names against GBIF database: {', '.join(unmatched_list)}")
            if len(unmatched_names) > 5:
                issues.append(f"... and {len(unmatched_names) - 5} more unmatched names")
            issues.append("These may be valid names not in GBIF, recent taxonomic changes, or require manual verification.")
        
        if issues:
            return " ".join(issues)
        else:
            print(f"All {len(names_to_check)} scientific names validated successfully against GBIF.")
            return None
    
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
                # Cast every column header to string first so mixed-type headers (e.g. ints) do not raise
                standardized_columns = {str(col).lower(): col for col in df.columns}
                matched_columns = {}
                for term in DARWIN_CORE_TERMS:
                    if term.lower() in standardized_columns:
                        original_col = standardized_columns[term.lower()]
                        matched_columns[term] = original_col

                # Determine columns that couldn't be matched *before* any renaming
                unmatched_columns = [col for col in df.columns if col not in matched_columns.values()]

                # Apply renaming now (mapping original ➜ standard term) so downstream logic sees the correct headers
                if matched_columns:
                    rename_mapping = {orig: term for term, orig in matched_columns.items() if term != orig}
                    if rename_mapping:
                        df.rename(columns=rename_mapping, inplace=True)

                table_results[table.id]['unmatched_columns'] = unmatched_columns
                
                validation_errors = {}
                allowed_basis_of_record = {'MaterialEntity', 'PreservedSpecimen', 'FossilSpecimen', 'LivingSpecimen', 'MaterialSample', 'Event', 'HumanObservation', 'MachineObservation', 'Taxon', 'Occurrence', 'MaterialCitation'}
                if 'basisOfRecord' in df.columns:
                    invalid_basis = df[~df['basisOfRecord'].isin(allowed_basis_of_record)]
                    if not invalid_basis.empty:
                        validation_errors['basisOfRecord'] = invalid_basis.index.tolist()
                if 'decimalLatitude' in df.columns:
                    lat_numeric = pd.to_numeric(df['decimalLatitude'], errors='coerce')
                    # Update the DataFrame so the column is stored as numeric (NaNs where conversion failed)
                    df['decimalLatitude'] = lat_numeric
                    invalid_latitude = df[lat_numeric.isna() | (lat_numeric < -90) | (lat_numeric > 90)]
                    if not invalid_latitude.empty:
                        validation_errors['decimalLatitude'] = invalid_latitude.index.tolist()
                if 'decimalLongitude' in df.columns:
                    lon_numeric = pd.to_numeric(df['decimalLongitude'], errors='coerce')
                    # Persist numeric conversion back to the DataFrame
                    df['decimalLongitude'] = lon_numeric
                    invalid_longitude = df[lon_numeric.isna() | (lon_numeric < -180) | (lon_numeric > 180)]
                    if not invalid_longitude.empty:
                        validation_errors['decimalLongitude'] = invalid_longitude.index.tolist()
                if 'individualCount' in df.columns:
                    ind_numeric = pd.to_numeric(df['individualCount'], errors='coerce')
                    # Persist numeric conversion back to the DataFrame
                    df['individualCount'] = ind_numeric
                    invalid_individual_count = df[ind_numeric.isna() | (ind_numeric <= 0) | (ind_numeric % 1 != 0)]
                    if not invalid_individual_count.empty:
                        validation_errors['individualCount'] = invalid_individual_count.index.tolist()
                if 'catalogNumber' in df.columns:
                    # Check for duplicate catalogNumbers
                    duplicate_catalog_numbers = df[df['catalogNumber'].duplicated(keep=False)]
                    if not duplicate_catalog_numbers.empty:
                        validation_errors['catalogNumber'] = duplicate_catalog_numbers.index.tolist()
                
                corrected_dates_df, event_date_error_indices, future_date_indices = self.validate_and_format_event_dates(df)
                validation_errors['eventDate'] = event_date_error_indices
                validation_errors['eventDateFuture'] = future_date_indices
                table_results[table.id]['validation_errors'] = validation_errors

                table.df = corrected_dates_df
                table.save()
                
                general_errors = {}

                if 'scientificName' not in df.columns:
                    general_errors['scientificName'] = 'scientificName is missing from this Table (this is fine if this Table is a Measurement or Fact extension)'
                else:
                    # Scientific name validation using GBIF API
                    scientific_name_issues = self.validate_scientific_names(df)
                    if scientific_name_issues:
                        general_errors['scientificName'] = scientific_name_issues

                if ('organismQuantity' in df.columns and 'organismQuantityType' not in df.columns):
                    general_errors['organismQuantity'] = 'organismQuantity is a column in this Table, but the corresponding required column "organismQuantityType" is missing.'
                elif ('organismQuantityType' in df.columns and 'organismQuantity' not in df.columns):
                    general_errors['organismQuantity'] = 'organismQuantityType is a column in this Table, but the corresponding required column "organismQuantity" is missing.'
                if 'basisOfRecord' not in df.columns:
                    general_errors['basisOfRecord'] = 'basisOfRecord is missing from this Table (this is fine if the core is Taxon or if this Table is a Measurement or Fact extension)'
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
    Run python code using `exec(code, globals={'Dataset': Dataset, 'Table': Table, 'pd': pd, 'np': np, 'uuid': uuid, 'datetime': datetime, 're': re, 'utm': utm, 'replace_table': replace_table, 'create_or_replace': create_or_replace, 'delete_tables': delete_tables}, {})`.
    You have access to a Django ORM with models `Table` and `Dataset` in scope. Do NOT import them, just start using them immediately, e.g. DO code="t = Table.objects.get(id=1); print(t.iloc[0])" NOT code="from Table import Table; t = Table.objects.get(id=1); print(t.iloc[0])"

    CRITICAL RULES FOR TABLE MANAGEMENT:
    - Prefer updating an existing Table in-place rather than creating a new one. Example:
      `t = Table.objects.get(id=old_id); t.df = new_df; t.title = 'occurrence_dwca'; t.save()`
    - If you DO create a replacement table, you MUST delete the superseded one(s) before finishing, e.g. `old.delete()`.
    - Helper functions are provided to make this safe and easy:
        • `replace_table(old_table_id, new_df, new_title=None, description=None) -> int` updates in place and returns the table id.
        • `create_or_replace(dataset_id, title, new_df, description=None) -> int` updates the latest table with the same title if it exists, else creates one; returns the table id.
        • `delete_tables(dataset_id, exclude_ids=None) -> list[int]` deletes all tables for the dataset except those in `exclude_ids` and returns deleted ids.

    Other notes:
    - Use print() for output – stdout is captured and truncated to 2000 chars.
    - State does not persist between calls.
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

            # Helper utilities for safe table replacement/deletion
            def replace_table(old_table_id, new_df, new_title=None, description=None):
                table = Table.objects.get(id=old_table_id)
                table.df = new_df
                if new_title is not None:
                    table.title = new_title
                if description is not None:
                    table.description = description
                table.save()
                print(f"Replaced table {old_table_id} in-place")
                return table.id

            def create_or_replace(dataset_id, title, new_df, description=None):
                existing = Table.objects.filter(dataset_id=dataset_id, title=title).order_by('-updated_at', '-id').first()
                if existing is None:
                    t = Table(dataset_id=dataset_id, title=title, df=new_df, description=description or '')
                    t.save()
                    print(f"Created table {t.id} with title '{title}'")
                    return t.id
                existing.df = new_df
                if description is not None:
                    existing.description = description
                existing.save()
                print(f"Updated existing table {existing.id} with title '{title}'")
                return existing.id

            def delete_tables(dataset_id, exclude_ids=None):
                exclude_ids = exclude_ids or []
                qs = Table.objects.filter(dataset_id=dataset_id).exclude(id__in=exclude_ids)
                deleted_ids = list(qs.values_list('id', flat=True))
                qs.delete()
                if deleted_ids:
                    print(f"Deleted tables {deleted_ids}")
                return deleted_ids

            context_locals = {}
            context_globals = {
                'Dataset': Dataset,
                'Table': Table,
                'pd': pd,
                'np': np,
                'uuid': uuid,
                'datetime': datetime,
                're': re,
                'utm': utm,
                'replace_table': replace_table,
                'create_or_replace': create_or_replace,
                'delete_tables': delete_tables,
            }
            combined_context = context_globals.copy()
            combined_context.update(context_locals)
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

class SetEML(OpenAIBaseModel):
    """Sets the EML (Metdata) for a Dataset via an Agent, returns a success or error message. Note that SetBasicMetadata should be used to set the dataset Title and Description."""
    agent_id: PositiveInt = Field(...)
    temporal_scope: Optional[str] = Field(None, description="Optional temporal coverage of the dataset (e.g. 1990-2020)")
    geographic_scope: Optional[str] = Field(None, description="Optional geographic coverage of the dataset (e.g. Amazon Basin, Brazil)")
    taxonomic_scope: Optional[str] = Field(None, description="Optional taxonomic coverage (e.g. Lepidoptera, Aves)")
    methodology: Optional[str] = Field(None, description="Optional description of the sampling / data collection methodology")
    users: Optional[List[EMLUser]] = Field(
        None,
        description="Optional list of people involved in the dataset. Each entry should be an object with first_name, last_name, email, and orcid keys."
    )

    def run(self):
        try:
            from api.models import Agent
            agent = Agent.objects.get(id=self.agent_id)
            dataset = agent.dataset
            eml = dataset.eml or {}
            if self.temporal_scope is not None:
                eml["temporal_scope"] = self.temporal_scope
            if self.geographic_scope is not None:
                eml["geographic_scope"] = self.geographic_scope
            if self.taxonomic_scope is not None:
                eml["taxonomic_scope"] = self.taxonomic_scope
            if self.methodology is not None:
                eml["methodology"] = self.methodology
            if self.users is not None:
                # Ensure we store plain dicts, not Pydantic objects
                eml["users"] = [u.dict() for u in self.users]
            dataset.eml = eml
            dataset.save()
            return 'EML has been successfully set.'
        except Exception as e:
            print('There has been an error with SetEML')
            return repr(e)[:2000]


class SetBasicMetadata(OpenAIBaseModel):
    """
    Sets the title and description (Metadata) for a Dataset via an Agent.
    
    REQUIRED FIELDS:
    - agent_id: The ID of the agent
    
    CONDITIONAL FIELDS:
    - title: Required ONLY if the dataset doesn't already have a title
    - description: Required ONLY if the dataset doesn't already have a description
    
    EXAMPLE USAGE (new dataset):
    {
        "agent_id": 123,
        "title": "Bird observations from Central Park 2020-2023",
        "description": "This dataset contains bird species observations collected during weekly surveys in Central Park, New York City from 2020 to 2023. Data was collected by volunteer citizen scientists as part of the Urban Bird Study project."
    }
    
    EXAMPLE USAGE (updating existing dataset):
    {
        "agent_id": 123,
        "structure_notes": "Fixed coordinate formatting issues in rows 15-23."
    }
    
    Returns a success or error message.
    """
    agent_id: PositiveInt = Field(..., description="REQUIRED: The ID of the agent making this request")
    title: Optional[str] = Field(None, description="CONDITIONAL: A short but descriptive title for the dataset as a whole (e.g. 'Bird observations from Central Park 2020-2023'). Required only if dataset doesn't already have a title.")
    description: Optional[str] = Field(None, description="CONDITIONAL: A longer description of what the dataset contains, including any important information about why the data was gathered (e.g. for a study) as well as how it was gathered. Required only if dataset doesn't already have a description.")
    user_language: str = Field('English', description="OPTIONAL: Note down if the user wants to speak in a particular language. Default is English.") 
    structure_notes: Optional[str] = Field(None, description="OPTIONAL: Use to note any significant data structural problems or oddities, and to record changes and corrections made to fix these. Ensure that any data already existing in this field does not get overwritten unless getting re-phrased and rewritten. This serves as the running history of corrections made to the dataset.") 
    suitable_for_publication_on_gbif: Optional[bool] = Field(default=True, description="OPTIONAL: Set to false if the data is deemed unsuitable for publication on GBIF. Defaults to True. Be cautious - only reject if you are certain this spreadsheet doesn't have any suitable data.")

    def run(self):
        try:
            from api.models import Agent
            agent = Agent.objects.get(id=self.agent_id)
            dataset = agent.dataset
            
            # Check if title and description are required
            if not dataset.title and not self.title:
                return 'Error: Dataset has no title and none was provided. Please provide a title.'
            if not dataset.description and not self.description:
                return 'Error: Dataset has no description and none was provided. Please provide a description.'
            
            # Update fields only if provided
            if self.title:
                dataset.title = self.title
            if self.description:
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


class UploadDwCA(OpenAIBaseModel):
    """
    Generates a Darwin Core Archive from the dataset and uploads it to object storage.
    Returns the publicly accessible DwCA URL.
    """
    agent_id: PositiveInt = Field(...)

    def run(self):
        from api.models import Agent, Task
        try:
            agent = Agent.objects.get(id=self.agent_id)
            dataset = agent.dataset
            tables = dataset.table_set.all()

            # Choose a core table – prefer one containing scientificName, otherwise fall back to any table with kingdom, otherwise first table
            core_table = next((t for t in tables if 'scientificName' in t.df.columns), None)
            if not core_table:
                core_table = next((t for t in tables if 'kingdom' in t.df.columns), tables.first())
            if not core_table:
                error_msg = 'Validation error: Could not identify a suitable core table (requires at least a scientificName or kingdom column).'
                # Notify developers of core table validation failure
                discord_bot.send_discord_message(f"⚠️ Core Table Error: {error_msg}\nDataset: {dataset.name if hasattr(dataset, 'name') else 'Unknown'}\nAgent ID: {self.agent_id}")
                return error_msg

            # If we find additional tables, treat the first as a MeasurementOrFact extension for now
            extension_tables = [tbl for tbl in tables if tbl != core_table]
            mof_table = extension_tables[0] if extension_tables else None

            if mof_table:
                dwca_url = upload_dwca(
                    core_table.df,
                    dataset.title,
                    dataset.description,
                    mof_table.df,
                    dataset.user,
                    eml_extra=dataset.eml,
                )
            else:
                dwca_url = upload_dwca(
                    core_table.df,
                    dataset.title,
                    dataset.description,
                    user=dataset.user,
                    eml_extra=dataset.eml,
                )

            dataset.dwca_url = dwca_url
            dataset.save()
            return f'DwCA successfully created and uploaded: {dwca_url}'
        except Exception as e:
            return repr(e)[:2000]


class PublishToGBIF(OpenAIBaseModel):
    """
    Registers an existing DwCA (previously uploaded with UploadDwCA) with the GBIF API.
    Returns the GBIF dataset URL on success.
    """
    agent_id: PositiveInt = Field(...)

    def run(self):
        from api.models import Agent, Task
        try:
            agent = Agent.objects.get(id=self.agent_id)
            dataset = agent.dataset
            if not dataset.dwca_url:
                error_msg = 'Error: Dataset has no DwCA URL. Please run UploadDwCA first.'
                # Notify developers of missing DwCA URL for publishing
                discord_bot.send_discord_message(f"⚠️ Publishing Error: {error_msg}\nDataset: {dataset.name if hasattr(dataset, 'name') else 'Unknown'}\nAgent ID: {self.agent_id}")
                return error_msg

            gbif_url = register_dataset_and_endpoint(dataset.title, dataset.description, dataset.dwca_url)
            dataset.gbif_url = gbif_url
            dataset.published_at = datetime.datetime.now()
            dataset.save()

            # If this is NOT the final task, automatically mark complete and advance.
            # For the final task (e.g., Data maintenance), keep the conversation open.
            last_task = Task.objects.last()
            if agent.task_id != (last_task.id if last_task else None):
                agent.completed_at = datetime.datetime.now()
                agent.save()

                # Create the next agent in the workflow and kick it off, if any
                new_agent = dataset.next_agent()
                if new_agent:
                    new_agent.next_message()

            return f'Successfully registered dataset with GBIF. URL: {gbif_url}'
        except Exception as e:
            return repr(e)[:2000]


class ValidateDwCA(OpenAIBaseModel):
    """
    Submits the dataset's DwCA URL to the GBIF validator, then polls the validator until the job finishes.

    This can take a long time (often >10 min). The calling agent should keep the user informed while polling.
    The polling interval can be customised via `poll_interval_seconds`; default is 60 seconds (1 min).
    """
    agent_id: PositiveInt = Field(...)
    poll_interval_seconds: PositiveInt = Field(60, description="Seconds to wait between polling attempts.")

    def run(self):
        from api.models import Agent
        import requests, time
        from requests.auth import HTTPBasicAuth
        from tenacity import retry, stop_after_attempt, wait_fixed

        try:
            agent = Agent.objects.get(id=self.agent_id)
            dataset = agent.dataset
            if not dataset.dwca_url:
                return 'Error: No DwCA URL found. Run UploadDwCA first.'
            auth = HTTPBasicAuth(os.getenv('GBIF_USER'), os.getenv('GBIF_PASSWORD'))

            # Align with GBIF Validator API: send the DwCA URL as a multipart/form-data field named "fileUrl" and
            # request a JSON response (same behaviour as: curl -u user:pass -H "Accept: application/json" \
            #   -F "fileUrl=<dwca_url>" https://api.gbif.org/v1/validation/url )
            headers = {'Accept': 'application/json'}
            files = {'fileUrl': (None, dataset.dwca_url)}  # (None, ...) ensures we send as a simple form field, not a file
            submit_resp = requests.post(
                'https://api.gbif.org/v1/validation/url',
                auth=auth,
                headers=headers,
                files=files,
                timeout=30,
            )
            if submit_resp.status_code not in (200, 201, 202):
                error_msg = f'Validator submission failed. Status: {submit_resp.status_code}, Body: {submit_resp.text}'
                # Notify developers of GBIF validator submission failure
                discord_bot.send_discord_message(f"🚨 GBIF Validator Error: {error_msg}\nDataset: {dataset.name if hasattr(dataset, 'name') else 'Unknown'}\nAgent ID: {self.agent_id}")
                return error_msg

            key = submit_resp.json().get('key')
            if not key:
                error_msg = f'Validator response did not contain a key: {submit_resp.text}'
                # Notify developers of missing validation key
                discord_bot.send_discord_message(f"⚠️ GBIF Validator Key Error: {error_msg}\nDataset: {dataset.name if hasattr(dataset, 'name') else 'Unknown'}\nAgent ID: {self.agent_id}")
                return error_msg

            @retry(stop=stop_after_attempt(1000), wait=wait_fixed(self.poll_interval_seconds))
            def fetch_status():
                resp = requests.get(f'https://api.gbif.org/v1/validation/{key}', auth=auth, timeout=30)
                if resp.status_code != 200:
                    # Retry on HTTP error
                    raise requests.HTTPError(f'Status fetch failed with {resp.status_code}')
                data = resp.json()
                # If still running, raise to retry
                if data.get('status') not in ('SUCCEEDED', 'FAILED', 'FINISHED'):
                    raise Exception('Validation still running')
                return resp.text

            try:
                result_json = fetch_status()
                return result_json
            except Exception as e:
                error_msg = f'Validation polling stopped after many attempts. Last error: {e}'
                # Notify developers of validation polling timeout
                discord_bot.send_discord_message(f"⏰ GBIF Validator Timeout: {error_msg}\nDataset: {dataset.name if hasattr(dataset, 'name') else 'Unknown'}\nAgent ID: {self.agent_id}")
                return error_msg

        except Exception as e:
            error_msg = repr(e)[:2000]
            # Notify developers of general validation error
            discord_bot.send_discord_message(f"❌ GBIF Validator Exception: {error_msg}\nAgent ID: {self.agent_id}")
            return error_msg


class SendDiscordMessage(OpenAIBaseModel):
    """
    Send a message to developers via Discord webhook when errors or important events occur.
    This tool should be used to notify developers of validation failures, critical errors, 
    or other issues that require attention during dataset processing.
    """
    message: str = Field(..., description="The message to send to developers")
    urgent: bool = Field(False, description="Whether this is an urgent message that needs immediate attention")

    def run(self):
        try:
            # Format the message with context if urgent
            formatted_message = self.message
            if self.urgent:
                formatted_message = f"🚨 **URGENT** 🚨\n{self.message}"
            
            # Use the existing Discord bot functionality
            discord_bot.send_discord_message(formatted_message)
            return "Message sent successfully to developers via Discord"
        
        except Exception as e:
            return f"Failed to send Discord message: {repr(e)}"

