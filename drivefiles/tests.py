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

        self.assertRedirects(
            response,
            "/login/?username=new-admin",
            fetch_redirect_response=False,
        )
        self.assertTrue(
            get_user_model().objects.filter(username="new-admin").exists()
        )
        self.assertNotIn("_auth_user_id", self.client.session)

        dashboard_response = self.client.get("/")
        self.assertEqual(dashboard_response.status_code, 302)
        self.assertIn("/login/", dashboard_response["Location"])

    def test_login_prefills_username_after_signup(self):
        self.client.post(
            "/signup/",
            data={
                "username": "prefilled-admin",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        response = self.client.get("/login/?username=prefilled-admin")

        self.assertContains(response, 'value="prefilled-admin"')

    def test_login_page_renders_secure_dashboard_ui(self):
        response = self.client.get("/login/")

        self.assertContains(response, "Secure Drive Access.")
        self.assertContains(response, "Drive Status")
        self.assertNotContains(response, "Remember me")
        self.assertContains(response, "bar-one")
        self.assertContains(response, "bar-two")
        self.assertContains(response, "bar-three")


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

    def test_completed_heartbeat_does_not_lower_drive_before_file_batch_finalizes(self):
        agent = ActiveAgent.objects.create(
            agent_id="stable-heartbeat-pc",
            host_name="STABLE-HEARTBEAT-PC",
            ip_address="192.168.1.51",
            drive_count=1,
            total_files=5,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=5,
            indexed_files=5,
            count_complete=True,
        )

        response = self.client.post(
            "/agent-heartbeat/",
            data={
                "agent_id": "stable-heartbeat-pc",
                "host_name": "STABLE-HEARTBEAT-PC",
                "drives": [
                    {
                        "label": "D:\\",
                        "value": "D:/",
                        "total_files": 2,
                        "indexed_files": 2,
                        "count_complete": True,
                    },
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(response.status_code, 200)
        drive.refresh_from_db()
        agent.refresh_from_db()
        self.assertEqual(drive.total_files, 5)
        self.assertEqual(drive.indexed_files, 5)
        self.assertFalse(drive.count_complete)
        self.assertEqual(agent.total_files, 5)

    def test_completed_heartbeat_lowers_drive_after_file_batch_finalizes(self):
        agent = ActiveAgent.objects.create(
            agent_id="delete-heartbeat-pc",
            host_name="DELETE-HEARTBEAT-PC",
            ip_address="192.168.1.52",
            drive_count=1,
            total_files=5,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=5,
            indexed_files=5,
            count_complete=True,
        )

        for index in range(2):
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=f"Current_File_{index}.txt",
                folder="D:\\Work",
                relative_path=f"Work\\Current_File_{index}.txt",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="1 KB",
                size_bytes=1024,
            )

        response = self.client.post(
            "/agent-heartbeat/",
            data={
                "agent_id": "delete-heartbeat-pc",
                "host_name": "DELETE-HEARTBEAT-PC",
                "drives": [
                    {
                        "label": "D:\\",
                        "value": "D:/",
                        "total_files": 2,
                        "indexed_files": 2,
                        "count_complete": True,
                    },
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(response.status_code, 200)
        drive.refresh_from_db()
        agent.refresh_from_db()
        self.assertEqual(drive.total_files, 2)
        self.assertEqual(drive.indexed_files, 2)
        self.assertTrue(drive.count_complete)
        self.assertEqual(agent.total_files, 2)

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

    def test_active_agents_data_hides_delayed_agents(self):
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
        self.assertEqual(data["total_agents"], 0)
        self.assertEqual(data["online_agents"], 0)
        self.assertEqual(data["agents"], [])

    def test_active_agents_data_keeps_selected_existing_agent_when_offline(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="offline-selected-pc",
            host_name="OFFLINE-SELECTED-PC",
            ip_address="192.168.1.45",
        )
        ActiveAgent.objects.filter(agent_id="offline-selected-pc").update(
            last_seen_at=timezone.now() - timedelta(minutes=10),
        )

        self.client.force_login(user)
        response = self.client.get(
            "/active-agents-data/",
            data={"agent_id": "offline-selected-pc"},
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["total_agents"], 0)
        self.assertEqual(data["agents"], [])
        self.assertEqual(data["selected_agent_id"], "offline-selected-pc")
        self.assertFalse(data["selected_agent_online"])
        self.assertFalse(data["selected_agent_removed"])

    def test_active_agents_data_marks_removed_selected_agent(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )

        self.client.force_login(user)
        response = self.client.get(
            "/active-agents-data/",
            data={"agent_id": "removed-selected-pc"},
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "")
        self.assertTrue(data["selected_agent_removed"])

    def test_active_agents_data_uses_stable_first_seen_order(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="beta-pc",
            host_name="BETA-PC",
            ip_address="192.168.1.42",
        )
        ActiveAgent.objects.create(
            agent_id="alpha-pc",
            host_name="ALPHA-PC",
            ip_address="192.168.1.41",
        )
        ActiveAgent.objects.filter(agent_id="beta-pc").update(
            first_seen_at=timezone.now() - timedelta(seconds=2),
            last_seen_at=timezone.now(),
        )
        ActiveAgent.objects.filter(agent_id="alpha-pc").update(
            first_seen_at=timezone.now() - timedelta(seconds=1),
            last_seen_at=timezone.now() - timedelta(seconds=1),
        )

        self.client.force_login(user)
        response = self.client.get("/active-agents-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [agent["host_name"] for agent in data["agents"]],
            ["BETA-PC", "ALPHA-PC"],
        )

    @override_settings(AGENT_ONLINE_SECONDS=15)
    def test_active_agents_data_keeps_recent_heartbeat_visible(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        ActiveAgent.objects.create(
            agent_id="jitter-pc",
            host_name="JITTER-PC",
            ip_address="192.168.1.46",
        )
        ActiveAgent.objects.filter(agent_id="jitter-pc").update(
            last_seen_at=timezone.now() - timedelta(seconds=8),
        )

        self.client.force_login(user)
        response = self.client.get("/active-agents-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["total_agents"], 1)
        self.assertEqual(data["online_agents"], 1)
        self.assertEqual(data["agents"][0]["host_name"], "JITTER-PC")

    def test_agent_heartbeat_replaces_duplicate_same_pc_identity(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        duplicate_agent = ActiveAgent.objects.create(
            agent_id="old-duplicate-id",
            host_name="DUPLICATE-PC",
            ip_address="192.168.1.50",
            mac_address="AA:BB:CC:DD:EE:FF",
            drive_count=1,
            total_files=1,
        )
        duplicate_drive = ActiveAgentDrive.objects.create(
            agent=duplicate_agent,
            label="D:\\",
            value="D:/",
            total_files=1,
            indexed_files=1,
        )
        ActiveAgentFile.objects.create(
            agent=duplicate_agent,
            drive=duplicate_drive,
            name="Old_Duplicate.txt",
            folder="D:\\Old",
            relative_path="Old\\Old_Duplicate.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="1 KB",
            size_bytes=1024,
        )

        heartbeat_response = self.client.post(
            "/agent-heartbeat/",
            data={
                "agent_id": "new-duplicate-id",
                "host_name": "DUPLICATE-PC",
                "ip_address": "192.168.1.50",
                "mac_address": "11:22:33:44:55:66",
                "drives": [
                    {
                        "label": "D:\\",
                        "value": "D:/",
                        "total_files": 2,
                    }
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(heartbeat_response.status_code, 200)
        self.assertFalse(
            ActiveAgent.objects.filter(agent_id="old-duplicate-id").exists()
        )
        self.assertFalse(
            ActiveAgentFile.objects.filter(agent__agent_id="old-duplicate-id").exists()
        )
        self.assertTrue(
            ActiveAgent.objects.filter(agent_id="new-duplicate-id").exists()
        )

        self.client.force_login(user)
        active_response = self.client.get("/active-agents-data/")
        active_data = active_response.json()

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_data["total_agents"], 1)
        self.assertEqual(active_data["agents"][0]["agent_id"], "new-duplicate-id")

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

    def test_selected_delayed_agent_is_cleared_from_active_users_payload(self):
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
        self.assertEqual(select_response.status_code, 404)

        response = self.client.get("/active-agents-data/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "")
        self.assertEqual(data["total_agents"], 0)
        self.assertEqual(data["agents"], [])

    def test_select_agent_rejects_delayed_agent(self):
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

        self.assertEqual(response.status_code, 404)
        self.assertEqual(data["selected_agent_id"], "")
        self.assertFalse(data["ok"])

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

    def test_agent_file_events_add_new_remote_file_at_top(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-events-add",
            host_name="EVENTS-PC",
            ip_address="192.168.1.82",
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
            name="Old_File.txt",
            folder="D:\\Work",
            relative_path="Work\\Old_File.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="1 KB",
            size_bytes=1024,
            freshness_timestamp=1000,
        )

        response = self.client.post(
            "/agent-file-events/",
            data={
                "agent_id": "remote-pc-events-add",
                "host_name": "EVENTS-PC",
                "drive_label": "D:\\",
                "drive_value": "D:/",
                "index_ready": True,
                "upsert_files": [
                    {
                        "name": "New_File.txt",
                        "folder": "D:\\Work",
                        "relative_path": "Work\\New_File.txt",
                        "extension": ".txt",
                        "type_badge": "TXT",
                        "type_class": "document",
                        "type_label": "TXT",
                        "size": "2 KB",
                        "size_bytes": 2048,
                        "modified_timestamp": 2000,
                        "freshness_timestamp": 3000,
                    }
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(response.status_code, 200)
        drive.refresh_from_db()
        agent.refresh_from_db()
        self.assertEqual(drive.total_files, 2)
        self.assertEqual(agent.total_files, 2)

        self.client.force_login(user)
        response = self.client.get(
            "/files-data/",
            data={"agent_id": "remote-pc-events-add", "drive_root": "D:/"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertLess(
            data["rows_html"].index("New_File.txt"),
            data["rows_html"].index("Old_File.txt"),
        )
        self.assertEqual(data["pagination"]["total_matching_display"], "2")

    def test_agent_file_events_delete_remote_file_and_lower_count(self):
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-events-delete",
            host_name="DELETE-EVENTS-PC",
            ip_address="192.168.1.83",
            drive_count=1,
            total_files=2,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=2,
            indexed_files=2,
            count_complete=True,
        )

        for name in ("Keep_File.txt", "Delete_File.txt"):
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=name,
                folder="D:\\Work",
                relative_path=f"Work\\{name}",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="1 KB",
                size_bytes=1024,
                freshness_timestamp=1000,
            )

        response = self.client.post(
            "/agent-file-events/",
            data={
                "agent_id": "remote-pc-events-delete",
                "host_name": "DELETE-EVENTS-PC",
                "drive_label": "D:\\",
                "drive_value": "D:/",
                "index_ready": True,
                "deleted_paths": ["Work\\Delete_File.txt"],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["total_files"], 1)
        self.assertEqual(data["deleted_files"], 1)
        drive.refresh_from_db()
        agent.refresh_from_db()
        self.assertEqual(drive.total_files, 1)
        self.assertEqual(drive.indexed_files, 1)
        self.assertEqual(agent.total_files, 1)
        self.assertFalse(
            ActiveAgentFile.objects.filter(
                drive=drive,
                relative_path="Work\\Delete_File.txt",
            ).exists()
        )
        self.assertTrue(
            ActiveAgentFile.objects.filter(
                drive=drive,
                relative_path="Work\\Keep_File.txt",
            ).exists()
        )

    def test_agent_rescan_preserves_previous_files_until_complete(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-rescan",
            host_name="RESCAN-PC",
            ip_address="192.168.1.81",
            drive_count=1,
            total_files=3,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=3,
            indexed_files=3,
            count_complete=True,
            scan_id="old-scan",
        )

        for index in range(3):
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=f"Old_File_{index}.txt",
                folder="D:\\Work",
                relative_path=f"Work\\Old_File_{index}.txt",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="1 KB",
                size_bytes=1024,
                reported_scan_id="old-scan",
            )

        partial_response = self.client.post(
            "/agent-files-batch/",
            data={
                "agent_id": "remote-pc-rescan",
                "host_name": "RESCAN-PC",
                "drive_label": "D:\\",
                "drive_value": "D:/",
                "scan_id": "new-scan",
                "batch_index": 0,
                "indexed_files": 1,
                "total_files": 1,
                "scan_complete": False,
                "files": [
                    {
                        "name": "Fresh_File.txt",
                        "folder": "D:\\Work",
                        "relative_path": "Work\\Fresh_File.txt",
                        "extension": ".txt",
                        "type_badge": "TXT",
                        "type_class": "document",
                        "type_label": "TXT",
                        "size": "1 KB",
                        "size_bytes": 1024,
                        "modified_timestamp": 2000,
                        "freshness_timestamp": 2000,
                    }
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(partial_response.status_code, 200)
        drive.refresh_from_db()
        self.assertEqual(drive.total_files, 4)
        self.assertEqual(drive.indexed_files, 4)
        self.assertFalse(drive.count_complete)
        self.assertEqual(drive.file_reports.count(), 4)

        self.client.force_login(user)
        self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-rescan"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        search_response = self.client.get(
            "/files-data/",
            data={"search": "Old_File_2"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        search_data = search_response.json()

        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_data["dashboard"]["total_files_display"], "4")
        self.assertIn("Old_File_2.txt", search_data["rows_html"])

        complete_response = self.client.post(
            "/agent-files-batch/",
            data={
                "agent_id": "remote-pc-rescan",
                "host_name": "RESCAN-PC",
                "drive_label": "D:\\",
                "drive_value": "D:/",
                "scan_id": "new-scan",
                "batch_index": 1,
                "indexed_files": 2,
                "total_files": 2,
                "scan_complete": True,
                "files": [
                    {
                        "name": "Second_Fresh_File.txt",
                        "folder": "D:\\Work",
                        "relative_path": "Work\\Second_Fresh_File.txt",
                        "extension": ".txt",
                        "type_badge": "TXT",
                        "type_class": "document",
                        "type_label": "TXT",
                        "size": "2 KB",
                        "size_bytes": 2048,
                        "modified_timestamp": 2100,
                        "freshness_timestamp": 2100,
                    }
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(complete_response.status_code, 200)
        drive.refresh_from_db()
        self.assertEqual(drive.total_files, 2)
        self.assertEqual(drive.indexed_files, 2)
        self.assertTrue(drive.count_complete)
        self.assertEqual(
            set(drive.file_reports.values_list("name", flat=True)),
            {"Fresh_File.txt", "Second_Fresh_File.txt"},
        )

    def test_incomplete_completed_scan_does_not_delete_previous_file_rows(self):
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-incomplete-final",
            host_name="INCOMPLETE-FINAL-PC",
            ip_address="192.168.1.84",
            drive_count=1,
            total_files=4,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=4,
            indexed_files=4,
            count_complete=True,
            scan_id="old-scan",
        )

        for index in range(4):
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=f"Protected_File_{index}.txt",
                folder="D:\\Work",
                relative_path=f"Work\\Protected_File_{index}.txt",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="1 KB",
                size_bytes=1024,
                reported_scan_id="old-scan",
            )

        response = self.client.post(
            "/agent-files-batch/",
            data={
                "agent_id": "remote-pc-incomplete-final",
                "host_name": "INCOMPLETE-FINAL-PC",
                "drive_label": "D:\\",
                "drive_value": "D:/",
                "scan_id": "new-scan",
                "batch_index": 2,
                "indexed_files": 3,
                "total_files": 3,
                "scan_complete": True,
                "files": [
                    {
                        "name": "Only_Received_File.txt",
                        "folder": "D:\\Work",
                        "relative_path": "Work\\Only_Received_File.txt",
                        "extension": ".txt",
                        "type_badge": "TXT",
                        "type_class": "document",
                        "type_label": "TXT",
                        "size": "1 KB",
                        "size_bytes": 1024,
                        "modified_timestamp": 2200,
                        "freshness_timestamp": 2200,
                    }
                ],
            },
            content_type="application/json",
            HTTP_X_AGENT_TOKEN="test-token",
        )

        self.assertEqual(response.status_code, 200)
        drive.refresh_from_db()
        self.assertGreaterEqual(drive.total_files, 4)
        self.assertEqual(drive.total_files, drive.indexed_files)
        self.assertFalse(drive.count_complete)
        self.assertTrue(
            drive.file_reports.filter(name="Protected_File_3.txt").exists()
        )
        self.assertTrue(
            drive.file_reports.filter(name="Only_Received_File.txt").exists()
        )

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
            total_files=10,
        )
        c_drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="C:\\",
            value="C:/",
            total_files=9,
            indexed_files=9,
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
        self.assertEqual(data["dashboard"]["total_files_display"], "1")
        self.assertEqual(data["dashboard"]["processed_files_display"], "1")
        self.assertIn("D_File.txt", data["rows_html"])
        self.assertNotIn("C_File.txt", data["rows_html"])

    def test_remote_agent_file_pagination_returns_next_ten_files(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-pagination",
            host_name="PAGE-PC",
            ip_address="192.168.1.94",
            drive_count=1,
            total_files=25,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=25,
            indexed_files=25,
            count_complete=True,
        )

        for index in range(25):
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
        selected_data = select_response.json()

        response = self.client.get(
            "/files-data/",
            data={"page": 2, "version": selected_data["version"]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data.get("unchanged", False))
        self.assertEqual(data["pagination"]["page_number"], 2)
        self.assertEqual(data["pagination"]["page_start_display"], "11")
        self.assertEqual(data["pagination"]["page_end_display"], "20")

        for index in range(5, 15):
            self.assertIn(f"D_File_{index:02d}.txt", data["rows_html"])

        self.assertNotIn("D_File_24.txt", data["rows_html"])
        self.assertNotIn("D_File_04.txt", data["rows_html"])

    def test_remote_agent_scroll_batch_returns_large_offset_window(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-scroll-batch",
            host_name="SCROLL-PC",
            ip_address="192.168.1.106",
            drive_count=1,
            total_files=10105,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=10105,
            indexed_files=10105,
            count_complete=True,
        )

        ActiveAgentFile.objects.bulk_create(
            ActiveAgentFile(
                agent=agent,
                drive=drive,
                name=f"Batch_File_{index:05d}.txt",
                folder="D:\\Batch",
                relative_path=f"Batch\\Batch_File_{index:05d}.txt",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="1 KB",
                size_bytes=1024,
                freshness_timestamp=index,
            )
            for index in range(10105)
        )

        self.client.force_login(user)
        response = self.client.get(
            "/files-data/",
            data={
                "agent_id": "remote-pc-scroll-batch",
                "drive_root": "D:/",
                "scope": "files",
                "offset": 10,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["files_count"], 10000)
        self.assertEqual(data["scroll"]["start_display"], "11")
        self.assertEqual(data["scroll"]["end_display"], "10,010")
        self.assertEqual(data["scroll"]["next_offset"], 10010)
        self.assertTrue(data["scroll"]["has_more"])
        self.assertIn("Batch_File_10094.txt", data["rows_html"])
        self.assertIn("Batch_File_00095.txt", data["rows_html"])
        self.assertNotIn("Batch_File_10104.txt", data["rows_html"])
        self.assertNotIn("Batch_File_00094.txt", data["rows_html"])

    def test_remote_agent_file_pagination_uses_explicit_agent_without_session(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-explicit-pagination",
            host_name="EXPLICIT-PAGE-PC",
            ip_address="192.168.1.104",
            drive_count=1,
            total_files=22,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=22,
            indexed_files=22,
            count_complete=True,
        )

        for index in range(22):
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=f"Explicit_D_File_{index:02d}.txt",
                folder="D:\\Work",
                relative_path=f"Work\\Explicit_D_File_{index:02d}.txt",
                extension=".txt",
                type_badge="TXT",
                type_class="document",
                type_label="TXT",
                size="1 KB",
                size_bytes=1024,
                freshness_timestamp=index,
            )

        self.client.force_login(user)
        response = self.client.get(
            "/files-data/",
            data={
                "agent_id": "remote-pc-explicit-pagination",
                "drive_root": "D:/",
                "scope": "files",
                "page": 2,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "remote-pc-explicit-pagination")
        self.assertEqual(data["selected_drive_value"], "D:/")
        self.assertEqual(data["pagination"]["page_number"], 2)
        self.assertEqual(data["pagination"]["page_start_display"], "11")
        self.assertEqual(data["pagination"]["page_end_display"], "20")

        for index in range(2, 12):
            self.assertIn(f"Explicit_D_File_{index:02d}.txt", data["rows_html"])

        self.assertNotIn("Explicit_D_File_21.txt", data["rows_html"])
        self.assertNotIn("Explicit_D_File_01.txt", data["rows_html"])

    def test_remote_agent_explicit_drive_without_session(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-explicit-drive",
            host_name="EXPLICIT-DRIVE-PC",
            ip_address="192.168.1.105",
            drive_count=2,
            total_files=2,
        )
        d_drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
        )
        c_drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="C:\\",
            value="C:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
        )
        ActiveAgentFile.objects.create(
            agent=agent,
            drive=d_drive,
            name="Remote_D_File.txt",
            folder="D:\\Work",
            relative_path="Work\\Remote_D_File.txt",
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
            name="Remote_C_File.txt",
            folder="C:\\Work",
            relative_path="Work\\Remote_C_File.txt",
            extension=".txt",
            type_badge="TXT",
            type_class="document",
            type_label="TXT",
            size="1 KB",
            size_bytes=1024,
        )

        self.client.force_login(user)
        response = self.client.get(
            "/files-data/",
            data={
                "agent_id": "remote-pc-explicit-drive",
                "drive_root": "C:/",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["selected_agent_id"], "remote-pc-explicit-drive")
        self.assertEqual(data["selected_drive_value"], "C:/")
        self.assertEqual(data["dashboard"]["drive_label"], "C:\\")
        self.assertIn("Remote_C_File.txt", data["rows_html"])
        self.assertNotIn("Remote_D_File.txt", data["rows_html"])

    def test_remote_agent_file_type_filter_returns_only_selected_type(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-filter",
            host_name="FILTER-PC",
            ip_address="192.168.1.96",
            drive_count=1,
            total_files=6,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=6,
            indexed_files=6,
            count_complete=True,
        )
        files = [
            ("Project_Report.DOCX", ".DOCX", "Document", "document"),
            ("Meeting_Notes.PDF", ".PDF", "PDF", "pdf"),
            ("Deck.PPTX", ".PPTX", "PPT", "document"),
            ("Team_Photo.JPG", ".JPG", "IMG", "image"),
            ("Design_Board.PNG", ".PNG", "IMG", "image"),
            ("Backup.ZIP", ".ZIP", "ZIP", "archive"),
        ]

        for index, (name, extension, badge, type_class) in enumerate(files):
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=name,
                folder="D:\\Mixed",
                relative_path=f"Mixed\\{name}",
                extension=extension,
                type_badge=badge,
                type_class=type_class,
                type_label=extension.strip("."),
                size="1 KB",
                size_bytes=1024,
                freshness_timestamp=index,
            )

        self.client.force_login(user)
        select_response = self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-filter"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(select_response.status_code, 200)

        documents_response = self.client.get(
            "/files-data/",
            data={"type": "documents"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        documents_data = documents_response.json()

        self.assertEqual(documents_response.status_code, 200)
        self.assertEqual(documents_data["pagination"]["total_matching_display"], "3")
        self.assertIn("Project_Report.DOCX", documents_data["rows_html"])
        self.assertIn("Meeting_Notes.PDF", documents_data["rows_html"])
        self.assertIn("Deck.PPTX", documents_data["rows_html"])
        self.assertNotIn("Team_Photo.JPG", documents_data["rows_html"])
        self.assertNotIn("Design_Board.PNG", documents_data["rows_html"])
        self.assertNotIn("Backup.ZIP", documents_data["rows_html"])

        images_response = self.client.get(
            "/files-data/",
            data={"type": "images"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        images_data = images_response.json()

        self.assertEqual(images_response.status_code, 200)
        self.assertEqual(images_data["pagination"]["total_matching_display"], "2")
        self.assertIn("Team_Photo.JPG", images_data["rows_html"])
        self.assertIn("Design_Board.PNG", images_data["rows_html"])
        self.assertNotIn("Project_Report.DOCX", images_data["rows_html"])
        self.assertNotIn("Meeting_Notes.PDF", images_data["rows_html"])
        self.assertNotIn("Backup.ZIP", images_data["rows_html"])

    def test_files_only_search_returns_matching_rows_without_dashboard_payload(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-fast-search",
            host_name="FAST-SEARCH-PC",
            ip_address="192.168.1.97",
            drive_count=1,
            total_files=3,
        )
        drive = ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=3,
            indexed_files=3,
            count_complete=True,
        )

        for index, name in enumerate(
            ["Budget_Report.pdf", "Budget_Notes.docx", "Team_Photo.jpg"]
        ):
            extension = f".{name.rsplit('.', 1)[-1]}"
            type_class = "image" if extension == ".jpg" else "document"
            ActiveAgentFile.objects.create(
                agent=agent,
                drive=drive,
                name=name,
                folder="D:\\Search",
                relative_path=f"Search\\{name}",
                extension=extension,
                type_badge="FILE",
                type_class=type_class,
                type_label=extension.strip(".").upper(),
                size="1 KB",
                size_bytes=1024,
                freshness_timestamp=index,
            )

        self.client.force_login(user)
        self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-fast-search"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        response = self.client.get(
            "/files-data/",
            data={"scope": "files", "search": "Budget"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["files_only"])
        self.assertNotIn("dashboard", data)
        self.assertNotIn("active_agents", data)
        self.assertNotIn("drive_options", data)
        self.assertEqual(data["pagination"]["total_matching_display"], "2")
        self.assertEqual(data["current_files_display"], "2")
        self.assertIn("Budget_Report.pdf", data["rows_html"])
        self.assertIn("Budget_Notes.docx", data["rows_html"])
        self.assertNotIn("Team_Photo.jpg", data["rows_html"])

    def test_files_only_remote_search_queues_selected_drive_scan(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-live-search",
            host_name="LIVE-SEARCH-PC",
            ip_address="192.168.1.99",
            drive_count=1,
            total_files=0,
            latest_payload={},
        )
        ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=0,
            indexed_files=0,
        )

        self.client.force_login(user)
        response = self.client.get(
            "/files-data/",
            data={
                "agent_id": "remote-pc-live-search",
                "drive_root": "D:/",
                "scope": "files",
                "search": "Quarterly Plan",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        agent.refresh_from_db()
        self.assertEqual(agent.latest_payload["requested_drive_values"], ["D:/"])

    def test_files_only_refresh_can_skip_unchanged_search_results(self):
        user = get_user_model().objects.create_user(
            username="admin-user",
            password="pass-12345",
        )
        agent = ActiveAgent.objects.create(
            agent_id="remote-pc-fast-unchanged",
            host_name="FAST-UNCHANGED-PC",
            ip_address="192.168.1.98",
            drive_count=1,
            total_files=1,
        )
        ActiveAgentDrive.objects.create(
            agent=agent,
            label="D:\\",
            value="D:/",
            total_files=1,
            indexed_files=1,
            count_complete=True,
        )

        self.client.force_login(user)
        self.client.post(
            "/select-agent/",
            data={"agent_id": "remote-pc-fast-unchanged"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        first_response = self.client.get(
            "/files-data/",
            data={"scope": "files", "search": "anything"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        first_data = first_response.json()
        second_response = self.client.get(
            "/files-data/",
            data={
                "scope": "files",
                "search": "anything",
                "version": first_data["version"],
                "unchanged_ok": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        second_data = second_response.json()

        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(second_data["unchanged"])
        self.assertNotIn("rows_html", second_data)

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
