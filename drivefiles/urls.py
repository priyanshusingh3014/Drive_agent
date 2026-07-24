from django.urls import path

from . import views


urlpatterns = [
    path("", views.drive_files, name="drive_files"),
    path("files-data/", views.drive_files_data, name="drive_files_data"),
    path("active-agents-data/", views.active_agents_data, name="active_agents_data"),
    path("agent-ping/", views.agent_ping, name="agent_ping"),
    path("agent-heartbeat/", views.agent_heartbeat, name="agent_heartbeat"),
    path("agent-files-batch/", views.agent_files_batch, name="agent_files_batch"),
    path("agent-file-download/", views.agent_file_download, name="agent_file_download"),
    path("agent-uninstall/", views.agent_uninstall, name="agent_uninstall"),
    path("select-agent/", views.select_agent, name="select_agent"),
    path("select-drive/", views.select_drive, name="select_drive"),
    path("scan-now/", views.scan_now, name="scan_now"),
    path("download/", views.download_file, name="download_file"),
]
