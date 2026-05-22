import os

from .base import *  # noqa: F401,F403

DEBUG = False

# Serve Django admin/static when hitting Gunicorn directly (e.g. :8000); nginx:/static still uses STATIC_ROOT.
_whitenoise_mw = "whitenoise.middleware.WhiteNoiseMiddleware"
if _whitenoise_mw not in MIDDLEWARE:  # type: ignore[name-defined]
    _sec = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")  # type: ignore[name-defined]
    MIDDLEWARE.insert(_sec + 1, _whitenoise_mw)  # type: ignore[name-defined]

WHITENOISE_USE_FINDERS = True  # fallback so admin CSS resolves before collectstatic in dev-like runs

# Defaults match docker/docker-compose.yml `db` service (override in production via env).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("DB_NAME", "mlm_db"),
        "USER": os.environ.get("DB_USER", "mlm_user"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "mlm_pass"),
        "HOST": os.environ.get("DB_HOST", "db"),
        "PORT": os.environ.get("DB_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4", "init_command": "SET sql_mode='STRICT_TRANS_TABLES'"},
    }
}

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

SECURE_SSL_REDIRECT = False
# Admin login over http://localhost or :8000 (not only https://).
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "http://localhost,http://127.0.0.1,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
]

# Set USE_INSECURE_COOKIES=1 when running behind HTTP only (e.g. local Docker without TLS).
if os.environ.get("USE_INSECURE_COOKIES", "").lower() in ("1", "true", "yes"):
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
else:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
