from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, Dataset, Task, Table, Agent, Message, UserFile, PdfExtraction


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
    list_display = ('title', 'user', 'orcid', 'source_mode', 'created_at', 'published_at', 'dwc_core')
    list_filter = ('source_mode', 'dwc_core', 'published_at', 'rejected_at', 'created_at')
    search_fields = ('title', 'description', 'user__email', 'orcid')
    readonly_fields = ('created_at', 'published_at', 'rejected_at')
    date_hierarchy = 'created_at'


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('name', 'order', 'id')
    search_fields = ('name', 'text')
    ordering = ('order', 'id')


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ('title', 'dataset', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('title', 'description', 'dataset__title')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(UserFile)
class UserFileAdmin(admin.ModelAdmin):
    list_display = ('filename', 'dataset', 'uploaded_at', 'get_file_type')
    list_filter = ('uploaded_at',)
    search_fields = ('file', 'dataset__title', 'dataset__user__email')
    readonly_fields = ('uploaded_at',)

    def get_file_type(self, obj):
        return obj.file_type_label

    get_file_type.short_description = 'File type'


@admin.register(PdfExtraction)
class PdfExtractionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_file', 'status', 'page_count', 'model', 'updated_at')
    list_filter = ('status', 'updated_at', 'created_at')
    search_fields = ('user_file__file', 'user_file__dataset__title', 'fingerprint', 'openai_file_id')
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
