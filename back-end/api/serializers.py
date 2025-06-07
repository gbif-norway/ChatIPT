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
        agent = Agent.create_with_system_message(dataset=dataset, task=Task.objects.first(), tables=tables)
        discord_bot.send_discord_message(f"Dataset ID assigned: {dataset.id}.")
        return dataset
