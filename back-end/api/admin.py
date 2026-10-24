from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from . import gbif_pause
from .models import (
    Agent,
    CustomUser,
    Dataset,
    DatasetAttentionNotification,
    GbifOutage,
    Message,
    OpenAIUsage,
    Table,
    Task,
    UserFile,
)


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
    list_filter = ('source_mode', 'dwc_core', 'published_at', 'created_at')
    search_fields = ('title', 'description', 'user__email', 'orcid')
    readonly_fields = ('created_at', 'published_at')
    date_hierarchy = 'created_at'


@admin.register(DatasetAttentionNotification)
class DatasetAttentionNotificationAdmin(admin.ModelAdmin):
    list_display = ('dataset', 'email', 'status', 'attention_kind', 'attempt_count', 'sent_at')
    list_filter = ('status', 'attention_kind', 'created_at', 'sent_at')
    search_fields = ('email', 'dataset__title')
    readonly_fields = ('created_at', 'updated_at', 'ready_at', 'sent_at', 'attempt_count', 'last_error')


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


@admin.register(OpenAIUsage)
class OpenAIUsageAdmin(admin.ModelAdmin):
    list_display = (
        'response_id',
        'dataset',
        'task_name',
        'retry_reason',
        'model',
        'input_tokens',
        'cached_input_tokens',
        'output_tokens',
        'duration_ms',
        'long_context',
        'estimated_cost_usd',
        'created_at',
    )
    list_filter = ('model', 'task_name', 'retry_reason', 'long_context', 'service_tier', 'created_at')
    search_fields = ('response_id', 'dataset__title', '=dataset__id', '=agent__id')
    readonly_fields = tuple(field.name for field in OpenAIUsage._meta.fields)
    date_hierarchy = 'created_at'


class GbifOutageForm(forms.ModelForm):
    class Meta:
        model = GbifOutage
        fields = ('message',)

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk is None and gbif_pause.is_paused():
            raise forms.ValidationError('ChatIPT is already paused for a GBIF outage. Edit that outage instead.')
        return cleaned


@admin.register(GbifOutage)
class GbifOutageAdmin(admin.ModelAdmin):
    """Adding an outage pauses all processing; the "End" action resumes it."""
    form = GbifOutageForm
    list_display = ('started_at', 'ended_at', 'started_by', 'ended_by', 'message')
    readonly_fields = ('started_at', 'ended_at', 'started_by', 'ended_by')
    actions = ['end_outage']

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        outage, _ = gbif_pause.start(obj.message, by=request.user.get_username())
        obj.pk, obj.started_at, obj.started_by = outage.pk, outage.started_at, outage.started_by

    def has_delete_permission(self, request, obj=None):
        return False  # Ended outages keep GBIF validation waits from counting paused time.

    @admin.action(description='End the GBIF outage and resume processing')
    def end_outage(self, request, queryset):
        if not queryset.filter(ended_at__isnull=True).exists():
            self.message_user(request, 'Select the ongoing outage to end it.', messages.WARNING)
        elif gbif_pause.end(by=request.user.get_username()):
            self.message_user(request, 'Processing resumed.')
