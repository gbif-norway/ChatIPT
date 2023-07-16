from rest_framework import viewsets
from api.serializers import DatasetSerializer, DataFrameSerializer, MessageSerializer, WorkerSerializer
from api.models import Dataset, DataFrame, Message, Worker
from rest_framework.response import Response
from rest_framework.decorators import action 


class DatasetViewSet(viewsets.ModelViewSet):
    queryset = Dataset.objects.all()
    serializer_class = DatasetSerializer
    filterset_fields = '__all__'

    @action(detail=True)
    def chat(self, request, *args, **kwargs):
        dataset = self.get_object()
        dfs = dataset.dataframe_set.all()
        # master_worker = Worker.objects.create(task....)
        # Make master plan worker, and let it spawn workers


class DataFrameViewSet(viewsets.ModelViewSet):
    queryset = DataFrame.objects.all()
    serializer_class = DataFrameSerializer
    filterset_fields = ['dataset', 'sheet_name']


class MessageViewSet(viewsets.ModelViewSet):
    queryset = Message.objects.all()
    serializer_class = MessageSerializer
    filterset_fields = '__all__'


class WorkerViewSet(viewsets.ModelViewSet):
    queryset = Worker.objects.all()
    serializer_class = WorkerSerializer
    filterset_fields = '__all__'
