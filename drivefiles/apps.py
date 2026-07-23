import os
import sys

from django.apps import AppConfig


class DrivefilesConfig(AppConfig):
    name = "drivefiles"

    def ready(self):
        from django.contrib.auth.models import update_last_login
        from django.contrib.auth.signals import user_logged_in

        user_logged_in.disconnect(update_last_login)

        if "runserver" not in sys.argv:
            return

        if os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv:
            from .scanner import start_background_scanner

            start_background_scanner()
