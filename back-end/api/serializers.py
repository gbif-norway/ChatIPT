from api.models import Dataset, Table, Agent, Message, Task
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


class DatasetSerializer(serializers.ModelSerializer):
    visible_agent_set = serializers.SerializerMethodField()

    class Meta:
        model = Dataset
        fields = '__all__'
    
    def get_visible_agent_set(self, dataset):
        agents = list(dataset.agent_set.filter(completed_at__isnull=False))
        next_active_agent = dataset.agent_set.filter(completed_at__isnull=True).first()
        if next_active_agent:
            agents.append(next_active_agent)
        return AgentSerializer(agents, many=True).data

    def create(self, data):
        discord_bot.send_discord_message(f"V2 New dataset publication starting on ChatIPT. User file: {data['file'].name}.")
        dataset = Dataset.objects.create(**data)

        try:
            dfs = Dataset.get_dfs_from_user_file(dataset.file, dataset.file.name.split('/')[1])
        except Exception as e:
            raise serializers.ValidationError(f"An error was encountered when loading your data. Error details: {e}.")

        if "error" in dfs:
            raise serializers.ValidationError(dfs["error"])
        
        for sheet_name, df in dfs.items():
            if len(df) < 2:
                raise serializers.ValidationError(f"Your sheet {sheet_name} has only {len(df) + 1} row(s), are you sure you uploaded the right thing? I need a larger spreadsheet to be able to help you with publication. Please refresh and try again.")

        tables = []
        for sheet_name, df in dfs.items():
            if not df.empty:
                tables.append(Table.objects.create(dataset=dataset, title=sheet_name, df=df))
        
        # Check if tasks exist
        first_task = Task.objects.first()
        if not first_task:
            raise serializers.ValidationError(
                "No tasks are configured in the system. Please contact the administrator to load the required tasks."
            )
        
        agent = Agent.create_with_system_message(dataset=dataset, task=first_task, tables=tables)
        discord_bot.send_discord_message(f"Dataset ID assigned: {dataset.id}.")
        return dataset


class DatasetListSerializer(serializers.ModelSerializer):
    record_count = serializers.SerializerMethodField()
    last_updated = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    last_message_preview = serializers.SerializerMethodField()

    class Meta:
        model = Dataset
        fields = [
            'id', 'title', 'description', 'filename', 'dwc_core',
            'created_at', 'published_at', 'rejected_at',
            'record_count', 'last_updated', 'status', 'progress', 'last_message_preview'
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
