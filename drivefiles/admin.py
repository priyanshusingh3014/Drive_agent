from django.contrib import admin

from .models import ActiveAgent, ActiveAgentDrive, ActiveAgentFile


@admin.register(ActiveAgent)
class ActiveAgentAdmin(admin.ModelAdmin):
    list_display = (
        "host_name",
        "ip_address",
        "mac_address",
        "drive_count",
        "total_files",
        "last_seen_at",
    )
    search_fields = ("agent_id", "host_name", "ip_address", "mac_address")
    readonly_fields = ("first_seen_at", "last_seen_at")


@admin.register(ActiveAgentDrive)
class ActiveAgentDriveAdmin(admin.ModelAdmin):
    list_display = (
        "agent",
        "label",
        "total_files",
        "indexed_files",
        "count_complete",
        "last_reported_at",
    )
    list_filter = ("count_complete",)
    search_fields = ("agent__host_name", "agent__agent_id", "label", "value")
    readonly_fields = ("last_reported_at",)


@admin.register(ActiveAgentFile)
class ActiveAgentFileAdmin(admin.ModelAdmin):
    list_display = ("name", "drive", "agent", "type_label", "size", "modified_display")
    list_filter = ("type_class",)
    search_fields = ("name", "relative_path", "folder", "agent__host_name")
