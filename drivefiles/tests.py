import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from .models import ActiveAgent, ActiveAgentDrive, ActiveAgentFile
from . import scanner


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

    def test_active_agents_data_hides_stale_agents(self):
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

    def test_agent_uninstall_removes_active_agent(self):
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
        self.assertIn("Remote", data["rows_html"])
