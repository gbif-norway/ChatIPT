"""
URL configuration for app project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from rest_framework import routers
from api import views as api_views
from drf_spectacular.views import SpectacularAPIView
from website import views

router = routers.DefaultRouter()
router.register(r'datasets', api_views.DatasetViewSet, basename='dataset')
router.register(r'tables', api_views.TableViewSet, basename='table')
router.register(r'messages', api_views.MessageViewSet, basename='message')
router.register(r'agents', api_views.AgentViewSet, basename='agent')  
router.register(r'tasks', api_views.TaskViewSet, basename='task')  

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('api/', include(router.urls)),
    path('api/auth/status/', api_views.auth_status, name='auth_status'),
    path('api/auth/profile/', api_views.user_profile, name='user_profile'),
    path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    path('api-schema/', SpectacularAPIView.as_view(), name='schema'),
    path('', views.DatasetListView.as_view(), name='index'),
    path('chat/', views.Chat.as_view(), name='chat_create'),
    path('chat/<int:pk>/', views.DatasetDetailView.as_view(), name='chat_detail'),
]
