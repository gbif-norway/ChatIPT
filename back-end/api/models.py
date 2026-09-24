import traceback
import csv
import hashlib
import datetime
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.postgres.fields import ArrayField
from django.contrib.auth.models import AbstractUser
from api import agent_tools
from api.helpers.openai_helpers import create_response_message
from picklefield.fields import PickledObjectField
import pandas as pd
from django.template.loader import render_to_string
import os
import json
import ujson
import logging
import openpyxl
import tempfile
import re
import numpy as np
import io
import zipfile
import copy
from pathlib import Path
from types import SimpleNamespace
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver


class CustomUser(AbstractUser):
    """Custom user model with ORCID integration"""
    orcid_id = models.CharField(max_length=50, blank=True, help_text="ORCID identifier")
    orcid_access_token = models.TextField(blank=True, help_text="ORCID OAuth access token")
    orcid_refresh_token = models.TextField(blank=True, help_text="ORCID OAuth refresh token")
    institution = models.CharField(max_length=500, blank=True, help_text="User's institution")
    department = models.CharField(max_length=500, blank=True, help_text="User's department")
    country = models.CharField(max_length=100, blank=True, help_text="User's country")
    
    def __str__(self):
        return f"{self.email} ({self.orcid_id})"
    
    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"


class Dataset(models.Model):
    SOURCE_COVERAGE_MARKER = "SOURCE COVERAGE: COMPLETE"
    PUBLICATION_INPUT_FIELDS = frozenset({"title", "description", "eml"})
    PUBLICATION_ARTIFACT_FIELDS = frozenset({
        "dwc_dp_url",
        "dwc_dp_validation",
        "dwca_url",
        "dwca_validation",
        "dwc_core",
    })

    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='datasets', null=True, blank=True)
    orcid = models.CharField(max_length=2000, blank=True)
    title = models.CharField(max_length=5000, blank=True, default='')
    structure_notes = models.TextField(default='', blank=True)
    description = models.CharField(max_length=5000, blank=True, default='')
    eml = models.JSONField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    dwca_url = models.CharField(max_length=2000, blank=True)
    dwca_validation = models.JSONField(null=True, blank=True)
    dwc_dp_url = models.CharField(max_length=2000, blank=True)
    dwc_dp_validation = models.JSONField(null=True, blank=True)
    gbif_url = models.CharField(max_length=2000, blank=True)
    user_language = models.CharField(max_length=100, blank=True)
    class DWCCore(models.TextChoices):
        EVENT = 'event_occurrences'
        OCCURRENCE = 'occurrence'
        TAXONOMY = 'taxonomy'
    dwc_core = models.CharField(max_length=30, choices=DWCCore.choices, blank=True)

    class SourceMode(models.TextChoices):
        TABULAR_ONLY = 'tabular_only', _('Tabular only')
        PDF_ONLY = 'pdf_only', _('PDF only')
        HYBRID = 'hybrid', _('Hybrid')
    source_mode = models.CharField(
        max_length=20,
        choices=SourceMode.choices,
        default=SourceMode.TABULAR_ONLY,
    )

    MANUSCRIPT_TASK_NAME = "Manuscript extraction and dataset scoping"

    @staticmethod
    def empty_dwca_artifacts():
        return {
            "dwca_url": "",
            "dwca_validation": None,
            "dwc_core": "",
        }

    @classmethod
    def empty_publication_artifacts(cls):
        return {
            "dwc_dp_url": "",
            "dwc_dp_validation": None,
            **cls.empty_dwca_artifacts(),
        }

    @classmethod
    def invalidate_dwca_artifacts_for(cls, dataset_id):
        cls.objects.filter(pk=dataset_id).update(**cls.empty_dwca_artifacts())

    @classmethod
    def invalidate_publication_artifacts_for(cls, dataset_id):
        cls.objects.filter(pk=dataset_id).update(**cls.empty_publication_artifacts())

    def invalidate_dwca_artifacts(self):
        """Discard a DwC-A derived from a superseded projection table."""
        if not self.pk:
            return
        values = self.empty_dwca_artifacts()
        type(self).invalidate_dwca_artifacts_for(self.pk)
        for field, value in values.items():
            setattr(self, field, value)

    def invalidate_publication_artifacts(self):
        """Discard packages and validations derived from superseded inputs."""
        if not self.pk:
            return
        values = self.empty_publication_artifacts()
        type(self).invalidate_publication_artifacts_for(self.pk)
        for field, value in values.items():
            setattr(self, field, value)

    def invalidate_source_coverage(self):
        """Remove the completion marker when the set of source files changes."""
        if not self.pk or not self.structure_notes:
            return
        retained_lines = [
            line
            for line in self.structure_notes.splitlines()
            if line.strip() != self.SOURCE_COVERAGE_MARKER
        ]
        updated_notes = "\n".join(retained_lines).strip()
        if updated_notes != self.structure_notes:
            self.structure_notes = updated_notes
            self.save(update_fields=["structure_notes"])

    @property
    def has_complete_source_coverage(self):
        has_manifests = any(
            (manifest or {}).get("tables")
            for manifest in self.user_files.values_list("source_manifest", flat=True)
        )
        if not has_manifests:
            return True
        note_lines = [line.strip() for line in (self.structure_notes or "").splitlines() if line.strip()]
        return bool(note_lines and note_lines[-1] == self.SOURCE_COVERAGE_MARKER)

    def refresh_active_agent_prompt(self):
        """Refresh immutable source context after an upload or deletion."""
        active_agent = self.agent_set.filter(completed_at__isnull=True).first()
        if active_agent:
            active_agent.regenerate_system_message()

    def handle_source_change(self):
        """Invalidate derived state and refresh the active prompt after commit."""
        self.refresh_source_mode(save=True)
        self.invalidate_source_coverage()
        self.invalidate_publication_artifacts()
        transaction.on_commit(self.refresh_active_agent_prompt, robust=True)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        checked_fields = self.PUBLICATION_INPUT_FIELDS
        if update_fields is not None:
            checked_fields &= set(update_fields)

        publication_inputs_changed = False
        if self.pk and checked_fields:
            previous = type(self).objects.filter(pk=self.pk).values(*checked_fields).first()
            publication_inputs_changed = bool(
                previous
                and any(previous[field] != getattr(self, field) for field in checked_fields)
            )

        if publication_inputs_changed:
            self.dwc_dp_url = ""
            self.dwc_dp_validation = None
            self.dwca_url = ""
            self.dwca_validation = None
            self.dwc_core = ""
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | self.PUBLICATION_ARTIFACT_FIELDS

        return super().save(*args, **kwargs)

    def rebuild_tables_from_user_files(self):
        """
        Regenerate tables from all stored user files.
        Existing tables should be cleared by the caller before invoking this.
        """
        created_tables = []
        for user_file in self.user_files.order_by('uploaded_at', 'id'):
            file_type, dfs = user_file.extract_data()
            if file_type != user_file.FileType.TABULAR:
                continue
            filtered_dfs = user_file.filter_dataframes(dfs)
            created_tables.extend(user_file.create_tables(filtered_dfs))
        return created_tables

    @property
    def compact_structure_notes(self):
        notes = self.structure_notes or ""
        max_chars = 8000
        if len(notes) <= max_chars:
            return notes
        return "[Earlier notes omitted from this turn]\n" + notes[-max_chars:]

    @property
    def package_ready(self):
        validation = self.dwc_dp_validation or {}
        quality_gate_complete = self.agent_set.filter(
            task__name=Task.PREPUBLICATION_QUALITY_TASK,
            completed_at__isnull=False,
        ).exists()
        return bool(
            self.dwc_dp_url
            and self.dwca_url
            and validation.get('valid')
            and self.has_current_dwca_validation
            and quality_gate_complete
        )

    @property
    def has_current_dwca_validation(self):
        validation = self.dwca_validation or {}
        return bool(
            self.dwca_url
            and validation.get('url') == self.dwca_url
            and validation.get('status') in {'FINISHED', 'SUCCEEDED'}
            and validation.get('metrics', {}).get('indexeable')
        )

    def next_agent(self):
        self.refresh_from_db()

        next_agent = self.agent_set.filter(completed_at=None).first()
        if next_agent:
            return next_agent

        last_completed_agent = self.agent_set.last()  # self.agent_set.exclude(completed_at=None).last()
        logger = logging.getLogger(__name__)
        logger.info(f'No next agent found, making new agent for new task based on {last_completed_agent}')
        if last_completed_agent:
            # Use order field to find next task (tasks are ordered by order, then id)
            next_task = Task.objects.filter(order__gt=last_completed_agent.task.order).first()
            
            # Skip the "Phylogenetic tree linking" task if no tree files are uploaded
            while next_task and self._should_skip_task(next_task):
                logger.info(f'Skipping task "{next_task.name}" for source_mode "{self.source_mode}"')
                next_task = Task.objects.filter(order__gt=next_task.order).first()
            
            if next_task:
                next_task.create_agent_with_system_messages(dataset=self)
                return self.next_agent()
            else:
                logger.info(f'PUBLISHED {self.published_at}')
                return None  # It's been published... self.published_at = datetime.now() # self.save()
        else:
            # No agents exist for this dataset - create the first agent
            logger.info('No agents found for dataset, creating first agent')
            first_task = Task.objects.first()
            if not first_task:
                raise Exception('No tasks are configured in the system. Please contact the administrator to load the required tasks.')
            
            # Get tables for this dataset
            tables = Table.objects.filter(dataset=self)
            if not tables.exists() and self.source_mode == self.SourceMode.TABULAR_ONLY:
                raise Exception('No tables found for this dataset. Please ensure the dataset has been properly processed.')

            while first_task and self._should_skip_task(first_task):
                logger.info(f'Skipping initial task "{first_task.name}" for source_mode "{self.source_mode}"')
                first_task = Task.objects.filter(order__gt=first_task.order).first()

            if not first_task:
                raise Exception('No eligible tasks are configured in the system for this dataset.')
            
            first_task.create_agent_with_system_messages(dataset=self)
            return self.next_agent()

    def _should_skip_task(self, task):
        if task.name == "Phylogenetic tree linking" and not self.has_tree_files():
            return True
        if task.name == self.MANUSCRIPT_TASK_NAME and self.source_mode != self.SourceMode.PDF_ONLY:
            return True
        return False


    def can_visualize_tree(self):
        """
        Check if tree visualization is available:
        - Has tree files (Newick or Nexus)
        """
        # Check for tree files by checking file extensions
        # file_type is a property, so we need to check extensions directly
        has_tree_files = False
        for user_file in self.user_files.all():
            ext = Path(user_file.file.name).suffix.lower()
            if ext in UserFile.TREE_EXTENSIONS:
                has_tree_files = True
                break
        
        return has_tree_files

    def has_tree_files(self):
        """Check if this dataset has any tree files uploaded."""
        for user_file in self.user_files.all():
            ext = Path(user_file.file.name).suffix.lower()
            if ext in UserFile.TREE_EXTENSIONS:
                return True
        return False

    def has_pdf_files(self):
        for user_file in self.user_files.all():
            ext = Path(user_file.file.name).suffix.lower()
            if ext in UserFile.PDF_EXTENSIONS:
                return True
        return False

    def refresh_source_mode(self, save=True):
        has_tabular = False
        has_pdf = False
        for user_file in self.user_files.all():
            file_type = user_file.file_type
            if file_type == UserFile.FileType.TABULAR:
                has_tabular = True
            elif file_type == UserFile.FileType.PDF:
                has_pdf = True

        if has_tabular and has_pdf:
            source_mode = self.SourceMode.HYBRID
        elif has_pdf:
            source_mode = self.SourceMode.PDF_ONLY
        else:
            source_mode = self.SourceMode.TABULAR_ONLY

        changed = source_mode != self.source_mode
        self.source_mode = source_mode
        if save and changed:
            self.save(update_fields=['source_mode'])
        return source_mode

    def get_tree_file_info(self, content_preview_chars=500, max_tip_labels=50):
        """
        Get information about tree files for display in prompts.
        
        Returns a list of dicts with:
        - filename: Name of the tree file
        - content_preview: First N characters of the file
        - tip_labels: List of parsed tip labels (truncated if many)
        - tip_labels_count: Total count of tip labels
        """
        from api.helpers.publish import parse_newick_tip_labels, parse_nexus_tip_labels
        
        tree_info = []
        for user_file in self.user_files.all():
            ext = Path(user_file.file.name).suffix.lower()
            if ext not in UserFile.TREE_EXTENSIONS:
                continue
            
            try:
                user_file.file.open('rb')
                file_content = user_file.file.read()
                user_file.file.close()
                
                # Decode content
                try:
                    content = file_content.decode('utf-8')
                except UnicodeDecodeError:
                    content = file_content.decode('latin-1', errors='ignore')
                
                # Get content preview
                content_preview = content[:content_preview_chars]
                if len(content) > content_preview_chars:
                    content_preview += '...'
                
                # Parse tip labels based on file type
                if ext in {'.nex', '.nexus'}:
                    tip_labels = parse_nexus_tip_labels(content)
                else:
                    tip_labels = parse_newick_tip_labels(content)
                
                tip_labels_count = len(tip_labels)
                # Truncate tip labels if too many
                if len(tip_labels) > max_tip_labels:
                    tip_labels = tip_labels[:max_tip_labels]
                
                tree_info.append({
                    'filename': user_file.filename,
                    'content_preview': content_preview,
                    'tip_labels': tip_labels,
                    'tip_labels_count': tip_labels_count,
                    'tip_labels_truncated': tip_labels_count > max_tip_labels,
                })
            except Exception as e:
                # If we can't read the file, still include it with an error note
                tree_info.append({
                    'filename': user_file.filename,
                    'content_preview': f'[Error reading file: {e}]',
                    'tip_labels': [],
                    'tip_labels_count': 0,
                    'tip_labels_truncated': False,
                })
        
        return tree_info

    class Meta:
        get_latest_by = 'created_at'
        ordering = ['created_at']


class UserFile(models.Model):
    class FileType(models.TextChoices):
        TABULAR = 'tabular', _('Tabular data')
        TREE = 'tree', _('Phylogenetic tree')
        PDF = 'pdf', _('PDF manuscript')
        UNKNOWN = 'unknown', _('Unknown')

    TABULAR_TEXT_EXTENSIONS = {'.csv', '.tsv', '.txt'}
    TABULAR_EXCEL_EXTENSIONS = {'.xlsx', '.xls', '.xlsm', '.xlsb', '.ods'}
    TREE_EXTENSIONS = {'.newick', '.nwk', '.nex', '.nexus', '.tre', '.tree'}
    PDF_EXTENSIONS = {'.pdf'}
    MAX_TABULAR_COLUMNS = 500

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='user_files')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    file = models.FileField(upload_to='user_files')
    openai_file_id = models.CharField(max_length=200, blank=True)
    openai_file_fingerprint = models.CharField(max_length=128, blank=True)
    source_manifest = models.JSONField(default=dict, editable=False)

    def __str__(self):
        label = self.file_type_label or 'unknown'
        return f"{self.filename} ({label})"

    @property
    def filename(self):
        return os.path.basename(self.file.name)

    @property
    def file_type(self):
        return self._infer_file_type()

    @property
    def file_type_label(self):
        file_type = self.file_type
        return file_type.label if hasattr(file_type, 'label') else str(file_type)

    def extract_data(self):
        """
        Determine the file type and return any tabular dataframes.
        """
        file_type = self._infer_file_type()
        dataframes = {}

        if file_type == self.FileType.TABULAR:
            dataframes = self._load_tabular_dataframes()

        return file_type, dataframes

    def _infer_file_type(self):
        ext = Path(self.file.name).suffix.lower()
        if ext in self.TREE_EXTENSIONS:
            return self.FileType.TREE
        if ext in self.PDF_EXTENSIONS:
            return self.FileType.PDF
        if ext in self.TABULAR_TEXT_EXTENSIONS or ext in self.TABULAR_EXCEL_EXTENSIONS:
            return self.FileType.TABULAR
        return self.FileType.UNKNOWN

    def _load_tabular_dataframes(self):
        ext = Path(self.file.name).suffix.lower()

        if ext in self.TABULAR_TEXT_EXTENSIONS:
            return self._load_text_delimited()

        if ext in self.TABULAR_EXCEL_EXTENSIONS:
            return self._load_excel_workbook()

        # Fallback: attempt delimited first, then Excel
        try:
            return self._load_text_delimited()
        except Exception:
            return self._load_excel_workbook()

    def _load_text_delimited(self):
        self.file.open('rb')
        try:
            file_content = self.file.read()
        finally:
            self.file.close()

        text = file_content.decode('utf-8', errors='surrogateescape')
        df = self._parse_delimited_text(text)
        return {self.filename: df}

    def _parse_delimited_text(self, text):
        sample_lines = self._collect_sample_lines(text)
        candidate_delimiters = self._build_delimiter_candidates(sample_lines, text)

        detection_errors = []
        fallback_single_column = None

        for delimiter in candidate_delimiters:
            file_io = io.StringIO(text)
            try:
                df = pd.read_csv(
                    file_io,
                    dtype='str',
                    encoding='utf-8',
                    encoding_errors='surrogateescape',
                    sep=delimiter,
                    engine='python',
                    header=0,
                )
            except Exception as exc:
                detection_errors.append((self._format_delimiter_label(delimiter), str(exc)))
                continue

            if delimiter is None or df.shape[1] > 1:
                return df

            if fallback_single_column is None:
                fallback_single_column = (df, self._format_delimiter_label(delimiter))

        if fallback_single_column is not None:
            return fallback_single_column[0]

        if detection_errors:
            detail = "; ".join(f"{label}: {message}" for label, message in detection_errors)
            raise ValueError(
                f"Unable to determine the delimiter for {self.filename}. Tried {detail}."
            )

        raise ValueError(f"{self.filename} appears to be empty or could not be parsed as tabular data.")

    @staticmethod
    def _collect_sample_lines(text, limit=50):
        lines = []
        for line in text.splitlines():
            if line.strip():
                lines.append(line)
            if len(lines) >= limit:
                break
        return lines

    @classmethod
    def _build_delimiter_candidates(cls, sample_lines, text):
        heuristics = cls._rank_delimiters(sample_lines)
        sniffed = cls._sniff_delimiter(sample_lines, text)

        candidates = []
        for delimiter in heuristics:
            if delimiter not in candidates:
                candidates.append(delimiter)

        if sniffed and sniffed not in candidates:
            candidates.append(sniffed)

        for delimiter in ['\t', ',', ';', '|']:
            if delimiter not in candidates:
                candidates.append(delimiter)

        candidates.append(None)
        return candidates

    @staticmethod
    def _rank_delimiters(sample_lines):
        stats = []
        for delimiter in ['\t', ',', ';', '|']:
            counts = [line.count(delimiter) for line in sample_lines if delimiter in line]
            if len(counts) < 2:
                continue
            spread = max(counts) - min(counts)
            non_zero = len(counts)
            average = sum(counts) / non_zero if non_zero else 0
            stats.append((spread, -non_zero, -average, delimiter))

        stats.sort()
        return [delimiter for _, _, _, delimiter in stats]

    @staticmethod
    def _sniff_delimiter(sample_lines, text):
        snippet = '\n'.join(sample_lines[:10]) or text[:2048]
        snippet = snippet.strip()
        if not snippet:
            return None
        try:
            sniffed = csv.Sniffer().sniff(snippet, delimiters=['\t', ',', ';', '|'])
            return sniffed.delimiter
        except Exception:
            return None

    @staticmethod
    def _format_delimiter_label(delimiter):
        if delimiter is None:
            return 'auto-detect'
        labels = {
            '\t': 'tab',
            ',': 'comma',
            ';': 'semicolon',
            '|': 'pipe',
        }
        return labels.get(delimiter, repr(delimiter))

    @staticmethod
    def _sanitize_excel_xml_font_families(file_bytes):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes), 'r') as source_zip:
                sanitized_buffer = io.BytesIO()
                modified = False

                with zipfile.ZipFile(sanitized_buffer, 'w', zipfile.ZIP_DEFLATED) as sanitized_zip:
                    for info in source_zip.infolist():
                        data = source_zip.read(info.filename)

                        if info.filename.endswith('.xml'):
                            try:
                                xml_text = data.decode('utf-8')
                            except UnicodeDecodeError:
                                pass
                            else:
                                repaired_xml = re.sub(
                                    r'family val="(\d+)"',
                                    lambda match: 'family val="14"' if int(match.group(1)) > 14 else match.group(0),
                                    xml_text,
                                )
                                if repaired_xml != xml_text:
                                    modified = True
                                    data = repaired_xml.encode('utf-8')

                        sanitized_zip.writestr(info, data)

                if modified:
                    return sanitized_buffer.getvalue(), True
        except zipfile.BadZipFile:
            pass

        return file_bytes, False

    @classmethod
    def _load_workbook_with_xml_repair(cls, file_bytes):
        try:
            return openpyxl.load_workbook(io.BytesIO(file_bytes))
        except ValueError:
            repaired_bytes, repaired = cls._sanitize_excel_xml_font_families(file_bytes)
            if not repaired:
                raise
            return openpyxl.load_workbook(io.BytesIO(repaired_bytes))

    def _load_excel_workbook(self):
        self.file.open('rb')
        try:
            file_bytes = self.file.read()
        finally:
            self.file.close()

        try:
            workbook = self._load_workbook_with_xml_repair(file_bytes)
            self._excel_visibility = {
                sheet.title: {
                    "sheet_state": sheet.sheet_state,
                    "hidden_columns": [
                        openpyxl.utils.get_column_letter(index)
                        for letter, dimension in sheet.column_dimensions.items()
                        if dimension.hidden
                        for index in range(
                            dimension.min or openpyxl.utils.column_index_from_string(letter),
                            (dimension.max or dimension.min or openpyxl.utils.column_index_from_string(letter)) + 1,
                        )
                    ],
                    "hidden_rows": [
                        number
                        for number, dimension in sheet.row_dimensions.items()
                        if dimension.hidden
                    ],
                }
                for sheet in workbook.worksheets
                if sheet.sheet_state != "visible"
                or any(dimension.hidden for dimension in sheet.column_dimensions.values())
                or any(dimension.hidden for dimension in sheet.row_dimensions.values())
            }
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.data_type == 'f':
                            cell.value = ''
                for merged_cell in list(sheet.merged_cells.ranges):
                    min_col, min_row, max_col, max_row = merged_cell.min_col, merged_cell.min_row, merged_cell.max_col, merged_cell.max_row
                    value = sheet.cell(row=min_row, column=min_col).value
                    sheet.unmerge_cells(str(merged_cell))
                    for row in range(min_row, max_row + 1):
                        for col in range(min_col, max_col + 1):
                            sheet.cell(row=row, column=col).value = f"{value} [UNMERGED CELL]"

            with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
                temp_file_name = tmp.name
                workbook.save(temp_file_name)

            try:
                dfs = pd.read_excel(temp_file_name, dtype='str', sheet_name=None)
            finally:
                os.remove(temp_file_name)

            return dfs
        except ValueError as ve:
            raise ValueError(f"Unable to read workbook: {ve}. The file may contain invalid XML or be corrupted.")
        except Exception as e:
            raise Exception(f"An error occurred while processing the file: {e}.")

    @staticmethod
    def filter_dataframes(dfs):
        if not isinstance(dfs, dict):
            return dfs

        UserFile.validate_dataframe_widths(dfs)

        original_sheet_count = len(dfs)
        if original_sheet_count > 1:
            filtered_dfs = {name: df for name, df in dfs.items() if len(df) >= 2}
            if not filtered_dfs:
                raise ValueError(
                    "All sheets in your spreadsheet have only 1 (or 0) data row(s). "
                    "I need a larger spreadsheet to be able to help you with publication. "
                    "Please refresh and try again."
                )
            return filtered_dfs

        for sheet_name, df in dfs.items():
            if len(df) < 2:
                raise ValueError(
                    f"Your sheet {sheet_name} has only {len(df) + 1} row(s), are you sure you uploaded the right thing? "
                    "I need a larger spreadsheet to be able to help you with publication. Please refresh and try again."
                )
        return dfs

    @classmethod
    def validate_dataframe_widths(cls, dfs):
        for sheet_name, df in dfs.items():
            column_count = len(df.columns)
            if column_count > cls.MAX_TABULAR_COLUMNS:
                raise ValueError(
                    f"Your sheet {sheet_name} has {column_count} columns. ChatIPT supports up to "
                    f"{cls.MAX_TABULAR_COLUMNS} columns per sheet. Please simplify the sheet and "
                    "upload it again."
                )

    def create_tables(self, dfs):
        tables = []
        for sheet_name, df in dfs.items():
            if hasattr(df, 'empty') and not df.empty:
                tables.append(Table.objects.create(dataset=self.dataset, title=sheet_name, df=df))
        return tables

    @staticmethod
    def build_source_manifest(dfs, excel_visibility=None):
        UserFile.validate_dataframe_widths(dfs)
        return {
            "tables": [
                {
                    "name": str(sheet_name),
                    **Table._column_manifest_data(df),
                    "content_fingerprint": Table.calculate_content_fingerprint(df),
                    **(
                        {"excel_visibility": excel_visibility[sheet_name]}
                        if excel_visibility and sheet_name in excel_visibility else {}
                    ),
                }
                for sheet_name, df in dfs.items()
            ]
        }

    @property
    def source_manifest_text(self):
        tables = self.source_manifest.get("tables", [])
        if not tables:
            return ""

        sections = [f"ORIGINAL UPLOAD MANIFEST: {self.filename}"]
        value_summary_budget = Table.MANIFEST_VALUE_SUMMARY_BUDGET
        for table in tables:
            rendered_manifest, value_summary_budget = Table._render_column_manifest_with_budget(
                table,
                value_summary_budget,
            )
            visibility = table.get("excel_visibility")
            visibility_text = (
                "Excel visibility (hidden cells are included in extraction): "
                f"{json.dumps(visibility, ensure_ascii=False)}\n"
                if visibility else ""
            )
            sections.append(
                f"Sheet {Table._bounded_manifest_text(table['name'])}:\n"
                + visibility_text
                + rendered_manifest
            )
        return "\n".join(sections)

    class Meta:
        ordering = ['uploaded_at', 'id']


class Task(models.Model):  # See tasks.yaml for the only objects this model is populated with
    PACKAGE_PREPARATION_TASK = "Publication package preparation"
    PREPUBLICATION_QUALITY_TASK = "Pre-publication quality gate"
    FINAL_PUBLICATION_TASK = "Final Review & Publication"
    MAINTENANCE_TASK = "Data maintenance"

    EFFICIENT_MODEL_TASKS = {
        "Data structure exploration",
        FINAL_PUBLICATION_TASK,
    }
    MEDIUM_REASONING_TASKS = {
        "Manuscript extraction and dataset scoping",
        "Data content exploration",
    }
    LOW_REASONING_TASKS = {
        "Data suitability assessment",
        FINAL_PUBLICATION_TASK,
    }

    name = models.CharField(max_length=300, unique=True)
    text = models.TextField()
    order = models.IntegerField(default=0, help_text='Order in which tasks should be executed (from tasks.yaml)')

    class Meta:
        get_latest_by = 'id'
        ordering = ['order', 'id']

    @property
    def functions(self):
        common_functions = [
            agent_tools.SetBasicMetadata.__name__,
            agent_tools.SetStructureNotes.__name__,
            agent_tools.SetEML.__name__,
            agent_tools.SetUserLanguage.__name__,
            agent_tools.SetAgentTaskToComplete.__name__,
            agent_tools.RequestUserInput.__name__,
            agent_tools.Python.__name__,
            agent_tools.CreateNewTables.__name__,
            agent_tools.RollBack.__name__,
            agent_tools.SendDiscordMessage.__name__,
            agent_tools.LogBugWithDeveloper.__name__,
        ]
        dwc_dp_functions = [
            agent_tools.GetDwcDpTableInfo.__name__,
            agent_tools.SetWorkingPlan.__name__,
            agent_tools.ReconcileSourceCoverage.__name__,
            agent_tools.ValidateDwcDp.__name__,
            agent_tools.PreviewDwcDpDescriptor.__name__,
        ]
        package_preparation_functions = [
            agent_tools.BasicValidationForSomeDwCTerms.__name__,
            agent_tools.GetDarwinCoreInfo.__name__,
            agent_tools.GetDwCExtensionInfo.__name__,
            agent_tools.ExportDwcDp.__name__,
            agent_tools.UploadDwCA.__name__,
        ]
        quality_gate_functions = [
            *package_preparation_functions,
            agent_tools.ValidateDwCA.__name__,
        ]

        functions = list(common_functions)
        if self.name in {
            "Data transformation",
            "Data validation and refinement",
            "Phylogenetic tree linking",
        }:
            functions.extend(dwc_dp_functions)
        if self.name == self.PACKAGE_PREPARATION_TASK:
            functions.extend(dwc_dp_functions)
            functions.extend(package_preparation_functions)
        if self.name == self.PREPUBLICATION_QUALITY_TASK:
            functions.extend(dwc_dp_functions)
            functions.extend(quality_gate_functions)
        if self.name == self.FINAL_PUBLICATION_TASK:
            functions.append(agent_tools.PublishToGBIF.__name__)
        if self.name == self.MAINTENANCE_TASK:
            functions.extend(dwc_dp_functions)
            functions.extend(quality_gate_functions)
            functions.append(agent_tools.PublishToGBIF.__name__)

        # Exclude the completion tool for the final task (Data maintenance),
        # so it remains indefinitely open to conversation with the user.
        last_task = Task.objects.last()
        if last_task and last_task.id == self.id:
            functions = [f for f in functions if f != agent_tools.SetAgentTaskToComplete.__name__]

        return [getattr(agent_tools, f) for f in functions]

    @property
    def model_name(self):
        if self.name == self.PREPUBLICATION_QUALITY_TASK:
            return getattr(settings, "OPENAI_MODEL_CRITICAL", "gpt-6-astra")
        if self.name in self.EFFICIENT_MODEL_TASKS:
            return getattr(settings, "OPENAI_MODEL_EFFICIENT", "gpt-6-luna")
        return getattr(settings, "OPENAI_MODEL_STANDARD", "gpt-6-sol")

    @property
    def reasoning_effort(self):
        if self.name in self.LOW_REASONING_TASKS:
            return getattr(settings, "OPENAI_SIMPLE_REASONING_EFFORT", "low")
        if self.name == "Data structure exploration":
            return "medium"
        if self.name in self.MEDIUM_REASONING_TASKS:
            return "medium"
        return getattr(settings, "OPENAI_REASONING_EFFORT", "medium")

    def create_agent_with_system_messages(self, dataset:Dataset):
        tables = Table.objects.filter(dataset=dataset)
        return Agent.create_with_system_message(dataset=dataset, task=self, tables=tables)


class Table(models.Model):
    MANIFEST_NAME_CHARS = 160
    MANIFEST_EXAMPLE_CHARS = 50
    MANIFEST_LOW_CARDINALITY_LIMIT = 10
    MANIFEST_VALUE_SUMMARY_BUDGET = 2000

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE)
    title = models.CharField(max_length=200, blank=True)
    df = PickledObjectField()
    description = models.CharField(max_length=2000, blank=True)
    row_count = models.PositiveBigIntegerField(default=0, editable=False)
    columns = models.JSONField(default=list, editable=False)

    @staticmethod
    def display_columns(raw_columns):
        """Return stable string labels, suffixing duplicate and blank columns."""
        def display_label(raw_column):
            try:
                missing = bool(pd.isna(raw_column))
            except (TypeError, ValueError):
                missing = False
            label = '' if missing else str(raw_column)
            label = label.encode('utf-8', 'replace').decode('utf-8')
            return label or 'Unnamed column'

        labels = []
        totals = {}
        for raw_column in raw_columns:
            label = display_label(raw_column)
            totals[label] = totals.get(label, 0) + 1

        seen = {}
        for raw_column in raw_columns:
            label = display_label(raw_column)
            seen[label] = seen.get(label, 0) + 1
            if totals[label] > 1:
                label = f"{label} ({seen[label]})"
            labels.append(label)
        return labels

    def save(self, *args, **kwargs):
        from api.dwc_dp_specs import RESERVED_TABLE_NAMES

        previous_title = None
        if self.pk:
            previous_title = type(self).objects.filter(pk=self.pk).values_list("title", flat=True).first()
        is_authoritative = self.title in RESERVED_TABLE_NAMES or previous_title in RESERVED_TABLE_NAMES

        if 'df' not in self.get_deferred_fields() and self.df is not None:
            self.row_count = int(len(self.df.index))
            self.columns = self.display_columns(self.df.columns)
            update_fields = kwargs.get('update_fields')
            if update_fields and 'df' in update_fields:
                kwargs['update_fields'] = set(update_fields) | {'row_count', 'columns'}
        result = super().save(*args, **kwargs)
        if is_authoritative:
            self.dataset.invalidate_publication_artifacts()
        else:
            self.dataset.invalidate_dwca_artifacts()
        return result

    def row_page(self, offset, limit):
        """Serialize one bounded DataFrame slice without copying the full table."""
        page = self.df.iloc[offset:offset + limit].copy()
        page.columns = self.columns or self.display_columns(page.columns)
        page = page.replace([np.inf, -np.inf], np.nan)
        for column in page.select_dtypes(include=['object']).columns:
            page[column] = page[column].map(
                lambda value: value.encode('utf-8', 'replace').decode('utf-8')
                if isinstance(value, str) else value
            )
        try:
            return json.loads(
                page.to_json(
                    orient='records',
                    date_format='iso',
                    force_ascii=False,
                    default_handler=str,
                )
            )
        except Exception as exc:
            raise ValueError(f"Unable to serialize table page: {exc}") from exc

    def _snapshot_df(self, df_obj):
        max_rows, max_columns, max_str_len = 10, 10, 70

        # Truncate long strings in cells
        df = df_obj.apply(lambda col: col.astype(str).map(lambda x: (x[:max_str_len - 3] + '...') if len(x) > max_str_len else x))
        df.columns = [
            f"[{position}] {self._bounded_manifest_label(column)}"
            for position, column in enumerate(df.columns, start=1)
        ]

        # Truncate columns
        if len(df.columns) > max_columns:
            left = df.iloc[:, :max_columns//2]
            right = df.iloc[:, -max_columns//2:]
            middle = pd.DataFrame({ '...': ['...']*len(df) }, index=df.index)
            df = pd.concat([left, middle, right], axis=1)

        df.fillna('', inplace=True)

        # Truncate rows
        if len(df) > max_rows:
            top = df.head(max_rows // 2)
            bottom = df.tail(max_rows // 2)
            middle = pd.DataFrame({col: ['...'] for col in df.columns}, index=[0])  # Use a temporary numeric index for middle
            df = pd.concat([top, middle, bottom], ignore_index=True)

        return df

    @staticmethod
    def calculate_content_fingerprint(df):
        """Return a stable digest for a table's ordered columns and cell values."""
        canonical_df = Table.make_columns_unique(df.copy())
        serialized = canonical_df.to_json(
            orient='split',
            date_format='iso',
            date_unit='ns',
            force_ascii=True,
            default_handler=str,
        )
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    @property
    def content_fingerprint(self):
        return self.calculate_content_fingerprint(self.df)

    @property
    def str_preview(self):
        df = self.make_columns_unique(self.df.copy())
        original_rows, original_cols = self.df.shape
        return self._snapshot_df(df).to_string() + f"\n\n[{original_rows} rows x {original_cols} columns]"

    @property
    def str_snapshot(self):
        return self.str_preview + "\n\n" + self.column_manifest

    @property
    def column_manifest(self):
        return self._generate_column_manifest(self.df)

    @staticmethod
    def _column_manifest_data(
        df,
        max_examples=1,
        max_example_chars=MANIFEST_EXAMPLE_CHARS,
    ):
        df = Table.make_columns_unique(df.copy())
        columns = []
        total_rows = len(df.index)

        for position, column in enumerate(df.columns, start=1):
            values = df[column].dropna().astype(str).map(str.strip)
            values = values[values.ne("")]
            unique_values = values.drop_duplicates()
            examples = []
            for value in unique_values.head(max_examples):
                compact = " ".join(value.split())
                if len(compact) > max_example_chars:
                    compact = compact[:max_example_chars - 3] + "..."
                examples.append(compact)
            column_data = {
                "position": position,
                "name": str(column),
                "populated": len(values),
                "unique": len(unique_values),
                "examples": examples,
            }
            if len(unique_values) <= Table.MANIFEST_LOW_CARDINALITY_LIMIT:
                column_data["value_counts"] = [
                    {
                        "value": Table._compact_manifest_text(
                            value,
                            max_chars=Table.MANIFEST_EXAMPLE_CHARS,
                        ),
                        "count": int(count),
                    }
                    for value, count in values.value_counts().items()
                ]
            columns.append(column_data)

        return {
            "row_count": total_rows,
            "column_count": len(columns),
            "columns": columns,
        }

    @staticmethod
    def _compact_manifest_text(value, max_chars):
        compact = " ".join(str(value).split())
        if len(compact) <= max_chars:
            return compact
        return compact[:max_chars - 3] + "..."

    @staticmethod
    def _bounded_manifest_text(value, max_chars=None):
        max_chars = max_chars or Table.MANIFEST_NAME_CHARS
        compact = " ".join(str(value).split())
        if len(compact) <= max_chars:
            return json.dumps(compact, ensure_ascii=False)
        preview = compact[:max_chars - 3] + "..."
        return f"{json.dumps(preview, ensure_ascii=False)} [truncated]"

    @staticmethod
    def _bounded_manifest_label(value, max_chars=None):
        max_chars = max_chars or Table.MANIFEST_NAME_CHARS
        compact = " ".join(str(value).split())
        if len(compact) <= max_chars:
            return compact
        return compact[:max_chars - 3] + "... [truncated]"

    @staticmethod
    def _render_column_manifest_with_budget(manifest, value_summary_budget):
        lines = [
            "COMPLETE COLUMN MANIFEST (machine-generated; every column is listed once):"
        ]
        total_rows = manifest["row_count"]

        for fallback_position, column in enumerate(manifest["columns"], start=1):
            position = column.get("position", fallback_position)
            line = (
                f"- [{position}] {Table._bounded_manifest_text(column['name'])}: "
                f"{column['populated']}/{total_rows} populated; "
                f"{column['unique']} unique"
            )
            value_counts = column.get("value_counts") or []
            value_summary = ", ".join(
                f"{json.dumps(item['value'], ensure_ascii=False)} ({item['count']})"
                for item in value_counts
            )
            if value_summary and len(value_summary) <= value_summary_budget:
                line += "; values: " + value_summary
                value_summary_budget -= len(value_summary)
            elif column.get("examples"):
                line += "; example: " + Table._bounded_manifest_text(
                    column["examples"][0],
                    max_chars=Table.MANIFEST_EXAMPLE_CHARS,
                )
            lines.append(line)

        return "\n".join(lines), value_summary_budget

    @staticmethod
    def _render_column_manifest(manifest):
        rendered, _ = Table._render_column_manifest_with_budget(
            manifest,
            Table.MANIFEST_VALUE_SUMMARY_BUDGET,
        )
        return rendered

    @staticmethod
    def _generate_column_manifest(df):
        """Describe every column without allowing high-cardinality fields to disappear."""
        return Table._render_column_manifest(Table._column_manifest_data(df))

    @staticmethod
    def make_columns_unique(df):
        cols = pd.Series(df.columns)
        nan_count = 0
        for i, col in enumerate(cols):
            if pd.isna(col):
                nan_count += 1
                cols[i] = f"NaN ({nan_count})"
            elif (cols == col).sum() > 1:
                dup_indices = cols[cols == col].index
                for j, idx in enumerate(dup_indices, start=1):
                    if j > 1:
                        cols[idx] = f"{col} ({j})"

        df.columns = cols
        return df


@receiver(post_delete, sender=Table)
def invalidate_publication_artifacts_after_table_delete(sender, instance, **kwargs):
    from api.dwc_dp_specs import RESERVED_TABLE_NAMES

    if instance.title in RESERVED_TABLE_NAMES:
        Dataset.invalidate_publication_artifacts_for(instance.dataset_id)
    elif not getattr(instance, "_preserve_dwca_artifacts_on_delete", False):
        # The uploaded archive is immutable. Quality-gate cleanup may delete
        # its temporary projection tables after GBIF validation; that must not
        # erase the archive URL and the validator result needed for completion.
        dataset = Dataset.objects.filter(pk=instance.dataset_id).first()
        if dataset and not dataset.has_current_dwca_validation:
            Dataset.invalidate_dwca_artifacts_for(instance.dataset_id)


class Agent(models.Model):
    # DwC-DP building tasks where the server enforces the no-progress limit.
    NO_PROGRESS_GUARDED_TASKS = {
        "Data transformation",
        "Data validation and refinement",
        "Phylogenetic tree linking",
        Task.PACKAGE_PREPARATION_TASK,
    }
    SOURCE_MANIFEST_TASKS = {
        "Data transformation",
        "Data validation and refinement",
        "Phylogenetic tree linking",
        Task.PACKAGE_PREPARATION_TASK,
        Task.PREPUBLICATION_QUALITY_TASK,
        Task.MAINTENANCE_TASK,
    }

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.PROTECT)
    tables = models.ManyToManyField(Table, blank=True)
    busy_thinking = models.BooleanField(default=False)

    class Meta:
        get_latest_by = 'created_at'
        ordering = ['created_at']

    @classmethod
    def create_with_system_message(cls, dataset, task, tables):
        if not task:
            raise ValueError("Task cannot be None. Please ensure tasks are loaded in the database.")
        
        agent = cls.objects.create(dataset=dataset, task=task)
        agent.tables.set([t.id for t in tables])
        system_message_text = agent.regenerate_system_message()
        logger = logging.getLogger(__name__)
        logger.debug(
            "Created system message for agent %s (%s characters)",
            agent.id,
            len(system_message_text),
        )
        return agent

    def regenerate_system_message(self, new_table_cutoff=None):
        tables = list(Table.objects.filter(dataset_id=self.dataset_id).order_by('created_at', 'id'))
        user_files = list(self.dataset.user_files.all())
        source_manifest_files = [
            user_file
            for user_file in user_files
            if (user_file.source_manifest or {}).get("tables")
        ]
        legacy_tabular_files = [
            user_file
            for user_file in user_files
            if user_file.file_type == user_file.FileType.TABULAR
            and not (user_file.source_manifest or {}).get("tables")
        ]
        self.tables.set([t.id for t in tables])
        package_focused_tasks = {
            "Data validation and refinement",
            "Phylogenetic tree linking",
            Task.PACKAGE_PREPARATION_TASK,
            Task.PREPUBLICATION_QUALITY_TASK,
            Task.FINAL_PUBLICATION_TASK,
            Task.MAINTENANCE_TASK,
        }
        snapshot_table_ids = {table.id for table in tables}
        if self.task.name in package_focused_tasks:
            from api.dwc_dp_specs import RESERVED_TABLE_NAMES

            snapshot_table_ids = {
                table.id for table in tables if table.title in RESERVED_TABLE_NAMES
            }
        include_source_manifests = self.task.name in self.SOURCE_MANIFEST_TASKS
        source_manifest_covered_table_ids = set()
        if include_source_manifests:
            source_table_signatures = {
                (
                    str(source_table.get('name', '')),
                    int(source_table.get('row_count', 0)),
                    tuple(
                        str(column.get('name', ''))
                        for column in source_table.get('columns', [])
                    ),
                    source_table.get('content_fingerprint'),
                )
                for user_file in source_manifest_files
                for source_table in (user_file.source_manifest or {}).get('tables', [])
            }
            source_manifest_covered_table_ids = {
                table.id
                for table in tables
                if (
                    str(table.title),
                    table.row_count,
                    tuple(table.columns or []),
                    table.content_fingerprint,
                ) in source_table_signatures
            }
        context = {
            'agent': self,
            'all_tasks_count': Task.objects.count(),
            'new_table_cutoff': new_table_cutoff,
            'snapshot_table_ids': snapshot_table_ids,
            'include_source_manifests': include_source_manifests,
            'source_manifest_covered_table_ids': source_manifest_covered_table_ids,
            'prompt_user_files': user_files,
            'source_manifest_files': source_manifest_files,
            'legacy_tabular_files': legacy_tabular_files,
        }
        system_message_text = render_to_string('prompt.txt', context=context)
        system_message = self.message_set.filter(openai_obj__role=Message.Role.SYSTEM).order_by('created_at').first()
        if system_message:
            openai_obj = system_message.openai_obj or {}
            openai_obj['content'] = system_message_text
            openai_obj['role'] = Message.Role.SYSTEM
            system_message.openai_obj = openai_obj
            system_message.save(update_fields=['openai_obj'])
        else:
            Message.objects.create(agent=self, openai_obj={'content': system_message_text, 'role': Message.Role.SYSTEM})
        return system_message_text

    def current_state_update(self, new_table_cutoff=None):
        tables = list(Table.objects.filter(dataset_id=self.dataset_id).order_by('created_at', 'id'))
        self.tables.set([table.id for table in tables])
        changed_tables = []
        if new_table_cutoff:
            changed_tables = [
                table
                for table in tables
                if table.created_at > new_table_cutoff or table.updated_at > new_table_cutoff
            ]
        relational_candidate_report = ""
        if self.task.name == "Data validation and refinement":
            from api.relational_candidates import render_relational_candidate_report

            relational_candidate_report = render_relational_candidate_report(tables)
        no_progress_turns = self.turns_without_progress() if self.no_progress_guarded else 0
        dwc_dp_export_current = dwca_uploaded_this_task = False
        if self.task.name == Task.PACKAGE_PREPARATION_TASK and no_progress_turns >= self.no_progress_warn_turns:
            from api.schema_ledger import PROGRESS_RESULT_PREFIXES, latest_tool_results

            dwc_dp_export_current = agent_tools.dwc_dp_export_is_current(self.dataset)
            dwca_uploaded_this_task = any(
                result.startswith(PROGRESS_RESULT_PREFIXES[agent_tools.UploadDwCA.__name__])
                for result in latest_tool_results(self._history_objs(), agent_tools.UploadDwCA.__name__)
            )
        return render_to_string(
            'state_update.txt',
            {
                'agent': self,
                'tables': tables,
                'changed_tables': changed_tables,
                'relational_candidate_report': relational_candidate_report,
                'schema_ledger': self.schema_ledger_text(),
                'no_progress_turns': no_progress_turns,
                'no_progress_warn_turns': self.no_progress_warn_turns,
                'no_progress_stop_turns': self.no_progress_stop_turns,
                'dwc_dp_export_current': dwc_dp_export_current,
                'dwca_uploaded_this_task': dwca_uploaded_this_task,
                'tool_call_count': self.tool_call_count,
                'call_count_nudge_threshold': getattr(
                    settings, "AGENT_CALL_COUNT_NUDGE_THRESHOLD", 20
                ),
            },
        )

    @property
    def tool_call_count(self):
        """How many tool-calling turns this agent has made so far in this task
        stage. Surfaced back to the model as a soft, ignorable nudge -- not a
        limit -- once it crosses a threshold, so a model that's been checking
        the same thing repeatedly has a chance to notice on its own."""
        return sum(
            1
            for message in self.message_set.all()
            if (message.openai_obj or {}).get('role') == Message.Role.ASSISTANT
            and (message.openai_obj or {}).get('tool_calls')
        )

    def _history_objs(self):
        return [message.openai_obj or {} for message in self.message_set.all()]

    def schema_ledger_entries(self, history_objs=None):
        from api.dwc_dp_specs import RESERVED_TABLE_NAMES
        from api.schema_ledger import schema_lookups

        objs = history_objs if history_objs is not None else self._history_objs()
        return schema_lookups(objs, RESERVED_TABLE_NAMES)

    def schema_ledger_text(self):
        """Schemas looked up and the working plan, rebuilt from the stored tool
        log every turn so they survive history compaction."""
        from api import source_coverage
        from api.dwc_dp_specs import get_table_spec
        from api.schema_ledger import (
            dwc_lookups,
            latest_tool_results,
            latest_working_plan,
            render_dwc_ledger,
            render_ledger,
        )

        objs = self._history_objs()
        dwc_reference = agent_tools.DwcTermReference()
        coverage_results = [
            result for result in latest_tool_results(objs, agent_tools.ReconcileSourceCoverage.__name__)
            if result.startswith(source_coverage.REPORT_PREFIX)
        ]
        coverage_open_items = None
        if coverage_results:
            tables = list(Table.objects.filter(dataset_id=self.dataset_id))
            user_files = list(self.dataset.user_files.all())
            current_state = source_coverage.coverage_state(
                ((table.id, table.title, table.updated_at.isoformat()) for table in tables),
                (
                    (user_file.id, user_file.filename, user_file.uploaded_at.isoformat(), user_file.source_manifest)
                    for user_file in user_files
                ),
                {
                    'title': self.dataset.title,
                    'description': self.dataset.description,
                    'eml': self.dataset.eml,
                },
            )
            latest_result = coverage_results[-1]
            if source_coverage.result_state(latest_result) == current_state:
                coverage_open_items = source_coverage.latest_open_items([latest_result])
        return render_ledger(
            self.schema_ledger_entries(objs),
            latest_working_plan(objs),
            get_table_spec,
            coverage_open_items=coverage_open_items,
            dwc_ledger=render_dwc_ledger(dwc_lookups(objs, dwc_reference), dwc_reference),
        )

    @property
    def no_progress_guarded(self):
        return self.task.name in self.NO_PROGRESS_GUARDED_TASKS

    @property
    def no_progress_warn_turns(self):
        return max(int(getattr(settings, "AGENT_NO_PROGRESS_WARN_TURNS", 8)), 0)

    @property
    def no_progress_stop_turns(self):
        return max(int(getattr(settings, "AGENT_NO_PROGRESS_STOP_TURNS", 15)), 0)

    def turns_without_progress(self):
        """Tool-calling turns since the last table write, non-read-only tool
        result, or user message."""
        from api.schema_ledger import turns_since_progress

        latest_table_revision = Table.objects.filter(dataset_id=self.dataset_id).aggregate(
            latest=models.Max('updated_at')
        )['latest']
        entries = [
            (message.created_at, message.openai_obj or {})
            for message in self.message_set.all()
        ]
        return turns_since_progress(entries, latest_table_revision)

    def _pause_for_no_progress(self, turns):
        logger = logging.getLogger(__name__)
        logger.warning(
            "Pausing agent %s (dataset %s, task %s) after %s turns without package progress",
            self.id,
            self.dataset_id,
            self.task.name,
            turns,
        )
        try:
            from api.helpers import discord_bot

            if os.getenv('DISCORD_WEBHOOK'):
                discord_bot.send_discord_message(
                    f"ChatIPT paused dataset {self.dataset_id} ({self.task.name}): "
                    f"{turns} tool turns without package progress."
                )
        except Exception:
            logger.exception("Failed to send no-progress notification for agent %s", self.id)
        return Message.objects.create(
            agent=self,
            openai_obj={
                'role': Message.Role.ASSISTANT,
                'content': (
                    "I've spent the last several steps checking details without adding anything new "
                    "to your data package, so I've paused here rather than keep going in circles. "
                    "Reply \"continue\" and I'll carry on from where I am, or tell me if there's "
                    "anything about your data you'd like to clarify first."
                ),
                'no_progress_pause': True,
            },
        )

    @property
    def dataset_estimated_cost_usd(self):
        """Recorded, priced OpenAI spend across every agent for this dataset."""
        total = self.dataset.openai_usage_records.aggregate(
            total=models.Sum('estimated_cost_usd')
        )['total']
        return total or Decimal('0')

    @property
    def dataset_cost_limit_usd(self):
        try:
            return max(
                Decimal(str(getattr(settings, 'OPENAI_DATASET_COST_LIMIT_USD', '2.20'))),
                Decimal('0'),
            )
        except (InvalidOperation, TypeError, ValueError):
            logging.getLogger(__name__).error(
                "Invalid OPENAI_DATASET_COST_LIMIT_USD setting"
            )
            return Decimal('0')

    def _dataset_cost_limit_reached(self):
        limit = self.dataset_cost_limit_usd
        return bool(limit and self.dataset_estimated_cost_usd >= limit)

    def _pause_for_cost_limit(self):
        logger = logging.getLogger(__name__)
        logger.warning(
            "Stopping dataset %s at estimated OpenAI cost %s (limit %s)",
            self.dataset_id,
            self.dataset_estimated_cost_usd,
            self.dataset_cost_limit_usd,
        )
        return Message.objects.create(
            agent=self,
            openai_obj={
                'role': Message.Role.ASSISTANT,
                'content': (
                    "This dataset has reached its automated processing limit, so I've stopped "
                    "here to prevent further charges. Please contact the ChatIPT team if you "
                    "would like the dataset reviewed or the limit adjusted."
                ),
                'cost_limit_pause': True,
            },
        )

    def messages_for_model(self):
        messages = list(self.message_set.all())
        tool_turn_limit = max(int(getattr(settings, "OPENAI_TOOL_HISTORY_TURNS", 8)), 0)
        full_tool_turn_limit = max(
            int(getattr(settings, "OPENAI_FULL_TOOL_HISTORY_TURNS", 2)),
            0,
        )
        compact_chars = max(
            int(getattr(settings, "OPENAI_COMPACT_TOOL_CHARS", 2500)),
            500,
        )
        tool_assistant_messages = [
            message
            for message in messages
            if (message.openai_obj or {}).get('role') == Message.Role.ASSISTANT
            and (message.openai_obj or {}).get('tool_calls')
        ]

        # Both retention (how far back anything is kept at all, even compacted) and
        # the full-vs-compacted split are expressed in fixed-size batches counted
        # from the start of this agent's tool-call history, rather than a sliding
        # "last N" window measured from the newest message. A sliding window drops
        # (or promotes/demotes) exactly one turn on every single new turn, which
        # rewrites a chunk of the prompt on every call and defeats prompt caching
        # for everything after that point. Batching means a turn's kept/compacted
        # status only changes once, when its batch closes, so the cached prefix can
        # actually grow turn over turn instead of shifting every time. This can
        # retain a few more older turns than OPENAI_TOOL_HISTORY_TURNS asks for
        # (rounded up to a whole batch) -- a small, bounded token-budget cost in
        # exchange for the cache actually holding.
        batch_size = full_tool_turn_limit if full_tool_turn_limit > 0 else max(tool_turn_limit, 1)
        kept_tool_messages = set()
        full_tool_messages = set()
        if tool_assistant_messages:
            total_batches = (len(tool_assistant_messages) - 1) // batch_size + 1
            current_batch = total_batches - 1

            if tool_turn_limit:
                retained_batches = max(-(-tool_turn_limit // batch_size), 1)  # ceil
                oldest_kept_batch = max(total_batches - retained_batches, 0)
                kept_tool_messages = {
                    message.id
                    for index, message in enumerate(tool_assistant_messages)
                    if index // batch_size >= oldest_kept_batch
                }

            if full_tool_turn_limit:
                full_tool_messages = {
                    message.id
                    for index, message in enumerate(tool_assistant_messages)
                    if message.id in kept_tool_messages and index // full_tool_turn_limit == current_batch
                }

        kept_call_ids = {
            str(tool_call.get('id'))
            for message in tool_assistant_messages
            if message.id in kept_tool_messages
            for tool_call in ((message.openai_obj or {}).get('tool_calls') or [])
            if tool_call.get('id')
        }
        full_call_ids = {
            str(tool_call.get('id'))
            for message in tool_assistant_messages
            if message.id in full_tool_messages
            for tool_call in ((message.openai_obj or {}).get('tool_calls') or [])
            if tool_call.get('id')
        }

        bounded = []
        for message in messages:
            openai_obj = message.openai_obj or {}
            role = openai_obj.get('role')
            if role == Message.Role.ASSISTANT and openai_obj.get('tool_calls'):
                if message.id not in kept_tool_messages:
                    # Keep what the agent said (its plan, its reply to the user)
                    # even when the old tool call itself is dropped; otherwise the
                    # latest user message looks unanswered and the agent restarts.
                    content = openai_obj.get('content')
                    if isinstance(content, str) and content.strip():
                        if len(content) > compact_chars:
                            content = content[:compact_chars] + "\n...[older assistant text compacted]..."
                        bounded.append(SimpleNamespace(openai_obj={
                            'role': Message.Role.ASSISTANT,
                            'content': content,
                        }))
                    continue
            if role == Message.Role.TOOL:
                if str(openai_obj.get('tool_call_id')) not in kept_call_ids:
                    continue
            should_compact = (
                role == Message.Role.ASSISTANT
                and openai_obj.get('tool_calls')
                and message.id not in full_tool_messages
            ) or (
                role == Message.Role.TOOL
                and str(openai_obj.get('tool_call_id')) not in full_call_ids
            )
            if not should_compact:
                bounded.append(message)
                continue

            compact_obj = copy.deepcopy(openai_obj)
            if role == Message.Role.ASSISTANT:
                for tool_call in compact_obj.get('tool_calls') or []:
                    function = tool_call.get('function') or {}
                    arguments = function.get('arguments')
                    if isinstance(arguments, str) and len(arguments) > compact_chars:
                        function['arguments'] = self._compact_tool_arguments(
                            arguments,
                            compact_chars,
                        )
            elif role == Message.Role.TOOL:
                content = compact_obj.get('content')
                if isinstance(content, str) and len(content) > compact_chars:
                    half = compact_chars // 2
                    compact_obj['content'] = (
                        content[:half]
                        + "\n...[older tool output compacted]...\n"
                        + content[-half:]
                    )
            bounded.append(SimpleNamespace(openai_obj=compact_obj))
        return bounded

    @staticmethod
    def _compact_tool_arguments(arguments, max_chars):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            compacted = {}
            for key, value in parsed.items():
                if isinstance(value, str) and len(value) > max_chars:
                    half = max_chars // 2
                    value = (
                        value[:half]
                        + "\n...[older tool argument compacted]...\n"
                        + value[-half:]
                    )
                compacted[key] = value
            return json.dumps(compacted, ensure_ascii=False)
        half = max_chars // 2
        return arguments[:half] + "...[compacted]..." + arguments[-half:]

    @staticmethod
    def _gbif_poll_status(last_message):
        """If the last tool result is ValidateDwCA reporting the archive is still
        running through the GBIF validator, return its parsed payload; otherwise
        None. GBIF validation of a real archive routinely takes well over 10
        minutes, so this lets next_message() recognise "we're just waiting on
        GBIF" and treat that turn differently instead of burning a full paid model
        call purely to be told 'still running' again."""
        openai_obj = getattr(last_message, 'openai_obj', None) or {}
        if openai_obj.get('role') != Message.Role.TOOL:
            return None
        content = openai_obj.get('content')
        if not isinstance(content, str):
            return None
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get('status') != 'RUNNING' or 'validation_key' not in payload:
            return None
        return payload

    def next_message(self):
        last_message = self.message_set.last()
        print(f'Last message role: {last_message.role}, Completed at value for this agent: {self.completed_at}')
        if last_message.role == Message.Role.ASSISTANT or self.completed_at:
            return None
        if self.busy_thinking:
            return last_message

        if self._dataset_cost_limit_reached():
            return [self._pause_for_cost_limit()]

        if self.no_progress_guarded and self.no_progress_stop_turns:
            turns = self.turns_without_progress()
            if turns >= self.no_progress_stop_turns:
                return [self._pause_for_no_progress(turns)]

        gbif_poll = self._gbif_poll_status(last_message)
        if gbif_poll:
            next_recheck_at = gbif_poll.get('next_recheck_at')
            if next_recheck_at:
                try:
                    recheck_time = datetime.datetime.fromisoformat(next_recheck_at)
                except (TypeError, ValueError):
                    recheck_time = None
                if recheck_time and timezone.now() < recheck_time:
                    # Not worth a full-context, full-price model turn just to be
                    # told "still running" again before GBIF could plausibly be
                    # done. The next poll (from the front end, or a scheduled
                    # check) will retry this same check.
                    return last_message

        # Otherwise we need to send it to GPT, last message was from the user, was the return from a function, or was the starting system message
        self.busy_thinking = True
        self.save()
        try:
            recent_non_system_messages = list(
                self.message_set.exclude(openai_obj__role=Message.Role.SYSTEM).order_by('-created_at')[:2]
            )
            previous_non_system_message = recent_non_system_messages[1] if len(recent_non_system_messages) > 1 else None
            new_table_cutoff = previous_non_system_message.created_at if previous_non_system_message else None
            model_messages = self.messages_for_model()
            state_items = []
            if recent_non_system_messages:
                state_items.append({
                    "role": "system",
                    "content": self.current_state_update(new_table_cutoff),
                })

            new_pdf_files_qs = self.dataset.user_files.filter(file__iendswith='.pdf').order_by('uploaded_at', 'id')
            if new_table_cutoff:
                new_pdf_files_qs = new_pdf_files_qs.filter(uploaded_at__gt=new_table_cutoff)
            latest_non_system_message = recent_non_system_messages[0] if recent_non_system_messages else None
            latest_openai_obj = getattr(latest_non_system_message, 'openai_obj', None) or {}
            if (
                latest_openai_obj.get('role') == Message.Role.USER
                and latest_openai_obj.get('pdf_attachments')
            ):
                new_pdf_files_qs = self.dataset.user_files.none()

            # Resuming a GBIF poll ("is it done yet? if not, call ValidateDwCA
            # again") is a mechanical continuation with no domain judgment involved
            # -- use the cheaper reasoning tier reserved for routine steps instead
            # of the task's normal effort.
            reasoning_effort = self.task.reasoning_effort
            if gbif_poll:
                reasoning_effort = getattr(
                    settings, "OPENAI_SIMPLE_REASONING_EFFORT", reasoning_effort
                )

            # Main GPT interaction
            model = self.task.model_name
            if gbif_poll:
                model = getattr(settings, "OPENAI_MODEL_EFFICIENT", "gpt-6-luna")
            response_message = create_response_message(
                model_messages,
                self.task.functions,
                model=model,
                reasoning_effort=reasoning_effort,
                pdf_user_files=new_pdf_files_qs,
                additional_input_items=state_items,
                usage_agent_id=self.id,
            )

            # A non-final workflow task must either act, ask through the structured
            # user-input tool, or complete. Retry once internally instead of making
            # the user type "please continue".
            completion_tool_available = any(
                function.__name__ == agent_tools.SetAgentTaskToComplete.__name__
                for function in self.task.functions
            )
            if not response_message.tool_calls and completion_tool_available:
                first_response_content = getattr(response_message, "content", "") or ""
                if self._dataset_cost_limit_reached():
                    return [self._pause_for_cost_limit()]
                response_message = create_response_message(
                    model_messages,
                    self.task.functions,
                    model=model,
                    reasoning_effort=self.task.reasoning_effort,
                    pdf_user_files=[],
                    additional_input_items=[
                        *state_items,
                        {
                            "role": "assistant",
                            "content": first_response_content,
                        },
                        {
                            "role": "user",
                            "content": (
                                "Internal workflow correction: your previous response took no action. "
                                "Continue now with a working tool, call RequestUserInput with the "
                                "question or questions you need answered, or call "
                                "SetAgentTaskToComplete if this task is finished. Do not ask the user "
                                "to say 'continue'."
                            ),
                        },
                    ],
                    usage_agent_id=self.id,
                    usage_retry_reason="no_tool_call",
                )
                asks_for_user_input = any(
                    tool_call.function.name == agent_tools.RequestUserInput.__name__
                    for tool_call in response_message.tool_calls
                )
                if (
                    response_message.tool_calls
                    and not asks_for_user_input
                    and not getattr(response_message, "content", "")
                ):
                    response_message.content = first_response_content

            # Store the assistant message returned by OpenAI
            message = Message.objects.create(agent=self, openai_obj=response_message.dict())  # response_message.__dict__

            # If no tool calls are requested, simply return the assistant message
            if not response_message.tool_calls:
                return [message]

            # One or more tool calls requested – execute them in sequence
            messages = [message]
            requested_user_input = None
            for tool_index, tool_call in enumerate(response_message.tool_calls):
                try:
                    result = self.run_function(tool_call.function)
                except Exception as e:
                    # Capture any error raised during the function execution but keep going so we don't strand busy_thinking
                    result = (
                        f'ERROR CALLING FUNCTION: Invalid JSON or code provided in your last response '
                        f'(Calling {tool_call.function.name} with the given arguments for {tool_call.id}), please try again. '\
                        f'\nError: {e}'
                    )

                tool_message = Message.create_function_message(
                    agent=self,
                    function_result=result,
                    tool_call_id=tool_call.id,
                )
                messages.append(tool_message)

                if tool_call.function.name == agent_tools.RequestUserInput.__name__:
                    try:
                        request = agent_tools.RequestUserInput(
                            **json.loads(tool_call.function.arguments, strict=False)
                        )
                        parsed_result = json.loads(result)
                        if (
                            request.agent_id == self.id
                            and parsed_result.get("status") == "awaiting_user_input"
                        ):
                            requested_user_input = request.user_message()
                    except Exception:
                        requested_user_input = None
                    # RequestUserInput is terminal for this turn.
                    # Record outputs for later calls without executing them so the
                    # Responses API receives a complete function-call/output pairing
                    # after the user answers.
                    for skipped_call in response_message.tool_calls[tool_index + 1:]:
                        skipped_message = Message.create_function_message(
                            agent=self,
                            function_result=(
                                "Skipped because RequestUserInput paused this turn. "
                                "Reconsider this action after the user responds."
                            ),
                            tool_call_id=skipped_call.id,
                        )
                        messages.append(skipped_message)
                    break

            if requested_user_input:
                question_message = Message.objects.create(
                    agent=self,
                    openai_obj={
                        "role": Message.Role.ASSISTANT,
                        "content": requested_user_input,
                    },
                )
                messages.append(question_message)

            # Refresh the agent so later updates (e.g. completed_at) are not overwritten
            self.refresh_from_db()

            return messages

        except Exception as e:
            # Any unexpected error – report back to the user
            error_message = (
                'Unfortunately there was a problem querying the OpenAI API or processing your request. '
                'Try again later, and please report this error to the developers. '
                f'Full error: {e}'
            )
            print(e)
            print("Error in next_message:")
            traceback.print_exc()
            return [
                Message.objects.create(
                    agent=self,
                    openai_obj={
                        'role': Message.Role.ASSISTANT,
                        'content': error_message,
                    },
                )
            ]

        finally:
            # Always clear the busy flag so the UI can recover even if we hit an exception
            if self.busy_thinking:
                self.busy_thinking = False
                self.save()

    def run_function(self, fn):
        function_model_class = getattr(agent_tools, fn.name)
        fnargs = fn.arguments
        if fn.name == 'Python':
            if not re.sub(r'[\s"\']', '', fn.arguments).startswith('{code'):
                fnargs = json.dumps({'code': fn.arguments})
        fn_args = json.loads(fnargs, strict=False)

        from api import schema_ledger

        if fn.name == 'Python' and schema_ledger.reads_schema_files(fn_args.get('code')):
            return schema_ledger.SCHEMA_FILE_NOTICE
        if fn.name == agent_tools.GetDwcDpTableInfo.__name__ and fn_args.get('table_name'):
            include_fields = fn_args.get('include_fields', True)
            if isinstance(include_fields, str):
                include_fields = include_fields.strip().lower() not in {'false', '0', 'no'}
            if schema_ledger.lookup_is_covered(
                self.schema_ledger_entries(),
                fn_args.get('table_name'),
                bool(include_fields),
                fn_args.get('field_details'),
            ):
                return schema_ledger.duplicate_lookup_notice(fn_args.get('table_name'), bool(include_fields))
        prefix = ''
        if fn.name in {schema_ledger.DWC_TERM_TOOL, schema_ledger.EXTENSION_TOOL}:
            dwc_reference = agent_tools.DwcTermReference()
            fn_args, prefix = schema_ledger.plan_dwc_lookup(
                schema_ledger.dwc_lookups(self._history_objs(), dwc_reference),
                fn.name,
                fn_args,
                dwc_reference,
            )
            if fn_args is None:
                return prefix

        function_model_obj = function_model_class(**fn_args)
        return prefix + function_model_obj.run()


class Message(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE)
    openai_obj = models.JSONField(null=True, blank=True)

    class Role(models.TextChoices):
        USER = 'user'
        SYSTEM = 'system'
        ASSISTANT = 'assistant'
        TOOL = 'tool'

    @classmethod
    def create_function_message(cls, agent, function_result, tool_call_id):
        return cls.objects.create(agent=agent, openai_obj={'content': function_result, 'role': cls.Role.TOOL, 'tool_call_id': tool_call_id})

    @property
    def role(self):
        return self.Role(self.openai_obj['role'])

    class Meta:
        get_latest_by = 'created_at'
        ordering = ['created_at']


class OpenAIUsage(models.Model):
    """Token usage and the contemporaneous cost estimate for one API response."""

    created_at = models.DateTimeField(auto_now_add=True)
    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        related_name='openai_usage_records',
    )
    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        related_name='openai_usage_records',
    )
    task_name = models.CharField(max_length=300, blank=True)
    response_id = models.CharField(max_length=200, unique=True)
    response_status = models.CharField(max_length=40, blank=True)
    retry_reason = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    reasoning_effort = models.CharField(max_length=30, blank=True)
    service_tier = models.CharField(max_length=30, blank=True)
    input_tokens = models.PositiveBigIntegerField(default=0)
    cached_input_tokens = models.PositiveBigIntegerField(default=0)
    cache_write_input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    reasoning_tokens = models.PositiveBigIntegerField(default=0)
    total_tokens = models.PositiveBigIntegerField(default=0)
    duration_ms = models.PositiveBigIntegerField(default=0)
    long_context = models.BooleanField(default=False)
    input_price_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    cached_input_price_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    cache_write_price_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    output_price_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    input_price_multiplier = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    output_price_multiplier = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    estimated_cost_usd = models.DecimalField(
        max_digits=16,
        decimal_places=6,
        null=True,
        blank=True,
    )
    pricing_source = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['dataset', 'created_at']),
            models.Index(fields=['agent', 'created_at']),
        ]

    def __str__(self):
        return f'{self.response_id}: {self.input_tokens} in / {self.output_tokens} out'


class DatasetAttentionNotification(models.Model):
    """A one-shot email request for the next time a dataset needs attention."""

    class Status(models.TextChoices):
        PENDING = 'pending', _('Waiting for attention')
        READY = 'ready', _('Ready to send')
        SENDING = 'sending', _('Sending')
        SENT = 'sent', _('Sent')
        CANCELLED = 'cancelled', _('Cancelled')

    class AttentionKind(models.TextChoices):
        NEEDS_INPUT = 'needs_input', _('Needs input')
        READY = 'ready', _('Package ready')
        PUBLISHED = 'published', _('Published')

    dataset = models.OneToOneField(
        Dataset,
        on_delete=models.CASCADE,
        related_name='attention_notification',
    )
    email = models.EmailField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attention_kind = models.CharField(
        max_length=20,
        choices=AttentionKind.choices,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'Dataset {self.dataset_id}: {self.email} ({self.status})'
