"""Settings for the Django self-tests: PostgreSQL from the standard PG* environment variables."""

import os

SECRET_KEY = "due-work-harness-tests"
USE_TZ = True
INSTALLED_APPS = ["django.contrib.contenttypes"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "due_work_harness"),
        "USER": os.environ.get("PGUSER", "postgres"),
        "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
        "HOST": os.environ.get("PGHOST", "localhost"),
        "PORT": os.environ.get("PGPORT", "5432"),
    }
}
CELERY_BEAT_SCHEDULE: dict[str, dict[str, object]] = {}
