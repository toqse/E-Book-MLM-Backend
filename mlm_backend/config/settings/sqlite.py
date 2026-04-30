"""
Local development with file-based SQLite (no MySQL / Redis required).

Usage:
    set DJANGO_SETTINGS_MODULE=config.settings.sqlite
Or one-off:
    django-admin migrate --settings=config.settings.sqlite

Then: python manage.py createsuperuser
"""
from .development import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "mlm-sqlite-local",
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
