from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, Dataset, Task, Table, Agent, Message


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ('email', 'username', 'orcid_id', 'institution', 'country', 'is_staff', 'is_active')
    list_filter = ('is_staff', 'is_active', 'country')
    search_fields = ('email', 'username', 'orcid_id', 'institution')
    ordering = ('email',)
    
    fieldsets = UserAdmin.fieldsets + (
        ('ORCID Information', {
            'fields': ('orcid_id', 'orcid_access_token', 'orcid_refresh_token')
        }),
        ('Profile Information', {
            'fields': ('institution', 'department', 'country')
        }),
    )
    
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('ORCID Information', {
            'fields': ('orcid_id', 'orcid_access_token', 'orcid_refresh_token')
        }),
        ('Profile Information', {
            'fields': ('institution', 'department', 'country')
        }),
    )


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'orcid', 'created_at', 'published_at', 'dwc_core')
    list_filter = ('dwc_core', 'published_at', 'rejected_at', 'created_at')
    search_fields = ('title', 'description', 'user__email', 'orcid')
    readonly_fields = ('created_at', 'published_at', 'rejected_at')
    date_hierarchy = 'created_at'


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('name', 'id')
    search_fields = ('name', 'text')
    ordering = ('id',)


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ('title', 'dataset', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('title', 'description', 'dataset__title')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ('dataset', 'task', 'created_at', 'completed_at', 'busy_thinking')
    list_filter = ('completed_at', 'busy_thinking', 'created_at', 'task')
    search_fields = ('dataset__title', 'task__name')
    readonly_fields = ('created_at', 'completed_at')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('agent', 'role', 'created_at')
    list_filter = ('created_at',)  # Removed 'role' since it's a property
    search_fields = ('agent__dataset__title',)
    readonly_fields = ('created_at',) 