import hmac
import ipaddress
import json
import math
import platform
import shutil
import socket
import time
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .drive_config import (
    DRIVE_ROOT,
    build_drive_context,
    drive_sort_key_for_value,
    drive_root_to_value,
    get_available_drive_roots,
    get_drive_options,
    normalize_drive_root,
)
from .forms import AgentSignupForm
from .models import (
    ActiveAgent,
    ActiveAgentDrive,
    ActiveAgentFile,
    DriveActivityLog,
    RemoteFileDownload,
)
from .scanner import (
    format_size,
    get_file_snapshot,
    get_scan_metadata,
    request_scan as request_drive_scan,
    set_drive_root,
    start_background_scanner,
)


FRONTEND_REFRESH_SECONDS = 0.5
ACTIVE_AGENTS_REFRESH_SECONDS = 0.5
TABLE_PAGE_SIZE = 10
SCROLL_BATCH_SIZE = 10000
MAX_SCROLL_BATCH_SIZE = 20000
SELECTED_DRIVE_SESSION_KEY = "drivefiles_selected_drive_root"
SELECTED_AGENT_SESSION_KEY = "drivefiles_selected_agent_id"
SELECTED_AGENT_DRIVE_SESSION_KEY = "drivefiles_selected_agent_drive"
STORAGE_CACHE_SECONDS = 5

FILE_TYPE_GROUPS = (
    {
        "name": "Documents",
        "key": "documents",
        "extensions": {
            ".doc",
            ".docx",
            ".log",
            ".md",
            ".odt",
            ".pdf",
            ".pps",
            ".ppsx",
            ".ppt",
            ".pptx",
            ".rtf",
            ".txt",
        },
        "color": "#2563eb",
    },
    {
        "name": "Images",
        "key": "images",
        "extensions": {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".webp"},
        "color": "#06b6d4",
    },
    {
        "name": "Videos",
        "key": "videos",
        "extensions": {".avi", ".flv", ".mkv", ".mov", ".mp4", ".webm", ".wmv"},
        "color": "#ef4444",
    },
    {
        "name": "Archives",
        "key": "archives",
        "extensions": {".7z", ".bz2", ".gz", ".rar", ".tar", ".xz", ".zip"},
        "color": "#f59e0b",
    },
    {
        "name": "Spreadsheets",
        "key": "spreadsheets",
        "extensions": {".csv", ".ods", ".tsv", ".xls", ".xlsx"},
        "color": "#14b8a6",
    },
    {
        "name": "Others",
        "key": "others",
        "extensions": set(),
        "color": "#94a3b8",
    },
)

FILTER_OPTIONS = (
    {"label": "All Files", "value": "all"},
    {"label": "Documents", "value": "documents"},
    {"label": "Images", "value": "images"},
    {"label": "Videos", "value": "videos"},
    {"label": "Archives", "value": "archives"},
    {"label": "Spreadsheets", "value": "spreadsheets"},
    {"label": "Others", "value": "others"},
)

FILE_TYPE_GROUP_BY_KEY = {group["key"]: group for group in FILE_TYPE_GROUPS}
SCANNER_CLASS_TO_DISTRIBUTION_KEY = {
    "archive": "archives",
    "document": "documents",
    "image": "images",
    "pdf": "documents",
    "spreadsheet": "spreadsheets",
    "video": "videos",
}
_SYSTEM_INFO_CACHE = None
_STORAGE_INFO_CACHE = {}
_DISTRIBUTION_CACHE = {}


def _is_agent_request_authorized(request):
    configured_token = getattr(settings, "AGENT_API_TOKEN", "")

    if not configured_token:
        return False

    provided_token = request.headers.get("X-Agent-Token", "").strip()
    authorization_header = request.headers.get("Authorization", "").strip()

    if authorization_header.lower().startswith("bearer "):
        provided_token = authorization_header[7:].strip()

    return hmac.compare_digest(provided_token, configured_token)


def _safe_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _safe_percent(value, default=0):
    try:
        return min(100, max(0, round(float(value), 1)))
    except (TypeError, ValueError):
        return default


def _safe_text(value, max_length, default=""):
    text = str(value if value is not None else default)
    return text[:max_length]


def _is_usable_agent_mac_address(value):
    normalized_value = str(value or "").strip().lower()

    return normalized_value not in {
        "",
        "00:00:00:00:00:00",
        "none",
        "unknown",
        "unavailable",
    }


def _is_usable_agent_ip_address(value):
    normalized_value = str(value or "").strip().lower()

    return normalized_value not in {
        "",
        "0.0.0.0",
        "none",
        "unknown",
        "unavailable",
    }


def _remove_duplicate_agent_reports(agent_id, host_name, mac_address, ip_address=""):
    if not host_name:
        return

    duplicate_identity_query = Q()

    if _is_usable_agent_mac_address(mac_address):
        duplicate_identity_query |= Q(mac_address__iexact=mac_address)

    if _is_usable_agent_ip_address(ip_address):
        duplicate_identity_query |= Q(ip_address__iexact=ip_address)

    if not duplicate_identity_query:
        return

    ActiveAgent.objects.filter(
        host_name__iexact=host_name,
    ).filter(duplicate_identity_query).exclude(agent_id=agent_id).delete()


def _stable_drive_file_count(drive_report):
    return max(drive_report.total_files, drive_report.indexed_files)


def _safe_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_agent_drives(raw_drives):
    if not isinstance(raw_drives, list):
        return []

    normalized_drives = []

    for drive in raw_drives[:16]:
        if not isinstance(drive, dict):
            continue

        storage = drive.get("storage")

        if not isinstance(storage, dict):
            storage = {}

        total_files = _safe_int(drive.get("total_files"))
        indexed_files = _safe_int(
            drive.get("indexed_files"),
            default=total_files,
        )

        normalized_drives.append(
            {
                "label": str(drive.get("label") or drive.get("value") or "Drive")[:32],
                "value": str(drive.get("value") or "")[:64],
                "total_files": total_files,
                "total_files_display": _format_number(total_files),
                "indexed_files": indexed_files,
                "indexed_files_display": _format_number(indexed_files),
                "count_complete": bool(drive.get("count_complete", True)),
                "storage": {
                    "used_display": str(storage.get("used_display") or "Unavailable")[:32],
                    "total_display": str(storage.get("total_display") or "Unavailable")[:32],
                    "free_display": str(storage.get("free_display") or "Unavailable")[:32],
                    "percent_used": _safe_percent(storage.get("percent_used")),
                },
            }
        )

    return normalized_drives


def _normalize_requested_drive_values(raw_values, available_values=()):
    if isinstance(raw_values, str):
        raw_drive_values = raw_values.replace(";", ",").split(",")
    elif isinstance(raw_values, (list, tuple, set)):
        raw_drive_values = raw_values
    else:
        raw_drive_values = []

    available_value_set = set(available_values)
    normalized_values = []

    for raw_value in raw_drive_values:
        drive_value = _safe_text(raw_value, 64).strip()

        if not drive_value:
            continue

        if available_value_set and drive_value not in available_value_set:
            continue

        if drive_value not in normalized_values:
            normalized_values.append(drive_value)

    return normalized_values[:8]


def _normalize_requested_file_downloads(raw_values, available_values=()):
    if not isinstance(raw_values, list):
        return []

    available_value_set = set(available_values)
    normalized_requests = []
    seen_request_ids = set()

    for raw_request in raw_values[:16]:
        if not isinstance(raw_request, dict):
            continue

        request_id = _safe_text(raw_request.get("request_id"), 64).strip()
        drive_value = _safe_text(raw_request.get("drive_value"), 64).strip()
        relative_path = _safe_text(raw_request.get("relative_path"), 2048).strip()

        if not request_id or not drive_value or not relative_path:
            continue

        if available_value_set and drive_value not in available_value_set:
            continue

        if request_id in seen_request_ids:
            continue

        seen_request_ids.add(request_id)
        normalized_requests.append(
            {
                "request_id": request_id,
                "drive_value": drive_value,
                "relative_path": relative_path,
            }
        )

    return normalized_requests[:8]


def _normalize_agent_file(raw_file):
    if not isinstance(raw_file, dict):
        return None

    relative_path = _safe_text(raw_file.get("relative_path"), 2048).strip()
    name = _safe_text(raw_file.get("name"), 255).strip()

    if not relative_path or not name:
        return None

    extension = _safe_text(raw_file.get("extension"), 64).strip()

    if not extension and "." in name:
        extension = f".{name.rsplit('.', 1)[-1]}"

    if extension.lower() == "no extension":
        extension = "No extension"
    elif extension:
        extension = extension.lower()

    return {
        "relative_path": relative_path,
        "name": name,
        "folder": _safe_text(raw_file.get("folder"), 2048),
        "extension": extension or "No extension",
        "type_badge": _safe_text(raw_file.get("type_badge") or "FILE", 16),
        "type_class": _safe_text(raw_file.get("type_class") or "other", 32).lower(),
        "type_label": _safe_text(raw_file.get("type_label") or extension or "File", 64),
        "size": _safe_text(raw_file.get("size") or "0 bytes", 32),
        "size_bytes": _safe_int(raw_file.get("size_bytes")),
        "modified_timestamp": _safe_float(raw_file.get("modified_timestamp")),
        "freshness_timestamp": _safe_float(raw_file.get("freshness_timestamp") or raw_file.get("modified_timestamp")),
        "modified_display": _safe_text(raw_file.get("modified_display"), 64),
    }


def _normalize_deleted_relative_paths(raw_paths):
    if not isinstance(raw_paths, list):
        return []

    normalized_paths = []
    seen_paths = set()

    for raw_path in raw_paths:
        relative_path = _safe_text(raw_path, 2048).strip()

        if not relative_path:
            continue

        path_key = relative_path.replace("/", "\\").lower()

        if path_key in seen_paths:
            continue

        seen_paths.add(path_key)
        normalized_paths.append(relative_path)

    return normalized_paths[:getattr(settings, "AGENT_FILE_BATCH_SIZE", 1000)]


def _drive_report_payload(drive_report):
    storage = drive_report.storage if isinstance(drive_report.storage, dict) else {}
    total_files = max(drive_report.total_files, drive_report.indexed_files)

    return {
        "label": drive_report.label,
        "value": drive_report.value,
        "total_files": total_files,
        "total_files_display": _format_number(total_files),
        "indexed_files": drive_report.indexed_files,
        "indexed_files_display": _format_number(drive_report.indexed_files),
        "count_complete": drive_report.count_complete,
        "storage": {
            "used_display": _safe_text(storage.get("used_display") or "Unavailable", 32),
            "total_display": _safe_text(storage.get("total_display") or "Unavailable", 32),
            "free_display": _safe_text(storage.get("free_display") or "Unavailable", 32),
            "percent_used": _safe_percent(storage.get("percent_used")),
        },
    }


def _format_relative_time(value):
    if not value:
        return "Never"

    seconds = max(0, int((timezone.now() - value).total_seconds()))

    if seconds < 10:
        return "Just now"

    if seconds < 60:
        return f"{seconds}s ago"

    minutes = seconds // 60

    if minutes < 60:
        return f"{minutes}m ago"

    hours = minutes // 60
    return f"{hours}h ago"


def _active_agent_cutoff():
    return timezone.now() - timedelta(
        seconds=getattr(settings, "AGENT_ONLINE_SECONDS", 120)
    )


def _get_or_create_local_agent_and_drive(drive_root=None):
    try:
        host_name = platform.node() or "Local Machine"
        agent_id = f"local-{host_name.lower().replace(' ', '-')}"
        agent, _ = ActiveAgent.objects.get_or_create(
            agent_id=agent_id,
            defaults={
                "host_name": host_name,
                "os_label": f"{platform.system()} {platform.release()}",
                "architecture": platform.machine(),
                "ip_address": "127.0.0.1",
            },
        )
        norm_root = normalize_drive_root(drive_root or DRIVE_ROOT)
        drive_value = drive_root_to_value(norm_root)
        drive_label = format_drive_label(norm_root)
        drive_report, _ = ActiveAgentDrive.objects.get_or_create(
            agent=agent,
            value=drive_value,
            defaults={
                "label": drive_label,
            },
        )
        return agent, drive_report
    except Exception:
        return None, None


def _log_drive_activity(agent, drive, activity_type, file_name, old_name="", details=""):
    if not file_name:
        return None

    if not agent or not drive:
        local_agent, local_drive = _get_or_create_local_agent_and_drive()
        agent = agent or local_agent
        drive = drive or local_drive

    try:
        return DriveActivityLog.objects.create(
            agent=agent,
            drive=drive,
            activity_type=activity_type,
            file_name=file_name[:255],
            old_name=old_name[:255],
            details=details[:512],
        )
    except Exception:
        return None


def log_local_scan_activities(added_files, renamed_files, deleted_files, drive_root=None):
    agent, drive = _get_or_create_local_agent_and_drive(drive_root)

    for item in renamed_files:
        _log_drive_activity(
            agent,
            drive,
            DriveActivityLog.TYPE_RENAMED,
            item["file_name"],
            old_name=item["old_name"],
            details=f"Renamed from {item['old_name']} in {item.get('folder', 'Root')}",
        )

    for item in deleted_files:
        _log_drive_activity(
            agent,
            drive,
            DriveActivityLog.TYPE_DELETED,
            item["file_name"],
            details=f"File deleted from {item.get('folder', 'Root')}",
        )

    for item in added_files:
        _log_drive_activity(
            agent,
            drive,
            DriveActivityLog.TYPE_ADDED,
            item["file_name"],
            details=f"File added in {item.get('folder', 'Root')}",
        )


def _build_recent_activities(drive_report=None, agent=None, limit=100, hours=48):
    cutoff_time = timezone.now() - timedelta(hours=hours)
    queryset = DriveActivityLog.objects.filter(timestamp__gte=cutoff_time).order_by('-timestamp')

    if drive_report:
        queryset = queryset.filter(drive=drive_report)
    elif agent:
        queryset = queryset.filter(agent=agent)

    logs = list(queryset.select_related("drive")[:limit])
    badge_map = {
        DriveActivityLog.TYPE_ADDED: ("added", "🟢 File Added", "activity-badge-added"),
        DriveActivityLog.TYPE_RENAMED: ("renamed", "✏️ File Renamed", "activity-badge-renamed"),
        DriveActivityLog.TYPE_DELETED: ("deleted", "🔴 File Deleted", "activity-badge-deleted"),
        DriveActivityLog.TYPE_EXTERNAL_COPY: ("copy", "💾 External Copy", "activity-badge-copy"),
    }

    activities = []
    for log in logs:
        key, label, badge_class = badge_map.get(
            log.activity_type,
            ("other", "ℹ️ Change", "activity-badge-info"),
        )
        activities.append(
            {
                "id": log.id,
                "type_key": key,
                "type_label": label,
                "badge_class": badge_class,
                "file_name": log.file_name,
                "old_name": log.old_name,
                "details": log.details or f"Activity in {log.drive.label if log.drive else 'Drive'}",
                "time_display": _format_relative_time(log.timestamp),
                "time_absolute": log.timestamp.strftime("%b %d, %Y %H:%M"),
                "drive_label": log.drive.label if log.drive else "",
            }
        )

    return activities



def _agent_is_online(agent, online_cutoff=None):
    if not agent.last_seen_at:
        return False

    return agent.last_seen_at >= (online_cutoff or _active_agent_cutoff())


def _build_active_agents(selected_agent_id=""):
    online_cutoff = _active_agent_cutoff()
    agents = list(
        ActiveAgent.objects
        .filter(last_seen_at__gte=online_cutoff)
        .prefetch_related("drive_reports")
        .order_by("first_seen_at", "agent_id")[:24]
    )

    if selected_agent_id and selected_agent_id not in {agent.agent_id for agent in agents}:
        selected_agent = (
            ActiveAgent.objects.filter(
                agent_id=selected_agent_id,
                last_seen_at__gte=online_cutoff,
            )
            .prefetch_related("drive_reports")
            .first()
        )

        if selected_agent:
            agents.append(selected_agent)

    agents.sort(key=lambda agent: (agent.first_seen_at, agent.agent_id))
    active_agents = []

    for agent in agents:
        payload = agent.latest_payload if isinstance(agent.latest_payload, dict) else {}
        drive_reports = _ordered_agent_drive_reports(agent.drive_reports.all())
        drives = (
            [_drive_report_payload(drive_report) for drive_report in drive_reports]
            if drive_reports
            else _normalize_agent_drives(payload.get("drives"))
        )

        active_agents.append(
            {
                "agent_id": agent.agent_id,
                "host_name": agent.host_name,
                "ip_address": agent.ip_address or "Unavailable",
                "mac_address": agent.mac_address or "Unavailable",
                "os_label": agent.os_label or "Unknown OS",
                "architecture": agent.architecture or "Unknown",
                "drive_count": agent.drive_count,
                "total_files": agent.total_files,
                "total_files_display": _format_number(agent.total_files),
                "last_seen_display": _format_relative_time(agent.last_seen_at),
                "is_online": True,
                "status_label": "Online",
                "drives": drives,
            }
        )

    return active_agents


def _active_agents_json_payload(selected_agent_id=""):
    selected_agent_id = _safe_text(selected_agent_id, 128).strip()
    active_agents = _build_active_agents(selected_agent_id)
    active_agent_ids = {agent["agent_id"] for agent in active_agents}
    selected_agent_exists = (
        ActiveAgent.objects.filter(agent_id=selected_agent_id).exists()
        if selected_agent_id
        else False
    )
    selected_agent_removed = bool(selected_agent_id and not selected_agent_exists)

    return {
        "agents": active_agents,
        "total_agents": len(active_agents),
        "online_agents": sum(1 for agent in active_agents if agent["is_online"]),
        "selected_agent_id": selected_agent_id if selected_agent_exists else "",
        "selected_agent_online": selected_agent_id in active_agent_ids,
        "selected_agent_removed": selected_agent_removed,
    }


def drive_context(drive_root=None):
    return build_drive_context(drive_root or DRIVE_ROOT)


def _available_drive_map():
    return {
        drive_root_to_value(root): root
        for root in get_available_drive_roots()
    }


def _resolve_drive_root(value):
    available_drives = _available_drive_map()

    if value:
        requested_value = drive_root_to_value(normalize_drive_root(value))

        if requested_value in available_drives:
            return available_drives[requested_value]

    default_value = drive_root_to_value(DRIVE_ROOT)

    if default_value in available_drives:
        return available_drives[default_value]

    return next(iter(available_drives.values()), DRIVE_ROOT)


def _get_selected_drive_root(request):
    return _resolve_drive_root(
        request.session.get(SELECTED_DRIVE_SESSION_KEY)
    )


def _set_selected_drive_root(request, drive_root):
    request.session[SELECTED_DRIVE_SESSION_KEY] = drive_root_to_value(drive_root)
    request.session.modified = True


def _get_selected_agent_id(request):
    return str(request.session.get(SELECTED_AGENT_SESSION_KEY) or "")


def _auto_selected_hosted_agent():
    if _local_drive_scanner_enabled() or not getattr(
        settings,
        "AGENT_AUTO_SELECT_SINGLE_ACTIVE_AGENT",
        True,
    ):
        return None

    online_agents = list(
        ActiveAgent.objects
        .filter(last_seen_at__gte=_active_agent_cutoff())
        .order_by("host_name", "agent_id")[:2]
    )

    if len(online_agents) == 1:
        return online_agents[0]

    return None


def _active_agent_queryset(require_online=True):
    queryset = ActiveAgent.objects.all()

    if require_online:
        queryset = queryset.filter(last_seen_at__gte=_active_agent_cutoff())

    return queryset


def _get_selected_agent(
    request,
    requested_agent_id="",
    require_online=True,
    persist_session=True,
    allow_auto_select=True,
):
    agent_id = _safe_text(requested_agent_id, 128).strip() or _get_selected_agent_id(request)

    if not agent_id:
        hosted_agent = _auto_selected_hosted_agent() if allow_auto_select else None

        if hosted_agent:
            if persist_session:
                _set_selected_agent(request, hosted_agent.agent_id)

            _queue_agent_drive_scan(
                hosted_agent,
                _get_selected_agent_drive_value(
                    request,
                    hosted_agent,
                    persist_session=persist_session,
                ),
            )
            return hosted_agent

        return None

    agent = _active_agent_queryset(require_online=require_online).filter(
        agent_id=agent_id,
    ).first()

    if not agent:
        if persist_session and agent_id == _get_selected_agent_id(request):
            request.session.pop(SELECTED_AGENT_SESSION_KEY, None)
            request.session.pop(SELECTED_AGENT_DRIVE_SESSION_KEY, None)
            request.session.modified = True

        return None

    if persist_session and requested_agent_id:
        _set_selected_agent(request, agent.agent_id)

    return agent


def _validated_selected_agent_id(request):
    agent_id = _safe_text(
        request.GET.get("agent_id") or request.POST.get("agent_id"),
        128,
    ).strip() or _get_selected_agent_id(request)

    if not agent_id:
        return ""

    if ActiveAgent.objects.filter(agent_id=agent_id).exists():
        return agent_id

    request.session.pop(SELECTED_AGENT_SESSION_KEY, None)
    request.session.pop(SELECTED_AGENT_DRIVE_SESSION_KEY, None)
    request.session.modified = True
    return ""


def _set_selected_agent(request, agent_id):
    if agent_id:
        request.session[SELECTED_AGENT_SESSION_KEY] = agent_id
    else:
        request.session.pop(SELECTED_AGENT_SESSION_KEY, None)
        request.session.pop(SELECTED_AGENT_DRIVE_SESSION_KEY, None)

    request.session.modified = True


def _get_selected_agent_drive_value(
    request,
    agent,
    requested_drive_value="",
    persist_session=True,
):
    selected_value = str(
        requested_drive_value
        or request.session.get(SELECTED_AGENT_DRIVE_SESSION_KEY)
        or ""
    )
    drive_reports = _ordered_agent_drive_reports(agent.drive_reports.all())
    available_values = {drive_report.value for drive_report in drive_reports}

    if selected_value in available_values:
        if persist_session:
            request.session[SELECTED_AGENT_DRIVE_SESSION_KEY] = selected_value
            request.session.modified = True

        return selected_value

    preferred_drive = _preferred_agent_drive_report(drive_reports)

    if not preferred_drive:
        return ""

    if persist_session:
        request.session[SELECTED_AGENT_DRIVE_SESSION_KEY] = preferred_drive.value
        request.session.modified = True

    return preferred_drive.value


def _agent_drive_sort_key(drive_report):
    return drive_sort_key_for_value(drive_report.value or drive_report.label)


def _ordered_agent_drive_reports(drive_reports):
    return sorted(list(drive_reports), key=_agent_drive_sort_key)


def _preferred_agent_drive_report(drive_reports):
    return drive_reports[0] if drive_reports else None


def _set_selected_agent_drive_value(request, agent, value):
    drive_value = str(value or "")

    if agent.drive_reports.filter(value=drive_value).exists():
        request.session[SELECTED_AGENT_DRIVE_SESSION_KEY] = drive_value
        request.session.modified = True
        return drive_value

    return _get_selected_agent_drive_value(request, agent)


def _queue_agent_drive_scan(agent, drive_value):
    requested_drive_value = _safe_text(drive_value, 64).strip()

    if not requested_drive_value:
        return

    latest_payload = agent.latest_payload if isinstance(agent.latest_payload, dict) else {}
    requested_values = _normalize_requested_drive_values(
        latest_payload.get("requested_drive_values"),
        [requested_drive_value],
    )

    if requested_values == [requested_drive_value]:
        return

    latest_payload["requested_drive_values"] = [requested_drive_value]
    agent.latest_payload = latest_payload
    agent.save(update_fields=("latest_payload",))


def _queue_agent_file_download(agent, drive, file_report):
    download_request = RemoteFileDownload.objects.create(
        request_id=uuid.uuid4().hex,
        agent=agent,
        drive=drive,
        relative_path=file_report.relative_path,
        name=file_report.name,
        modified_timestamp=file_report.modified_timestamp,
    )
    latest_payload = agent.latest_payload if isinstance(agent.latest_payload, dict) else {}
    queued_downloads = _normalize_requested_file_downloads(
        latest_payload.get("requested_file_downloads"),
        [drive.value],
    )
    queued_downloads.append(
        {
            "request_id": download_request.request_id,
            "drive_value": drive.value,
            "relative_path": file_report.relative_path,
        }
    )
    latest_payload["requested_file_downloads"] = queued_downloads[-8:]
    agent.latest_payload = latest_payload
    agent.save(update_fields=("latest_payload",))
    return download_request


def _remote_download_storage_path(download_request):
    download_root = Path(settings.REMOTE_FILE_DOWNLOAD_ROOT)
    return download_root / f"{download_request.request_id}.download"


def _ready_remote_download_for_file(agent, drive, file_report):
    return (
        RemoteFileDownload.objects.filter(
            agent=agent,
            drive=drive,
            relative_path=file_report.relative_path,
            modified_timestamp=file_report.modified_timestamp,
            status=RemoteFileDownload.STATUS_READY,
        )
        .order_by("-updated_at")
        .first()
    )


def _remote_download_file_response(download_request):
    download_path = Path(download_request.file_path)

    if not download_path.exists() or not download_path.is_file():
        return None

    return FileResponse(
        open(download_path, "rb"),
        as_attachment=True,
        filename=download_request.name,
    )


def _safe_redirect_target(request):
    redirect_target = request.POST.get("next") or request.GET.get("next")

    if redirect_target and url_has_allowed_host_and_scheme(
        redirect_target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect_target

    return "drive_files"


class AgentLoginView(LoginView):
    template_name = "drivefiles/login.html"
    redirect_authenticated_user = True
    extra_context = {
        "auth_mode": "login",
        **drive_context(),
    }

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["username"].widget.attrs.update(
            {
                "placeholder": "Enter your username",
                "autocomplete": "username",
            }
        )
        form.fields["password"].widget.attrs.update(
            {
                "placeholder": "Enter your password",
                "autocomplete": "current-password",
            }
        )
        return form

    def get_initial(self):
        initial = super().get_initial()
        username = self.request.GET.get("username", "").strip()

        if username:
            initial["username"] = username

        return initial

    def form_valid(self, form):
        response = super().form_valid(form)

        if self.request.POST.get("remember_me"):
            self.request.session.set_expiry(60 * 60 * 24 * 14)
        else:
            self.request.session.set_expiry(0)

        return response


def signup(request):
    if request.user.is_authenticated:
        return redirect("drive_files")

    if request.method == "POST":
        form = AgentSignupForm(request.POST)

        if form.is_valid():
            user = form.save()
            query_params = {"username": user.get_username()}
            redirect_target = request.POST.get("next") or request.GET.get("next")

            if redirect_target and url_has_allowed_host_and_scheme(
                redirect_target,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                query_params["next"] = redirect_target

            return redirect(f"{reverse('login')}?{urlencode(query_params)}")
    else:
        form = AgentSignupForm()

    return render(
        request,
        "drivefiles/login.html",
        {
            "auth_mode": "signup",
            **drive_context(),
            "form": form,
            "redirect_field_name": "next",
            "redirect_field_value": request.GET.get("next", ""),
        },
    )


def _format_number(value):
    return f"{value:,}"


def _parse_positive_int(value, default=1):
    try:
        parsed_value = int(value)
    except (TypeError, ValueError):
        return default

    return parsed_value if parsed_value > 0 else default


def _parse_non_negative_int(value, default=0):
    try:
        parsed_value = int(value)
    except (TypeError, ValueError):
        return default

    return parsed_value if parsed_value >= 0 else default


def _requested_scroll_window(request):
    if "offset" not in request.GET:
        return None

    return {
        "offset": _parse_non_negative_int(request.GET.get("offset")),
        "limit": min(
            _parse_positive_int(
                request.GET.get("limit"),
                default=SCROLL_BATCH_SIZE,
            ),
            MAX_SCROLL_BATCH_SIZE,
        ),
    }


def _build_scroll_info(total_items, offset, limit):
    safe_offset = min(max(offset, 0), total_items)
    safe_limit = min(max(limit, 1), MAX_SCROLL_BATCH_SIZE)
    end_index = min(safe_offset + safe_limit, total_items)
    next_offset = end_index if end_index < total_items else None

    return {
        "offset": safe_offset,
        "limit": safe_limit,
        "start_display": _format_number(safe_offset + 1) if total_items else "0",
        "end_display": _format_number(end_index),
        "total_display": _format_number(total_items),
        "next_offset": next_offset,
        "has_more": next_offset is not None,
    }


def _is_usable_ipv4(value):
    try:
        parsed_address = ipaddress.ip_address(value)
    except ValueError:
        return False

    return (
        parsed_address.version == 4
        and not parsed_address.is_loopback
        and not parsed_address.is_link_local
    )


def _get_primary_ip_address(host_name):
    candidates = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.settimeout(0.2)
            probe_socket.connect(("10.255.255.255", 1))
            candidates.append(probe_socket.getsockname()[0])
    except OSError:
        pass

    try:
        candidates.extend(socket.gethostbyname_ex(host_name)[2])
    except OSError:
        pass

    unique_candidates = []
    seen_candidates = set()

    for candidate in candidates:
        if candidate not in seen_candidates and _is_usable_ipv4(candidate):
            seen_candidates.add(candidate)
            unique_candidates.append(candidate)

    private_candidates = [
        candidate
        for candidate in unique_candidates
        if ipaddress.ip_address(candidate).is_private
    ]

    if private_candidates:
        return private_candidates[0]

    if unique_candidates:
        return unique_candidates[0]

    return "Unavailable"


def _format_mac_address(mac_value):
    return ":".join(
        f"{(mac_value >> shift) & 0xFF:02X}" for shift in range(40, -1, -8)
    )


def _get_system_info():
    global _SYSTEM_INFO_CACHE

    if _SYSTEM_INFO_CACHE:
        return _SYSTEM_INFO_CACHE

    host_name = socket.gethostname()

    _SYSTEM_INFO_CACHE = {
        "host_name": host_name,
        "ip_address": _get_primary_ip_address(host_name),
        "mac_address": _format_mac_address(uuid.getnode()),
        "os_label": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine() or "Unknown architecture",
    }
    return _SYSTEM_INFO_CACHE


def _get_storage_info(drive_root):
    cache_key = drive_root_to_value(drive_root)
    now = time.monotonic()
    cached_storage = _STORAGE_INFO_CACHE.get(cache_key)

    if cached_storage and now - cached_storage["cached_at"] < STORAGE_CACHE_SECONDS:
        return cached_storage["data"]

    try:
        usage = shutil.disk_usage(drive_root)
    except OSError:
        storage_info = {
            "available": False,
            "used_display": "Unavailable",
            "total_display": "Unavailable",
            "free_display": "Unavailable",
            "percent_used": 0,
        }
        _STORAGE_INFO_CACHE[cache_key] = {
            "cached_at": now,
            "data": storage_info,
        }
        return storage_info

    used_bytes = usage.total - usage.free
    percent_used = round((used_bytes / usage.total) * 100, 1) if usage.total else 0

    storage_info = {
        "available": True,
        "used_display": format_size(used_bytes),
        "total_display": format_size(usage.total),
        "free_display": format_size(usage.free),
        "percent_used": percent_used,
    }
    _STORAGE_INFO_CACHE[cache_key] = {
        "cached_at": now,
        "data": storage_info,
    }
    return storage_info


def _normalize_extension(file_information):
    extension = file_information.get("extension") or ""

    if extension == "No extension":
        return ""

    return extension.lower()


def _file_type_category_key(file_information):
    type_class = (file_information.get("type_class") or "").lower()
    mapped_type = SCANNER_CLASS_TO_DISTRIBUTION_KEY.get(type_class)

    if mapped_type:
        return mapped_type

    extension = _normalize_extension(file_information)

    for group in FILE_TYPE_GROUPS:
        if group["key"] == "others":
            continue

        if extension in group["extensions"]:
            return group["key"]

    return "others"


def _matches_file_type_filter(file_information, filter_type):
    if filter_type == "all":
        return True

    return _file_type_category_key(file_information) == filter_type


def _distribution_from_counts(total_files, category_counts, cache_key=None):
    items = []

    for group in FILE_TYPE_GROUPS:
        count = category_counts.get(group["key"], 0)

        if not count:
            continue

        percent = (count / total_files) * 100 if total_files else 0
        percent_display = f"{percent:.1f}".rstrip("0").rstrip(".")
        items.append(
            {
                "name": group["name"],
                "key": group["key"],
                "count": count,
                "count_display": _format_number(count),
                "percent": percent,
                "percent_display": percent_display,
                "color": group["color"],
            }
        )

    if not total_files:
        distribution = {
            "items": items,
            "gradient": "#e2e8f0 0deg 360deg",
        }
        if cache_key:
            _DISTRIBUTION_CACHE[cache_key] = distribution
        return distribution

    cursor = 0
    gradient_parts = []

    for item in items:
        if not item["count"]:
            continue

        end = cursor + ((item["count"] / total_files) * 360)
        gradient_parts.append(f"{item['color']} {cursor:.2f}deg {end:.2f}deg")
        cursor = end

    distribution = {
        "items": items,
        "gradient": ", ".join(gradient_parts) or "#e2e8f0 0deg 360deg",
    }

    if cache_key:
        _DISTRIBUTION_CACHE[cache_key] = distribution

        if len(_DISTRIBUTION_CACHE) > 24:
            oldest_key = next(iter(_DISTRIBUTION_CACHE))
            _DISTRIBUTION_CACHE.pop(oldest_key, None)

    return distribution


def _build_file_type_distribution(files, cache_key=None):
    if cache_key and cache_key in _DISTRIBUTION_CACHE:
        return _DISTRIBUTION_CACHE[cache_key]

    total_files = len(files)
    category_counts = Counter(_file_type_category_key(file_information) for file_information in files)
    return _distribution_from_counts(total_files, category_counts, cache_key)


def _build_recent_activity(all_snapshot, system_info, total_files, drive_label):
    if all_snapshot["last_scanned"]:
        activity_time = all_snapshot["last_scanned"].strftime("%I:%M %p")
    else:
        activity_time = "Pending"

    activity = [
        {
            "tone": "success",
            "title": "Sync Running" if all_snapshot["is_scanning"] else "Sync Complete",
            "detail": f"{_format_number(total_files)} files indexed from {drive_label}",
            "time": activity_time,
        },
        {
            "tone": "info",
            "title": "File Scan Started",
            "detail": f"Scanning {drive_label} for files...",
            "time": activity_time,
        },
        {
            "tone": "accent",
            "title": "Agent Connected",
            "detail": f"{system_info['host_name']} is authenticated",
            "time": activity_time if activity_time != "Pending" else "Now",
        },
    ]

    return activity


def _build_dashboard_summary(all_snapshot, current_snapshot, drive_root):
    all_files = list(all_snapshot["files"])
    current_files = list(current_snapshot["files"])
    total_files = len(all_files)
    current_file_count = len(current_files)
    system_info = _get_system_info()
    distribution_cache_key = (
        drive_root_to_value(drive_root),
        all_snapshot["version"],
    )
    type_distribution = _build_file_type_distribution(
        all_files,
        cache_key=distribution_cache_key,
    )
    selected_drive_context = drive_context(drive_root)

    return {
        **selected_drive_context,
        "data_source": "local",
        "selected_agent_id": "",
        "selected_agent_host": "",
        "total_files": total_files,
        "total_files_display": _format_number(total_files),
        "current_files_display": _format_number(current_file_count),
        "processed_files_display": _format_number(total_files),
        "failed_files_display": "0",
        "progress_percent": 100 if total_files else 0,
        "sync_status_title": "Sync Running" if all_snapshot["is_scanning"] else "Sync Complete",
        "system_info": system_info,
        "storage_info": _get_storage_info(drive_root),
        "distribution_items": type_distribution["items"],
        "distribution_gradient": type_distribution["gradient"],
        "recent_activity": _build_recent_activities(),
    }


def _drive_initial_from_label(label):
    normalized_label = str(label or "").strip()

    if normalized_label:
        return normalized_label[:1].upper()

    return "R"


def _drive_name_from_label(label):
    normalized_label = str(label or "").strip("\\/ ")

    if normalized_label.endswith(":"):
        return f"{normalized_label[:1].upper()} Drive"

    return normalized_label or "Remote Drive"


def _remote_file_to_dict(file_report):
    return {
        "name": file_report.name,
        "folder": file_report.folder,
        "relative_path": file_report.relative_path,
        "extension": file_report.extension,
        "type_badge": file_report.type_badge or "FILE",
        "type_class": file_report.type_class or "other",
        "type_label": file_report.type_label or file_report.extension or "File",
        "size": file_report.size,
        "size_bytes": file_report.size_bytes,
        "modified_timestamp": file_report.modified_timestamp,
        "freshness_timestamp": file_report.freshness_timestamp,
        "modified_display": file_report.modified_display,
        "downloadable": True,
        "remote_file": True,
    }


def _remote_file_matches_search(file_information, search_query):
    if not search_query:
        return True

    normalized_query = search_query.lower()
    haystack = " ".join(
        str(file_information.get(key) or "")
        for key in ("name", "folder", "relative_path", "extension", "type_label")
    ).lower()

    return normalized_query in haystack


def _remote_search_q(search_query):
    if not search_query:
        return Q()

    return (
        Q(name__icontains=search_query)
        | Q(folder__icontains=search_query)
        | Q(relative_path__icontains=search_query)
        | Q(extension__icontains=search_query)
        | Q(type_label__icontains=search_query)
    )


def _extension_query(extensions):
    if not extensions:
        return Q()

    normalized_exts = tuple(str(ext or "").strip().lower() for ext in extensions if ext)

    if not normalized_exts:
        return Q()

    return Q(extension__in=normalized_exts)


def _type_class_query(type_classes):
    if not type_classes:
        return Q()

    normalized_classes = tuple(str(tc or "").strip().lower() for tc in type_classes if tc)

    if not normalized_classes:
        return Q()

    return Q(type_class__in=normalized_classes)


def _remote_type_q(filter_type):
    if filter_type == "all":
        return Q()

    if filter_type not in FILE_TYPE_GROUP_BY_KEY:
        return Q()

    category_classes = {
        "archives": ("archive",),
        "documents": ("document", "pdf"),
        "images": ("image",),
        "spreadsheets": ("spreadsheet",),
        "videos": ("video",),
    }

    all_known_type_classes = ("archive", "document", "pdf", "image", "spreadsheet", "video")
    all_known_extensions = tuple(
        ext.lower()
        for group in FILE_TYPE_GROUPS
        for ext in group["extensions"]
        if ext
    )

    if filter_type == "others":
        return ~Q(type_class__in=all_known_type_classes) & ~Q(extension__in=all_known_extensions)

    group = FILE_TYPE_GROUP_BY_KEY[filter_type]
    query = _type_class_query(category_classes.get(filter_type, ()))

    if group["extensions"]:
        query |= _extension_query(group["extensions"])

    return query


def _remote_files_queryset(drive_report, search_query="", filter_type="all"):
    queryset = ActiveAgentFile.objects.filter(drive=drive_report)
    query = _remote_search_q(search_query) & _remote_type_q(filter_type)

    if query:
        queryset = queryset.filter(query)

    return queryset


def _build_remote_file_type_distribution(drive_report, cache_key=None):
    if cache_key and cache_key in _DISTRIBUTION_CACHE:
        return _DISTRIBUTION_CACHE[cache_key]

    if not drive_report:
        return _distribution_from_counts(0, Counter(), cache_key)

    category_counts = Counter()
    total_files = 0

    for row in (
        drive_report.file_reports.values("type_class", "extension")
        .annotate(file_count=Count("id"))
        .iterator()
    ):
        file_count = row["file_count"]
        total_files += file_count
        category_counts[
            _file_type_category_key(
                {
                    "type_class": row["type_class"],
                    "extension": row["extension"],
                }
            )
        ] += file_count

    return _distribution_from_counts(total_files, category_counts, cache_key)


def _agent_drive_options(agent, selected_drive_value):
    drive_reports = _ordered_agent_drive_reports(agent.drive_reports.all())
    options = [
        {
            "label": drive_report.label,
            "name": _drive_name_from_label(drive_report.label),
            "selected": drive_report.value == selected_drive_value,
            "value": drive_report.value,
        }
        for drive_report in drive_reports
    ]

    if options:
        return options

    return [
        {
            "label": "No drives reported",
            "name": "No drives reported",
            "selected": True,
            "value": "",
        }
    ]


def _timestamp_version(value):
    if not value:
        value = timezone.now()

    return int(value.timestamp() * 1000)


def _agent_drive_version(agent, drive_report):
    last_scan_source = (
        drive_report.last_reported_at
        if drive_report
        else agent.last_seen_at
    )

    return _timestamp_version(last_scan_source)


def _build_agent_dashboard_summary(agent, drive_report, all_files, current_files):
    drive_label = drive_report.label if drive_report else "No drive"
    total_files = drive_report.total_files if drive_report else 0
    indexed_files = len(all_files)
    current_file_count = len(current_files)
    storage_info = (
        _drive_report_payload(drive_report)["storage"]
        if drive_report
        else {
            "used_display": "Unavailable",
            "total_display": "Unavailable",
            "free_display": "Unavailable",
            "percent_used": 0,
        }
    )
    type_distribution = _build_file_type_distribution(
        all_files,
        cache_key=(
            "agent",
            agent.agent_id,
            drive_report.value if drive_report else "",
            _timestamp_version(drive_report.last_reported_at if drive_report else agent.last_seen_at),
            indexed_files,
        ),
    )
    progress_percent = 100 if drive_report and drive_report.count_complete else 0

    if drive_report and total_files:
        progress_percent = min(
            100,
            round((indexed_files / total_files) * 100, 1),
        )
    elif indexed_files:
        progress_percent = 100

    return {
        "app_name": f"{agent.host_name} DriveAgent",
        "dashboard_title": f"{agent.host_name} Dashboard",
        "drive_initial": _drive_initial_from_label(drive_label),
        "drive_label": drive_label,
        "drive_name": _drive_name_from_label(drive_label),
        "data_source": "agent",
        "selected_agent_id": agent.agent_id,
        "selected_agent_host": agent.host_name,
        "total_files": total_files,
        "total_files_display": _format_number(total_files),
        "current_files_display": _format_number(current_file_count),
        "processed_files_display": _format_number(indexed_files),
        "failed_files_display": "0",
        "progress_percent": progress_percent,
        "sync_status_title": (
            "Remote Sync Complete"
            if drive_report and drive_report.count_complete
            else "Remote Sync Running"
        ),
        "system_info": {
            "host_name": agent.host_name,
            "ip_address": agent.ip_address or "Unavailable",
            "mac_address": agent.mac_address or "Unavailable",
            "os_label": agent.os_label or "Unknown OS",
            "architecture": agent.architecture or "Unknown architecture",
        },
        "storage_info": storage_info,
        "distribution_items": type_distribution["items"],
        "distribution_gradient": type_distribution["gradient"],
        "recent_activity": [],
    }


def _build_agent_dashboard_summary_from_counts(
    agent,
    drive_report,
    indexed_files,
    current_file_count,
    type_distribution,
):
    drive_label = drive_report.label if drive_report else "No drive"
    total_files = (
        max(drive_report.total_files, drive_report.indexed_files, indexed_files)
        if drive_report
        else 0
    )
    storage_info = (
        _drive_report_payload(drive_report)["storage"]
        if drive_report
        else {
            "used_display": "Unavailable",
            "total_display": "Unavailable",
            "free_display": "Unavailable",
            "percent_used": 0,
        }
    )
    progress_percent = 100 if drive_report and drive_report.count_complete else 0

    if drive_report and total_files:
        progress_percent = min(
            100,
            round((indexed_files / total_files) * 100, 1),
        )
    elif indexed_files:
        progress_percent = 100

    return {
        "app_name": f"{agent.host_name} DriveAgent",
        "dashboard_title": f"{agent.host_name} Dashboard",
        "drive_initial": _drive_initial_from_label(drive_label),
        "drive_label": drive_label,
        "drive_name": _drive_name_from_label(drive_label),
        "data_source": "agent",
        "selected_agent_id": agent.agent_id,
        "selected_agent_host": agent.host_name,
        "total_files": total_files,
        "total_files_display": _format_number(total_files),
        "current_files_display": _format_number(current_file_count),
        "processed_files_display": _format_number(indexed_files),
        "failed_files_display": "0",
        "progress_percent": progress_percent,
        "sync_status_title": (
            "Remote Sync Complete"
            if drive_report and drive_report.count_complete
            else "Remote Sync Running"
        ),
        "system_info": {
            "host_name": agent.host_name,
            "ip_address": agent.ip_address or "Unavailable",
            "mac_address": agent.mac_address or "Unavailable",
            "os_label": agent.os_label or "Unknown OS",
            "architecture": agent.architecture or "Unknown architecture",
        },
        "storage_info": storage_info,
        "distribution_items": type_distribution["items"],
        "distribution_gradient": type_distribution["gradient"],
        "recent_activity": _build_recent_activities(drive_report, agent),
    }


def _dashboard_json_payload(context):
    return {
        "app_name": context["app_name"],
        "dashboard_title": context["dashboard_title"],
        "drive_initial": context["drive_initial"],
        "drive_label": context["drive_label"],
        "drive_name": context["drive_name"],
        "total_files": context["total_files"],
        "total_files_display": context["total_files_display"],
        "current_files_display": context["current_files_display"],
        "processed_files_display": context["processed_files_display"],
        "failed_files_display": context["failed_files_display"],
        "progress_percent": context["progress_percent"],
        "sync_status_title": context["sync_status_title"],
        "system_info": context["system_info"],
        "data_source": context.get("data_source", "local"),
        "selected_agent_id": context.get("selected_agent_id", ""),
        "selected_agent_host": context.get("selected_agent_host", ""),
        "storage_info": context["storage_info"],
        "distribution_items": context["distribution_items"],
        "distribution_gradient": context["distribution_gradient"],
        "recent_activity": context["recent_activity"],
        "last_scan_date_display": context["last_scan_date_display"],
        "last_scan_time_display": context["last_scan_time_display"],
    }


def _drive_options_json_payload(context):
    return [
        {
            "label": option["label"],
            "name": option["name"],
            "selected": option["selected"],
            "value": option["value"],
        }
        for option in context["drive_options"]
    ]


def _drive_files_json_response(context, request=None, extra_payload=None):
    rows_html = render_to_string(
        "drivefiles/_file_rows.html",
        context,
        request=request,
    )

    payload = {
        "rows_html": rows_html,
        "files_count": len(context["files"]),
        "error_message": context["error_message"],
        "maximum_reached": context["maximum_reached"],
        "is_scanning": context["is_scanning"],
        "last_scanned_display": context["last_scanned_display"],
        "next_scan_display": context["next_scan_display"],
        "version": context["version"],
        "unchanged": False,
        "selected_drive_value": context["selected_drive_value"],
        "selected_agent_id": context.get("selected_agent_id", ""),
        "selected_agent_host": context.get("selected_agent_host", ""),
        "drive_options": _drive_options_json_payload(context),
        "active_agents": context["active_agents"],
        "active_agents_total": context["active_agents_total"],
        "active_agents_online": context["active_agents_online"],
        "dashboard": _dashboard_json_payload(context),
        "pagination": {
            "page_number": context["page_number"],
            "total_pages": context["total_pages"],
            "page_start_display": context["page_start_display"],
            "page_end_display": context["page_end_display"],
            "total_matching_display": context["total_matching_display"],
            "pagination_pages": context["pagination_pages"],
            "has_previous_page": context["has_previous_page"],
            "has_next_page": context["has_next_page"],
        },
    }

    if extra_payload:
        payload.update(extra_payload)

    if context.get("scroll_info"):
        payload["scroll"] = context["scroll_info"]

    return JsonResponse(payload)


def _files_only_json_response(context, request=None, extra_payload=None):
    rows_html = render_to_string(
        "drivefiles/_file_rows.html",
        context,
        request=request,
    )

    payload = {
        "rows_html": rows_html,
        "files_count": len(context["files"]),
        "files_only": True,
        "error_message": context["error_message"],
        "maximum_reached": context["maximum_reached"],
        "is_scanning": context["is_scanning"],
        "last_scanned_display": context["last_scanned_display"],
        "next_scan_display": context["next_scan_display"],
        "sync_status_title": context["sync_status_title"],
        "current_files_display": context["current_files_display"],
        "version": context["version"],
        "unchanged": False,
        "selected_drive_value": context["selected_drive_value"],
        "selected_agent_id": context.get("selected_agent_id", ""),
        "selected_agent_host": context.get("selected_agent_host", ""),
        "filter_type": context["filter_type"],
        "pagination": {
            "page_number": context["page_number"],
            "total_pages": context["total_pages"],
            "page_start_display": context["page_start_display"],
            "page_end_display": context["page_end_display"],
            "total_matching_display": context["total_matching_display"],
            "pagination_pages": context["pagination_pages"],
            "has_previous_page": context["has_previous_page"],
            "has_next_page": context["has_next_page"],
        },
    }

    if extra_payload:
        payload.update(extra_payload)

    if context.get("scroll_info"):
        payload["scroll"] = context["scroll_info"]

    return JsonResponse(payload)


def _local_drive_scanner_enabled():
    return bool(getattr(settings, "LOCAL_DRIVE_SCANNER_ENABLED", True))


def _build_pagination_context(total_items, page_number):
    total_pages = max(1, math.ceil(total_items / TABLE_PAGE_SIZE))
    page_number = min(max(page_number, 1), total_pages)
    start_index = (page_number - 1) * TABLE_PAGE_SIZE
    end_index = min(start_index + TABLE_PAGE_SIZE, total_items)
    pages = [page for page in range(1, min(total_pages, 3) + 1)]

    if total_pages > 4:
        pages.append("ellipsis")
        pages.append(total_pages)
    elif total_pages > 3:
        pages.append(total_pages)

    return {
        "page_number": page_number,
        "total_pages": total_pages,
        "page_start": start_index + 1 if total_items else 0,
        "page_end": end_index,
        "total_matching": total_items,
        "page_start_display": _format_number(start_index + 1) if total_items else "0",
        "page_end_display": _format_number(end_index),
        "total_matching_display": _format_number(total_items),
        "pagination_pages": pages,
        "has_previous_page": page_number > 1,
        "has_next_page": page_number < total_pages,
    }


def _filter_files(files, filter_type):
    if filter_type != "all" and filter_type not in FILE_TYPE_GROUP_BY_KEY:
        filter_type = "all"

    return [
        file_information
        for file_information in files
        if _matches_file_type_filter(file_information, filter_type)
    ], filter_type


def _build_hosted_waiting_context(request, selected_drive_root):
    search_query = request.GET.get("search", "").strip()
    filter_type = request.GET.get("type", "all").strip().lower()
    page_number = _parse_positive_int(request.GET.get("page"), default=1)
    _empty_files, filter_type = _filter_files([], filter_type)
    pagination = _build_pagination_context(0, page_number)
    type_distribution = _build_file_type_distribution(
        [],
        cache_key=("hosted-waiting",),
    )
    active_agents_payload = _active_agents_json_payload("")

    return {
        **drive_context(selected_drive_root),
        **_format_datetime_parts(None),
        **pagination,
        "data_source": "hosted",
        "selected_agent_id": "",
        "selected_agent_host": "",
        "files": [],
        "all_files": [],
        "content_signature": (),
        "error_message": "Select an Active User to view files from a PC running DriveAgent.exe.",
        "filter_options": FILTER_OPTIONS,
        "filter_type": filter_type,
        "is_scanning": False,
        "last_scanned": None,
        "last_scanned_display": "Waiting for Active User",
        "maximum_reached": False,
        "next_scan_at": None,
        "next_scan_display": "Waiting for Active User",
        "search_query": search_query,
        "selected_drive_value": drive_root_to_value(selected_drive_root),
        "drive_options": get_drive_options(selected_drive_root),
        "refresh_interval_ms": FRONTEND_REFRESH_SECONDS * 1000,
        "active_agents_refresh_interval_ms": ACTIVE_AGENTS_REFRESH_SECONDS * 1000,
        "active_agents": active_agents_payload["agents"],
        "active_agents_total": active_agents_payload["total_agents"],
        "active_agents_online": active_agents_payload["online_agents"],
        "version": 0,
        "total_files": 0,
        "total_files_display": "0",
        "current_files_display": "0",
        "processed_files_display": "0",
        "failed_files_display": "0",
        "progress_percent": 0,
        "sync_status_title": "Waiting for Active User",
        "system_info": {
            "host_name": "Select Active User",
            "ip_address": "Unavailable",
            "mac_address": "Unavailable",
            "os_label": "Waiting for DriveAgent.exe",
            "architecture": "Select a hostname below",
        },
        "storage_info": {
            "available": False,
            "used_display": "Unavailable",
            "total_display": "Unavailable",
            "free_display": "Unavailable",
            "percent_used": 0,
        },
        "distribution_items": type_distribution["items"],
        "distribution_gradient": type_distribution["gradient"],
        "recent_activity": [],
    }


def _format_last_scan_parts(snapshot):
    if not snapshot["last_scanned"]:
        return {
            "last_scan_date_display": "Pending",
            "last_scan_time_display": "Starting soon",
        }

    return {
        "last_scan_date_display": snapshot["last_scanned"].strftime("%b %d, %Y"),
        "last_scan_time_display": snapshot["last_scanned"].strftime("%I:%M %p"),
    }


def _format_datetime_parts(value):
    if not value:
        return {
            "last_scan_date_display": "Pending",
            "last_scan_time_display": "Waiting for agent",
        }

    return {
        "last_scan_date_display": value.strftime("%b %d, %Y"),
        "last_scan_time_display": value.strftime("%I:%M %p"),
    }


def _build_agent_files_context(
    request,
    selected_agent,
    requested_drive_value="",
    persist_drive_selection=True,
):
    selected_drive_value = _get_selected_agent_drive_value(
        request,
        selected_agent,
        requested_drive_value=requested_drive_value,
        persist_session=persist_drive_selection,
    )
    selected_drive = (
        selected_agent.drive_reports.filter(value=selected_drive_value).first()
        if selected_drive_value
        else None
    )
    search_query = request.GET.get("search", "").strip()
    filter_type = request.GET.get("type", "all").strip().lower()
    page_number = _parse_positive_int(request.GET.get("page"), default=1)

    indexed_files = selected_drive.indexed_files if selected_drive else 0

    if filter_type != "all" and filter_type not in FILE_TYPE_GROUP_BY_KEY:
        filter_type = "all"

    filtered_queryset = (
        _remote_files_queryset(selected_drive, search_query, filter_type)
        if selected_drive
        else ActiveAgentFile.objects.none()
    )
    last_scan_source = (
        selected_drive.last_reported_at
        if selected_drive
        else selected_agent.last_seen_at
    )
    active_agents_payload = _active_agents_json_payload(selected_agent.agent_id)
    type_distribution = _build_remote_file_type_distribution(
        selected_drive,
        cache_key=(
            "agent-db",
            selected_agent.agent_id,
            selected_drive.value if selected_drive else "",
            _timestamp_version(last_scan_source),
            indexed_files,
        ),
    )

    if selected_drive and not search_query:
        if filter_type == "all":
            filtered_count = indexed_files
        else:
            matching_item = next(
                (item for item in type_distribution["items"] if item["key"] == filter_type),
                None,
            )
            filtered_count = matching_item["count"] if matching_item else 0
    else:
        filtered_count = filtered_queryset.count()

    pagination = _build_pagination_context(filtered_count, page_number)
    page_start_index = (pagination["page_number"] - 1) * TABLE_PAGE_SIZE
    paged_files = [
        _remote_file_to_dict(file_report)
        for file_report in filtered_queryset.order_by(
            "-freshness_timestamp",
            "name",
        ).only(
            "name",
            "folder",
            "relative_path",
            "extension",
            "type_badge",
            "type_class",
            "type_label",
            "size",
            "size_bytes",
            "modified_timestamp",
            "freshness_timestamp",
            "modified_display",
        )[page_start_index:pagination["page_end"]]
    ]
    dashboard_summary = _build_agent_dashboard_summary_from_counts(
        selected_agent,
        selected_drive,
        indexed_files,
        filtered_count,
        type_distribution,
    )

    return {
        **dashboard_summary,
        **_format_datetime_parts(last_scan_source),
        **pagination,
        "files": paged_files,
        "all_files": [],
        "content_signature": (),
        "error_message": None,
        "filter_options": FILTER_OPTIONS,
        "filter_type": filter_type,
        "is_scanning": selected_drive is not None and not selected_drive.count_complete,
        "last_scanned": last_scan_source,
        "last_scanned_display": _format_relative_time(last_scan_source),
        "maximum_reached": False,
        "next_scan_at": None,
        "next_scan_display": "Waiting for agent heartbeat",
        "search_query": search_query,
        "selected_drive_value": selected_drive_value,
        "drive_options": _agent_drive_options(selected_agent, selected_drive_value),
        "refresh_interval_ms": FRONTEND_REFRESH_SECONDS * 1000,
        "active_agents_refresh_interval_ms": ACTIVE_AGENTS_REFRESH_SECONDS * 1000,
        "active_agents": active_agents_payload["agents"],
        "active_agents_total": active_agents_payload["total_agents"],
        "active_agents_online": active_agents_payload["online_agents"],
        "version": _agent_drive_version(selected_agent, selected_drive),
    }


def _build_agent_files_only_context(
    request,
    selected_agent,
    requested_drive_value="",
    persist_drive_selection=True,
):
    selected_drive_value = _get_selected_agent_drive_value(
        request,
        selected_agent,
        requested_drive_value=requested_drive_value,
        persist_session=persist_drive_selection,
    )
    selected_drive = (
        selected_agent.drive_reports.filter(value=selected_drive_value).first()
        if selected_drive_value
        else None
    )
    search_query = request.GET.get("search", "").strip()
    filter_type = request.GET.get("type", "all").strip().lower()
    page_number = _parse_positive_int(request.GET.get("page"), default=1)

    if filter_type != "all" and filter_type not in FILE_TYPE_GROUP_BY_KEY:
        filter_type = "all"

    filtered_queryset = (
        _remote_files_queryset(selected_drive, search_query, filter_type)
        if selected_drive
        else ActiveAgentFile.objects.none()
    )
    indexed_files = selected_drive.indexed_files if selected_drive else 0
    last_scan_source = (
        selected_drive.last_reported_at
        if selected_drive
        else selected_agent.last_seen_at
    )

    if selected_drive and not search_query:
        if filter_type == "all":
            filtered_count = indexed_files
        else:
            type_distribution = _build_remote_file_type_distribution(
                selected_drive,
                cache_key=(
                    "agent-db",
                    selected_agent.agent_id,
                    selected_drive.value if selected_drive else "",
                    _timestamp_version(last_scan_source),
                    indexed_files,
                ),
            )
            matching_item = next(
                (item for item in type_distribution["items"] if item["key"] == filter_type),
                None,
            )
            filtered_count = matching_item["count"] if matching_item else 0
    else:
        filtered_count = filtered_queryset.count()

    pagination = _build_pagination_context(filtered_count, page_number)
    scroll_window = _requested_scroll_window(request)

    if scroll_window:
        scroll_info = _build_scroll_info(
            filtered_count,
            scroll_window["offset"],
            scroll_window["limit"],
        )
        page_start_index = scroll_info["offset"]
        page_end_index = page_start_index + scroll_info["limit"]
    else:
        scroll_info = None
        page_start_index = (pagination["page_number"] - 1) * TABLE_PAGE_SIZE
        page_end_index = pagination["page_end"]

    paged_files = [
        _remote_file_to_dict(file_report)
        for file_report in filtered_queryset.order_by(
            "-freshness_timestamp",
            "name",
        ).only(
            "name",
            "folder",
            "relative_path",
            "extension",
            "type_badge",
            "type_class",
            "type_label",
            "size",
            "size_bytes",
            "modified_timestamp",
            "freshness_timestamp",
            "modified_display",
        )[page_start_index:page_end_index]
    ]
    last_scan_source = (
        selected_drive.last_reported_at
        if selected_drive
        else selected_agent.last_seen_at
    )

    return {
        **pagination,
        "files": paged_files,
        "error_message": None,
        "filter_type": filter_type,
        "is_scanning": selected_drive is not None and not selected_drive.count_complete,
        "last_scanned_display": _format_relative_time(last_scan_source),
        "maximum_reached": False,
        "next_scan_display": "Waiting for agent heartbeat",
        "search_query": search_query,
        "selected_drive_value": selected_drive_value,
        "selected_agent_id": selected_agent.agent_id,
        "selected_agent_host": selected_agent.host_name,
        "scroll_info": scroll_info,
        "current_files_display": _format_number(filtered_count),
        "sync_status_title": (
            "Remote Sync Complete"
            if selected_drive and selected_drive.count_complete
            else "Remote Sync Running"
        ),
        "version": _agent_drive_version(selected_agent, selected_drive),
        "recent_activity": _build_recent_activities(drive_report=selected_drive, agent=selected_agent),
    }


def _build_drive_files_context(request, selected_drive_root=None, scanner_ready=False):
    selected_agent = _get_selected_agent(request, require_online=False)

    if selected_agent:
        return _build_agent_files_context(request, selected_agent)

    if selected_drive_root is None:
        selected_drive_root = _get_selected_drive_root(request)

    if not _local_drive_scanner_enabled():
        return _build_hosted_waiting_context(request, selected_drive_root)

    if not scanner_ready:
        set_drive_root(selected_drive_root)
        start_background_scanner()

    search_query = request.GET.get("search", "").strip()
    filter_type = request.GET.get("type", "all").strip().lower()
    page_number = _parse_positive_int(request.GET.get("page"), default=1)
    all_snapshot = get_file_snapshot("")
    if not all_snapshot.get("files"):
        scan_drive()
        all_snapshot = get_file_snapshot("")

    snapshot = get_file_snapshot(search_query)
    filtered_files, filter_type = _filter_files(snapshot["files"], filter_type)
    pagination = _build_pagination_context(len(filtered_files), page_number)
    page_start_index = (pagination["page_number"] - 1) * TABLE_PAGE_SIZE
    paged_files = filtered_files[page_start_index:pagination["page_end"]]
    current_snapshot_for_summary = {
        **snapshot,
        "files": filtered_files,
    }
    active_agents_payload = _active_agents_json_payload("")

    return {
        **snapshot,
        **_build_dashboard_summary(
            all_snapshot,
            current_snapshot_for_summary,
            selected_drive_root,
        ),
        **_format_last_scan_parts(all_snapshot),
        **pagination,
        "drive_options": get_drive_options(selected_drive_root),
        "files": paged_files,
        "filter_options": FILTER_OPTIONS,
        "filter_type": filter_type,
        "search_query": search_query,
        "selected_drive_value": drive_root_to_value(selected_drive_root),
        "refresh_interval_ms": FRONTEND_REFRESH_SECONDS * 1000,
        "active_agents_refresh_interval_ms": ACTIVE_AGENTS_REFRESH_SECONDS * 1000,
        "active_agents": active_agents_payload["agents"],
        "active_agents_total": active_agents_payload["total_agents"],
        "active_agents_online": active_agents_payload["online_agents"],
    }


def _build_local_files_only_context(request, selected_drive_root):
    search_query = request.GET.get("search", "").strip()
    filter_type = request.GET.get("type", "all").strip().lower()
    page_number = _parse_positive_int(request.GET.get("page"), default=1)
    snapshot = get_file_snapshot(search_query)
    filtered_files, filter_type = _filter_files(snapshot["files"], filter_type)
    pagination = _build_pagination_context(len(filtered_files), page_number)
    scroll_window = _requested_scroll_window(request)

    if scroll_window:
        scroll_info = _build_scroll_info(
            len(filtered_files),
            scroll_window["offset"],
            scroll_window["limit"],
        )
        page_start_index = scroll_info["offset"]
        page_end_index = page_start_index + scroll_info["limit"]
    else:
        scroll_info = None
        page_start_index = (pagination["page_number"] - 1) * TABLE_PAGE_SIZE
        page_end_index = pagination["page_end"]

    paged_files = filtered_files[page_start_index:page_end_index]

    return {
        **snapshot,
        **pagination,
        "files": paged_files,
        "filter_type": filter_type,
        "search_query": search_query,
        "selected_drive_value": drive_root_to_value(selected_drive_root),
        "scroll_info": scroll_info,
        "current_files_display": _format_number(len(filtered_files)),
        "sync_status_title": "Sync Running" if snapshot["is_scanning"] else "Sync Complete",
    }


def _version_shortcut_allowed(request):
    return bool(request.GET.get("version")) and request.GET.get("unchanged_ok") == "1"


def _files_only_requested(request):
    return request.GET.get("scope") == "files"


@login_required
def drive_files_data(request):
    requested_agent_id = _safe_text(request.GET.get("agent_id"), 128).strip()
    requested_drive_value = _safe_text(request.GET.get("drive_root"), 64).strip()
    allow_live_auto_select = bool(
        not requested_agent_id and not _local_drive_scanner_enabled()
    )
    selected_agent = _get_selected_agent(
        request,
        requested_agent_id=requested_agent_id,
        require_online=False,
        persist_session=allow_live_auto_select,
        allow_auto_select=allow_live_auto_select,
    )
    files_only = _files_only_requested(request)

    if selected_agent:
        selected_drive_value = _get_selected_agent_drive_value(
            request,
            selected_agent,
            requested_drive_value=requested_drive_value,
            persist_session=False,
        )
        search_query = request.GET.get("search", "").strip()

        if search_query and selected_drive_value:
            _queue_agent_drive_scan(selected_agent, selected_drive_value)

        selected_drive = (
            selected_agent.drive_reports.filter(value=selected_drive_value).first()
            if selected_drive_value
            else None
        )
        remote_version = _agent_drive_version(selected_agent, selected_drive)
        client_version = (
            request.GET.get("version")
            if _version_shortcut_allowed(request)
            else None
        )

        if client_version and client_version == str(remote_version):
            return JsonResponse(
                {
                    "unchanged": True,
                    "version": remote_version,
                    "error_message": None,
                    "is_scanning": selected_drive is not None and not selected_drive.count_complete,
                    "sync_status_title": (
                        "Remote Sync Complete"
                        if selected_drive and selected_drive.count_complete
                        else "Remote Sync Running"
                    ),
                }
            )

        if files_only:
            context = _build_agent_files_only_context(
                request,
                selected_agent,
                requested_drive_value=requested_drive_value,
                persist_drive_selection=False,
            )

            return _files_only_json_response(context, request=request)

        context = _build_agent_files_context(
            request,
            selected_agent,
            requested_drive_value=requested_drive_value,
            persist_drive_selection=False,
        )

        return _drive_files_json_response(context, request=request)

    selected_drive_root = _get_selected_drive_root(request)

    if not _local_drive_scanner_enabled():
        context = _build_hosted_waiting_context(request, selected_drive_root)
        return _drive_files_json_response(context, request=request)

    set_drive_root(selected_drive_root)
    start_background_scanner()
    scan_metadata = get_scan_metadata()
    client_version = (
        request.GET.get("version")
        if _version_shortcut_allowed(request)
        else None
    )

    if client_version and client_version == str(scan_metadata["version"]):
        return JsonResponse(
            {
                "unchanged": True,
                "version": scan_metadata["version"],
                "error_message": scan_metadata["error_message"],
                "is_scanning": scan_metadata["is_scanning"],
                "sync_status_title": "Sync Running" if scan_metadata["is_scanning"] else "Sync Complete",
            }
        )

    if files_only:
        context = _build_local_files_only_context(request, selected_drive_root)

        return _files_only_json_response(context, request=request)

    context = _build_drive_files_context(
        request,
        selected_drive_root=selected_drive_root,
        scanner_ready=True,
    )

    return _drive_files_json_response(context, request=request)


@login_required
def active_agents_data(request):
    selected_agent_id = _safe_text(request.GET.get("agent_id"), 128).strip()

    if not selected_agent_id:
        selected_agent_id = _validated_selected_agent_id(request)

    return JsonResponse(_active_agents_json_payload(selected_agent_id))


def agent_ping(request):
    return JsonResponse(
        {
            "ok": True,
            "app": "drive-agent-dashboard",
            "heartbeat_path": "/agent-heartbeat/",
        }
    )


@csrf_exempt
@require_POST
def agent_heartbeat(request):
    if not _is_agent_request_authorized(request):
        return JsonResponse(
            {
                "ok": False,
                "error": "Unauthorized agent token.",
            },
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid JSON payload.",
            },
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "ok": False,
                "error": "Payload must be a JSON object.",
            },
            status=400,
        )

    agent_id = str(payload.get("agent_id") or "").strip()[:128]

    if not agent_id:
        return JsonResponse(
            {
                "ok": False,
                "error": "agent_id is required.",
            },
            status=400,
        )

    drives = _normalize_agent_drives(payload.get("drives"))
    available_drive_values = [drive["value"] for drive in drives if drive["value"]]
    existing_agent = ActiveAgent.objects.filter(agent_id=agent_id).first()
    existing_payload = (
        existing_agent.latest_payload
        if existing_agent and isinstance(existing_agent.latest_payload, dict)
        else {}
    )
    requested_drive_values = _normalize_requested_drive_values(
        existing_payload.get("requested_drive_values"),
        available_drive_values,
    )
    requested_file_downloads = _normalize_requested_file_downloads(
        existing_payload.get("requested_file_downloads"),
        available_drive_values,
    )
    total_files = sum(drive["total_files"] for drive in drives)
    host_name = str(payload.get("host_name") or agent_id)[:255]
    ip_address = str(
        payload.get("ip_address")
        or request.META.get("REMOTE_ADDR")
        or ""
    )[:64]
    mac_address = str(payload.get("mac_address") or "")[:64]
    os_label = str(payload.get("os_label") or "")[:128]
    architecture = str(payload.get("architecture") or "")[:64]
    _remove_duplicate_agent_reports(agent_id, host_name, mac_address, ip_address)
    latest_payload = {
        "drives": drives,
        "reported_at": timezone.now().isoformat(),
        "requested_drive_values": [],
        "requested_file_downloads": [],
    }

    agent, _created = ActiveAgent.objects.update_or_create(
        agent_id=agent_id,
        defaults={
            "host_name": host_name,
            "ip_address": ip_address,
            "mac_address": mac_address,
            "os_label": os_label,
            "architecture": architecture,
            "drive_count": len(drives),
            "total_files": total_files,
            "latest_payload": latest_payload,
        },
    )
    reported_drive_values = []

    for drive in drives:
        if not drive["value"]:
            continue

        reported_drive_values.append(drive["value"])
        drive_report, _drive_created = ActiveAgentDrive.objects.get_or_create(
            agent=agent,
            value=drive["value"],
            defaults={
                "label": drive["label"],
            },
        )
        incoming_total_files = drive["total_files"]
        incoming_indexed_files = drive["indexed_files"]
        incoming_file_count = max(incoming_total_files, incoming_indexed_files)
        stable_file_count = _stable_drive_file_count(drive_report)

        if drive["count_complete"]:
            if incoming_file_count >= stable_file_count:
                total_files_value = incoming_file_count
                indexed_files_value = incoming_file_count
                count_complete_value = True
            elif drive_report.file_reports.count() == incoming_file_count:
                total_files_value = incoming_file_count
                indexed_files_value = incoming_file_count
                count_complete_value = True
            else:
                total_files_value = stable_file_count
                indexed_files_value = stable_file_count
                count_complete_value = False
        else:
            total_files_value = max(
                stable_file_count,
                incoming_file_count,
            )
            indexed_files_value = total_files_value
            count_complete_value = False

        drive_report.label = drive["label"]
        drive_report.total_files = total_files_value
        drive_report.indexed_files = indexed_files_value
        drive_report.count_complete = count_complete_value
        drive_report.storage = drive["storage"]
        drive_report.save(
            update_fields=(
                "label",
                "total_files",
                "indexed_files",
                "count_complete",
                "storage",
                "last_reported_at",
            )
        )

    if reported_drive_values:
        agent.drive_reports.exclude(value__in=reported_drive_values).delete()

    agent.total_files = sum(
        max(total_files, indexed_files)
        for total_files, indexed_files in agent.drive_reports.values_list(
            "total_files",
            "indexed_files",
        )
    )
    agent.drive_count = agent.drive_reports.count()
    agent.save(update_fields=("total_files", "drive_count", "last_seen_at"))

    return JsonResponse(
        {
            "ok": True,
            "agent_id": agent_id,
            "server_time": timezone.now().isoformat(),
            "requested_drive_values": requested_drive_values,
            "requested_file_downloads": requested_file_downloads,
        }
    )


@csrf_exempt
@require_POST
def agent_files_batch(request):
    if not _is_agent_request_authorized(request):
        return JsonResponse(
            {
                "ok": False,
                "error": "Unauthorized agent token.",
            },
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid JSON payload.",
            },
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "ok": False,
                "error": "Payload must be a JSON object.",
            },
            status=400,
        )

    agent_id = _safe_text(payload.get("agent_id"), 128).strip()
    drive_value = _safe_text(payload.get("drive_value"), 64).strip()

    if not agent_id or not drive_value:
        return JsonResponse(
            {
                "ok": False,
                "error": "agent_id and drive_value are required.",
            },
            status=400,
        )

    host_name = _safe_text(payload.get("host_name") or agent_id, 255)
    ip_address = _safe_text(
        payload.get("ip_address") or request.META.get("REMOTE_ADDR", ""),
        64,
    )
    mac_address = _safe_text(payload.get("mac_address"), 64)
    os_label = _safe_text(payload.get("os_label"), 128)
    architecture = _safe_text(payload.get("architecture"), 64)
    _remove_duplicate_agent_reports(agent_id, host_name, mac_address, ip_address)

    agent, _created = ActiveAgent.objects.get_or_create(
        agent_id=agent_id,
        defaults={
            "host_name": host_name,
            "ip_address": ip_address,
        },
    )
    agent.host_name = host_name or agent.host_name or agent_id
    agent.ip_address = ip_address or agent.ip_address
    agent.mac_address = mac_address or agent.mac_address
    agent.os_label = os_label or agent.os_label
    agent.architecture = architecture or agent.architecture
    storage = payload.get("storage")

    if not isinstance(storage, dict):
        storage = {}

    scan_id = _safe_text(payload.get("scan_id"), 96)
    scan_complete = bool(payload.get("scan_complete"))
    incoming_total_files = _safe_int(payload.get("total_files"))
    incoming_indexed_files = _safe_int(payload.get("indexed_files"))
    storage_payload = {
        "used_display": _safe_text(storage.get("used_display") or "Unavailable", 32),
        "total_display": _safe_text(storage.get("total_display") or "Unavailable", 32),
        "free_display": _safe_text(storage.get("free_display") or "Unavailable", 32),
        "percent_used": _safe_percent(storage.get("percent_used")),
    }
    drive_report, _created = ActiveAgentDrive.objects.get_or_create(
        agent=agent,
        value=drive_value,
        defaults={
            "label": _safe_text(payload.get("drive_label") or drive_value, 32),
        },
    )
    drive_report.label = _safe_text(payload.get("drive_label") or drive_value, 32)
    drive_report.total_files = max(
        drive_report.total_files,
        drive_report.indexed_files,
        incoming_total_files,
        incoming_indexed_files,
    )
    drive_report.indexed_files = max(
        drive_report.indexed_files,
        incoming_indexed_files,
    )
    drive_report.count_complete = False
    drive_report.storage = storage_payload
    drive_report.scan_id = scan_id
    drive_report.save(
        update_fields=(
            "label",
            "total_files",
            "indexed_files",
            "count_complete",
            "storage",
            "scan_id",
            "last_reported_at",
        )
    )
    batch_index = _safe_int(payload.get("batch_index"))

    raw_files = payload.get("files")

    if not isinstance(raw_files, list):
        raw_files = []

    batch_limit = getattr(settings, "AGENT_FILE_BATCH_SIZE", 1000)
    normalized_files = [
        normalized_file
        for normalized_file in (
            _normalize_agent_file(raw_file)
            for raw_file in raw_files[:batch_limit]
        )
        if normalized_file
    ]
    relative_paths = [file_information["relative_path"] for file_information in normalized_files]

    if relative_paths:
        ActiveAgentFile.objects.filter(
            drive=drive_report,
            relative_path__in=relative_paths,
        ).delete()

    ActiveAgentFile.objects.bulk_create(
        [
            ActiveAgentFile(
                agent=agent,
                drive=drive_report,
                reported_scan_id=scan_id,
                **file_information,
            )
            for file_information in normalized_files
        ],
        batch_size=batch_limit,
    )

    current_file_count = drive_report.file_reports.count()

    if scan_complete:
        completed_scan_file_count = max(incoming_total_files, incoming_indexed_files)
        received_scan_file_count = (
            drive_report.file_reports.filter(reported_scan_id=scan_id).count()
            if scan_id
            else current_file_count
        )
        scan_rows_are_complete = received_scan_file_count >= completed_scan_file_count

        if scan_rows_are_complete and scan_id:
            drive_report.file_reports.exclude(reported_scan_id=scan_id).delete()

        current_file_count = drive_report.file_reports.count()

        if scan_rows_are_complete:
            drive_report.total_files = current_file_count
            drive_report.indexed_files = current_file_count
            drive_report.count_complete = True
        else:
            protected_file_count = max(
                _stable_drive_file_count(drive_report),
                completed_scan_file_count,
                current_file_count,
            )
            drive_report.total_files = protected_file_count
            drive_report.indexed_files = protected_file_count
            drive_report.count_complete = False

        drive_report.save(
            update_fields=(
                "total_files",
                "indexed_files",
                "count_complete",
                "scan_id",
                "last_reported_at",
            )
        )
    elif current_file_count > drive_report.indexed_files:
        drive_report.total_files = max(drive_report.total_files, current_file_count)
        drive_report.indexed_files = current_file_count
        drive_report.save(
            update_fields=(
                "total_files",
                "indexed_files",
                "last_reported_at",
            )
        )

    agent.total_files = sum(
        agent.drive_reports.values_list("total_files", flat=True)
    )
    agent.drive_count = agent.drive_reports.count()
    agent.save(
        update_fields=(
            "host_name",
            "ip_address",
            "mac_address",
            "os_label",
            "architecture",
            "total_files",
            "drive_count",
            "last_seen_at",
        )
    )

    return JsonResponse(
        {
            "ok": True,
            "agent_id": agent.agent_id,
            "drive_value": drive_report.value,
            "accepted_files": len(normalized_files),
        }
    )


@csrf_exempt
@require_POST
def agent_file_events(request):
    if not _is_agent_request_authorized(request):
        return JsonResponse(
            {
                "ok": False,
                "error": "Unauthorized agent token.",
            },
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid JSON payload.",
            },
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "ok": False,
                "error": "Payload must be a JSON object.",
            },
            status=400,
        )

    agent_id = _safe_text(payload.get("agent_id"), 128).strip()
    drive_value = _safe_text(payload.get("drive_value"), 64).strip()

    if not agent_id or not drive_value:
        return JsonResponse(
            {
                "ok": False,
                "error": "agent_id and drive_value are required.",
            },
            status=400,
        )

    host_name = _safe_text(payload.get("host_name") or agent_id, 255)
    ip_address = _safe_text(
        payload.get("ip_address") or request.META.get("REMOTE_ADDR", ""),
        64,
    )
    mac_address = _safe_text(payload.get("mac_address"), 64)
    os_label = _safe_text(payload.get("os_label"), 128)
    architecture = _safe_text(payload.get("architecture"), 64)
    _remove_duplicate_agent_reports(agent_id, host_name, mac_address, ip_address)

    agent, _created = ActiveAgent.objects.get_or_create(
        agent_id=agent_id,
        defaults={
            "host_name": host_name,
            "ip_address": ip_address,
        },
    )
    agent.host_name = host_name or agent.host_name or agent_id
    agent.ip_address = ip_address or agent.ip_address
    agent.mac_address = mac_address or agent.mac_address
    agent.os_label = os_label or agent.os_label
    agent.architecture = architecture or agent.architecture

    storage = payload.get("storage")

    if not isinstance(storage, dict):
        storage = {}

    storage_payload = {
        "used_display": _safe_text(storage.get("used_display") or "Unavailable", 32),
        "total_display": _safe_text(storage.get("total_display") or "Unavailable", 32),
        "free_display": _safe_text(storage.get("free_display") or "Unavailable", 32),
        "percent_used": _safe_percent(storage.get("percent_used")),
    }
    drive_report, _created = ActiveAgentDrive.objects.get_or_create(
        agent=agent,
        value=drive_value,
        defaults={
            "label": _safe_text(payload.get("drive_label") or drive_value, 32),
        },
    )
    drive_report.label = _safe_text(payload.get("drive_label") or drive_value, 32)
    drive_report.storage = storage_payload

    batch_limit = getattr(settings, "AGENT_FILE_BATCH_SIZE", 1000)
    raw_upsert_files = payload.get("upsert_files")

    if not isinstance(raw_upsert_files, list):
        raw_upsert_files = []

    normalized_files_by_path = {}

    for raw_file in raw_upsert_files[:batch_limit]:
        normalized_file = _normalize_agent_file(raw_file)

        if normalized_file:
            normalized_files_by_path[normalized_file["relative_path"]] = normalized_file

    normalized_files = list(normalized_files_by_path.values())
    relative_paths = list(normalized_files_by_path.keys())
    deleted_paths = _normalize_deleted_relative_paths(payload.get("deleted_paths"))

    existing_paths = set(
        ActiveAgentFile.objects.filter(
            drive=drive_report,
            relative_path__in=relative_paths,
        ).values_list("relative_path", flat=True)
    )

    matched_deletions = set()
    matched_upserts = set()

    if deleted_paths and normalized_files:
        for del_path in deleted_paths:
            del_name = del_path.replace("\\", "/").rsplit("/", 1)[-1]
            del_dir = del_path.replace("\\", "/").rsplit("/", 1)[0] if "/" in del_path.replace("\\", "/") else ""
            for norm_file in normalized_files:
                rel_p = norm_file["relative_path"]
                if rel_p in matched_upserts:
                    continue
                norm_name = norm_file["name"]
                norm_dir = rel_p.replace("\\", "/").rsplit("/", 1)[0] if "/" in rel_p.replace("\\", "/") else ""
                if del_dir == norm_dir and del_name != norm_name:
                    _log_drive_activity(
                        agent,
                        drive_report,
                        DriveActivityLog.TYPE_RENAMED,
                        norm_name,
                        old_name=del_name,
                        details=f"Renamed from {del_name} in {norm_file.get('folder') or 'Root'}",
                    )
                    matched_deletions.add(del_path)
                    matched_upserts.add(rel_p)
                    break

    if deleted_paths:
        unmatched_deleted = [p for p in deleted_paths if p not in matched_deletions]
        for rel_path in unmatched_deleted[:10]:
            file_name = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
            _log_drive_activity(
                agent,
                drive_report,
                DriveActivityLog.TYPE_DELETED,
                file_name,
                details=f"File deleted from drive {drive_report.label}",
            )
        ActiveAgentFile.objects.filter(
            drive=drive_report,
            relative_path__in=deleted_paths,
        ).delete()

    if relative_paths:
        ActiveAgentFile.objects.filter(
            drive=drive_report,
            relative_path__in=relative_paths,
        ).delete()

    if normalized_files:
        for file_information in normalized_files:
            rel_p = file_information["relative_path"]
            if rel_p in matched_upserts:
                continue
            file_name = file_information["name"]
            folder = file_information.get("folder") or "Root"
            old_name = _safe_text(file_information.get("old_name") or file_information.get("old_relative_path") or "", 255).strip()
            if old_name and ("\\" in old_name or "/" in old_name):
                old_name = old_name.replace("\\", "/").rsplit("/", 1)[-1]

            if old_name and old_name != file_name:
                _log_drive_activity(
                    agent,
                    drive_report,
                    DriveActivityLog.TYPE_RENAMED,
                    file_name,
                    old_name=old_name,
                    details=f"Renamed from {old_name} in {folder}",
                )
            elif rel_p not in existing_paths and drive_report.count_complete:
                _log_drive_activity(
                    agent,
                    drive_report,
                    DriveActivityLog.TYPE_ADDED,
                    file_name,
                    details=f"File added in {folder}",
                )
        ActiveAgentFile.objects.bulk_create(
            [
                ActiveAgentFile(
                    agent=agent,
                    drive=drive_report,
                    reported_scan_id=drive_report.scan_id,
                    **file_information,
                )
                for file_information in normalized_files
            ],
            batch_size=batch_limit,
        )

    current_file_count = drive_report.file_reports.count()
    index_ready = bool(payload.get("index_ready")) or drive_report.count_complete

    if index_ready:
        drive_report.total_files = current_file_count
        drive_report.indexed_files = current_file_count
        drive_report.count_complete = True
    else:
        drive_report.total_files = max(
            _stable_drive_file_count(drive_report),
            current_file_count,
        )
        drive_report.indexed_files = max(
            drive_report.indexed_files,
            current_file_count,
        )
        drive_report.count_complete = False

    drive_report.save(
        update_fields=(
            "label",
            "total_files",
            "indexed_files",
            "count_complete",
            "storage",
            "last_reported_at",
        )
    )

    agent.total_files = sum(
        agent.drive_reports.values_list("total_files", flat=True)
    )
    agent.drive_count = agent.drive_reports.count()
    agent.save(
        update_fields=(
            "host_name",
            "ip_address",
            "mac_address",
            "os_label",
            "architecture",
            "total_files",
            "drive_count",
            "last_seen_at",
        )
    )

    return JsonResponse(
        {
            "ok": True,
            "agent_id": agent.agent_id,
            "drive_value": drive_report.value,
            "accepted_files": len(normalized_files),
            "deleted_files": len(deleted_paths),
            "total_files": drive_report.total_files,
            "count_complete": drive_report.count_complete,
        }
    )


@csrf_exempt
@require_POST
def agent_file_download(request):
    if not _is_agent_request_authorized(request):
        return JsonResponse(
            {
                "ok": False,
                "error": "Unauthorized agent token.",
            },
            status=403,
        )

    agent_id = _safe_text(request.headers.get("X-Agent-Id"), 128).strip()
    request_id = _safe_text(request.headers.get("X-Download-Request-Id"), 64).strip()
    upload_status = _safe_text(
        request.headers.get("X-Download-Status") or "ready",
        16,
    ).strip().lower()

    if not agent_id or not request_id:
        return JsonResponse(
            {
                "ok": False,
                "error": "agent id and download request id are required.",
            },
            status=400,
        )

    download_request = (
        RemoteFileDownload.objects.select_related("agent")
        .filter(
            request_id=request_id,
            agent__agent_id=agent_id,
        )
        .first()
    )

    if not download_request:
        return JsonResponse(
            {
                "ok": False,
                "error": "download request was not found.",
            },
            status=404,
        )

    if upload_status == RemoteFileDownload.STATUS_FAILED:
        download_request.status = RemoteFileDownload.STATUS_FAILED
        download_request.error_message = _safe_text(
            request.headers.get("X-Download-Error"),
            512,
            default="Agent could not read this file.",
        )
        download_request.save(update_fields=("status", "error_message", "updated_at"))
        return JsonResponse({"ok": True, "status": download_request.status})

    download_path = _remote_download_storage_path(download_request)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    total_size = 0

    try:
        with download_path.open("wb") as output_file:
            while True:
                chunk = request.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)
                output_file.write(chunk)
    except OSError as error:
        download_request.status = RemoteFileDownload.STATUS_FAILED
        download_request.error_message = _safe_text(str(error), 512)
        download_request.save(update_fields=("status", "error_message", "updated_at"))
        return JsonResponse(
            {
                "ok": False,
                "error": "server could not store the uploaded file.",
            },
            status=500,
        )

    download_request.status = RemoteFileDownload.STATUS_READY
    download_request.file_path = str(download_path)
    download_request.size_bytes = total_size
    download_request.error_message = ""
    download_request.save(
        update_fields=(
            "status",
            "file_path",
            "size_bytes",
            "error_message",
            "updated_at",
        )
    )

    return JsonResponse(
        {
            "ok": True,
            "status": download_request.status,
            "size_bytes": total_size,
        }
    )


@csrf_exempt
@require_POST
def agent_uninstall(request):
    if not _is_agent_request_authorized(request):
        return JsonResponse(
            {
                "ok": False,
                "error": "Unauthorized agent token.",
            },
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {
                "ok": False,
                "error": "Invalid JSON payload.",
            },
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "ok": False,
                "error": "Payload must be a JSON object.",
            },
            status=400,
        )

    agent_id = _safe_text(payload.get("agent_id"), 128).strip()

    if not agent_id:
        return JsonResponse(
            {
                "ok": False,
                "error": "agent_id is required.",
            },
            status=400,
        )

    deleted_count, _deleted_details = ActiveAgent.objects.filter(
        agent_id=agent_id,
    ).delete()

    return JsonResponse(
        {
            "ok": True,
            "agent_id": agent_id,
            "removed": deleted_count > 0,
        }
    )


@login_required
@require_POST
def select_agent(request):
    requested_agent_id = _safe_text(request.POST.get("agent_id"), 128).strip()

    if requested_agent_id:
        selected_agent = ActiveAgent.objects.filter(
            agent_id=requested_agent_id,
            last_seen_at__gte=_active_agent_cutoff(),
        ).first()

        if not selected_agent:
            _set_selected_agent(request, "")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Selected DriveAgent user is not active.",
                    "selected_agent_id": "",
                },
                status=404,
            )

        _set_selected_agent(request, selected_agent.agent_id)
        _queue_agent_drive_scan(
            selected_agent,
            _get_selected_agent_drive_value(request, selected_agent),
        )
    else:
        _set_selected_agent(request, "")

    context = _build_drive_files_context(request)

    return _drive_files_json_response(
        context,
        request=request,
        extra_payload={
            "agent_selected": bool(requested_agent_id),
        },
    )


@login_required
@require_POST
def select_drive(request):
    requested_agent_id = _safe_text(request.POST.get("agent_id"), 128).strip()
    selected_agent = _get_selected_agent(
        request,
        requested_agent_id=requested_agent_id,
        require_online=False,
        persist_session=True,
        allow_auto_select=False,
    )

    if selected_agent:
        selected_drive_value = _set_selected_agent_drive_value(
            request,
            selected_agent,
            request.POST.get("drive_root"),
        )
        _queue_agent_drive_scan(selected_agent, selected_drive_value)
        context = _build_agent_files_context(request, selected_agent)

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return _drive_files_json_response(
                context,
                request=request,
                extra_payload={
                    "drive_changed": True,
                    "drive_switch_requested": True,
                    "selected_drive_value": selected_drive_value,
                },
            )

        return redirect("drive_files")

    selected_drive_root = _resolve_drive_root(request.POST.get("drive_root"))
    _set_selected_drive_root(request, selected_drive_root)

    if not _local_drive_scanner_enabled():
        context = _build_hosted_waiting_context(request, selected_drive_root)

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return _drive_files_json_response(
                context,
                request=request,
                extra_payload={
                    "drive_changed": False,
                    "drive_switch_requested": True,
                },
            )

        return redirect("drive_files")

    drive_changed = set_drive_root(selected_drive_root)
    request_drive_scan()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        context = _build_drive_files_context(request)

        return _drive_files_json_response(
            context,
            request=request,
            extra_payload={
                "drive_changed": drive_changed,
                "drive_switch_requested": True,
            },
        )

    return redirect("drive_files")


@login_required
@require_POST
def scan_now(request):
    requested_agent_id = _safe_text(request.POST.get("agent_id"), 128).strip()
    selected_agent = _get_selected_agent(
        request,
        requested_agent_id=requested_agent_id,
        require_online=False,
        persist_session=bool(requested_agent_id),
        allow_auto_select=False,
    )

    if selected_agent:
        selected_drive_value = _get_selected_agent_drive_value(
            request,
            selected_agent,
            requested_drive_value=_safe_text(request.POST.get("drive_root"), 64).strip(),
        )
        _queue_agent_drive_scan(selected_agent, selected_drive_value)

        return JsonResponse(
            {
                "requested": True,
                "remote_agent": True,
                "selected_drive_value": selected_drive_value,
            }
        )

    if not _local_drive_scanner_enabled():
        return JsonResponse(
            {
                "requested": False,
                "local_scanner_disabled": True,
            }
        )

    set_drive_root(_get_selected_drive_root(request))
    request_drive_scan()

    return JsonResponse(
        {
            "requested": True,
        }
    )


@login_required
def drive_files(request):
    """
    Displays the latest cached configured drive scan.
    """

    context = _build_drive_files_context(request)

    return render(
        request,
        "drivefiles/drive_files.html",
        context,
    )


@login_required
def download_file(request):
    """
    Downloads a file from the configured drive.

    It prevents users from accessing files outside the configured drive.
    """

    relative_path = request.GET.get("path")
    selected_agent = _get_selected_agent(request)

    if selected_agent:
        if not relative_path:
            raise Http404("File path was not provided.")

        selected_drive_value = _get_selected_agent_drive_value(request, selected_agent)
        selected_drive = selected_agent.drive_reports.filter(
            value=selected_drive_value,
        ).first()

        if not selected_drive:
            raise Http404("Selected remote drive was not found.")

        file_report = selected_drive.file_reports.filter(
            relative_path=relative_path,
        ).first()

        if not file_report:
            raise Http404("Remote file was not found.")

        _log_drive_activity(
            selected_agent,
            selected_drive,
            DriveActivityLog.TYPE_EXTERNAL_COPY,
            file_report.name,
            details="Copied to external storage / downloaded",
        )

        ready_download = _ready_remote_download_for_file(
            selected_agent,
            selected_drive,
            file_report,
        )

        if ready_download:
            ready_response = _remote_download_file_response(ready_download)

            if ready_response:
                return ready_response

        download_request = _queue_agent_file_download(
            selected_agent,
            selected_drive,
            file_report,
        )
        deadline = time.monotonic() + getattr(
            settings,
            "AGENT_FILE_DOWNLOAD_WAIT_SECONDS",
            20,
        )

        while time.monotonic() < deadline:
            time.sleep(0.5)
            download_request.refresh_from_db()

            if download_request.status == RemoteFileDownload.STATUS_READY:
                ready_response = _remote_download_file_response(download_request)

                if ready_response:
                    return ready_response

            if download_request.status == RemoteFileDownload.STATUS_FAILED:
                raise Http404(
                    download_request.error_message
                    or "Agent could not download this file."
                )

        raise Http404("File is being prepared. Please try again in a few seconds.")

    if not _local_drive_scanner_enabled():
        raise Http404("Hosted local file download is disabled.")

    if not relative_path:
        raise Http404("File path was not provided.")

    selected_drive_root = _get_selected_drive_root(request)
    root_path = selected_drive_root.resolve()
    requested_file = (root_path / relative_path).resolve()

    # Security check: requested file must remain inside the configured drive.
    try:
        requested_file.relative_to(root_path)
    except ValueError:
        raise Http404("Invalid file path.")

    if not requested_file.exists() or not requested_file.is_file():
        raise Http404("File was not found.")

    try:
        return FileResponse(
            open(requested_file, "rb"),
            as_attachment=True,
            filename=requested_file.name,
        )
    except PermissionError:
        raise Http404("Permission denied for this file.")
