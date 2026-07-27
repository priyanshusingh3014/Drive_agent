from django.db import models


class ActiveAgent(models.Model):
    agent_id = models.CharField(max_length=128, unique=True)
    host_name = models.CharField(max_length=255)
    ip_address = models.CharField(max_length=64, blank=True)
    mac_address = models.CharField(max_length=64, blank=True)
    os_label = models.CharField(max_length=128, blank=True)
    architecture = models.CharField(max_length=64, blank=True)
    drive_count = models.PositiveIntegerField(default=0)
    total_files = models.PositiveIntegerField(default=0)
    latest_payload = models.JSONField(default=dict, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-last_seen_at", "host_name")

    def __str__(self):
        return f"{self.host_name} ({self.agent_id})"


class ActiveAgentDrive(models.Model):
    agent = models.ForeignKey(
        ActiveAgent,
        on_delete=models.CASCADE,
        related_name="drive_reports",
    )
    value = models.CharField(max_length=64)
    label = models.CharField(max_length=32)
    total_files = models.PositiveIntegerField(default=0)
    indexed_files = models.PositiveIntegerField(default=0)
    count_complete = models.BooleanField(default=False)
    storage = models.JSONField(default=dict, blank=True)
    scan_id = models.CharField(max_length=96, blank=True)
    last_reported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("label",)
        unique_together = (("agent", "value"),)

    def __str__(self):
        return f"{self.agent.host_name} {self.label}"


class ActiveAgentFile(models.Model):
    agent = models.ForeignKey(
        ActiveAgent,
        on_delete=models.CASCADE,
        related_name="file_reports",
    )
    drive = models.ForeignKey(
        ActiveAgentDrive,
        on_delete=models.CASCADE,
        related_name="file_reports",
    )
    relative_path = models.CharField(max_length=2048)
    name = models.CharField(max_length=255)
    folder = models.CharField(max_length=2048, blank=True)
    extension = models.CharField(max_length=64, blank=True)
    type_badge = models.CharField(max_length=16, blank=True)
    type_class = models.CharField(max_length=32, blank=True)
    type_label = models.CharField(max_length=64, blank=True)
    size = models.CharField(max_length=32, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    modified_timestamp = models.FloatField(default=0)
    freshness_timestamp = models.FloatField(default=0)
    modified_display = models.CharField(max_length=64, blank=True)
    reported_scan_id = models.CharField(max_length=96, blank=True)

    class Meta:
        indexes = (
            models.Index(fields=("drive", "name")),
            models.Index(fields=("drive", "-freshness_timestamp")),
            models.Index(fields=("drive", "-freshness_timestamp", "name")),
            models.Index(fields=("drive", "type_class")),
            models.Index(
                fields=("drive", "type_class", "-freshness_timestamp", "name"),
                name="df_file_type_fast_idx",
            ),
            models.Index(fields=("drive", "extension")),
            models.Index(
                fields=("drive", "extension", "-freshness_timestamp", "name"),
                name="df_file_ext_fast_idx",
            ),
        )
        ordering = ("-freshness_timestamp", "name")
        unique_together = (("drive", "relative_path"),)

    def __str__(self):
        return self.relative_path


class RemoteFileDownload(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "Queued"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    )

    request_id = models.CharField(max_length=64, unique=True)
    agent = models.ForeignKey(
        ActiveAgent,
        on_delete=models.CASCADE,
        related_name="download_requests",
    )
    drive = models.ForeignKey(
        ActiveAgentDrive,
        on_delete=models.CASCADE,
        related_name="download_requests",
    )
    relative_path = models.CharField(max_length=2048)
    name = models.CharField(max_length=255)
    modified_timestamp = models.FloatField(default=0)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_QUEUED,
    )
    file_path = models.CharField(max_length=2048, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    error_message = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = (
            models.Index(fields=("agent", "drive", "relative_path")),
            models.Index(fields=("request_id", "status")),
        )
        ordering = ("-updated_at",)

    def __str__(self):
        return f"{self.agent.host_name} {self.relative_path}"
