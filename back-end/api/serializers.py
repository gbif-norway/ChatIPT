from api.models import Dataset, Table, Agent, Message, OpenAIUsage, Task, TaxonNameMatch, UserFile
from django.db import transaction
from rest_framework import serializers
from api.helpers import discord_bot
import logging


logger = logging.getLogger(__name__)


class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = ['id', 'name', 'text']


class TableSerializer(serializers.ModelSerializer):
    is_dwc_dp = serializers.SerializerMethodField()

    class Meta:
        model = Table
        fields = [
            'id',
            'created_at',
            'updated_at',
            'dataset',
            'title',
            'description',
            'row_count',
            'columns',
            'is_dwc_dp',
        ]
        read_only_fields = fields

    def get_is_dwc_dp(self, obj):
        from api.dwc_dp_specs import RESERVED_TABLE_NAMES

        return obj.title in RESERVED_TABLE_NAMES


class TaxonNameMatchSerializer(serializers.ModelSerializer):
    suggestion_status = serializers.CharField(read_only=True)
    preprocessed = serializers.SerializerMethodField()
    bulk_acceptable = serializers.SerializerMethodField()
    replacements = serializers.SerializerMethodField()

    class Meta:
        model = TaxonNameMatch
        fields = [
            'id', 'dataset', 'verbatim_label', 'context_key', 'context_note', 'record_count',
            'source_table', 'context_column', 'query', 'preprocessing_note', 'preprocessed', 'bulk_acceptable', 'replacements',
            'identification_qualifier',
            'match', 'suggestion_status', 'review_aids', 'col_release', 'matched_at',
            'decision', 'decided_usage', 'decided_by', 'decided_at', 'applied_at',
        ]
        read_only_fields = fields

    def get_preprocessed(self, obj):
        from api.taxon_matching import is_preprocessed
        return is_preprocessed(obj)

    def get_bulk_acceptable(self, obj):
        from api.taxon_matching import bulk_acceptable
        return bulk_acceptable(obj)

    def get_replacements(self, obj):
        # What accepting the suggestion or an alternative would replace; such a choice needs confirm_coarser.
        from api.taxon_matching import choice_replacements
        return choice_replacements(obj)


class TaxonDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=TaxonNameMatch.Decision.choices)
    usage_id = serializers.CharField(required=False, allow_blank=False)
    scientificName = serializers.CharField(required=False, allow_blank=True)
    scientificNameAuthorship = serializers.CharField(required=False, allow_blank=True)
    taxonRank = serializers.CharField(required=False, allow_blank=True)
    confirm_coarser = serializers.BooleanField(required=False, default=False)


class TablePageQuerySerializer(serializers.Serializer):
    offset = serializers.IntegerField(default=0, min_value=0)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=200)
    search = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, max_length=200)
    column = serializers.CharField(required=False, allow_blank=False, max_length=500)
    exact = serializers.BooleanField(default=False)


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
    table_set = serializers.SerializerMethodField()

    class Meta:
        model = Agent
        fields = '__all__'

    def get_table_set(self, agent):
        return list(
            agent.tables.order_by('id').values(
                'id', 'title', 'updated_at', 'row_count', 'columns'
            )
        )


class OpenAIUsageSerializer(serializers.ModelSerializer):
    class Meta:
        model = OpenAIUsage
        fields = [
            'id',
            'created_at',
            'dataset',
            'agent',
            'task_name',
            'response_id',
            'response_status',
            'cache_prefix_hash',
            'cache_diagnostics',
            'retry_reason',
            'model',
            'reasoning_effort',
            'service_tier',
            'input_tokens',
            'cached_input_tokens',
            'cache_write_input_tokens',
            'output_tokens',
            'reasoning_tokens',
            'total_tokens',
            'duration_ms',
            'long_context',
            'input_price_per_million',
            'cached_input_price_per_million',
            'cache_write_price_per_million',
            'output_price_per_million',
            'input_price_multiplier',
            'output_price_multiplier',
            'estimated_cost_usd',
            'pricing_source',
        ]
        read_only_fields = fields


class UserFileSerializer(serializers.ModelSerializer):
    filename = serializers.CharField(read_only=True)
    file_url = serializers.SerializerMethodField()
    file_type = serializers.SerializerMethodField()

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
        ]
        read_only_fields = [
            'id',
            'uploaded_at',
            'filename',
            'file_url',
            'file_type',
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

    @staticmethod
    def _delete_stored_file(user_file):
        if not user_file or not user_file.file:
            return
        try:
            user_file.file.delete(save=False)
        except Exception:
            logger.exception("Failed to clean up rejected upload %s", user_file.file.name)

    def create(self, validated_data):
        if validated_data.get('dataset') and validated_data['dataset'].workflow_type == Dataset.WorkflowType.DWCA_CONVERSION:
            raise serializers.ValidationError('Conversion source files are immutable. Start a new conversion to use different files.')
        user_file = None
        try:
            with transaction.atomic():
                user_file = UserFile(**validated_data)
                user_file.save()
                file_type, dfs = user_file.extract_data()

                if file_type == UserFile.FileType.UNKNOWN:
                    raise serializers.ValidationError(
                        "Unsupported file type. Please upload a spreadsheet/delimited file, "
                        "a phylogenetic tree file, or a PDF manuscript."
                    )

                if file_type == UserFile.FileType.TABULAR:
                    try:
                        source_manifest = UserFile.build_source_manifest(
                            dfs,
                            excel_visibility=getattr(user_file, "_excel_visibility", None),
                            excel_comments=getattr(user_file, "_excel_comments", None),
                        )
                        filtered_dfs = UserFile.filter_dataframes(dfs)
                    except ValueError as exc:
                        raise serializers.ValidationError(str(exc)) from exc
                    user_file.source_manifest = source_manifest
                    user_file.save(update_fields=["source_manifest"])
                    user_file.create_tables(filtered_dfs)

                dataset = user_file.dataset
                # Give a multi-file upload time to finish and its follow-up
                # message time to arrive. The upload alone still starts work.
                dataset.handle_source_change(queue_delay_seconds=15)
        except serializers.ValidationError:
            self._delete_stored_file(user_file)
            raise
        except Exception as exc:
            self._delete_stored_file(user_file)
            raise serializers.ValidationError(
                f"An error was encountered when loading your data. Error details: {exc}."
            ) from exc

        return user_file


class DatasetSerializer(serializers.ModelSerializer):
    user_files = UserFileSerializer(many=True, read_only=True)
    visible_agent_set = serializers.SerializerMethodField()
    user_info = serializers.SerializerMethodField()
    can_visualize_tree = serializers.SerializerMethodField()
    package_ready = serializers.BooleanField(read_only=True)
    dwc_dp_standard = serializers.SerializerMethodField()

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
            'dwca_url',
            'dwc_dp_url',
            'dwc_dp_validation',
            'dwc_dp_standard',
            'gbif_url',
            'user_language',
            'dwc_core',
            'source_mode',
            'workflow_type',
            'visible_agent_set',
            'user_info',
            'user_files',
            'can_visualize_tree',
            'package_ready',
        ]
        read_only_fields = [
            'created_at',
            'user',
            'visible_agent_set',
            'user_info',
            'user_files',
            'published_at',
            'can_visualize_tree',
            'source_mode',
            'dwc_dp_url',
            'dwc_dp_validation',
            'dwc_dp_standard',
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

    def get_dwc_dp_standard(self, dataset):
        from api.dwc_dp_specs import DWC_DP_PROFILE_URL, dwc_dp_schema_snapshot

        return {
            'profile': DWC_DP_PROFILE_URL,
            'schema': dwc_dp_schema_snapshot(),
        }

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

        if validated_data.get('workflow_type') == Dataset.WorkflowType.DWCA_CONVERSION:
            return self._create_conversion(validated_data, uploaded_files)

        dataset = Dataset.objects.create(**validated_data)
        uploaded_names = []
        try:
            for uploaded_file in uploaded_files:
                file_serializer = UserFileSerializer(
                    data={'file': uploaded_file},
                    context=self.context,
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

        discord_bot.send_discord_message(
            f"V3 New dataset publication starting on ChatIPT. User files: {', '.join(uploaded_names) if uploaded_names else 'none'}."
        )

        first_agent = dataset.next_agent()
        if not first_agent:
            dataset.delete()
            raise serializers.ValidationError(
                "No tasks are configured in the system. Please contact the administrator to load the required tasks."
            )

        initial_note = (request.data.get('upload_context_message') or '').strip() if request else ''
        if initial_note:
            Message.objects.create(
                agent=first_agent,
                openai_obj={'role': Message.Role.USER, 'content': initial_note},
            )

        discord_bot.send_discord_message(f"Dataset ID assigned: {dataset.id}.")
        return dataset

    def validate_workflow_type(self, value):
        if self.instance and value != self.instance.workflow_type:
            raise serializers.ValidationError('A dataset workflow cannot be changed after creation.')
        return value

    def update(self, instance, validated_data):
        if instance.workflow_type == Dataset.WorkflowType.DWCA_CONVERSION:
            raise serializers.ValidationError('Conversion inputs are immutable. Start a new conversion to use different inputs.')
        return super().update(instance, validated_data)

    def _create_conversion(self, validated_data, uploads):
        from api.dwca_import import ConversionError, ImportFailure, MAX_BYTES, read_inputs
        from api.models import DwcConversion, DwcConversionJob

        if sum(file.size for file in uploads) > MAX_BYTES:
            raise serializers.ValidationError('Upload at most 200 MB.')
        try:
            # Validate the input layout before storing files. A recoverable link mismatch
            # must reach the worker so its preview and row-removal action can be shown.
            read_inputs((file.name, file.read()) for file in uploads)
        except ConversionError as exc:
            if exc.evidence.get('kind') != 'unlinked-extension-records':
                raise serializers.ValidationError(str(exc)) from exc
        except ImportFailure as exc:
            raise serializers.ValidationError(str(exc)) from exc
        stored_files = []
        try:
            with transaction.atomic():
                dataset = Dataset.objects.create(**validated_data)
                for file in uploads:
                    file.seek(0)
                    source = UserFile.objects.create(dataset=dataset, file=file, source_manifest={'original_name': file.name})
                    stored_files.append(source)
                conversion = DwcConversion.objects.create(dataset=dataset)
                DwcConversionJob.objects.create(conversion=conversion)
        except Exception:
            for source in stored_files:
                self._delete_conversion_file(source)
            raise
        return dataset

    @staticmethod
    def _delete_conversion_file(source):
        UserFileSerializer._delete_stored_file(source)


class DatasetListSerializer(serializers.ModelSerializer):
    user_files = UserFileSerializer(many=True, read_only=True)
    record_count = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()
    last_updated = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    last_message_preview = serializers.SerializerMethodField()
    user_info = serializers.SerializerMethodField()
    package_ready = serializers.BooleanField(read_only=True)

    class Meta:
        model = Dataset
        fields = [
            'id', 'title', 'description', 'dwc_core',
            'created_at', 'published_at',
            'record_count', 'counts', 'last_updated', 'status', 'progress',
            'last_message_preview', 'user_info', 'user_files', 'source_mode',
            'package_ready',
            'workflow_type',
        ]

    def get_record_count(self, obj):
        counts = self.get_counts(obj)
        resources = counts['resources']
        for resource_name in ('occurrence', 'event', 'material', 'taxon'):
            if resource_name in resources:
                return resources[resource_name]
        return counts['source_rows']

    def get_counts(self, obj):
        from api.dwc_dp_specs import RESERVED_TABLE_NAMES

        resources = {}
        source_rows = 0
        for table in obj.table_set.only('title', 'row_count'):
            if table.title in RESERVED_TABLE_NAMES:
                resources[table.title] = resources.get(table.title, 0) + table.row_count
            else:
                source_rows += table.row_count
        return {
            'resources': dict(sorted(resources.items())),
            'package_rows': sum(resources.values()),
            'source_rows': source_rows,
        }

    def get_last_updated(self, obj):
        if obj.workflow_type == Dataset.WorkflowType.DWCA_CONVERSION and getattr(obj, 'conversion', None):
            return obj.conversion.updated_at
        last_table = obj.table_set.only('updated_at').order_by('-updated_at').first()
        return last_table.updated_at if last_table else obj.created_at

    def get_status(self, obj):
        if obj.workflow_type == Dataset.WorkflowType.DWCA_CONVERSION:
            state = getattr(obj, 'conversion', None)
            return {'complete': 'ready', 'review': 'needs_input', 'failed': 'failed'}.get(state.status if state else '', 'preparing')
        if obj.published_at: 
            return 'published'
        active_agent = obj.agent_set.filter(completed_at__isnull=True).order_by('created_at').first()
        if not active_agent:
            return 'ready' if obj.package_ready else 'preparing'

        last_message = active_agent.message_set.order_by('-created_at').first()
        openai_obj = (last_message.openai_obj or {}) if last_message else {}
        if openai_obj.get('workflow_error'):
            return 'failed'
        if obj.package_ready:
            return 'ready'
        has_tool_calls = bool(openai_obj.get('tool_calls'))
        is_working = (
            active_agent.busy_thinking
            or last_message is None
            or last_message.role != Message.Role.ASSISTANT
            or has_tool_calls
        )
        return 'preparing' if is_working else 'needs_input'

    def get_progress(self, obj):
        if obj.workflow_type == Dataset.WorkflowType.DWCA_CONVERSION:
            state = getattr(obj, 'conversion', None)
            return {'done': 3 if state and state.status == 'complete' else 1 if state and state.plan else 0, 'total': 3}
        applicable_tasks = [
            task
            for task in Task.objects.order_by('order', 'id')
            if task.name != 'Data maintenance' and not obj._should_skip_task(task)
        ]
        total = len(applicable_tasks)
        completed_task_ids = set(
            obj.agent_set.filter(completed_at__isnull=False).values_list('task_id', flat=True)
        )
        done = sum(task.id in completed_task_ids for task in applicable_tasks)
        if obj.package_ready or obj.published_at:
            done = total
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
