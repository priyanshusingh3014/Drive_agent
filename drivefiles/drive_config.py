import os
import string
from pathlib import Path


DRIVE_AGENT_ROOT_ENV = "DRIVE_AGENT_ROOT"
DEFAULT_DRIVE_ROOT_ENV = "DRIVE_AGENT_DEFAULT_DRIVE"
DRIVE_PRIORITY_ENV = "DRIVE_AGENT_DRIVE_PRIORITY"
DEFAULT_DRIVE_ROOT = os.environ.get(DEFAULT_DRIVE_ROOT_ENV, "D:/").strip() or "D:/"
DEFAULT_DRIVE_PRIORITY = os.environ.get(DRIVE_PRIORITY_ENV, "D").strip() or "D"


def _fallback_drive_root():
    return Path(Path.cwd().anchor or Path.home().anchor or "/")


def _discover_drive_roots():
    if os.name == "nt":
        try:
            import ctypes

            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except (AttributeError, OSError):
            drive_mask = 0

        return tuple(
            Path(f"{letter}:/")
            for index, letter in enumerate(string.ascii_uppercase)
            if drive_mask & (1 << index)
        )

    return (Path("/"),)


def _drive_letter(value):
    value = str(value).replace("\\", "/").strip().upper()

    if len(value) >= 2 and value[1] == ":":
        return value[0]

    if len(value) == 1 and value in string.ascii_uppercase:
        return value

    return ""


def _system_drive_letter():
    return _drive_letter(os.environ.get("SystemDrive") or "C:")


def get_preferred_drive_letters():
    letters = []
    seen_letters = set()

    for raw_part in DEFAULT_DRIVE_PRIORITY.replace(";", ",").split(","):
        letter = _drive_letter(raw_part)

        if letter and letter not in seen_letters:
            seen_letters.add(letter)
            letters.append(letter)

    default_letter = _drive_letter(DEFAULT_DRIVE_ROOT)

    if default_letter and default_letter not in seen_letters:
        letters.insert(0, default_letter)

    return tuple(letters)


def drive_sort_key_for_value(value):
    letter = _drive_letter(value)
    system_drive_last_index = 1 if letter and letter == _system_drive_letter() else 0
    preferred_letters = get_preferred_drive_letters()
    preferred_index = (
        preferred_letters.index(letter)
        if letter in preferred_letters
        else len(preferred_letters)
    )

    return (system_drive_last_index, preferred_index, str(value).lower())


def _drive_sort_key(path):
    return drive_sort_key_for_value(path)


def normalize_drive_root(root):
    raw_root = str(root or "").strip()

    if not raw_root:
        return _fallback_drive_root()

    if os.name == "nt" and len(raw_root) == 2 and raw_root[1] == ":":
        raw_root = f"{raw_root}/"

    return Path(raw_root).expanduser()


def _configured_drive_root():
    configured_root = os.environ.get(DRIVE_AGENT_ROOT_ENV, DEFAULT_DRIVE_ROOT).strip()

    if configured_root:
        configured_drive_root = normalize_drive_root(configured_root)

        if (
            DRIVE_AGENT_ROOT_ENV in os.environ
            or configured_drive_root.exists()
        ):
            return configured_drive_root

    available_roots = _discover_drive_roots()

    if available_roots:
        return available_roots[0]

    return _fallback_drive_root()


def format_drive_label(path):
    label = str(path).replace("/", "\\")

    if label.endswith(":"):
        return f"{label}\\"

    if not label.endswith("\\"):
        return f"{label}\\"

    return label


def format_drive_initial(path):
    drive = path.drive.rstrip(":").upper()

    if drive:
        return drive[:1]

    label = format_drive_label(path).strip("\\/")
    return (label[:1] or "F").upper()


def format_drive_name(path):
    drive = path.drive.rstrip(":").upper()

    if drive:
        return f"{drive[:1]} Drive"

    label = format_drive_label(path).strip("\\/")
    return label or "File"


def drive_root_to_value(path):
    value = str(path).replace("\\", "/")

    if value.endswith(":"):
        value = f"{value}/"

    return value


def build_drive_context(path):
    drive_root = normalize_drive_root(path)
    drive_name = format_drive_name(drive_root)

    return {
        "app_name": f"{drive_name} File Agent",
        "dashboard_title": f"{drive_name} Agent Dashboard",
        "drive_initial": format_drive_initial(drive_root),
        "drive_label": format_drive_label(drive_root),
        "drive_name": drive_name,
    }


def get_available_drive_roots():
    drive_roots = list(_discover_drive_roots())
    configured_root = DRIVE_ROOT
    configured_value = os.path.normcase(os.path.normpath(str(configured_root)))
    discovered_values = {
        os.path.normcase(os.path.normpath(str(path))) for path in drive_roots
    }

    if configured_value not in discovered_values and configured_root.exists():
        drive_roots.append(configured_root)

    if not drive_roots:
        drive_roots = [configured_root]

    return tuple(sorted(drive_roots, key=_drive_sort_key))


def get_drive_options(selected_root):
    selected_value = drive_root_to_value(normalize_drive_root(selected_root))

    return [
        {
            "label": format_drive_label(root),
            "name": format_drive_name(root),
            "selected": drive_root_to_value(root) == selected_value,
            "value": drive_root_to_value(root),
        }
        for root in get_available_drive_roots()
    ]


DRIVE_ROOT = _configured_drive_root()
DRIVE_LABEL = format_drive_label(DRIVE_ROOT)
DRIVE_INITIAL = format_drive_initial(DRIVE_ROOT)
DRIVE_NAME = format_drive_name(DRIVE_ROOT)
APP_NAME = f"{DRIVE_NAME} File Agent"
DASHBOARD_TITLE = f"{DRIVE_NAME} Agent Dashboard"
DRIVE_CONTEXT = build_drive_context(DRIVE_ROOT)
