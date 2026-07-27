import argparse
import concurrent.futures
import ipaddress
import json
import os
import platform
import subprocess
import shutil
import socket
import string
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


SERVER_URL_ENV = "DRIVE_AGENT_SERVER_URL"
API_TOKEN_ENV = "DRIVE_AGENT_API_TOKEN"
HEARTBEAT_SECONDS_ENV = "DRIVE_AGENT_HEARTBEAT_SECONDS"
COUNT_REFRESH_SECONDS_ENV = "DRIVE_AGENT_COUNT_REFRESH_SECONDS"
FILE_BATCH_SIZE_ENV = "DRIVE_AGENT_FILE_BATCH_SIZE"
CHANGE_DEBOUNCE_SECONDS_ENV = "DRIVE_AGENT_CHANGE_DEBOUNCE_SECONDS"
FIRST_FILE_BATCH_SIZE_ENV = "DRIVE_AGENT_FIRST_FILE_BATCH_SIZE"
FILE_BATCH_INTERVAL_SECONDS_ENV = "DRIVE_AGENT_FILE_BATCH_INTERVAL_SECONDS"
DRIVE_PRIORITY_ENV = "DRIVE_AGENT_DRIVE_PRIORITY"
SYSTEM_DRIVE_DELAY_SECONDS_ENV = "DRIVE_AGENT_SYSTEM_DRIVE_DELAY_SECONDS"
PRIORITY_FOLDERS_ENV = "DRIVE_AGENT_PRIORITY_FOLDERS"
LOG_FILE_ENV = "DRIVE_AGENT_LOG_FILE"
LAN_DISCOVERY_ENABLED_ENV = "DRIVE_AGENT_LAN_DISCOVERY_ENABLED"
FALLBACK_HEARTBEAT_SECONDS = 1
FALLBACK_COUNT_REFRESH_SECONDS = 60
FALLBACK_FILE_BATCH_SIZE = 1000
FALLBACK_CHANGE_DEBOUNCE_SECONDS = 1
FALLBACK_LAN_DISCOVERY_ENABLED = False
DEFAULT_DISCOVERY_PORT = 8000
LOCAL_DEFAULT_SERVER_URL = "http://127.0.0.1:8000/agent-heartbeat/"
FALLBACK_DRIVE_PRIORITY = "D"
FALLBACK_FIRST_FILE_BATCH_SIZE = 10
FALLBACK_FILE_BATCH_INTERVAL_SECONDS = 0.15
FALLBACK_SYSTEM_DRIVE_DELAY_SECONDS = 1
APP_DIRECTORY_NAME = "DriveAgent"
RUN_REGISTRY_NAME = "DriveAgent"
UNINSTALL_REGISTRY_NAME = "DriveAgent"
EXCLUDED_FOLDER_NAMES = {
    "$recycle.bin",
    "recycled",
    "recycler",
    "system volume information",
}
WINDOWS_FILE_ATTRIBUTE_HIDDEN = 0x2
WINDOWS_FILE_ATTRIBUTE_SYSTEM = 0x4
WINDOWS_HIDDEN_OR_SYSTEM_ATTRIBUTES = (
    WINDOWS_FILE_ATTRIBUTE_HIDDEN | WINDOWS_FILE_ATTRIBUTE_SYSTEM
)
PRIORITY_FOLDER_NAMES = {
    "desktop",
    "documents",
    "downloads",
    "onedrive",
    "pictures",
    "users",
}
PDF_EXTENSIONS = frozenset({".pdf"})
DOCUMENT_EXTENSIONS = frozenset({".doc", ".docx", ".rtf", ".odt"})
TEXT_EXTENSIONS = frozenset({".md", ".log", ".txt"})
PRESENTATION_EXTENSIONS = frozenset({".pps", ".ppsx", ".ppt", ".pptx"})
SPREADSHEET_EXTENSIONS = frozenset({".xls", ".xlsx", ".csv", ".tsv", ".ods"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".heic"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"})

try:
    import agent_build_defaults as build_defaults
except ImportError:
    build_defaults = None

BUILD_DEFAULT_SERVER_URL = getattr(
    build_defaults,
    "DEFAULT_SERVER_URL",
    LOCAL_DEFAULT_SERVER_URL,
)
BUILD_DEFAULT_API_TOKEN = getattr(
    build_defaults,
    "DEFAULT_API_TOKEN",
    "drive-agent-local-token",
)
BUILD_DEFAULT_DRIVE_PRIORITY = getattr(
    build_defaults,
    "DEFAULT_DRIVE_PRIORITY",
    FALLBACK_DRIVE_PRIORITY,
)
BUILD_DEFAULT_FIRST_FILE_BATCH_SIZE = getattr(
    build_defaults,
    "DEFAULT_FIRST_FILE_BATCH_SIZE",
    FALLBACK_FIRST_FILE_BATCH_SIZE,
)
BUILD_DEFAULT_FILE_BATCH_INTERVAL_SECONDS = getattr(
    build_defaults,
    "DEFAULT_FILE_BATCH_INTERVAL_SECONDS",
    FALLBACK_FILE_BATCH_INTERVAL_SECONDS,
)
BUILD_DEFAULT_SYSTEM_DRIVE_DELAY_SECONDS = getattr(
    build_defaults,
    "DEFAULT_SYSTEM_DRIVE_DELAY_SECONDS",
    FALLBACK_SYSTEM_DRIVE_DELAY_SECONDS,
)

DEFAULT_SERVER_URL = BUILD_DEFAULT_SERVER_URL
DEFAULT_API_TOKEN = BUILD_DEFAULT_API_TOKEN
DEFAULT_HEARTBEAT_SECONDS = getattr(
    build_defaults,
    "DEFAULT_HEARTBEAT_SECONDS",
    FALLBACK_HEARTBEAT_SECONDS,
)
DEFAULT_COUNT_REFRESH_SECONDS = getattr(
    build_defaults,
    "DEFAULT_COUNT_REFRESH_SECONDS",
    FALLBACK_COUNT_REFRESH_SECONDS,
)
DEFAULT_FILE_BATCH_SIZE = getattr(
    build_defaults,
    "DEFAULT_FILE_BATCH_SIZE",
    FALLBACK_FILE_BATCH_SIZE,
)
DEFAULT_CHANGE_DEBOUNCE_SECONDS = getattr(
    build_defaults,
    "DEFAULT_CHANGE_DEBOUNCE_SECONDS",
    FALLBACK_CHANGE_DEBOUNCE_SECONDS,
)
DEFAULT_LAN_DISCOVERY_ENABLED = getattr(
    build_defaults,
    "DEFAULT_LAN_DISCOVERY_ENABLED",
    FALLBACK_LAN_DISCOVERY_ENABLED,
)
DEFAULT_DRIVE_PRIORITY = BUILD_DEFAULT_DRIVE_PRIORITY
DEFAULT_FIRST_FILE_BATCH_SIZE = BUILD_DEFAULT_FIRST_FILE_BATCH_SIZE
DEFAULT_FILE_BATCH_INTERVAL_SECONDS = BUILD_DEFAULT_FILE_BATCH_INTERVAL_SECONDS
DEFAULT_SYSTEM_DRIVE_DELAY_SECONDS = BUILD_DEFAULT_SYSTEM_DRIVE_DELAY_SECONDS


def _runtime_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent


def _application_directory():
    local_app_data = os.environ.get("LOCALAPPDATA")

    if local_app_data:
        return Path(local_app_data) / APP_DIRECTORY_NAME

    return _runtime_directory() / APP_DIRECTORY_NAME


def _installed_executable_path():
    return _application_directory() / "DriveAgent.exe"


def _same_file(first_path, second_path):
    try:
        return Path(first_path).resolve().samefile(Path(second_path).resolve())
    except OSError:
        return Path(first_path).resolve() == Path(second_path).resolve()


def _startup_command(executable_path, server_url, api_token):
    return " ".join(
        [
            _quoted_windows_argument(executable_path),
            "--run-agent",
            "--server-url",
            _quoted_windows_argument(server_url),
            "--api-token",
            _quoted_windows_argument(api_token),
        ]
    )


def _quoted_windows_argument(value):
    escaped_value = str(value).replace('"', r'\"')
    return f'"{escaped_value}"'


def _uninstall_command(executable_path, server_url, api_token):
    return " ".join(
        [
            _quoted_windows_argument(executable_path),
            "--uninstall",
            "--server-url",
            _quoted_windows_argument(server_url),
            "--api-token",
            _quoted_windows_argument(api_token),
        ]
    )


def add_startup_entry(executable_path, server_url, api_token):
    if os.name != "nt":
        return

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as registry_key:
            winreg.SetValueEx(
                registry_key,
                RUN_REGISTRY_NAME,
                0,
                winreg.REG_SZ,
                _startup_command(executable_path, server_url, api_token),
            )
    except OSError as error:
        log_message(f"Unable to register DriveAgent startup: {error}")


def remove_startup_entry():
    if os.name != "nt":
        return

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as registry_key:
            winreg.DeleteValue(registry_key, RUN_REGISTRY_NAME)
    except FileNotFoundError:
        pass
    except OSError as error:
        log_message(f"Unable to remove DriveAgent startup: {error}")


def add_uninstall_entry(executable_path, server_url, api_token):
    if os.name != "nt":
        return

    try:
        import winreg

        registry_path = (
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall"
            rf"\{UNINSTALL_REGISTRY_NAME}"
        )

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            registry_path,
            0,
            winreg.KEY_SET_VALUE,
        ) as registry_key:
            uninstall_command = _uninstall_command(
                executable_path,
                server_url,
                api_token,
            )
            winreg.SetValueEx(
                registry_key,
                "DisplayName",
                0,
                winreg.REG_SZ,
                "DriveAgent",
            )
            winreg.SetValueEx(
                registry_key,
                "DisplayVersion",
                0,
                winreg.REG_SZ,
                "1.0",
            )
            winreg.SetValueEx(
                registry_key,
                "Publisher",
                0,
                winreg.REG_SZ,
                "DriveAgent",
            )
            winreg.SetValueEx(
                registry_key,
                "InstallLocation",
                0,
                winreg.REG_SZ,
                str(executable_path.parent),
            )
            winreg.SetValueEx(
                registry_key,
                "DisplayIcon",
                0,
                winreg.REG_SZ,
                str(executable_path),
            )
            winreg.SetValueEx(
                registry_key,
                "UninstallString",
                0,
                winreg.REG_SZ,
                uninstall_command,
            )
            winreg.SetValueEx(
                registry_key,
                "QuietUninstallString",
                0,
                winreg.REG_SZ,
                uninstall_command,
            )
            winreg.SetValueEx(registry_key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(registry_key, "NoRepair", 0, winreg.REG_DWORD, 1)
    except OSError as error:
        log_message(f"Unable to register DriveAgent uninstall entry: {error}")


def remove_uninstall_entry():
    if os.name != "nt":
        return

    try:
        import winreg

        registry_path = (
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall"
            rf"\{UNINSTALL_REGISTRY_NAME}"
        )
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_path)
    except FileNotFoundError:
        pass
    except OSError as error:
        log_message(f"Unable to remove DriveAgent uninstall entry: {error}")


def start_installed_agent(executable_path, server_url, api_token):
    arguments = [
        str(executable_path),
        "--run-agent",
        "--server-url",
        server_url,
        "--api-token",
        api_token,
    ]
    creation_flags = 0

    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )

    try:
        subprocess.Popen(
            arguments,
            cwd=str(executable_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as error:
        log_message(f"Unable to start installed DriveAgent: {error}")


def should_self_install(args):
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return False

    if args.run_agent or args.once or args.unregister or args.uninstall:
        return False

    return True


def install_and_start_agent(server_url, api_token):
    app_directory = _application_directory()
    installed_executable = _installed_executable_path()

    try:
        app_directory.mkdir(parents=True, exist_ok=True)
        stop_other_agent_processes()
        time.sleep(0.5)
        current_executable = Path(sys.executable).resolve()

        if not _same_file(current_executable, installed_executable):
            shutil.copy2(current_executable, installed_executable)

        add_startup_entry(installed_executable, server_url, api_token)
        add_uninstall_entry(installed_executable, server_url, api_token)
        registration_ok = send_initial_agent_heartbeat(server_url, api_token)
        start_installed_agent(installed_executable, server_url, api_token)
        log_message(f"DriveAgent installed at {installed_executable}.")

        if not registration_ok:
            log_message(
                "DriveAgent installed, but the dashboard rejected the first "
                "connection. Check the Render DRIVE_AGENT_API_TOKEN and rebuild "
                "the EXE with the same token."
            )
            time.sleep(8)

        return True
    except OSError as error:
        log_message(f"DriveAgent install failed: {error}")
        return False


def remove_installed_files():
    app_directory = _application_directory()

    if not app_directory.exists():
        return

    if os.name == "nt":
        removal_command = (
            f'timeout /t 2 /nobreak >nul & rmdir /s /q "{app_directory}"'
        )
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )

        try:
            subprocess.Popen(
                ["cmd.exe", "/c", removal_command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except OSError as error:
            log_message(f"Unable to schedule DriveAgent removal: {error}")
        return

    try:
        shutil.rmtree(app_directory)
    except OSError as error:
        log_message(f"Unable to remove DriveAgent files: {error}")


def stop_agent_processes_after_delay():
    if os.name != "nt":
        return

    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )

    try:
        subprocess.Popen(
            [
                "cmd.exe",
                "/c",
                "timeout /t 1 /nobreak >nul & taskkill /IM DriveAgent.exe /F >nul 2>nul",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as error:
        log_message(f"Unable to stop DriveAgent processes: {error}")


def stop_other_agent_processes():
    if os.name != "nt":
        return

    try:
        tasklist_output = subprocess.check_output(
            [
                "tasklist",
                "/FI",
                "IMAGENAME eq DriveAgent.exe",
                "/FO",
                "CSV",
                "/NH",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        log_message(f"Unable to inspect existing DriveAgent processes: {error}")
        return

    current_pid = str(os.getpid())

    for line in tasklist_output.splitlines():
        columns = [
            column.strip().strip('"')
            for column in line.split(",")
        ]

        if len(columns) < 2:
            continue

        pid = columns[1]

        if pid == current_pid or not pid.isdigit():
            continue

        try:
            subprocess.run(
                ["taskkill", "/PID", pid, "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as error:
            log_message(f"Unable to stop existing DriveAgent process {pid}: {error}")


def log_message(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted_message = f"[{timestamp}] {message}"

    if sys.stdout:
        try:
            print(formatted_message, flush=True)
        except OSError:
            pass

    log_file_value = os.environ.get(LOG_FILE_ENV, "").strip()

    if not log_file_value:
        return

    log_path = (
        Path(log_file_value)
        if log_file_value.lower() not in {"1", "true", "yes", "on"}
        else _application_directory() / "agent.log"
    )

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{formatted_message}\n")
    except OSError:
        pass


def load_config():
    config_path = _runtime_directory() / "agent_config.json"

    if not config_path.exists():
        return {}

    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            loaded_config = json.load(config_file)
    except (OSError, json.JSONDecodeError):
        return {}

    return loaded_config if isinstance(loaded_config, dict) else {}


def _config_value(config, key, env_name, default):
    value = os.environ.get(env_name)

    if value not in (None, ""):
        return value

    return config.get(key, default)


def _positive_int(value, default):
    try:
        parsed_value = int(value)
    except (TypeError, ValueError):
        return default

    return parsed_value if parsed_value > 0 else default


def _positive_float(value, default):
    try:
        parsed_value = float(value)
    except (TypeError, ValueError):
        return default

    return parsed_value if parsed_value > 0 else default


def _boolean_value(value, default=False):
    if isinstance(value, bool):
        return value

    if value in (None, ""):
        return default

    normalized_value = str(value).strip().lower()

    if normalized_value in {"1", "true", "yes", "y", "on"}:
        return True

    if normalized_value in {"0", "false", "no", "n", "off"}:
        return False

    return default


def _split_config_values(value):
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = str(value or "").replace(";", ",").split(",")

    return [str(raw_value).strip() for raw_value in raw_values if str(raw_value).strip()]


def _drive_letter(value):
    normalized_value = str(value or "").replace("\\", "/").strip().upper()

    if len(normalized_value) >= 2 and normalized_value[1] == ":":
        return normalized_value[0]

    if len(normalized_value) == 1 and normalized_value in string.ascii_uppercase:
        return normalized_value

    return ""


def _system_drive_letter():
    return _drive_letter(os.environ.get("SystemDrive") or "C:")


def _drive_priority_letters(value, default_value=FALLBACK_DRIVE_PRIORITY):
    letters = []
    seen_letters = set()

    for raw_value in _split_config_values(value):
        letter = _drive_letter(raw_value)

        if letter and letter not in seen_letters:
            seen_letters.add(letter)
            letters.append(letter)

    if letters:
        return tuple(letters)

    if value != default_value:
        return _drive_priority_letters(default_value, "")

    return ()


def _priority_folder_names(value):
    configured_folder_names = {
        folder_name.lower()
        for folder_name in _split_config_values(value)
    }

    if not configured_folder_names:
        configured_folder_names = set(PRIORITY_FOLDER_NAMES)

    current_user_folder_name = Path.home().name.lower()

    if current_user_folder_name:
        configured_folder_names.add(current_user_folder_name)

    return frozenset(configured_folder_names)


def discover_drive_roots():
    if os.name == "nt":
        try:
            import ctypes

            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except (AttributeError, OSError):
            drive_mask = 0

        return [
            Path(f"{letter}:/")
            for index, letter in enumerate(string.ascii_uppercase)
            if drive_mask & (1 << index)
        ]

    return [Path("/")]


def _drive_sort_key(root, drive_priority):
    letter = _drive_letter(root)
    system_drive_last_index = 1 if letter and letter == _system_drive_letter() else 0
    preferred_index = (
        drive_priority.index(letter)
        if letter in drive_priority
        else len(drive_priority)
    )

    return (system_drive_last_index, preferred_index, str(root).lower())


def ordered_drive_roots(drive_priority=()):
    return sorted(
        discover_drive_roots(),
        key=lambda root: _drive_sort_key(root, drive_priority),
    )


def drive_value(root):
    value = str(root).replace("\\", "/")

    if value.endswith(":"):
        value = f"{value}/"

    return value


def normalize_drive_value(value):
    normalized_value = str(value or "").replace("\\", "/").strip()

    if normalized_value.endswith(":"):
        normalized_value = f"{normalized_value}/"

    return normalized_value


def drive_label(root):
    label = str(root).replace("/", "\\")

    if label.endswith(":"):
        return f"{label}\\"

    if not label.endswith("\\"):
        return f"{label}\\"

    return label


def format_size(size_in_bytes):
    size = float(size_in_bytes)

    if size < 1024:
        return f"{size:.0f} bytes"

    size = size / 1024

    if size < 1024:
        return f"{size:.2f} KB"

    size = size / 1024

    if size < 1024:
        return f"{size:.2f} MB"

    size = size / 1024
    return f"{size:.2f} GB"


def format_mac_address(mac_value):
    return ":".join(
        f"{(mac_value >> shift) & 0xFF:02X}" for shift in range(40, -1, -8)
    )


def primary_ip_address(host_name):
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

    for candidate in candidates:
        if candidate and not candidate.startswith("127."):
            return candidate

    return "Unavailable"


def machine_identity():
    host_name = socket.gethostname()
    mac_address = format_mac_address(uuid.getnode())

    return {
        "agent_id": f"{host_name}-{mac_address.replace(':', '')}".lower(),
        "host_name": host_name,
        "ip_address": primary_ip_address(host_name),
        "mac_address": mac_address,
        "os_label": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine() or "Unknown",
    }


def file_type_metadata(extension):
    normalized_extension = extension.lower()

    if normalized_extension in PDF_EXTENSIONS:
        return {"type_badge": "PDF", "type_class": "pdf", "type_label": "PDF"}

    if normalized_extension in DOCUMENT_EXTENSIONS:
        return {"type_badge": "W", "type_class": "document", "type_label": normalized_extension[1:].upper()}

    if normalized_extension in TEXT_EXTENSIONS:
        return {"type_badge": "TXT", "type_class": "document", "type_label": normalized_extension[1:].upper()}

    if normalized_extension in PRESENTATION_EXTENSIONS:
        return {"type_badge": "PPT", "type_class": "document", "type_label": normalized_extension[1:].upper()}

    if normalized_extension in SPREADSHEET_EXTENSIONS:
        return {"type_badge": "X", "type_class": "spreadsheet", "type_label": normalized_extension[1:].upper()}

    if normalized_extension in IMAGE_EXTENSIONS:
        return {"type_badge": "IMG", "type_class": "image", "type_label": normalized_extension[1:].upper()}

    if normalized_extension in VIDEO_EXTENSIONS:
        return {"type_badge": "VID", "type_class": "video", "type_label": normalized_extension[1:].upper()}

    if normalized_extension in ARCHIVE_EXTENSIONS:
        return {"type_badge": "ZIP", "type_class": "archive", "type_label": normalized_extension[1:].upper()}

    if extension == "No extension":
        return {"type_badge": "FILE", "type_class": "other", "type_label": "File"}

    return {
        "type_badge": normalized_extension[1:4].upper() or "FILE",
        "type_class": "other",
        "type_label": normalized_extension[1:].upper() or "File",
    }


def _folder_scan_key(folder_path, priority_folder_names):
    folder_name = Path(folder_path).name.lower()

    return (
        0 if folder_name in priority_folder_names else 1,
        folder_name,
    )


def _visible_entry_stat(entry):
    if entry.name.startswith("."):
        return None

    try:
        stat_result = entry.stat(follow_symlinks=False)
    except OSError:
        return None

    file_attributes = getattr(stat_result, "st_file_attributes", 0)

    if file_attributes & WINDOWS_HIDDEN_OR_SYSTEM_ATTRIBUTES:
        return None

    return stat_result


def _visible_path_stat(file_path):
    file_path = Path(file_path)

    if file_path.name.startswith("."):
        return None

    try:
        stat_result = file_path.stat()
    except OSError:
        return None

    file_attributes = getattr(stat_result, "st_file_attributes", 0)

    if file_attributes & WINDOWS_HIDDEN_OR_SYSTEM_ATTRIBUTES:
        return None

    return stat_result


def file_metadata_from_path(root, file_path, stat_result=None):
    file_path = Path(file_path)

    if stat_result is None:
        stat_result = _visible_path_stat(file_path)

        if stat_result is None:
            return None

    if not file_path.is_file():
        return None

    extension = file_path.suffix or "No extension"
    modified_timestamp = stat_result.st_mtime

    try:
        relative_path = str(file_path.relative_to(root))
    except ValueError:
        relative_path = file_path.name

    metadata = {
        "name": file_path.name,
        "folder": str(file_path.parent),
        "relative_path": relative_path,
        "extension": extension,
        "size": format_size(stat_result.st_size),
        "size_bytes": stat_result.st_size,
        "modified_timestamp": modified_timestamp,
        "freshness_timestamp": modified_timestamp,
        "modified_display": time.strftime(
            "%d-%m-%Y %I:%M:%S %p",
            time.localtime(modified_timestamp),
        ),
    }
    metadata.update(file_type_metadata(extension))
    return metadata


def file_metadata(root, entry, stat_result=None):
    return file_metadata_from_path(root, Path(entry.path), stat_result)


def build_agent_endpoint_url(server_url, endpoint_name):
    parsed_url = urllib.parse.urlparse(server_url)
    heartbeat_path = parsed_url.path or "/agent-heartbeat/"

    if heartbeat_path.endswith("/agent-heartbeat/"):
        endpoint_path = f"{heartbeat_path[:-len('agent-heartbeat/')]}{endpoint_name}/"
    elif heartbeat_path.endswith("/agent-heartbeat"):
        endpoint_path = f"{heartbeat_path[:-len('agent-heartbeat')]}{endpoint_name}/"
    else:
        endpoint_path = f"{heartbeat_path.rstrip('/')}/{endpoint_name}/"

    return urllib.parse.urlunparse(parsed_url._replace(path=endpoint_path))


def build_files_batch_url(server_url):
    return build_agent_endpoint_url(server_url, "agent-files-batch")


def build_file_events_url(server_url):
    return build_agent_endpoint_url(server_url, "agent-file-events")


def build_file_download_url(server_url):
    return build_agent_endpoint_url(server_url, "agent-file-download")


def build_uninstall_url(server_url):
    return build_agent_endpoint_url(server_url, "agent-uninstall")


def build_ping_url(server_url):
    return build_agent_endpoint_url(server_url, "agent-ping")


def heartbeat_url_from_host(host, port=DEFAULT_DISCOVERY_PORT):
    return f"http://{host}:{port}/agent-heartbeat/"


def local_ipv4_addresses():
    candidates = []
    host_name = socket.gethostname()

    try:
        candidates.append(primary_ip_address(host_name))
    except OSError:
        pass

    try:
        candidates.extend(socket.gethostbyname_ex(host_name)[2])
    except OSError:
        pass

    usable_addresses = []
    seen_addresses = set()

    for candidate in candidates:
        if not candidate or candidate in seen_addresses:
            continue

        seen_addresses.add(candidate)

        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue

        if address.version == 4 and not address.is_loopback and not address.is_link_local:
            usable_addresses.append(candidate)

    return usable_addresses


def dashboard_ping_ok(server_url, timeout=1):
    request = urllib.request.Request(
        build_ping_url(server_url),
        headers={
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return False

    return payload.get("ok") is True and payload.get("app") == "drive-agent-dashboard"


def discovery_candidates(preferred_url):
    candidates = []
    seen_urls = set()

    def add_candidate(url):
        if url and url not in seen_urls:
            seen_urls.add(url)
            candidates.append(url)

    add_candidate(preferred_url)

    parsed_preferred = urllib.parse.urlparse(preferred_url)
    preferred_port = parsed_preferred.port or DEFAULT_DISCOVERY_PORT

    for address in local_ipv4_addresses():
        try:
            network = ipaddress.ip_network(f"{address}/24", strict=False)
        except ValueError:
            continue

        for host in network.hosts():
            add_candidate(heartbeat_url_from_host(str(host), preferred_port))

    return candidates


def discover_dashboard_server(
    preferred_url,
    force_scan=False,
    lan_discovery_enabled=False,
):
    if not force_scan and dashboard_ping_ok(preferred_url):
        return preferred_url

    if not lan_discovery_enabled:
        return preferred_url

    candidates = [
        candidate
        for candidate in discovery_candidates(preferred_url)
        if force_scan or candidate != preferred_url
    ]

    if not candidates:
        return preferred_url

    log_message("Searching LAN for DriveAgent dashboard server.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as executor:
        future_to_url = {
            executor.submit(dashboard_ping_ok, candidate, 0.55): candidate
            for candidate in candidates
        }

        for future in concurrent.futures.as_completed(future_to_url):
            candidate = future_to_url[future]

            try:
                if future.result():
                    log_message(f"Discovered DriveAgent dashboard at {candidate}.")
                    return candidate
            except Exception:
                continue

    log_message(f"DriveAgent dashboard discovery failed. Using {preferred_url}.")
    return preferred_url


class FileCountCache:
    def __init__(
        self,
        refresh_seconds,
        server_url,
        api_token,
        batch_size,
        first_batch_size,
        batch_interval_seconds,
        system_drive_delay_seconds,
        drive_priority,
        priority_folder_names,
    ):
        self.refresh_seconds = refresh_seconds
        self.server_url = server_url
        self.files_batch_url = build_files_batch_url(server_url)
        self.file_events_url = build_file_events_url(server_url)
        self.api_token = api_token
        self.batch_size = batch_size
        self.first_batch_size = first_batch_size
        self.batch_interval_seconds = batch_interval_seconds
        self.system_drive_delay_seconds = system_drive_delay_seconds
        self.drive_priority = drive_priority
        self.priority_folder_names = priority_folder_names
        self.identity = machine_identity()
        self._lock = threading.RLock()
        self._counts = {}
        self._active_scan_values = set()
        self._rescan_after_active_values = set()
        self._condition = threading.Condition()
        self._requested_drive_values = set()
        self._known_paths_by_drive = {}
        self._stop_event = threading.Event()
        self._skip_next_full_scan = True
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._start_initial_drive_scans()
        self._thread.start()

    def stop(self):
        self._stop_event.set()

        with self._condition:
            self._condition.notify_all()

    def update_server_url(self, server_url):
        with self._lock:
            self.server_url = server_url
            self.files_batch_url = build_files_batch_url(server_url)
            self.file_events_url = build_file_events_url(server_url)

    def _root_for_drive_value(self, requested_drive_value):
        normalized_requested_value = normalize_drive_value(requested_drive_value)

        for root in ordered_drive_roots(self.drive_priority):
            if drive_value(root) == normalized_requested_value:
                return root

        return None

    def request_scan_value(self, requested_drive_value):
        root = self._root_for_drive_value(requested_drive_value)

        if root:
            self.request_scan(root)

    def resolve_file_request(self, drive_value_to_resolve, relative_path):
        root = self._root_for_drive_value(drive_value_to_resolve)

        if not root:
            return None, "Requested drive is not available."

        try:
            root_path = root.resolve()
            requested_file = (root_path / str(relative_path)).resolve()
            requested_file.relative_to(root_path)
        except (OSError, ValueError):
            return None, "Requested file path is invalid."

        if not requested_file.exists() or not requested_file.is_file():
            return None, "Requested file was not found."

        return requested_file, ""

    def _safe_relative_file_path(self, root, relative_path):
        raw_relative_path = str(relative_path or "").strip()

        if not raw_relative_path:
            return None, ""

        normalized_input = raw_relative_path.replace("/", os.sep).lstrip("\\/")

        try:
            root_path = root.resolve()
            file_path = (root_path / normalized_input).resolve()
            normalized_relative_path = str(file_path.relative_to(root_path))
        except (OSError, ValueError):
            return None, ""

        path_parts = [part.lower() for part in Path(normalized_relative_path).parts]

        if any(part in EXCLUDED_FOLDER_NAMES or part.startswith(".") for part in path_parts):
            return None, ""

        return file_path, normalized_relative_path

    def request_scan(self, root):
        root_value = drive_value(root)

        with self._condition:
            self._requested_drive_values.add(root_value)
            self._condition.notify_all()

        self._start_priority_scan(root)
        log_message(f"Queued real-time scan for {drive_label(root)}.")

    def _start_priority_scan(self, root):
        scan_thread = threading.Thread(
            target=self._run_drive_scan,
            args=(root,),
            daemon=True,
        )
        scan_thread.start()

    def _start_initial_scan_for_root(self, root):
        if self._stop_event.is_set():
            return

        root_value = drive_value(root)

        with self._lock:
            scan_snapshot = self._counts.get(root_value)

            if root_value in self._active_scan_values:
                return

            if scan_snapshot and (
                scan_snapshot.get("indexed_files")
                or scan_snapshot.get("count_complete")
            ):
                return

        self._start_priority_scan(root)
        log_message(f"Started immediate scan for {drive_label(root)}.")

    def _start_delayed_initial_scan(self, root, delay_seconds):
        timer = threading.Timer(
            delay_seconds,
            self._start_initial_scan_for_root,
            args=(root,),
        )
        timer.daemon = True
        timer.start()

    def _start_initial_drive_scans(self):
        roots = ordered_drive_roots(self.drive_priority)
        system_letter = _system_drive_letter()
        non_system_roots = [
            root for root in roots if _drive_letter(root) != system_letter
        ]
        system_roots = [
            root for root in roots if _drive_letter(root) == system_letter
        ]

        for root in non_system_roots:
            self._start_initial_scan_for_root(root)

        system_delay = self.system_drive_delay_seconds if non_system_roots else 0

        for root in system_roots:
            self._start_delayed_initial_scan(root, system_delay)

            if system_delay:
                log_message(
                    f"Scheduled {drive_label(root)} scan after other drives start."
                )

    def snapshot(self, value):
        with self._lock:
            return dict(
                self._counts.get(
                    value,
                    {
                        "total_files": 0,
                        "count_complete": False,
                    },
                )
            )

    def _set_count(self, value, total_files, count_complete):
        with self._lock:
            self._counts[value] = {
                "total_files": total_files,
                "indexed_files": total_files,
                "count_complete": count_complete,
            }

    def _scan_baseline(self, value):
        with self._lock:
            count_snapshot = self._counts.get(value, {})
            return (
                set(self._known_paths_by_drive.get(value, set())),
                bool(count_snapshot.get("count_complete")),
            )

    def _set_known_paths(self, value, known_paths):
        with self._lock:
            self._known_paths_by_drive[value] = set(known_paths)

    def _post_files_batch(
        self,
        root,
        scan_id,
        batch_index,
        files,
        indexed_files,
        scan_complete,
        storage_info,
    ):
        payload = {
            **self.identity,
            "drive_label": drive_label(root),
            "drive_value": drive_value(root),
            "scan_id": scan_id,
            "batch_index": batch_index,
            "indexed_files": indexed_files,
            "total_files": indexed_files,
            "scan_complete": scan_complete,
            "storage": storage_info,
            "files": files,
        }

        for attempt in range(1, 4):
            try:
                status = post_json(self.files_batch_url, self.api_token, payload)
                log_message(
                    f"Reported {len(files)} files for {drive_label(root)} "
                    f"batch {batch_index} with status {status}."
                )
                return True
            except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
                if attempt == 3:
                    log_message(f"File batch failed for {drive_label(root)}: {error}")
                    return False

                log_message(
                    f"File batch retry {attempt} for {drive_label(root)} "
                    f"batch {batch_index}: {error}"
                )
                time.sleep(0.4 * attempt)

        return False

    def report_file_events(self, root, changed_relative_paths=None, deleted_relative_paths=None):
        root_value = drive_value(root)
        event_timestamp = time.time()
        storage_info = drive_storage(root)
        known_paths, previous_scan_completed = self._scan_baseline(root_value)
        upsert_files = []
        deleted_paths = []
        seen_changed_paths = set()
        seen_deleted_paths = set()
        directory_scan_needed = False

        for relative_path in changed_relative_paths or ():
            file_path, normalized_relative_path = self._safe_relative_file_path(
                root,
                relative_path,
            )

            if not normalized_relative_path:
                continue

            path_key = os.path.normcase(normalized_relative_path).lower()

            if path_key in seen_changed_paths:
                continue

            seen_changed_paths.add(path_key)

            if file_path and file_path.exists() and file_path.is_dir():
                directory_scan_needed = True
                continue

            metadata = (
                file_metadata_from_path(root.resolve(), file_path)
                if file_path
                else None
            )

            if metadata:
                metadata["freshness_timestamp"] = max(
                    metadata["freshness_timestamp"],
                    event_timestamp,
                )
                upsert_files.append(metadata)
            elif path_key not in seen_deleted_paths:
                deleted_paths.append(normalized_relative_path)
                seen_deleted_paths.add(path_key)

        for relative_path in deleted_relative_paths or ():
            _file_path, normalized_relative_path = self._safe_relative_file_path(
                root,
                relative_path,
            )

            if not normalized_relative_path:
                continue

            path_key = os.path.normcase(normalized_relative_path).lower()

            if path_key in seen_deleted_paths:
                continue

            deleted_paths.append(normalized_relative_path)
            seen_deleted_paths.add(path_key)

        if not upsert_files and not deleted_paths:
            return not directory_scan_needed

        payload = {
            **self.identity,
            "drive_label": drive_label(root),
            "drive_value": root_value,
            "event_timestamp": event_timestamp,
            "index_ready": previous_scan_completed,
            "storage": storage_info,
            "upsert_files": upsert_files,
            "deleted_paths": deleted_paths,
        }

        for attempt in range(1, 4):
            try:
                status, response_payload = post_json(
                    self.file_events_url,
                    self.api_token,
                    payload,
                    return_payload=True,
                )
                log_message(
                    f"Reported {len(upsert_files)} file updates and "
                    f"{len(deleted_paths)} deletes for {drive_label(root)} "
                    f"with status {status}."
                )

                with self._lock:
                    known_path_set = set(
                        self._known_paths_by_drive.get(root_value, known_paths)
                    )

                    for file_information in upsert_files:
                        known_path_set.add(
                            os.path.normcase(
                                file_information["relative_path"]
                            ).lower()
                        )

                    for relative_path in deleted_paths:
                        known_path_set.discard(
                            os.path.normcase(relative_path).lower()
                        )

                    if known_path_set or previous_scan_completed:
                        self._known_paths_by_drive[root_value] = known_path_set

                    count_snapshot = self._counts.get(root_value, {})
                    current_total = count_snapshot.get("total_files", 0)
                    reported_total = _positive_int(
                        response_payload.get("total_files"),
                        current_total,
                    )
                    count_complete = bool(
                        response_payload.get("count_complete")
                        or previous_scan_completed
                    )
                    total_files = (
                        reported_total
                        if count_complete
                        else max(current_total, reported_total)
                    )
                    self._counts[root_value] = {
                        "total_files": total_files,
                        "indexed_files": total_files,
                        "count_complete": count_complete,
                    }

                return not directory_scan_needed
            except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
                if attempt == 3:
                    log_message(
                        f"File event report failed for {drive_label(root)}: {error}"
                    )
                    return False

                log_message(
                    f"File event retry {attempt} for {drive_label(root)}: {error}"
                )
                time.sleep(0.2 * attempt)

        return False

    def _run_drive_scan(self, root):
        root_value = drive_value(root)

        with self._lock:
            if root_value in self._active_scan_values:
                self._rescan_after_active_values.add(root_value)
                return False

            self._active_scan_values.add(root_value)

        try:
            self._count_drive_files(root)
            return True
        finally:
            should_rescan = False

            with self._lock:
                self._active_scan_values.discard(root_value)
                should_rescan = root_value in self._rescan_after_active_values
                self._rescan_after_active_values.discard(root_value)

            if should_rescan and not self._stop_event.is_set():
                self._start_priority_scan(root)

    def _count_drive_files(self, root):
        total_files = 0
        root_value = drive_value(root)
        pending_folders = [str(root)]
        file_batch = []
        current_paths = set()
        batch_index = 0
        scan_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        scan_started_timestamp = time.time()
        storage_info = drive_storage(root)
        last_batch_posted_at = time.monotonic()
        known_paths, previous_scan_completed = self._scan_baseline(root_value)

        self._set_count(root_value, total_files, False)

        def post_partial_batch(scan_complete=False):
            nonlocal batch_index
            nonlocal file_batch
            nonlocal last_batch_posted_at

            batch_posted = self._post_files_batch(
                root,
                scan_id,
                batch_index,
                file_batch,
                total_files,
                scan_complete,
                storage_info,
            )
            self._set_count(root_value, total_files, scan_complete and batch_posted)
            batch_index += 1
            file_batch = []
            last_batch_posted_at = time.monotonic()
            return batch_posted

        while pending_folders and not self._stop_event.is_set():
            current_folder = pending_folders.pop()

            try:
                folder_paths = []

                with os.scandir(current_folder) as entries:
                    for entry in entries:
                        try:
                            entry_stat = _visible_entry_stat(entry)

                            if entry_stat is None:
                                continue

                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() not in EXCLUDED_FOLDER_NAMES:
                                    folder_paths.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                metadata = file_metadata(root, entry, entry_stat)

                                if metadata:
                                    path_key = os.path.normcase(
                                        metadata["relative_path"]
                                    ).lower()
                                    current_paths.add(path_key)

                                    if (
                                        previous_scan_completed
                                        and path_key not in known_paths
                                    ):
                                        metadata["freshness_timestamp"] = max(
                                            metadata["freshness_timestamp"],
                                            scan_started_timestamp,
                                        )

                                    total_files += 1
                                    file_batch.append(metadata)

                                next_batch_size = (
                                    min(self.first_batch_size, self.batch_size)
                                    if batch_index == 0
                                    else self.batch_size
                                )
                                should_post_batch = (
                                    file_batch
                                    and (
                                        len(file_batch) >= next_batch_size
                                        or time.monotonic() - last_batch_posted_at
                                        >= self.batch_interval_seconds
                                    )
                                )

                                if should_post_batch:
                                    post_partial_batch(False)

                                if total_files % self.batch_size == 0:
                                    self._set_count(root_value, total_files, False)
                        except OSError:
                            continue

                pending_folders.extend(
                    reversed(
                        sorted(
                            folder_paths,
                            key=lambda folder_path: _folder_scan_key(
                                folder_path,
                                self.priority_folder_names,
                            ),
                        )
                    )
                )
            except OSError:
                continue

        if post_partial_batch(True):
            self._set_known_paths(root_value, current_paths)

    def _run(self):
        while not self._stop_event.is_set():
            roots = ordered_drive_roots(self.drive_priority)

            if self._skip_next_full_scan:
                self._skip_next_full_scan = False
            else:
                for root in roots:
                    if self._stop_event.is_set():
                        break

                    self._run_drive_scan(root)

            next_full_scan_at = time.monotonic() + self.refresh_seconds

            while not self._stop_event.is_set():
                with self._condition:
                    timeout = max(0, next_full_scan_at - time.monotonic())

                    if not self._requested_drive_values and timeout > 0:
                        self._condition.wait(timeout)

                    requested_drive_values = set(self._requested_drive_values)
                    self._requested_drive_values.clear()

                if self._stop_event.is_set():
                    break

                if time.monotonic() >= next_full_scan_at:
                    break

                if not requested_drive_values:
                    continue

                available_roots = {
                    drive_value(root): root
                    for root in ordered_drive_roots(self.drive_priority)
                }

                requested_roots = [
                    available_roots[requested_drive_value]
                    for requested_drive_value in requested_drive_values
                    if requested_drive_value in available_roots
                ]

                for root in sorted(
                    requested_roots,
                    key=lambda requested_root: _drive_sort_key(
                        requested_root,
                        self.drive_priority,
                    ),
                ):
                    if not self._stop_event.is_set():
                        self._run_drive_scan(root)


class DriveChangeWatcher:
    def __init__(self, count_cache, debounce_seconds, drive_priority):
        self.count_cache = count_cache
        self.debounce_seconds = debounce_seconds
        self.drive_priority = drive_priority
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._timers = {}
        self._threads = []

    def start(self):
        if os.name != "nt":
            log_message("Real-time drive watching is only available on Windows.")
            return

        for root in ordered_drive_roots(self.drive_priority):
            watcher_thread = threading.Thread(
                target=self._watch_drive,
                args=(root,),
                daemon=True,
            )
            watcher_thread.start()
            self._threads.append(watcher_thread)

    def stop(self):
        self._stop_event.set()

        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()

        for timer in timers:
            timer.cancel()

    def _queue_debounced_scan(self, root):
        root_value = drive_value(root)

        def request_scan():
            with self._lock:
                self._timers.pop(root_value, None)

            if not self._stop_event.is_set():
                self.count_cache.request_scan(root)

        with self._lock:
            existing_timer = self._timers.get(root_value)

            if existing_timer:
                existing_timer.cancel()

            timer = threading.Timer(self.debounce_seconds, request_scan)
            timer.daemon = True
            self._timers[root_value] = timer
            timer.start()

    def _parse_change_events(self, buffer, byte_count):
        events = []
        offset = 0
        raw_buffer = buffer.raw

        while offset + 12 <= byte_count:
            next_entry_offset = int.from_bytes(
                raw_buffer[offset:offset + 4],
                "little",
            )
            action = int.from_bytes(raw_buffer[offset + 4:offset + 8], "little")
            file_name_length = int.from_bytes(
                raw_buffer[offset + 8:offset + 12],
                "little",
            )
            file_name_start = offset + 12
            file_name_end = file_name_start + file_name_length

            if file_name_end > byte_count:
                break

            file_name = raw_buffer[file_name_start:file_name_end].decode(
                "utf-16-le",
                errors="ignore",
            )
            events.append((action, file_name))

            if not next_entry_offset:
                break

            offset += next_entry_offset

        return events

    def _handle_drive_change_events(self, root, events):
        file_action_added = 1
        file_action_removed = 2
        file_action_modified = 3
        file_action_renamed_old_name = 4
        file_action_renamed_new_name = 5
        changed_paths = []
        deleted_paths = []
        scan_needed = False

        for action, relative_path in events:
            if not relative_path:
                continue

            if action in (file_action_removed, file_action_renamed_old_name):
                deleted_paths.append(relative_path)
                scan_needed = True
                continue

            if action in (
                file_action_added,
                file_action_modified,
                file_action_renamed_new_name,
            ):
                try:
                    changed_path = (root / relative_path).resolve()
                except OSError:
                    scan_needed = True
                    continue

                if changed_path.exists() and changed_path.is_dir():
                    scan_needed = True
                    continue

                changed_paths.append(relative_path)
                continue

            scan_needed = True

        if changed_paths or deleted_paths:
            if not self.count_cache.report_file_events(
                root,
                changed_paths,
                deleted_paths,
            ):
                scan_needed = True

        if scan_needed:
            self._queue_debounced_scan(root)

    def _watch_drive(self, root):
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        file_list_directory = 0x0001
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        notify_filter = (
            0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
            | 0x00000002  # FILE_NOTIFY_CHANGE_DIR_NAME
            | 0x00000008  # FILE_NOTIFY_CHANGE_SIZE
            | 0x00000010  # FILE_NOTIFY_CHANGE_LAST_WRITE
            | 0x00000040  # FILE_NOTIFY_CHANGE_CREATION
        )

        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateFileW(
            str(root),
            file_list_directory,
            file_share_read | file_share_write | file_share_delete,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value

        if handle in (None, invalid_handle):
            log_message(f"Unable to watch {drive_label(root)} for changes.")
            return

        buffer = ctypes.create_string_buffer(64 * 1024)
        bytes_returned = wintypes.DWORD()
        log_message(f"Watching {drive_label(root)} for real-time file changes.")

        try:
            while not self._stop_event.is_set():
                success = kernel32.ReadDirectoryChangesW(
                    handle,
                    buffer,
                    len(buffer),
                    True,
                    notify_filter,
                    ctypes.byref(bytes_returned),
                    None,
                    None,
                )

                if success and bytes_returned.value:
                    events = self._parse_change_events(
                        buffer,
                        bytes_returned.value,
                    )

                    if events:
                        self._handle_drive_change_events(root, events)
                    else:
                        self._queue_debounced_scan(root)
                elif success:
                    self._queue_debounced_scan(root)
                elif not self._stop_event.is_set():
                    error_code = ctypes.get_last_error()
                    log_message(
                        f"Drive watch failed for {drive_label(root)} with error {error_code}."
                    )
                    time.sleep(5)
        finally:
            kernel32.CloseHandle(handle)


def drive_storage(root):
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return {
            "used_display": "Unavailable",
            "total_display": "Unavailable",
            "free_display": "Unavailable",
            "percent_used": 0,
        }

    used_bytes = usage.total - usage.free
    percent_used = round((used_bytes / usage.total) * 100, 1) if usage.total else 0

    return {
        "used_display": format_size(used_bytes),
        "total_display": format_size(usage.total),
        "free_display": format_size(usage.free),
        "percent_used": percent_used,
    }


def collect_payload(count_cache):
    payload = machine_identity()
    drives = []
    drive_priority = count_cache.drive_priority if count_cache else _drive_priority_letters(
        DEFAULT_DRIVE_PRIORITY
    )

    for root in ordered_drive_roots(drive_priority):
        value = drive_value(root)
        count_snapshot = count_cache.snapshot(value) if count_cache else {
            "total_files": 0,
            "count_complete": False,
        }

        drives.append(
            {
                "label": drive_label(root),
                "value": value,
                "total_files": count_snapshot["total_files"],
                "indexed_files": count_snapshot.get("indexed_files", count_snapshot["total_files"]),
                "count_complete": count_snapshot["count_complete"],
                "storage": drive_storage(root),
            }
        )

    payload["drives"] = drives
    return payload


def post_json(url, api_token, payload, return_payload=False):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Agent-Token": api_token,
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        if not return_payload:
            return response.status

        response_body = response.read().decode("utf-8")

        try:
            response_payload = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError:
            response_payload = {}

        return response.status, response_payload


def post_heartbeat(server_url, api_token, payload):
    return post_json(server_url, api_token, payload, return_payload=True)


def send_initial_agent_heartbeat(server_url, api_token):
    try:
        status, _response_payload = post_heartbeat(
            server_url,
            api_token,
            collect_payload(None),
        )
        log_message(
            f"Initial DriveAgent registration sent to {server_url} "
            f"with status {status}."
        )
        return True
    except urllib.error.HTTPError as error:
        log_message(
            f"Initial DriveAgent registration failed with HTTP {error.code}. "
            "This usually means the EXE token does not match Render "
            "DRIVE_AGENT_API_TOKEN."
        )
    except (OSError, urllib.error.URLError) as error:
        log_message(f"Initial DriveAgent registration failed: {error}")

    return False


def report_file_download_failure(server_url, api_token, identity, download_request, error):
    request = urllib.request.Request(
        build_file_download_url(server_url),
        data=b"",
        headers={
            "X-Agent-Token": api_token,
            "X-Agent-Id": identity["agent_id"],
            "X-Download-Request-Id": str(download_request.get("request_id") or ""),
            "X-Download-Status": "failed",
            "X-Download-Error": str(error)[:512],
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def upload_file_download(server_url, api_token, identity, download_request, file_path):
    file_size = file_path.stat().st_size

    with file_path.open("rb") as file_stream:
        request = urllib.request.Request(
            build_file_download_url(server_url),
            data=file_stream,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
                "X-Agent-Token": api_token,
                "X-Agent-Id": identity["agent_id"],
                "X-Download-Request-Id": str(download_request.get("request_id") or ""),
                "X-Download-Status": "ready",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status


def unregister_agent(server_url, api_token):
    payload = machine_identity()
    unregister_url = build_uninstall_url(server_url)

    try:
        status = post_json(unregister_url, api_token, payload)
        log_message(
            f"Uninstall report sent to {unregister_url} with status {status}."
        )
        return True
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
        log_message(f"Uninstall report failed: {error}")
        return False


def handle_requested_file_download(server_url, api_token, count_cache, download_request):
    request_id = str(download_request.get("request_id") or "")
    file_path, error = count_cache.resolve_file_request(
        download_request.get("drive_value"),
        download_request.get("relative_path"),
    )

    try:
        if error:
            status = report_file_download_failure(
                server_url,
                api_token,
                count_cache.identity,
                download_request,
                error,
            )
            log_message(
                f"Reported file download failure for request {request_id} "
                f"with status {status}."
            )
            return

        status = upload_file_download(
            server_url,
            api_token,
            count_cache.identity,
            download_request,
            file_path,
        )
        log_message(
            f"Uploaded requested file for request {request_id} with status {status}."
        )
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as upload_error:
        log_message(
            f"Unable to upload requested file for request {request_id}: {upload_error}"
        )


def queue_requested_file_downloads(server_url, api_token, count_cache, response_payload):
    requested_downloads = response_payload.get("requested_file_downloads", [])

    if not isinstance(requested_downloads, list):
        return

    for download_request in requested_downloads[:8]:
        if not isinstance(download_request, dict):
            continue

        worker = threading.Thread(
            target=handle_requested_file_download,
            args=(server_url, api_token, count_cache, download_request),
            daemon=True,
        )
        worker.start()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Report this PC's drive information to the Drive File Agent dashboard."
    )
    parser.add_argument("--server-url", help="Admin server heartbeat endpoint.")
    parser.add_argument("--api-token", help="Shared agent API token.")
    parser.add_argument("--heartbeat-seconds", type=int, help="Seconds between reports.")
    parser.add_argument("--count-refresh-seconds", type=int, help="Seconds between full file recounts.")
    parser.add_argument("--file-batch-size", type=int, help="Files sent in each metadata batch.")
    parser.add_argument("--first-file-batch-size", type=int, help="Files sent before the first partial metadata report.")
    parser.add_argument("--file-batch-interval-seconds", type=float, help="Maximum seconds to wait before sending a partial metadata report.")
    parser.add_argument("--system-drive-delay-seconds", type=float, help="Seconds to wait before starting the Windows system drive after other drives.")
    parser.add_argument("--change-debounce-seconds", type=int, help="Seconds to wait after file changes before rescanning a drive.")
    parser.add_argument("--drive-priority", help="Comma-separated drive priority, for example D,C,E.")
    parser.add_argument("--priority-folders", help="Comma-separated folder names to scan early on large drives.")
    parser.add_argument("--lan-discovery-enabled", help="Set true only for same-LAN local testing. Public hosted dashboards should keep this false.")
    parser.add_argument("--once", action="store_true", help="Send one report and exit.")
    parser.add_argument("--unregister", action="store_true", help="Remove this PC from the admin dashboard and exit.")
    parser.add_argument("--uninstall", action="store_true", help="Remove this PC from the dashboard, startup, and local install.")
    parser.add_argument("--install", action="store_true", help="Install this executable for the current Windows user and start it.")
    parser.add_argument("--run-agent", action="store_true", help=argparse.SUPPRESS)
    return parser


def main():
    config = load_config()
    args = build_parser().parse_args()
    server_url = args.server_url or _config_value(
        config,
        "server_url",
        SERVER_URL_ENV,
        DEFAULT_SERVER_URL,
    )
    api_token = args.api_token or _config_value(
        config,
        "api_token",
        API_TOKEN_ENV,
        DEFAULT_API_TOKEN,
    )
    lan_discovery_enabled = _boolean_value(
        args.lan_discovery_enabled
        if args.lan_discovery_enabled not in (None, "")
        else _config_value(
            config,
            "lan_discovery_enabled",
            LAN_DISCOVERY_ENABLED_ENV,
            DEFAULT_LAN_DISCOVERY_ENABLED,
        ),
        DEFAULT_LAN_DISCOVERY_ENABLED,
    )
    server_url = discover_dashboard_server(
        server_url,
        lan_discovery_enabled=lan_discovery_enabled,
    )

    if args.unregister or args.uninstall:
        unregister_agent(server_url, api_token)

        if args.uninstall:
            remove_startup_entry()
            remove_uninstall_entry()
            remove_installed_files()
            stop_agent_processes_after_delay()

        return

    if args.install or should_self_install(args):
        if getattr(sys, "frozen", False) and os.name == "nt":
            install_and_start_agent(server_url, api_token)
        else:
            log_message("Install mode is available only in the packaged Windows exe.")

        return

    if args.run_agent:
        stop_other_agent_processes()

    heartbeat_seconds = _positive_int(
        args.heartbeat_seconds
        or _config_value(
            config,
            "heartbeat_seconds",
            HEARTBEAT_SECONDS_ENV,
            DEFAULT_HEARTBEAT_SECONDS,
        ),
        DEFAULT_HEARTBEAT_SECONDS,
    )
    count_refresh_seconds = _positive_int(
        args.count_refresh_seconds
        or _config_value(
            config,
            "count_refresh_seconds",
            COUNT_REFRESH_SECONDS_ENV,
            DEFAULT_COUNT_REFRESH_SECONDS,
        ),
        DEFAULT_COUNT_REFRESH_SECONDS,
    )
    file_batch_size = _positive_int(
        args.file_batch_size
        or _config_value(
            config,
            "file_batch_size",
            FILE_BATCH_SIZE_ENV,
            DEFAULT_FILE_BATCH_SIZE,
        ),
        DEFAULT_FILE_BATCH_SIZE,
    )
    first_file_batch_size = _positive_int(
        args.first_file_batch_size
        or _config_value(
            config,
            "first_file_batch_size",
            FIRST_FILE_BATCH_SIZE_ENV,
            DEFAULT_FIRST_FILE_BATCH_SIZE,
        ),
        DEFAULT_FIRST_FILE_BATCH_SIZE,
    )
    file_batch_interval_seconds = _positive_float(
        args.file_batch_interval_seconds
        or _config_value(
            config,
            "file_batch_interval_seconds",
            FILE_BATCH_INTERVAL_SECONDS_ENV,
            DEFAULT_FILE_BATCH_INTERVAL_SECONDS,
        ),
        DEFAULT_FILE_BATCH_INTERVAL_SECONDS,
    )
    system_drive_delay_seconds = _positive_float(
        args.system_drive_delay_seconds
        or _config_value(
            config,
            "system_drive_delay_seconds",
            SYSTEM_DRIVE_DELAY_SECONDS_ENV,
            DEFAULT_SYSTEM_DRIVE_DELAY_SECONDS,
        ),
        DEFAULT_SYSTEM_DRIVE_DELAY_SECONDS,
    )
    change_debounce_seconds = _positive_int(
        args.change_debounce_seconds
        or _config_value(
            config,
            "change_debounce_seconds",
            CHANGE_DEBOUNCE_SECONDS_ENV,
            DEFAULT_CHANGE_DEBOUNCE_SECONDS,
        ),
        DEFAULT_CHANGE_DEBOUNCE_SECONDS,
    )
    drive_priority = _drive_priority_letters(
        args.drive_priority
        or _config_value(
            config,
            "drive_priority",
            DRIVE_PRIORITY_ENV,
            DEFAULT_DRIVE_PRIORITY,
        ),
        DEFAULT_DRIVE_PRIORITY,
    )
    priority_folder_names = _priority_folder_names(
        args.priority_folders
        or _config_value(
            config,
            "priority_folders",
            PRIORITY_FOLDERS_ENV,
            ",".join(sorted(PRIORITY_FOLDER_NAMES)),
        )
    )
    count_cache = FileCountCache(
        count_refresh_seconds,
        server_url,
        api_token,
        file_batch_size,
        first_file_batch_size,
        file_batch_interval_seconds,
        system_drive_delay_seconds,
        drive_priority,
        priority_folder_names,
    )
    change_watcher = DriveChangeWatcher(
        count_cache,
        change_debounce_seconds,
        drive_priority,
    )
    count_cache.start()
    change_watcher.start()
    log_message(f"DriveAgent started. Reporting to {server_url}.")

    try:
        while True:
            payload = collect_payload(count_cache)

            try:
                status, response_payload = post_heartbeat(server_url, api_token, payload)
                log_message(f"Heartbeat sent to {server_url} with status {status}.")

                if not isinstance(response_payload, dict):
                    response_payload = {}

                requested_drive_values = _split_config_values(
                    response_payload.get("requested_drive_values", [])
                )

                for requested_drive_value in requested_drive_values:
                    count_cache.request_scan_value(requested_drive_value)

                queue_requested_file_downloads(
                    server_url,
                    api_token,
                    count_cache,
                    response_payload,
                )
            except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
                log_message(f"Heartbeat failed: {error}")
                discovered_server_url = discover_dashboard_server(
                    server_url,
                    force_scan=True,
                    lan_discovery_enabled=lan_discovery_enabled,
                )

                if discovered_server_url != server_url:
                    server_url = discovered_server_url
                    count_cache.update_server_url(server_url)
                    log_message(f"DriveAgent switched to {server_url}.")

            if args.once:
                break

            time.sleep(heartbeat_seconds)
    except KeyboardInterrupt:
        log_message("Agent stopped.")
    finally:
        change_watcher.stop()
        count_cache.stop()
        log_message("DriveAgent exited.")


if __name__ == "__main__":
    main()
