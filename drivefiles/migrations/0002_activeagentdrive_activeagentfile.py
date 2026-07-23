from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("drivefiles", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ActiveAgentDrive",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("value", models.CharField(max_length=64)),
                ("label", models.CharField(max_length=32)),
                ("total_files", models.PositiveIntegerField(default=0)),
                ("indexed_files", models.PositiveIntegerField(default=0)),
                ("count_complete", models.BooleanField(default=False)),
                ("storage", models.JSONField(blank=True, default=dict)),
                ("scan_id", models.CharField(blank=True, max_length=96)),
                ("last_reported_at", models.DateTimeField(auto_now=True)),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="drive_reports",
                        to="drivefiles.activeagent",
                    ),
                ),
            ],
            options={
                "ordering": ("label",),
                "unique_together": {("agent", "value")},
            },
        ),
        migrations.CreateModel(
            name="ActiveAgentFile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("relative_path", models.CharField(max_length=2048)),
                ("name", models.CharField(max_length=255)),
                ("folder", models.CharField(blank=True, max_length=2048)),
                ("extension", models.CharField(blank=True, max_length=64)),
                ("type_badge", models.CharField(blank=True, max_length=16)),
                ("type_class", models.CharField(blank=True, max_length=32)),
                ("type_label", models.CharField(blank=True, max_length=64)),
                ("size", models.CharField(blank=True, max_length=32)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("modified_timestamp", models.FloatField(default=0)),
                ("freshness_timestamp", models.FloatField(default=0)),
                ("modified_display", models.CharField(blank=True, max_length=64)),
                ("reported_scan_id", models.CharField(blank=True, max_length=96)),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="file_reports",
                        to="drivefiles.activeagent",
                    ),
                ),
                (
                    "drive",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="file_reports",
                        to="drivefiles.activeagentdrive",
                    ),
                ),
            ],
            options={
                "ordering": ("-freshness_timestamp", "name"),
                "unique_together": {("drive", "relative_path")},
            },
        ),
        migrations.AddIndex(
            model_name="activeagentfile",
            index=models.Index(fields=["drive", "name"], name="drivefiles__drive_i_3b7a32_idx"),
        ),
        migrations.AddIndex(
            model_name="activeagentfile",
            index=models.Index(fields=["drive", "-freshness_timestamp"], name="drivefiles__drive_i_6c8025_idx"),
        ),
    ]
