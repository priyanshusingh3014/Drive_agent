from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ActiveAgent",
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
                ("agent_id", models.CharField(max_length=128, unique=True)),
                ("host_name", models.CharField(max_length=255)),
                ("ip_address", models.CharField(blank=True, max_length=64)),
                ("mac_address", models.CharField(blank=True, max_length=64)),
                ("os_label", models.CharField(blank=True, max_length=128)),
                ("architecture", models.CharField(blank=True, max_length=64)),
                ("drive_count", models.PositiveIntegerField(default=0)),
                ("total_files", models.PositiveIntegerField(default=0)),
                ("latest_payload", models.JSONField(blank=True, default=dict)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ("-last_seen_at", "host_name"),
            },
        ),
    ]
