from api.models import Dataset, Table, Agent, Message, Task, UserFile
from rest_framework import serializers
from api.helpers import discord_bot


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

    def create(self, validated_data):
        try:
            user_file = UserFile.objects.create(**validated_data)
            file_type, dfs = user_file.extract_data()
        except Exception as exc:
            if 'user_file' in locals():
                user_file.delete()
            raise serializers.ValidationError(f"An error was encountered when loading your data. Error details: {exc}.")

        if file_type == UserFile.FileType.TABULAR:
            try:
                filtered_dfs = UserFile.filter_dataframes(dfs)
            except ValueError as exc:
                user_file.delete()
                raise serializers.ValidationError(str(exc))
            user_file.create_tables(filtered_dfs)

        return user_file


class DatasetSerializer(serializers.ModelSerializer):
    user_files = UserFileSerializer(many=True, read_only=True)
    visible_agent_set = serializers.SerializerMethodField()
    user_info = serializers.SerializerMethodField()

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
            'visible_agent_set',
            'user_info',
            'user_files',
        ]
        read_only_fields = [
            'created_at',
            'user',
            'visible_agent_set',
            'user_info',
            'user_files',
            'published_at',
            'rejected_at',
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

        if not dataset.table_set.exists():
            dataset.delete()
            raise serializers.ValidationError(
                "No tabular data could be loaded from your files. "
                "Please upload at least one spreadsheet or delimited text file with two or more data rows."
            )

        discord_bot.send_discord_message(
            f"V2 New dataset publication starting on ChatIPT. User files: {', '.join(uploaded_names) if uploaded_names else 'none'}."
        )

        first_task = Task.objects.first()
        if not first_task:
            dataset.delete()
            raise serializers.ValidationError(
                "No tasks are configured in the system. Please contact the administrator to load the required tasks."
            )

        Agent.create_with_system_message(
            dataset=dataset,
            task=first_task,
            tables=list(dataset.table_set.all()),
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
            'record_count', 'last_updated', 'status', 'progress', 'last_message_preview', 'user_info', 'user_files'
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
