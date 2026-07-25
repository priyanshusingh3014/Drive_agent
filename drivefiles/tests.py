import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from .models import (
    ActiveAgent,
    ActiveAgentDrive,
    ActiveAgentFile,
    RemoteFileDownload,
)
from . import scanner
from .views import SELECTED_AGENT_SESSION_KEY


TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


class ScannerTests(SimpleTestCase):
    def setUp(self):
        self.first_temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.second_temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.first_root = Path(self.first_temp_dir.name)
        self.second_root = Path(self.second_temp_dir.name)

    def tearDown(self):
        self.first_temp_dir.cleanup()
        self.second_temp_dir.cleanup()

    def test_scan_uses_selected_drive_root(self):
        (self.first_root / "selected-drive-file.txt").write_text(
            "indexed from selected root"
        )

        scanner.set_drive_root(self.first_root)
        scanner.scan_drive()

        snapshot = scanner.get_file_snapshot("selected-drive")
        file_names = {file_information["name"] for file_information in snapshot["files"]}

        self.assertEqual(file_names, {"selected-drive-file.txt"})

    def test_scan_skips_hidden_dot_files_and_folders(self):
        (self.first_root / "visible-file.txt").write_text("visible")
        (self.first_root / ".hidden-file.txt").write_text("hidden")
        hidden_folder = self.first_root / ".hidden-folder"
        hidden_folder.mkdir()
        (hidden_folder / "nested-hidden-file.txt").write_text("nested")

        scanner.set_drive_root(self.first_root)
        scanner.scan_drive()

        snapshot = scanner.get_file_snapshot("")
        file_names = {file_information["name"] for file_information in snapshot["files"]}

        self.assertEqual(file_names, {"visible-file.txt"})

    def test_switching_back_to_drive_restores_cached_files(self):
        (self.first_root / "first-drive.txt").write_text("first")
        (self.second_root / "second-drive.pdf").write_text("second")

        scanner.set_drive_root(self.first_root)
        scanner.scan_drive()
        scanner.set_drive_root(self.second_root)
        scanner.scan_drive()
        scanner.set_drive_root(self.first_root)

        snapshot = scanner.get_file_snapshot("")
        file_names = {file_information["name"] for file_information in snapshot["files"]}

        self.assertIn("first-drive.txt", file_names)
        self.assertNotIn("second-drive.pdf", file_names)

    def test_repeat_scan_without_file_changes_keeps_same_version(self):
        (self.first_root / "stable-file.txt").write_text("stable")

        scanner.set_drive_root(self.first_root)
        scanner.scan_drive()
        first_version = scanner.get_file_snapshot("")["version"]
        scanner.scan_drive()
        second_version = scanner.get_file_snapshot("")["version"]

        self.assertEqual(first_version, second_version)

    def test_scan_version_changes_when_file_is_added(self):
        (self.first_root / "before.txt").write_text("before")

        scanner.set_drive_root(self.first_root)
        scanner.scan_drive()
        first_version = scanner.get_file_snapshot("")["version"]

        (self.first_root / "after.txt").write_text("after")
        scanner.scan_drive()
        second_snapshot = scanner.get_file_snapshot("")
        file_names = {
            file_information["name"]
            for file_information in second_snapshot["files"]
        }

        self.assertGreater(second_snapshot["version"], first_version)
        self.assertIn("after.txt", file_names)


class AuthFlowTests(TestCase):
    def test_signup_redirects_to_login_without_authenticating_user(self):
        response = self.client.post(
            "/signup/",
            data={
                "username": "new-admin",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertRedirects(response, "/login/", fetch_redirect_response=False)
        self.assertTrue(
            get_user_model().objects.filter(username="new-admin").exists()
        )
        self.assertNotIn("_auth_user_id", self.client.session)

        dashboard_response = self.client.get("/")
        self.assertEqual(dashboard_response.status_code, 302)
        self.assertIn("/login/", dashboard_response["Location"])


@override_settings(AGENT_API_TOKEN="test-token")
class ActiveAgentHeartbeatTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_agent_heartbeat_requires_token(self):
        response = self.client.post(
            "/agent-heartbeat/",
            data={"agent_id": "pc-1"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(ActiveAgent.objects.exists())

    def test_agent_ping_identifies_dashboard_server(self):
        response = self.client.get("/agent-ping/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["app"], "drive-agent-dashboard")
        self.assertEqual(data["heartbeat_path"], "/agent-heartbeat/")

    def test_agent_heartbeat_creates_active_agent(self):
        response = self.client.post(
            "/agent-heartbeat/",
            data={
                "agent_id": "desktop-priyanshu-a45e",
                "host_name": "DESKTOP-PRIYANSHU",
                "ip_address": "192.168.1.24",
                "mac_address": "A4:5E:60:8B:21:E3",
                "os_label": "Windows 11",
                "architecture": "AMD64",
                "drives": [
                    {
                        "label": "C:\\",
                        "value": "C:/",
                        "total_files": 12,
                        "storage": {
                            "used_display": "50 GB",
                            "total_display": "100 GB",
                            "free_display": "50 GB",
                            "percent_used": 50.5,
                        },
                    },
                    {
                        "label": "D:\\",
                        "value": "D:/",
                        "total_files": 8,
                        "storage": {
                            "used_display": "20 GB",
                            "total_display": "80 GB",
                            "free_display": "60 GB",
                            "percent_used": 25,
                        },
                    },
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(response.status_code, 200)
        agent = ActiveAgent.objects.get(agent_id="desktop-priyanshu-a45e")
        self.assertEqual(agent.host_name, "DESKTOP-PRIYANSHU")
        self.assertEqual(agent.drive_count, 2)
        self.assertEqual(agent.total_files, 20)
        self.assertEqual(agent.drive_reports.count(), 2)

    def test_active_agents_data_requires_login_and_returns_agents(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="pc-2",
            host_name="OFFICE-PC",
            ip_address="192.168.1.55",
            mac_address="00:11:22:33:44:55",
            drive_count=1,
            total_files=5,
            latest_payload={
                "drives": [
                    {
                        "label": "D:\\",
                        "value": "D:/",
                        "total_files": 5,
                        "storage": {
                            "used_display": "10 GB",
                            "total_display": "20 GB",
                            "free_display": "10 GB",
                            "percent_used": 50,
                        },
                    }
                ]
            },
        )

        anonymous_response = self.client.get("/active-agents-data/")
        self.assertEqual(anonymous_response.status_code, 302)

        self.client.force_login(user)
        response = self.client.get("/active-agents-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["total_agents"], 1)
        self.assertEqual(data["online_agents"], 1)
        self.assertEqual(data["agents"][0]["host_name"], "OFFICE-PC")
        self.assertEqual(data["agents"][0]["drives"][0]["total_files_display"], "5")

    def test_active_agents_data_keeps_delayed_agents_visible(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="old-pc",
            host_name="OLD-PC",
            ip_address="192.168.1.44",
        )
        ActiveAgent.objects.filter(agent_id="old-pc").update(
            last_seen_at=timezone.now() - timedelta(minutes=10),
        )

        self.client.force_login(user)
        response = self.client.get("/active-agents-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["total_agents"], 1)
        self.assertEqual(data["online_agents"], 0)
        self.assertEqual(data["agents"][0]["host_name"], "OLD-PC")
        self.assertFalse(data["agents"][0]["is_online"])

    def test_agent_heartbeat_does_not_select_remote_user_in_sidebar_feed(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )

        self.client.force_login(user)
        response = self.client.post(
            "/agent-heartbeat/",
            data={
                "agent_id": "new-pc",
                "host_name": "NEW-PC",
                "drives": [
                    {
                        "label": "C:\\",
                        "value": "C:/",
                        "total_files": 7,
                    }
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )
        self.assertEqual(response.status_code, 200)

        active_response = self.client.get("/active-agents-data/")
        active_data = active_response.json()

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_data["total_agents"], 1)
        self.assertEqual(active_data["agents"][0]["host_name"], "NEW-PC")
        self.assertEqual(active_data["selected_agent_id"], "")

    def test_selected_delayed_agent_stays_selected_in_active_users_payload(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="stale-selected-pc",
            host_name="STALE-PC",
            ip_address="192.168.1.99",
        )
        ActiveAgent.objects.filter(agent_id="stale-selected-pc").update(
            last_seen_at=timezone.now() - timedelta(minutes=10),
        )

        self.client.force_login(user)
        select_response = self.client.post(
            "/select-agent/",
            data={"agent_id": "stale-selected-pc"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(select_response.status_code, 200)

        response = self.client.get("/active-agents-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "stale-selected-pc")
        self.assertEqual(data["total_agents"], 1)
        self.assertFalse(data["agents"][0]["is_online"])

    def test_select_agent_allows_delayed_agent(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="old-select-pc",
            host_name="OLD-SELECT-PC",
            ip_address="192.168.1.33",
        )
        ActiveAgent.objects.filter(agent_id="old-select-pc").update(
            last_seen_at=timezone.now() - timedelta(minutes=10),
        )

        self.client.force_login(user)
        session = self.client.session
        session[SELECTED_AGENT_SESSION_KEY] = "old-select-pc"
        session.save()

        response = self.client.post(
            "/select-agent/",
            data={"agent_id": "old-select-pc"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "old-select-pc")
        self.assertEqual(data["dashboard"]["system_info"]["host_name"], "OLD-SELECT-PC")

    @override_settings(LOCAL_DRIVE_SCANNER_ENABLED=False)
    def test_hosted_dashboard_waits_for_active_user_when_no_agent_selected(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )

        self.client.force_login(user)
        response = self.client.get("/files-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "")
        self.assertEqual(data["dashboard"]["sync_status_title"], "Waiting for Active User")
        self.assertEqual(data["dashboard"]["system_info"]["host_name"], "Select Active User")
        self.assertEqual(data["pagination"]["total_matching_display"], "0")
        self.assertIn("Select an Active User", data["error_message"])

    @override_settings(
        LOCAL_DRIVE_SCANNER_ENABLED=False,
        AGENT_AUTO_SELECT_SINGLE_ACTIVE_AGENT=True,
    )
    def test_hosted_dashboard_auto_selects_single_installed_agent(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="own-installed-pc",
            host_name="OWN-PC",
            ip_address="192.168.1.101",
            mac_address="AA:11:BB:22:CC:33",
            drive_count=1,
            total_files=1,
            latest_payload={},
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=drive,
            name="Own_PC_File.txt",
            folder="D:\\Work",
            relative_path="Work\\Own_PC_File.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="1 KB",
            size_bytes=1024,
        )

        self.client.force_login(user)
        response = self.client.get("/files-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "own-installed-pc")
        self.assertEqual(data["dashboard"]["system_info"]["host_name"], "OWN-PC")
        self.assertIn("Own_PC_File.txt", data["rows_html"])
        self.assertEqual(
            self.client.session[SELECTED_AGENT_SESSION_KEY],
            "own-installed-pc",
        )

        agent.refresh_from_db()
        self.assertEqual(agent.latest_payload["requested_drive_values"], ["D:/"])

    @override_settings(
        LOCAL_DRIVE_SCANNER_ENABLED=False,
        STORAGES=TEST_STORAGES,
    )
    def test_active_users_are_sidebar_only(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="sidebar-user-pc",
            host_name="SIDEBAR-PC",
            ip_address="192.168.1.92",
        )

        self.client.force_login(user)
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="sidebar-user-panel"')
        self.assertContains(response, "SIDEBAR-PC")
        self.assertNotContains(response, "active-users-card")

    def test_agent_files_batch_stores_remote_files(self):
        response = self.client.post(
            "/agent-files-batch/",
            data={
                "agent_id": "remote-pc-1",
                "host_name": "REMOTE-PC",
                "ip_address": "192.168.1.80",
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "drive_label": "D:\\",
                "drive_value": "D:/",
                "scan_id": "scan-1",
                "batch_index": 0,
                "indexed_files": 1,
                "total_files": 1,
                "scan_complete": True,
                "storage": {
                    "used_display": "5 GB",
                    "total_display": "10 GB",
                    "free_display": "5 GB",
                    "percent_used": 50,
                },
                "files": [
                    {
                        "name": "Remote_Report.pdf",
                        "folder": "D:\\Reports",
                        "relative_path": "Reports\\Remote_Report.pdf",
                        "extension": ".pdf",
                        "type_badge": "PDF",
                        "type_class": "pdf",
                        "type_label": "PDF",
                        "size": "1.25 MB",
                        "size_bytes": 1310720,
                        "modified_timestamp": 1000,
                        "freshness_timestamp": 1000,
                        "modified_display": "01-01-2026 10:00:00 AM",
                    }
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(response.status_code, 200)
        agent = ActiveAgent.objects.get(agent_id="remote-pc-1")
        drive = ActiveAgentDrive.objects.get(agent=agent, value="D:/")
        file_report = ActiveAgentFile.objects.get(drive=drive)

        self.assertEqual(agent.host_name, "REMOTE-PC")
        self.assertEqual(drive.total_files, 1)
        self.assertTrue(drive.count_complete)
        self.assertEqual(file_report.name, "Remote_Report.pdf")

    def test_agent_file_download_upload_stores_requested_file(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as download_root:
            with override_settings(REMOTE_FILE_DOWNLOAD_ROOT=Path(download_root)):
                agent = ActiveAgent.objects.create(
                    agent_id="download-agent",
                    host_name="DOWNLOAD-PC",
                    ip_address="192.168.1.82",
                )
                drive = ActiveAgentDrive.objects.create(
                    agent=agent,
                    label="D:\\",
                    value="D:/",
                )
                download_request = RemoteFileDownload.objects.create(
                    request_id="download-request-1",
                    agent=agent,
                    drive=drive,
                    relative_path="Reports\\Ready.txt",
                    name="Ready.txt",
                )

                response = self.client.post(
                    "/agent-file-download/",
                    data=b"remote file bytes",
                    content_type="application/octet-stream",
                    HTTP_X_AGENT_TOKEN="test-token",
                    HTTP_X_AGENT_ID="download-agent",
                    HTTP_X_DOWNLOAD_REQUEST_ID="download-request-1",
                    HTTP_X_DOWNLOAD_STATUS="ready",
                )

                self.assertEqual(response.status_code, 200)
                download_request.refresh_from_db()
                self.assertEqual(download_request.status, RemoteFileDownload.STATUS_READY)
                self.assertEqual(download_request.size_bytes, len(b"remote file bytes"))
                self.assertEqual(
                    Path(download_request.file_path).read_bytes(),
                    b"remote file bytes",
                )

    def test_dashboard_download_route_is_not_available(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as download_root:
            ready_file_path = Path(download_root) / "ready.download"
            ready_file_path.write_bytes(b"ready remote content")
            agent = ActiveAgent.objects.create(
                agent_id="ready-download-agent",
                host_name="READY-PC",
                ip_address="192.168.1.83",
            )
            drive = ActiveAgentDrive.objects.create(
                agent=agent,
                label="D:\\",
                value="D:/",
            )
            file_report = ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name="Ready_Remote.txt",
                folder="D:\\Reports",
                relative_path="Reports\\Ready_Remote.txt",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="20 bytes",
                size_bytes=20,
                modified_timestamp=3000,
            )
            RemoteFileDownload.objects.create(
                request_id="ready-request-1",
                agent=agent,
                drive=drive,
                relative_path=file_report.relative_path,
                name=file_report.name,
                modified_timestamp=file_report.modified_timestamp,
                status=RemoteFileDownload.STATUS_READY,
                file_path=str(ready_file_path),
                size_bytes=ready_file_path.stat().st_size,
            )

            self.client.force_login(user)
            select_response = self.client.post(
                "/select-agent/",
                data={"agent_id": agent.agent_id},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(select_response.status_code, 200)

            response = self.client.get(
                "/download/",
                data={"path": file_report.relative_path},
            )
            self.assertEqual(response.status_code, 404)

    def test_agent_uninstall_removes_active_agent(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-remove",
            host_name="REMOVE-PC",
            ip_address="192.168.1.81",
            mac_address="AA:BB:CC:DD:EE:11",
            drive_count=1,
            total_files=1,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=drive,
            name="Remove_Me.txt",
            folder="D:\\Temp",
            relative_path="Temp\\Remove_Me.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="12 bytes",
            size_bytes=12,
        )

        response = self.client.post(
            "/agent-uninstall/",
            data={
                "agent_id": "remote-pc-remove",
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["removed"])
        self.assertFalse(
            ActiveAgent.objects.filter(agent_id="remote-pc-remove").exists()
        )
        self.assertFalse(
            ActiveAgentFile.objects.filter(agent__agent_id="remote-pc-remove").exists()
        )

        self.client.force_login(user)
        active_response = self.client.get("/active-agents-data/")
        active_data = active_response.json()

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_data["total_agents"], 0)
        self.assertEqual(active_data["agents"], [])

    def test_selecting_active_agent_shows_remote_dashboard_files(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-2",
            host_name="DESIGN-PC",
            ip_address="192.168.1.90",
            mac_address="11:22:33:44:55:66",
            os_label="Windows 11",
            architecture="AMD64",
            drive_count=1,
            total_files=1,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="E:\\",
            value="E:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
            storage={
                "used_display": "20 GB",
                "total_display": "40 GB",
                "free_display": "20 GB",
                "percent_used": 50,
            },
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=drive,
            name="Design_Board.png",
            folder="E:\\Assets",
            relative_path="Assets\\Design_Board.png",
            extension=".png",
            type_badge="IMG",
            type_class="image",
            type_label="PNG",
            size="2 MB",
            size_bytes=2097152,
            modified_timestamp=2000,
            freshness_timestamp=2000,
            modified_display="01-01-2026 11:00:00 AM",
        )

        self.client.force_login(user)
        response = self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-2"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "remote-pc-2")
        self.assertEqual(data["dashboard"]["system_info"]["host_name"], "DESIGN-PC")
        self.assertIn("Design_Board.png", data["rows_html"])
        self.assertNotIn("Download", data["rows_html"])

    def test_selecting_active_agent_prefers_d_drive(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-d-default",
            host_name="D-DEFAULT-PC",
            ip_address="192.168.1.91",
            mac_address="11:22:33:44:55:77",
            drive_count=2,
            total_files=2,
        )
        c_drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="C:\\",
            value="C:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
        )
        d_drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
        )
        ActiveAgentDrive.objects.create(
            agent=agent,
            label="E:\\",
            value="E:/",
            total_files=0,
            indexed_files=0,
            count_complete=False,
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=c_drive,
            name="C_File.txt",
            folder="C:\\Temp",
            relative_path="Temp\\C_File.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="1 KB",
            size_bytes=1024,
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=d_drive,
            name="D_File.txt",
            folder="D:\\Work",
            relative_path="Work\\D_File.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="2 KB",
            size_bytes=2048,
        )

        self.client.force_login(user)
        response = self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-d-default"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_drive_value"], "D:/")
        self.assertEqual(
            [option["value"] for option in data["drive_options"]],
            ["D:/", "E:/", "C:/"],
        )
        self.assertEqual(data["dashboard"]["drive_label"], "D:\\")
        self.assertIn("D_File.txt", data["rows_html"])
        self.assertNotIn("C_File.txt", data["rows_html"])

    def test_remote_agent_file_pagination_returns_next_page(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-pagination",
            host_name="PAGE-PC",
            ip_address="192.168.1.94",
            drive_count=1,
            total_files=12,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=12,
            indexed_files=12,
            count_complete=True,
        )

        for index in range(12):
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=f"D_File_{index:02d}.txt",
                folder="D:\\Work",
                relative_path=f"Work\\D_File_{index:02d}.txt",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="1 KB",
                size_bytes=1024,
                freshness_timestamp=index,
            )

        self.client.force_login(user)
        select_response = self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-pagination"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(select_response.status_code, 200)

        response = self.client.get(
            "/files-data/",
            data={"page": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["pagination"]["page_number"], 2)
        self.assertEqual(data["pagination"]["page_start_display"], "11")
        self.assertEqual(data["pagination"]["page_end_display"], "12")
        self.assertIn("D_File_01.txt", data["rows_html"])
        self.assertIn("D_File_00.txt", data["rows_html"])
        self.assertNotIn("D_File_11.txt", data["rows_html"])

    def test_selecting_remote_drive_queues_priority_scan_for_agent(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-priority-scan",
            host_name="PRIORITY-PC",
            ip_address="192.168.1.95",
            mac_address="11:22:33:44:55:88",
            drive_count=2,
            total_files=0,
            latest_payload={},
        )
        d_drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=1,
            indexed_files=1,
        )
        c_drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="C:\\",
            value="C:/",
            total_files=1,
            indexed_files=1,
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=d_drive,
            name="D_Selected.txt",
            folder="D:\\Work",
            relative_path="Work\\D_Selected.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="1 KB",
            size_bytes=1024,
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=c_drive,
            name="C_Selected.txt",
            folder="C:\\Work",
            relative_path="Work\\C_Selected.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="1 KB",
            size_bytes=1024,
        )

        self.client.force_login(user)
        select_agent_response = self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-priority-scan"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(select_agent_response.status_code, 200)

        select_drive_response = self.client.post(
            "/select-drive/",
            data={"drive_root": "C:/"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(select_drive_response.status_code, 200)
        select_drive_data = select_drive_response.json()
        self.assertEqual(select_drive_data["selected_drive_value"], "C:/")
        self.assertEqual(select_drive_data["dashboard"]["drive_label"], "C:\\")
        self.assertIn("C_Selected.txt", select_drive_data["rows_html"])
        self.assertNotIn("D_Selected.txt", select_drive_data["rows_html"])

        agent.refresh_from_db()
        self.assertEqual(agent.latest_payload["requested_drive_values"], ["C:/"])

        heartbeat_response = self.client.post(
            "/agent-heartbeat/",
            data={
                "agent_id": "remote-pc-priority-scan",
                "host_name": "PRIORITY-PC",
                "drives": [
                    {
                        "label": "D:\\",
                        "value": "D:/",
                    },
                    {
                        "label": "C:\\",
                        "value": "C:/",
                    },
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )
        heartbeat_data = heartbeat_response.json()

        self.assertEqual(heartbeat_response.status_code, 200)
        self.assertEqual(heartbeat_data["requested_drive_values"], ["C:/"])

        agent.refresh_from_db()
        self.assertEqual(agent.latest_payload["requested_drive_values"], [])
