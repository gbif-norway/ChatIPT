from rest_framework import viewsets, status
from api.serializers import DatasetSerializer, TableSerializer, MessageSerializer, AgentSerializer, TaskSerializer
from api.models import Dataset, Table, Message, Agent, Task
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from rest_framework.serializers import ModelSerializer

User = get_user_model()


class UserSerializer(ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'orcid_id', 'institution', 'department', 'country']
        read_only_fields = ['id', 'email', 'orcid_id']


@api_view(['GET'])
@permission_classes([AllowAny])
def auth_status(request):
    """Check authentication status and return user info if authenticated"""
    if request.user.is_authenticated:
        serializer = UserSerializer(request.user)
        return Response({
            'authenticated': True,
            'user': serializer.data
        })
    else:
        return Response({
            'authenticated': False,
            'user': None
        })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def user_profile(request):
    """Get current user's profile information"""
    serializer = UserSerializer(request.user)
    return Response(serializer.data)


class DatasetViewSet(viewsets.ModelViewSet):
    serializer_class = DatasetSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['created_at', 'orcid']

    def get_queryset(self):
        """Filter datasets to only show those belonging to the authenticated user"""
        return Dataset.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        """Automatically assign the current user to the dataset"""
        serializer.save(user=self.request.user)

    # @action(detail=True)
    # def next_agent(self, request, *args, **kwargs):
    #     dataset = self.get_object()
    #     serializer = AgentSerializer(dataset.next_agent())
    #     return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True)
    def refresh(self, request, *args, **kwargs):
        dataset = self.get_object()

        # Do a refresh of agents and messages, so we get the next one of each if necessary
        next_agent = dataset.next_agent()
        if next_agent:
            next_agent.next_message()
            dataset.refresh_from_db()

        serializer = self.get_serializer(dataset)
        return Response(serializer.data)


class TableViewSet(viewsets.ModelViewSet):
    serializer_class = TableSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['dataset', 'title']
    ordering = ['-updated_at']

    def get_queryset(self):
        """Filter tables to only show those belonging to the authenticated user's datasets"""
        return Table.objects.filter(dataset__user=self.request.user)


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = '__all__'

    def get_queryset(self):
        """Return all tasks (they are system-wide, not user-specific)"""
        return Task.objects.all()


class MessageViewSet(viewsets.ModelViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['agent', 'created_at']

    def get_queryset(self):
        """Filter messages to only show those belonging to the authenticated user's datasets"""
        return Message.objects.filter(agent__dataset__user=self.request.user)


class AgentViewSet(viewsets.ModelViewSet):
    serializer_class = AgentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['created_at', 'completed_at', 'dataset', 'task']

    def get_queryset(self):
        """Filter agents to only show those belonging to the authenticated user's datasets"""
        return Agent.objects.filter(dataset__user=self.request.user)
