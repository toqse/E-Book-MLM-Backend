import os
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
# Load project .env regardless of process cwd (fixes missing DEFAULT_* when running from repo root/Docker).
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")
# Optional separate secret for agreement acceptance proof HMAC; falls back to SECRET_KEY in proof_service.
AGREEMENT_PROOF_SIGNING_SECRET = os.environ.get("AGREEMENT_PROOF_SIGNING_SECRET", "").strip()
DEBUG = os.environ.get("DEBUG", "False").lower() in ("1", "true", "yes")

# When True, OTP is included in JSON and logged (never use in real production unless you intend it).
EXPOSE_OTP_IN_RESPONSE = os.environ.get("EXPOSE_OTP_IN_RESPONSE", "true").lower() in (
    "1",
    "true",
    "yes",
)


def _env_positive_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        n = int(str(raw).strip())
    except ValueError:
        return default
    return n if n > 0 else default


# Upload size limits (Django defaults are small for large PDFs).
MAX_UPLOAD_MB = _env_positive_int("MAX_UPLOAD_MB", 100)
_MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = _MAX_UPLOAD_BYTES
FILE_UPLOAD_MAX_MEMORY_SIZE = _MAX_UPLOAD_BYTES

# OTP send burst limit per phone/email (cache-backed; same window as OTP_SEND_WINDOW_SECONDS).
OTP_SEND_MAX_PER_WINDOW = _env_positive_int("OTP_SEND_MAX_PER_WINDOW", 3)
OTP_SEND_WINDOW_SECONDS = _env_positive_int("OTP_SEND_WINDOW_SECONDS", 600)

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# Trust the upstream proxy/tunnel (cloudflared, nginx, Azure Front Door) for scheme + host so
# request.build_absolute_uri() emits https:// public URLs (signed PDF links, media, invoice URLs).
# Safe in dev: only takes effect when the header is actually present.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "apps.common",
    "apps.users",
    "apps.authentication",
    "apps.mlm_tree",
    "apps.commissions",
    "apps.wallet",
    "apps.sponsor_slots",
    "apps.courses",
    "apps.cart",
    "apps.payments",
    "apps.finance",
    "apps.admin_panel",
    "apps.notifications",
    "apps.audit",
    "apps.agreements",
    "apps.tds",
    "apps.banners",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
# Local/proxied media serving toggle (use nginx/object storage in real production).
SERVE_MEDIA = os.environ.get("SERVE_MEDIA", "false").lower() in ("1", "true", "yes")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.common.auth.JWTAuthenticationWithAccountStatus",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_PARSER_CLASSES": (
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "user": "100/min",
        "anon": "30/min",
    },
    "EXCEPTION_HANDLER": "apps.common.exceptions.envelope_exception_handler",
    # Ensure consistent date rendering across API responses (DRF DateField).
    "DATE_FORMAT": "%d/%m/%Y",
}

ACCESS_MIN = max(1, int(os.environ.get("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", "60")))

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=ACCESS_MIN),
    # Access-only JWTs — refresh flow disabled (no issuance, no rotation).
    "REFRESH_TOKEN_LIFETIME": timedelta(days=36500),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if o.strip()
]

CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 30

CELERY_BEAT_SCHEDULE = {
    "expire-sponsor-slots-daily": {
        "task": "apps.sponsor_slots.tasks.expire_sponsor_slots",
        "schedule": crontab(hour=0, minute=5),
    },
    "auto-place-pending-binary": {
        "task": "apps.mlm_tree.tasks.auto_place_pending_placements",
        "schedule": crontab(minute="*/5"),
    },
    "kyc-invitations-after-refund": {
        "task": "apps.users.tasks.send_kyc_invitations_after_refund",
        "schedule": crontab(minute=15),
    },
}

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_PAYOUT_KEY_ID = os.environ.get("RAZORPAY_PAYOUT_KEY_ID", "")
RAZORPAY_PAYOUT_KEY_SECRET = os.environ.get("RAZORPAY_PAYOUT_KEY_SECRET", "")

FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000")
KYC_INVITE_WEB_PATH = os.environ.get("KYC_INVITE_WEB_PATH", "/compliance")
KYC_INVITE_MOBILE_URL = os.environ.get("KYC_INVITE_MOBILE_URL", "").strip()
KYC_INVITE_TOKEN_MAX_AGE_DAYS = int(os.environ.get("KYC_INVITE_TOKEN_MAX_AGE_DAYS", "30"))
# Reserved referral code for the company root; maps to the primary superuser (see users.services).
DEFAULT_COMPANY_REFERRAL_CODE = (
    os.environ.get("DEFAULT_COMPANY_REFERRAL_CODE", "Admin") or "Admin"
).strip()
COMPANY_SUPERUSER_MEMBER_ID = os.environ.get("COMPANY_SUPERUSER_MEMBER_ID", "SYS000001")
GST_NUMBER = os.environ.get("GST_NUMBER", "")
COMPANY_PAN = os.environ.get("PAN_NUMBER", "")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Just 200")
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")
COMPANY_PHONE = os.environ.get("COMPANY_PHONE", "")
COMPANY_EMAIL = os.environ.get("COMPANY_EMAIL", "")
COMPANY_WEBSITE = os.environ.get("COMPANY_WEBSITE", "")
# Multiline OK in .env when quoted — used on GST invoice PDF (payment / footer text).
INVOICE_PAYMENT_DETAILS = os.environ.get("INVOICE_PAYMENT_DETAILS", "").strip()
INVOICE_TERMS_AND_CONDITIONS = os.environ.get("INVOICE_TERMS_AND_CONDITIONS", "").strip()

# OTP send endpoints log to stderr when EXPOSE_OTP_IN_RESPONSE is true.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "{levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "loggers": {
        "apps.authentication": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
