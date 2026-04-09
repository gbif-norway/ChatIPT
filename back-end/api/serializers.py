from api.models import Dataset, Table, Agent, Message, Task, UserFile
from rest_framework import serializers
from api.helpers import discord_bot
from django.conf import settings
from django.utils import timezone
from pathlib import Path
from api.helpers.pdf_extraction import (
    PDF_EXTRACTION_MODE_METADATA_AND_TABLES,
    PDF_EXTRACTION_MODE_METADATA_ONLY,
    PdfExtractionHardReject,
    build_dataset_conversation_digest,
    extract_pdf_for_user_file,
)


class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = ['id', 'name', 'text']


class TableSerializer(serializers.ModelSerializer):
    df_str = serializers.CharField(source='df', read_only=True)

    class Meta:
        model = Table
        fields = ['id', 'created_at', 'updated_at', 'dataset', 'title', 'df_str', 'description', 'df_json']


class TableShortSerializer(serializers.ModelSerializer):
    class Meta:
        model = Table
        fields = ['id', 'title', 'updated_at']


class MessageSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = '__all__'

    def get_role(self, obj):
        return obj.role  


class AgentSerializer(serializers.ModelSerializer):
    message_set = MessageSerializer(many=True, read_only=True)
    task = TaskSerializer(read_only=True)
    table_set = TableShortSerializer(many=True, read_only=True)

    class Meta:
        model = Agent
        fields = '__all__'


class UserFileSerializer(serializers.ModelSerializer):
    filename = serializers.CharField(read_only=True)
    file_url = serializers.SerializerMethodField()
    file_type = serializers.SerializerMethodField()
    extraction_status = serializers.SerializerMethodField()
    extraction_outcome = serializers.SerializerMethodField()
    extraction_summary = serializers.SerializerMethodField()
    extraction_table_ids = serializers.SerializerMethodField()

    class Meta:
        model = UserFile
        fields = [
            'id',
            'dataset',
            'file',
            'uploaded_at',
            'filename',
            'file_url',
            'file_type',
            'extraction_status',
            'extraction_outcome',
            'extraction_summary',
            'extraction_table_ids',
        ]
        read_only_fields = [
            'id',
            'uploaded_at',
            'filename',
            'file_url',
            'file_type',
            'extraction_status',
            'extraction_outcome',
            'extraction_summary',
            'extraction_table_ids',
        ]
        extra_kwargs = {
            'dataset': {'required': False}
        }

    def get_file_url(self, obj):
        if not obj.file:
            return ''
        request = self.context.get('request')
        file_url = obj.file.url
        if request is not None:
            return request.build_absolute_uri(file_url)
        return file_url

    def get_file_type(self, obj):
        return obj.file_type_label

    def get_extraction_status(self, obj):
        extraction = obj.get_pdf_extraction()
        return extraction.status if extraction else None

    def get_extraction_outcome(self, obj):
        extraction = obj.get_pdf_extraction()
        if not extraction:
            return None
        return extraction.extraction_outcome

    def get_extraction_summary(self, obj):
        extraction = obj.get_pdf_extraction()
        if not extraction:
            return None
        extracted_json = extraction.extracted_json or {}
        if extraction.error:
            return extraction.error
        return extracted_json.get('summary')

    def get_extraction_table_ids(self, obj):
        extraction = obj.get_pdf_extraction()
        if not extraction:
            return []
        extracted_json = extraction.extracted_json or {}
        return extracted_json.get('materialized_table_ids') or []

    def create(self, validated_data):
        try:
            user_file = UserFile.objects.create(**validated_data)
            file_type, dfs = user_file.extract_data()
        except Exception as exc:
            if 'user_file' in locals():
                user_file.delete()
            raise serializers.ValidationError(f"An error was encountered when loading your data. Error details: {exc}.")

        if file_type == UserFile.FileType.UNKNOWN:
            user_file.delete()
            raise serializers.ValidationError(
                "Unsupported file type. Please upload a spreadsheet/delimited file, a phylogenetic tree file, or a PDF manuscript."
            )

        if file_type == UserFile.FileType.TABULAR:
            try:
                filtered_dfs = UserFile.filter_dataframes(dfs)
            except ValueError as exc:
                user_file.delete()
                raise serializers.ValidationError(str(exc))
            user_file.create_tables(filtered_dfs)
        elif file_type == UserFile.FileType.PDF and settings.ENABLE_PDF_PIPELINE:
            request = self.context.get('request')
            latest_user_message = ''
            if request:
                latest_user_message = str(request.data.get('upload_context_message') or '').strip()
            conversation_digest = build_dataset_conversation_digest(user_file.dataset)
            is_new_dataset_upload = bool(self.context.get('is_new_dataset_upload'))
            dataset_has_tabular_uploads = any(
                existing_file.file_type == UserFile.FileType.TABULAR
                for existing_file in user_file.dataset.user_files.exclude(id=user_file.id)
            )
            extraction_mode = str(
                self.context.get('pdf_extraction_mode')
                or (
                    PDF_EXTRACTION_MODE_METADATA_ONLY
                    if dataset_has_tabular_uploads
                    else PDF_EXTRACTION_MODE_METADATA_AND_TABLES
                )
            ).strip().lower()
            if extraction_mode not in {PDF_EXTRACTION_MODE_METADATA_ONLY, PDF_EXTRACTION_MODE_METADATA_AND_TABLES}:
                extraction_mode = PDF_EXTRACTION_MODE_METADATA_AND_TABLES
            try:
                extract_pdf_for_user_file(
                    user_file,
                    is_new_dataset=is_new_dataset_upload,
                    latest_user_message=latest_user_message,
                    conversation_digest=conversation_digest,
                    extraction_mode=extraction_mode,
                )
            except PdfExtractionHardReject as exc:
                user_file.delete()
                raise serializers.ValidationError(str(exc))

        user_file.dataset.refresh_source_mode(save=True)

        return user_file


class DatasetSerializer(serializers.ModelSerializer):
    user_files = UserFileSerializer(many=True, read_only=True)
    visible_agent_set = serializers.SerializerMethodField()
    user_info = serializers.SerializerMethodField()
    can_visualize_tree = serializers.SerializerMethodField()

    class Meta:
        model = Dataset
        fields = [
            'id',
            'created_at',
            'user',
            'orcid',
            'title',
            'structure_notes',
            'description',
            'eml',
            'published_at',
            'rejected_at',
            'dwca_url',
            'gbif_url',
            'user_language',
            'dwc_core',
            'source_mode',
            'visible_agent_set',
            'user_info',
            'user_files',
            'can_visualize_tree',
        ]
        read_only_fields = [
            'created_at',
            'user',
            'visible_agent_set',
            'user_info',
            'user_files',
            'published_at',
            'rejected_at',
            'can_visualize_tree',
            'source_mode',
        ]
    
    def get_visible_agent_set(self, dataset):
        agents = list(dataset.agent_set.filter(completed_at__isnull=False))
        next_active_agent = dataset.agent_set.filter(completed_at__isnull=True).first()
        if next_active_agent:
            agents.append(next_active_agent)
        return AgentSerializer(agents, many=True).data

    def get_user_info(self, dataset):
        # Only include user info if the requesting user is a superuser
        request = self.context.get('request')
        if request and request.user and request.user.is_superuser and dataset.user:
            return {
                'id': dataset.user.id,
                'email': dataset.user.email,
                'first_name': dataset.user.first_name,
                'last_name': dataset.user.last_name,
                'orcid_id': dataset.user.orcid_id,
                'institution': dataset.user.institution,
                'department': dataset.user.department,
                'country': dataset.user.country
            }
        return None

    def get_can_visualize_tree(self, dataset):
        """Check if tree visualization is available for this dataset"""
        return dataset.can_visualize_tree()

    def create(self, validated_data):
        request = self.context.get('request')
        uploaded_files = []
        if request:
            uploaded_files = request.FILES.getlist('files')
            if not uploaded_files and request.FILES.get('file'):
                uploaded_files = [request.FILES['file']]

        if not uploaded_files:
            raise serializers.ValidationError(
                "Please upload at least one data file so I have something to work with."
            )

        dataset = Dataset.objects.create(**validated_data)
        uploaded_names = []
        uploaded_exts = [Path(getattr(uploaded_file, 'name', '')).suffix.lower() for uploaded_file in uploaded_files]
        incoming_has_tabular = any(
            ext in (UserFile.TABULAR_TEXT_EXTENSIONS | UserFile.TABULAR_EXCEL_EXTENSIONS)
            for ext in uploaded_exts
        )
        incoming_has_pdf = any(ext in UserFile.PDF_EXTENSIONS for ext in uploaded_exts)
        new_dataset_pdf_extraction_mode = (
            PDF_EXTRACTION_MODE_METADATA_ONLY
            if incoming_has_tabular and incoming_has_pdf
            else PDF_EXTRACTION_MODE_METADATA_AND_TABLES
        )

        try:
            for uploaded_file in uploaded_files:
                file_serializer = UserFileSerializer(
                    data={'file': uploaded_file},
                    context={
                        **self.context,
                        'is_new_dataset_upload': True,
                        'pdf_extraction_mode': new_dataset_pdf_extraction_mode,
                    },
                )
                file_serializer.is_valid(raise_exception=True)
                user_file = file_serializer.save(dataset=dataset)
                uploaded_names.append(user_file.filename)
        except serializers.ValidationError as exc:
            dataset.delete()
            raise exc
        except Exception as exc:
            dataset.delete()
            raise serializers.ValidationError(
                f"An error was encountered when loading your data. Error details: {exc}."
            )

        file_types = [user_file.file_type for user_file in dataset.user_files.all()]
        has_tabular = any(file_type == UserFile.FileType.TABULAR for file_type in file_types)
        has_pdf = any(file_type == UserFile.FileType.PDF for file_type in file_types)

        if not has_tabular and not has_pdf:
            dataset.delete()
            raise serializers.ValidationError(
                "No usable dataset source was found. "
                "Please upload at least one spreadsheet/delimited text file or a PDF manuscript."
            )

        dataset.refresh_source_mode(save=True)

        if not dataset.table_set.exists() and has_tabular and not has_pdf:
            dataset.delete()
            raise serializers.ValidationError(
                "No tabular data could be loaded from your files. "
                "Please upload at least one spreadsheet or delimited text file with two or more data rows."
            )

        if not dataset.table_set.exists() and has_pdf:
            successful_pdf_extractions = []
            for user_file in dataset.user_files.all():
                if user_file.file_type != UserFile.FileType.PDF:
                    continue
                extraction = user_file.get_pdf_extraction()
                if extraction and extraction.status == extraction.Status.SUCCESS:
                    successful_pdf_extractions.append(extraction)

            if successful_pdf_extractions and all(
                extraction.extraction_outcome in {'SUCCESS_NO_RAW_DATA', 'SUCCESS_METADATA_ONLY'}
                for extraction in successful_pdf_extractions
            ):
                dataset.rejected_at = timezone.now()
                dataset.save(update_fields=['rejected_at'])

        discord_bot.send_discord_message(
            f"V3 New dataset publication starting on ChatIPT. User files: {', '.join(uploaded_names) if uploaded_names else 'none'}."
        )

        if not dataset.rejected_at:
            first_agent = dataset.next_agent()
            if not first_agent:
                dataset.delete()
                raise serializers.ValidationError(
                    "No tasks are configured in the system. Please contact the administrator to load the required tasks."
                )

        discord_bot.send_discord_message(f"Dataset ID assigned: {dataset.id}.")
        return dataset


class DatasetListSerializer(serializers.ModelSerializer):
    user_files = UserFileSerializer(many=True, read_only=True)
    record_count = serializers.SerializerMethodField()
    last_updated = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    last_message_preview = serializers.SerializerMethodField()
    user_info = serializers.SerializerMethodField()

    class Meta:
        model = Dataset
        fields = [
            'id', 'title', 'description', 'dwc_core',
            'created_at', 'published_at', 'rejected_at',
            'record_count', 'last_updated', 'status', 'progress', 'last_message_preview', 'user_info', 'user_files', 'source_mode'
        ]

    def get_record_count(self, obj):
        # sum table rows; df is PickledObjectField on Table
        return sum(getattr(t.df, 'shape', [0])[0] for t in obj.table_set.all())

    def get_last_updated(self, obj):
        ts = [t.updated_at for t in obj.table_set.all()]
        return max(ts) if ts else obj.created_at

    def get_status(self, obj):
        if obj.rejected_at: 
            return 'rejected'
        if obj.published_at: 
            return 'published'
        has_active = obj.agent_set.filter(completed_at__isnull=True).exists()
        return 'processing' if has_active else 'draft'

    def get_progress(self, obj):
        total = Task.objects.count()
        done = obj.agent_set.filter(completed_at__isnull=False).count()
        return {'done': done, 'total': total}

    def get_last_message_preview(self, obj):
        a = obj.agent_set.order_by('-created_at').first()
        if not a: 
            return ''
        m = a.message_set.order_by('-created_at').first()
        if not m or 'content' not in (m.openai_obj or {}): 
            return ''
        c = str(m.openai_obj['content'])
        return (c[:140] + '…') if len(c) > 140 else c

    def get_user_info(self, obj):
        # Only include user info if the requesting user is a superuser
        request = self.context.get('request')
        if request and request.user and request.user.is_superuser and obj.user:
            return {
                'id': obj.user.id,
                'email': obj.user.email,
                'first_name': obj.user.first_name,
                'last_name': obj.user.last_name,
                'orcid_id': obj.user.orcid_id,
                'institution': obj.user.institution,
                'department': obj.user.department,
                'country': obj.user.country
            }
        return None
