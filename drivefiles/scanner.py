import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .drive_config import (
    DRIVE_LABEL,
    DRIVE_ROOT,
    format_drive_label,
    normalize_drive_root,
)

MAX_DISPLAYED_FILES = None
FALLBACK_SCAN_INTERVAL_SECONDS = 30
REALTIME_DEBOUNCE_SECONDS = 1.0
PARTIAL_SCAN_BATCH_SIZE = 1000
PARTIAL_SCAN_INTERVAL_SECONDS = 1.0
EXCLUDED_FOLDER_NAMES = {
    "$recycle.bin",
    "recycled",
    "recycler",
    "system volume information",
}
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

_user_folder_name = Path.home().name.lower()
_priority_folder_names = frozenset(
    folder_name
    for folder_name in (*PRIORITY_FOLDER_NAMES, _user_folder_name)
    if folder_name
)

_state_lock = threading.RLock()
_scanner_started = False
_scanner_thread = None
_watcher_thread = None
_started_watcher_generations = set()
_scan_requested_event = threading.Event()
_indexed_paths = set()
_scan_has_completed = False
_drive_root = DRIVE_ROOT
_drive_label = DRIVE_LABEL
_drive_generation = 0


def _empty_state(version=0, is_scanning=False):
    return {
        "files": (),
        "content_signature": (),
        "error_message": None,
        "is_scanning": is_scanning,
        "last_scanned": None,
        "last_scanned_display": "Not scanned yet",
        "next_scan_at": None,
        "next_scan_display": "Starting soon",
        "version": version,
    }


_state = _empty_state()
_drive_state_cache = {}


def _drive_state_key(path):
    return os.path.normcase(os.path.normpath(str(normalize_drive_root(path))))


def _copy_state_for_cache(state, is_scanning=False):
    cached_state = dict(state)
    cached_state["is_scanning"] = is_scanning
    return cached_state


def _cache_current_drive_state(is_scanning=False):
    _drive_state_cache[_drive_state_key(_drive_root)] = {
        "state": _copy_state_for_cache(_state, is_scanning=is_scanning),
        "indexed_paths": set(_indexed_paths),
        "scan_has_completed": _scan_has_completed,
    }


def _same_drive_root(left, right):
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def _is_current_drive_generation(drive_generation):
    with _state_lock:
        return drive_generation == _drive_generation


def get_current_drive_root():
    with _state_lock:
        return _drive_root


def get_current_drive_label():
    with _state_lock:
        return _drive_label


def set_drive_root(drive_root):
    global _state
    global _drive_root
    global _drive_label
    global _drive_generation
    global _indexed_paths
    global _scan_has_completed

    new_drive_root = normalize_drive_root(drive_root)
    new_drive_label = format_drive_label(new_drive_root)

    with _state_lock:
        if _same_drive_root(_drive_root, new_drive_root):
            return False

        was_scanning = _state["is_scanning"]
        next_version = _state["version"] + 1
        new_drive_key = _drive_state_key(new_drive_root)
        cached_drive_state = _drive_state_cache.get(new_drive_key)

        _cache_current_drive_state(is_scanning=False)
        _drive_root = new_drive_root
        _drive_label = new_drive_label
        _drive_generation += 1

        if cached_drive_state:
            _state = dict(cached_drive_state["state"])
            _state["is_scanning"] = was_scanning
            _state["version"] = max(_state["version"] + 1, next_version)
            _indexed_paths = set(cached_drive_state["indexed_paths"])
            _scan_has_completed = cached_drive_state["scan_has_completed"]
        else:
            _indexed_paths = set()
            _scan_has_completed = False
            _state = _empty_state(version=next_version, is_scanning=was_scanning)

        scanner_started = _scanner_started

    if scanner_started:
        _start_watcher_for_current_drive()

    _request_scan()
    return True


def _get_file_type_metadata(extension):
    normalized_extension = extension.lower()

    if normalized_extension in PDF_EXTENSIONS:
        return {"badge": "PDF", "class": "pdf", "label": "PDF"}

    if normalized_extension in DOCUMENT_EXTENSIONS:
        return {"badge": "W", "class": "document", "label": normalized_extension[1:].upper()}

    if normalized_extension in TEXT_EXTENSIONS:
        return {"badge": "TXT", "class": "document", "label": normalized_extension[1:].upper()}

    if normalized_extension in PRESENTATION_EXTENSIONS:
        return {"badge": "PPT", "class": "document", "label": normalized_extension[1:].upper()}

    if normalized_extension in SPREADSHEET_EXTENSIONS:
        return {"badge": "X", "class": "spreadsheet", "label": normalized_extension[1:].upper()}

    if normalized_extension in IMAGE_EXTENSIONS:
        return {"badge": "IMG", "class": "image", "label": normalized_extension[1:].upper()}

    if normalized_extension in VIDEO_EXTENSIONS:
        return {"badge": "VID", "class": "video", "label": normalized_extension[1:].upper()}

    if normalized_extension in ARCHIVE_EXTENSIONS:
        return {"badge": "ZIP", "class": "archive", "label": normalized_extension[1:].upper()}

    if extension == "No extension":
        return {"badge": "FILE", "class": "other", "label": "File"}

    return {"badge": normalized_extension[1:4].upper(), "class": "other", "label": normalized_extension[1:].upper()}


def _is_excluded_drive_path(path):
    return any(part.lower() in EXCLUDED_FOLDER_NAMES for part in path.parts)


def _folder_scan_key(folder_name):
    normalized_name = folder_name.lower()

    return (
        0 if normalized_name in _priority_folder_names else 1,
        normalized_name,
    )


def format_size(size_in_bytes):
    """
    Converts bytes into KB, MB or GB.
    """

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


def _display_time(value):
    return value.strftime("%d-%m-%Y %I:%M:%S %p")


def _sort_files_by_freshness(files):
    return tuple(
        sorted(
            files,
            key=lambda file_information: file_information["freshness_timestamp"],
            reverse=True,
        )
    )


def _build_content_signature(files):
    return tuple(
        sorted(
            (
                file_information["path_key"],
                file_information["size_bytes"],
                file_information["modified_timestamp"],
            )
            for file_information in files
        )
    )


def _publish_partial_scan(drive_root, drive_generation, files, errors):
    sorted_files = _sort_files_by_freshness(files)

    with _state_lock:
        if drive_generation != _drive_generation or not _same_drive_root(
            drive_root,
            _drive_root,
        ):
            return False

        _state.update(
            {
                "files": sorted_files,
                "content_signature": _build_content_signature(sorted_files),
                "error_message": errors[0] if errors and not files else None,
                "is_scanning": True,
                "version": _state["version"] + 1,
            }
        )

    return True


def _collect_files(
    drive_root,
    drive_label,
    known_paths,
    scan_has_completed,
    drive_generation,
):
    files = []
    errors = []
    current_paths = set()
    scan_started_timestamp = time.time()
    last_partial_publish_at = time.monotonic()
    files_since_partial_publish = 0

    if not drive_root.exists():
        return {
            "files": (),
            "content_signature": (),
            "error_message": f"{drive_label} was not found.",
            "indexed_paths": current_paths,
        }

    def handle_walk_error(error):
        errors.append(str(error))

    for folder_path, subfolders, filenames in os.walk(
        drive_root,
        onerror=handle_walk_error,
    ):
        if not _is_current_drive_generation(drive_generation):
            return {
                "files": (),
                "error_message": None,
                "indexed_paths": current_paths,
                "aborted": True,
            }

        subfolders[:] = sorted(
            [
                subfolder
                for subfolder in subfolders
                if subfolder.lower() not in EXCLUDED_FOLDER_NAMES
            ],
            key=_folder_scan_key,
        )

        for filename in filenames:
            if not _is_current_drive_generation(drive_generation):
                return {
                    "files": (),
                    "error_message": None,
                    "indexed_paths": current_paths,
                    "aborted": True,
                }

            full_path = Path(folder_path) / filename

            try:
                file_information = full_path.stat()
                relative_path = full_path.relative_to(drive_root)
                extension = full_path.suffix or "No extension"
                type_metadata = _get_file_type_metadata(extension)
                path_key = os.path.normcase(str(full_path))
                current_paths.add(path_key)
                freshness_timestamp = max(
                    file_information.st_ctime,
                    file_information.st_mtime,
                )

                if scan_has_completed and path_key not in known_paths:
                    freshness_timestamp = scan_started_timestamp

                files.append(
                    {
                        "name": filename,
                        "path_key": path_key,
                        "full_path": str(full_path),
                        "relative_path": str(relative_path),
                        "folder": str(full_path.parent),
                        "size": format_size(file_information.st_size),
                        "size_bytes": file_information.st_size,
                        "extension": extension,
                        "type_badge": type_metadata["badge"],
                        "type_class": type_metadata["class"],
                        "type_label": type_metadata["label"],
                        "modified": datetime.fromtimestamp(
                            file_information.st_mtime
                        ).strftime("%d-%m-%Y %I:%M %p"),
                        "modified_timestamp": file_information.st_mtime,
                        "freshness_timestamp": freshness_timestamp,
                    }
                )
                files_since_partial_publish += 1

                should_publish_partial = (
                    not scan_has_completed
                    and (
                        len(files) == 1
                        or files_since_partial_publish >= PARTIAL_SCAN_BATCH_SIZE
                        or time.monotonic() - last_partial_publish_at
                        >= PARTIAL_SCAN_INTERVAL_SECONDS
                    )
                )

                if should_publish_partial:
                    if not _publish_partial_scan(
                        drive_root,
                        drive_generation,
                        files,
                        errors,
                    ):
                        return {
                            "files": (),
                            "error_message": None,
                            "indexed_paths": current_paths,
                            "aborted": True,
                        }

                    files_since_partial_publish = 0
                    last_partial_publish_at = time.monotonic()

            except (PermissionError, FileNotFoundError, OSError):
                continue

    sorted_files = _sort_files_by_freshness(files)

    return {
        "files": sorted_files,
        "content_signature": _build_content_signature(sorted_files),
        "error_message": errors[0] if errors and not files else None,
        "indexed_paths": current_paths,
        "aborted": False,
    }


def scan_drive():
    global _indexed_paths
    global _scan_has_completed

    with _state_lock:
        if _state["is_scanning"]:
            return

        _state["is_scanning"] = True
        known_paths = set(_indexed_paths)
        scan_has_completed = _scan_has_completed
        drive_root = _drive_root
        drive_label = _drive_label
        drive_generation = _drive_generation

    try:
        snapshot = _collect_files(
            drive_root,
            drive_label,
            known_paths,
            scan_has_completed,
            drive_generation,
        )
    except OSError as error:
        with _state_lock:
            snapshot = {
                "files": _state["files"],
                "content_signature": _state.get("content_signature", ()),
                "error_message": str(error),
                "indexed_paths": _indexed_paths,
            }

    now = datetime.now()
    next_scan_at = now + timedelta(seconds=FALLBACK_SCAN_INTERVAL_SECONDS)
    indexed_paths = snapshot.pop("indexed_paths")
    was_aborted = snapshot.pop("aborted", False)

    with _state_lock:
        if was_aborted or drive_generation != _drive_generation or not _same_drive_root(
            drive_root,
            _drive_root,
        ):
            _state["is_scanning"] = False
            _request_scan()
            return

        _indexed_paths = indexed_paths
        previous_scan_has_completed = _scan_has_completed
        previous_content_signature = _state.get("content_signature", ())
        previous_error_message = _state["error_message"]
        next_content_signature = snapshot.get("content_signature", ())
        next_error_message = snapshot.get("error_message")
        should_publish_final_snapshot = (
            not previous_scan_has_completed
            or next_content_signature != previous_content_signature
            or next_error_message != previous_error_message
        )

        _scan_has_completed = True

        if should_publish_final_snapshot:
            _state.update(snapshot)

        _state.update(
            {
                "is_scanning": False,
                "last_scanned": now,
                "last_scanned_display": _display_time(now),
                "next_scan_at": next_scan_at,
                "next_scan_display": _display_time(next_scan_at),
            }
        )

        if should_publish_final_snapshot:
            _state["version"] = _state["version"] + 1

        _cache_current_drive_state(is_scanning=False)


def _scanner_loop():
    while True:
        scan_drive()

        was_requested = _scan_requested_event.wait(FALLBACK_SCAN_INTERVAL_SECONDS)

        if was_requested:
            time.sleep(REALTIME_DEBOUNCE_SECONDS)
            _scan_requested_event.clear()


def _request_scan():
    _scan_requested_event.set()


def request_scan():
    start_background_scanner()
    _request_scan()


def _watch_drive_changes(drive_root, drive_generation):
    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    file_list_directory = 0x0001
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    notify_filter = (
        0x00000001
        | 0x00000002
        | 0x00000008
        | 0x00000010
        | 0x00000040
    )
    invalid_handle_value = ctypes.c_void_p(-1).value

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

    while True:
        with _state_lock:
            if drive_generation != _drive_generation:
                return

        if not drive_root.exists():
            time.sleep(5)
            continue

        handle = kernel32.CreateFileW(
            str(drive_root),
            file_list_directory,
            file_share_read | file_share_write | file_share_delete,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )

        if handle == invalid_handle_value:
            time.sleep(5)
            continue

        try:
            buffer = ctypes.create_string_buffer(64 * 1024)
            bytes_returned = wintypes.DWORD()

            while True:
                changed = kernel32.ReadDirectoryChangesW(
                    handle,
                    buffer,
                    len(buffer),
                    True,
                    notify_filter,
                    ctypes.byref(bytes_returned),
                    None,
                    None,
                )

                if not changed:
                    break

                if bytes_returned.value:
                    with _state_lock:
                        if drive_generation != _drive_generation:
                            return

                    _request_scan()
        finally:
            kernel32.CloseHandle(handle)
            time.sleep(2)


def _start_watcher_for_current_drive():
    global _watcher_thread

    if os.name != "nt":
        return

    with _state_lock:
        drive_generation = _drive_generation

        if drive_generation in _started_watcher_generations:
            return

        drive_root = _drive_root
        _started_watcher_generations.add(drive_generation)

    watcher_thread = threading.Thread(
        target=_watch_drive_changes,
        args=(drive_root, drive_generation),
        name=f"drivefiles-watcher-{drive_generation}",
        daemon=True,
    )
    watcher_thread.start()

    with _state_lock:
        _watcher_thread = watcher_thread


def start_background_scanner():
    global _scanner_started
    global _scanner_thread

    scanner_thread = None

    with _state_lock:
        if not _scanner_started:
            _scanner_started = True
            scanner_thread = threading.Thread(
                target=_scanner_loop,
                name="drivefiles-scanner",
                daemon=True,
            )
            _scanner_thread = scanner_thread

    if scanner_thread:
        scanner_thread.start()

    _start_watcher_for_current_drive()


def get_scan_metadata():
    with _state_lock:
        return {
            "drive_label": _drive_label,
            "drive_root": _drive_root,
            "error_message": _state["error_message"],
            "is_scanning": _state["is_scanning"],
            "last_scanned": _state["last_scanned"],
            "last_scanned_display": _state["last_scanned_display"],
            "next_scan_at": _state["next_scan_at"],
            "next_scan_display": _state["next_scan_display"],
            "version": _state["version"],
        }


def get_file_snapshot(search_query=""):
    normalized_query = search_query.strip().lower()
    search_terms = [term for term in normalized_query.split() if term]

    with _state_lock:
        files = _state["files"]
        snapshot = get_scan_metadata()

    if search_terms:
        files = [
            file_information
            for file_information in files
            if all(term in " ".join(
                (
                    file_information["name"],
                    file_information["folder"],
                    file_information["full_path"],
                    file_information["extension"],
                    file_information["type_label"],
                )
            ).lower() for term in search_terms)
        ]

    snapshot["maximum_reached"] = (
        MAX_DISPLAYED_FILES is not None and len(files) > MAX_DISPLAYED_FILES
    )
    snapshot["files"] = files if MAX_DISPLAYED_FILES is None else files[:MAX_DISPLAYED_FILES]
    return snapshot
