# Drive File Agent

A Django admin dashboard for monitoring drive files on the server PC and seeing active user PCs that run the lightweight Drive Agent executable.

The dashboard uses configurable drive defaults. Out of the box it prefers `D:/` and scans it before larger drives such as `C:/`, but you can change that through environment variables, `agent_config.json`, or EXE build parameters. The `Select Drive` control lists available drives for the current data source. The new `Active Users` section is populated by remote user PCs that run `agent_client.py` or its packaged `.exe` and send heartbeat reports to the admin server.

## Project File Structure

```text
Agent/
|-- manage.py
|-- requirements.txt
|-- README.md
|-- Procfile
|-- build.sh
|-- start.sh
|-- build_drive_agent.ps1
|-- render.yaml
|-- runtime.txt
|-- .python-version
|-- agent_client.py
|-- agent_config.example.json
|-- agent_build_defaults.example.py
|-- config/
|   |-- settings.py
|   |-- urls.py
|   |-- asgi.py
|   |-- wsgi.py
|   `-- __init__.py
`-- drivefiles/
    |-- admin.py
    |-- apps.py
    |-- drive_config.py
    |-- forms.py
    |-- models.py
    |-- scanner.py
    |-- tests.py
    |-- urls.py
    |-- views.py
    |-- migrations/
    |   |-- 0001_initial.py
    |   |-- 0002_activeagentdrive_activeagentfile.py
    |   |-- 0003_remotefiledownload_and_more.py
    |   `-- __init__.py
    |-- templates/
    |   `-- drivefiles/
    |       |-- drive_files.html
    |       |-- login.html
    |       `-- _file_rows.html
    `-- static/
        `-- drivefiles/
            `-- css/
                |-- auth.css
                `-- dashboard.css
```

## Frontend

The frontend uses Django templates and external CSS only.

`drivefiles/templates/drivefiles/login.html`
- Login and signup screen.
- Password show/hide eye icon.
- Creative animated left-side design.

`drivefiles/templates/drivefiles/drive_files.html`
- Main admin dashboard.
- Shows file pull status, host name, IP address, MAC address, indexed file table, file type distribution, and storage usage.
- Shows Active Users only in the left sidebar selector.
- Includes one-minute idle logout, independent search bars, drive switching, pagination, filter menu, and Admin logout dropdown.

`drivefiles/templates/drivefiles/_file_rows.html`
- Reusable file table rows.
- Shows file name, path, type, size, and Download action.

`drivefiles/static/drivefiles/css/auth.css`
- Login/signup page styling.

`drivefiles/static/drivefiles/css/dashboard.css`
- Dashboard styling, including the sidebar, cards, charts, file table, drive selector, Admin menu, and Active Users selector.

## Backend

`config/settings.py`
- Django settings.
- Uses SQLite locally by default, and switches to PostgreSQL automatically when `DATABASE_URL` is set.
- Defines `STATIC_ROOT` for deployment static collection.
- Uses WhiteNoise for static serving when it is installed on the deployment server.
- Disables local drive scanning automatically on Render, so the hosted dashboard waits for installed DriveAgent users instead of scanning Render's Linux filesystem.
- Uses signed-cookie sessions.
- Uses a randomized session cookie name so users must log in again after each server restart.
- Reads deployment and agent settings from environment variables.

`drivefiles/models.py`
- `ActiveAgent`: stores every authorized PC that runs the agent executable and reports to the admin dashboard.
- `ActiveAgentDrive`: stores each drive reported by that PC, including totals and storage usage.
- `ActiveAgentFile`: stores uploaded file metadata rows for each reported drive.

`drivefiles/views.py`
- Builds dashboard data.
- Handles selected-drive scanning.
- In hosted mode, automatically shows the single online installed agent as the default dashboard when exactly one PC is reporting.
- Handles real-time file search and pagination.
- Handles secure file downloads.
- Handles the Active Users JSON feed.
- Handles `/agent-heartbeat/`, where installed agents report host, IP, MAC, drives, file counts, and storage usage.
- Handles `/agent-files-batch/`, where installed agents upload file metadata in batches.
- Handles `/agent-file-download/`, where installed agents upload requested file bytes for dashboard downloads.
- Handles `/agent-uninstall/`, where uninstalling agents remove themselves from Active Users.
- Handles `/agent-ping/`, where the hosted platform and user EXE confirm the dashboard API is reachable.
- Handles `/select-agent/`, where the admin switches the dashboard to a selected active user's PC.

Routes:

```text
/                    Dashboard
/login/              Login page
/signup/             Signup page
/logout/             Logout endpoint
/select-drive/       Dashboard drive selector
/files-data/         Live file/search dashboard data
/active-agents-data/ Live Active Users data
/agent-ping/         Agent dashboard health/connectivity API
/agent-heartbeat/    Agent reporting API
/agent-files-batch/ Agent file metadata batch API
/agent-file-download/ Agent requested file upload API
/agent-uninstall/   Agent uninstall/removal API
/select-agent/      Select or clear an Active User dashboard
/download/           File download endpoint
/admin/              Django admin
```

## Local Scanner

`drivefiles/drive_config.py`
- Chooses the default drive.
- Uses `DRIVE_AGENT_DEFAULT_DRIVE` for the default selected drive.
- Uses `DRIVE_AGENT_DRIVE_PRIORITY` to order drives, for example `D,C,E`.
- Discovers all available drives on the server PC.
- Supports startup override with `DRIVE_AGENT_ROOT`.

`drivefiles/scanner.py`
- Scans the selected drive on the server PC.
- Detects new, changed, and deleted files.
- Excludes Recycle Bin and protected system folders.
- Keeps per-drive in-memory cache for faster switching.
- Publishes partial results while large drives are scanning.
- Shows the first files quickly while the rest of a large drive continues scanning in the background.

## User PC Agent

`agent_client.py` is the lightweight reporting agent that is built into one self-installing `.exe`. The user download is kept at `agent_download/DriveAgent.exe`.

When it runs on a user PC, it sends this PC information to the admin server:

- Host name
- IP address
- MAC address
- OS and architecture
- Available drives
- Drive storage usage
- File counts per drive
- File metadata per drive, including file name, path, type, size, and modified time

It does not upload file contents.

The user package can still be a single file because build-time defaults are embedded into the EXE. The admin endpoint is not fixed in source code; it is supplied by the build command, an environment variable, or `agent_config.json`.

```text
https://your-render-service.onrender.com/agent-heartbeat/
```

For the real internet-based setup, build the EXE with the public Render dashboard URL. The user PC does not need to be on the same Wi-Fi or LAN as the admin. It only needs internet access to the dashboard URL and the correct API token.

For development, the same settings can be overridden with environment variables or an optional sidecar `agent_config.json`. LAN discovery is optional and disabled by default. Enable it only for same-network local testing, not for the hosted Render dashboard.

Example `agent_config.json`:

```json
{
  "server_url": "https://your-render-service.onrender.com/agent-heartbeat/",
  "api_token": "drive-agent-local-token",
  "heartbeat_seconds": 1,
  "count_refresh_seconds": 60,
  "file_batch_size": 250,
  "first_file_batch_size": 10,
  "file_batch_interval_seconds": 1,
  "change_debounce_seconds": 1,
  "lan_discovery_enabled": false,
  "drive_priority": "D,C",
  "priority_folders": "Users,Desktop,Documents,Downloads,OneDrive,Pictures"
}
```

Use the same token on the admin server and every user agent.

## Run Admin Dashboard

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe manage.py migrate
```

For local-only testing:

```powershell
.\venv\Scripts\python.exe manage.py runserver
```

Open:

```text
http://127.0.0.1:8000/
```

## Deploy Admin Dashboard

The project now includes these deployment files:

```text
Procfile
runtime.txt
.python-version
build.sh
render.yaml
.gitignore
```

### Render Blueprint Deployment

The fastest Render setup is to use the included `render.yaml`.

1. Push this project to GitHub.
2. In Render, open **Blueprints**.
3. Create a new Blueprint from this repository.
4. Render will create:
   - `drive-agent-dashboard` web service
   - `drive-agent-db` PostgreSQL database
5. Wait for the deploy to finish.
6. Open the generated `.onrender.com` dashboard URL.

The Blueprint uses:

```text
Build Command: bash build.sh
Start Command: bash start.sh
Health Check Path: /agent-ping/
Python Version: 3.12.13
```

### Render Manual Deployment

If you create the services manually:

1. Create a Render PostgreSQL database first.
2. Create a Render Web Service from this repository.
3. Set Runtime/Language to Python.
4. Set Build Command:

```bash
bash build.sh
```

5. Set Start Command:

```bash
bash start.sh
```

6. Set Health Check Path:

```text
/agent-ping/
```

Use these environment variables on the hosting platform:

```text
DJANGO_SECRET_KEY=<long-random-secret>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=<your-dashboard-domain>
DJANGO_CSRF_TRUSTED_ORIGINS=https://<your-dashboard-domain>
DJANGO_SECURE_SSL_REDIRECT=True
DJANGO_SECURE_HSTS_SECONDS=31536000
DRIVE_AGENT_API_TOKEN=<same-secret-token-used-by-the-exe>
DRIVE_AGENT_ENABLE_LOCAL_SCANNER=False
DRIVE_AGENT_ONLINE_SECONDS=3
DRIVE_AGENT_DEFAULT_DRIVE=D:/
DRIVE_AGENT_DRIVE_PRIORITY=D,C
DRIVE_AGENT_FILE_BATCH_SIZE=250
DRIVE_AGENT_FILE_DOWNLOAD_WAIT_SECONDS=20
DRIVE_AGENT_AUTO_SELECT_SINGLE_ACTIVE_AGENT=True
DATABASE_URL=<postgres-database-url>
```

On Render, use the PostgreSQL **internal database URL** for `DATABASE_URL` when the web service and database are in the same Render region.

Render also provides `RENDER_EXTERNAL_HOSTNAME` automatically. The project uses it for `ALLOWED_HOSTS` and CSRF trusted origin defaults, but set `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` manually if you add a custom domain.

Free Render services are useful for testing, but they are not ideal for the final agent dashboard. Free web services can spin down when idle, and free PostgreSQL databases have temporary/limited behavior. For a real dashboard receiving heartbeats and file metadata from installed EXE agents, use a paid web service and paid PostgreSQL database.

Render Blueprint defaults in this repo are free so testing is easy. For production, change these in `render.yaml`:

```text
services[0].plan: starter
databases[0].plan: basic-256mb
```

The app should not use `db.sqlite3` on Render. Render web services have an ephemeral filesystem, so SQLite data can disappear after restart, spin-down, or redeploy.

After deploying, rebuild `DriveAgent.exe` with the public dashboard endpoint:

```text
https://<your-render-service>.onrender.com/agent-heartbeat/
```

SQLite is fine for local testing, but it is not recommended for the online admin dashboard. The DriveAgent EXE can send frequent heartbeats and file batches from multiple PCs, and SQLite can raise `database is locked` under that write load. Many hosting platforms also use temporary filesystems, so a deployed `db.sqlite3` may disappear after restart or redeploy. Use PostgreSQL on the hosted dashboard by setting `DATABASE_URL`.

The old local endpoint, such as `http://192.168.x.x:8000/agent-heartbeat/`, only works on the same LAN and will not work through the internet.

On Render, the dashboard cannot directly read files from your laptop or another user's PC. Render only sees its own cloud server filesystem, so local drive scanning is disabled there by default. To show your own PC as the main dashboard, install the generated `DriveAgent.exe` on your PC. If it is the only online installed agent, it becomes the default dashboard automatically and also appears under `Active Users`. If multiple PCs are online, select the hostname under `Active Users`.

For local development on your own Windows PC, the scanner remains enabled by default. To override this behavior manually:

```text
DRIVE_AGENT_ENABLE_LOCAL_SCANNER=True
DRIVE_AGENT_ENABLE_LOCAL_SCANNER=False
```

For optional same-LAN local testing only, run the server on the network:

```powershell
$env:DRIVE_AGENT_API_TOKEN = "change-this-token"
$env:DJANGO_ALLOWED_HOSTS = "*"
.\venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

Then user agents should use:

```text
http://ADMIN-PC-IP:8000/agent-heartbeat/
```

This local IP mode is not required when the admin dashboard is hosted on Render. For users on different networks, always use the public hosted URL.

## Run User Agent Without EXE

On a user PC:

```powershell
$env:DRIVE_AGENT_SERVER_URL = "https://your-render-service.onrender.com/agent-heartbeat/"
$env:DRIVE_AGENT_API_TOKEN = "change-this-token"
python agent_client.py
```

After the first heartbeat, that PC hostname appears under `Active Users` and in the sidebar under the Active Users button. As the agent scans, it uses the configured drive priority, defaulting to `D,C`, so `D:/` is reported first when available. When the admin selects another reported drive such as `C:/`, the dashboard queues that selected drive for the agent. On the next heartbeat, the EXE receives that request and starts a priority scan for that drive immediately. For large drives, the first batch is sent after the configured first-batch size, defaulting to 10 files, or after the configured interval, defaulting to about 1 second. The remaining files continue uploading in configurable batches, defaulting to 250 files, so the dashboard table appears quickly and the total count increases as scanning continues. Click a hostname/user card to switch the dashboard table, drive selector, storage card, file type distribution, and host/IP/MAC cards to that PC. The Windows agent watches drive changes, so when the user adds, edits, or deletes files, the changed drive is rescanned and the selected dashboard updates when the next batch reaches the server.

When the admin clicks Download for a remote file, the dashboard queues that exact file for the selected agent. On the next heartbeat, the EXE reads the file from the selected drive, uploads it to the dashboard through `/agent-file-download/`, and the browser receives a normal file download. Temporary remote-download files are stored under `remote_downloads/` by default and are ignored by Git.

## Build User Agent EXE

Install PyInstaller:

```powershell
.\venv\Scripts\python.exe -m pip install pyinstaller
```

After the dashboard is deployed, build the one-file user executable with the public dashboard URL and the same `DRIVE_AGENT_API_TOKEN` configured on Render:

```powershell
.\build_drive_agent.ps1 `
    -ServerUrl "https://your-render-service.onrender.com/agent-heartbeat/" `
    -ApiToken "paste-the-same-token-used-in-render" `
    -HeartbeatSeconds 1 `
    -CountRefreshSeconds 60 `
    -FileBatchSize 250 `
    -DrivePriority "D,C" `
    -FirstFileBatchSize 10 `
    -FileBatchIntervalSeconds 1 `
    -ChangeDebounceSeconds 1 `
    -LanDiscoveryEnabled $false
```

The script creates this file:

```text
agent_download/DriveAgent.exe
```

It also creates `agent_build_defaults.py` locally so the one-file EXE has your Render endpoint and token built in. That file is ignored by Git and must not be pushed to GitHub.

Copy only this file to the user PC:

```text
agent_download/DriveAgent.exe
```

The user PC does not need to run Django. When the user double-clicks `DriveAgent.exe`, a terminal opens briefly, the EXE copies itself to `%LOCALAPPDATA%\DriveAgent\DriveAgent.exe`, registers itself in Windows startup for the current user, starts the background agent, and then closes. The background agent starts scanning and reports that PC to `Active Users`. Future Windows startup launches are hidden and do not leave a terminal open.

The installed EXE does not create `agent.log` or any text file by default. For debugging only, set `DRIVE_AGENT_LOG_FILE=1` or set `DRIVE_AGENT_LOG_FILE` to a full log path before running the agent.

The user download folder intentionally contains only:

```text
agent_download/DriveAgent.exe
```

To remove the installed agent from a user PC, run this on that user PC:

```powershell
%LOCALAPPDATA%\DriveAgent\DriveAgent.exe --uninstall
```

If the admin dashboard is reachable during uninstall, the PC is removed from `Active Users`.

## Select Or Change Drive

Use the dashboard `Select Drive` control to switch the admin dashboard between available drives on the server PC. Files, storage usage, file type distribution, totals, and labels update for the selected drive.

For startup override:

```powershell
$env:DRIVE_AGENT_ROOT = "<drive-letter>:/"
.\venv\Scripts\python.exe manage.py runserver
```

Examples:

```powershell
$env:DRIVE_AGENT_ROOT = "C:/"
$env:DRIVE_AGENT_ROOT = "D:/Projects"
$env:DRIVE_AGENT_ROOT = "E:/"
```

## Main Features

- Login and signup pages.
- Password show/hide eye icon.
- Login required for dashboard access.
- Forced login again after server restart.
- Auto logout after 1 minute of no mouse, keyboard, scroll, touch, or pointer activity.
- Real-time file pull from the selected server drive.
- Default selected drive is configurable with `DRIVE_AGENT_DEFAULT_DRIVE`.
- Drive priority is configurable with `DRIVE_AGENT_DRIVE_PRIORITY`.
- Select any available drive on the server PC.
- Switching to a large drive shows initial rows quickly while the remaining file count grows in the background.
- Genuine file type distribution from scanned files.
- Genuine storage usage from the selected drive.
- New files appear near the top.
- Deleted files reduce the file count.
- Recycle/system folders are ignored.
- Independent real-time search bars.
- Download action for indexed files.
- Local and active-user remote files are downloadable from the dashboard.
- Active Users section for installed/running user agents.
- Hostnames appear only under the left sidebar Active Users option.
- In hosted mode, a single installed/running PC becomes the default dashboard automatically.
- Click an Active User hostname to view that user's reported drives and files.
- Reported PC default drive follows the configured drive priority.
- Select a reported drive for the selected Active User and the file table, storage card, file type distribution, totals, and host/IP/MAC area update to that PC.
- User agents send heartbeats every 1 second by default, watch Windows drive changes, and report changed drive data after additions, edits, and deletions.
- Agents are treated as offline after 3 seconds without a heartbeat, so Active Users updates quickly without flickering on one delayed heartbeat.
- User agents scan drives in the configured priority order, and selected remote drives are requested through heartbeat so the first batch appears quickly while the remaining drive data streams.
- Uninstalling the DriveAgent user package removes that PC from Active Users.
- Agent heartbeat API protected by a shared token.

## Sending The Project ZIP

Do not zip the full folder with `venv/`, `.git/`, cache files, database files, or server logs. Those files make the ZIP large, and `venv/` contains executable files that email or WhatsApp can block for security.

Send only the source files:

```text
manage.py
requirements.txt
README.md
agent_client.py
agent_config.example.json
config/
drivefiles/
```

Exclude:

```text
venv/
.git/
.agents/
__pycache__/
*.pyc
db.sqlite3
runserver.out
runserver.err
*.zip
dist/
build/
*.spec
```

After receiving the ZIP on another PC, create a fresh virtual environment and install dependencies again.

## Tests

```powershell
.\venv\Scripts\python.exe manage.py test drivefiles
.\venv\Scripts\python.exe manage.py check
```

## Requirements

```text
asgiref==3.12.1
Django==6.0.7
dj-database-url>=2.2,<4
gunicorn>=23,<24
psycopg[binary]>=3.2,<4
sqlparse==0.5.5
tzdata==2026.3
whitenoise>=6.7,<7
```
