"""Project configuration, with local SQLite defaults and environment overrides."""

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

from financial_helpdesk.storage import build_storages_setting

BASE_DIR = Path(__file__).resolve().parent.parent

# Read environment configuration without requiring a dotenv dependency.
DEBUG = os.getenv("DJANGO_DEBUG", os.getenv("DEBUG", "True")).lower() in {
    "1",
    "true",
    "yes",
}
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", os.getenv("SECRET_KEY", ""))
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is false.")
    SECRET_KEY = "local-development-only-change-before-deployment"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "DJANGO_ALLOWED_HOSTS", os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1")
    ).split(",")
    if host.strip()
]

# Register Django's built-ins, the REST API, and the requisition domain app.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "requisitions.apps.RequisitionsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "requisitions.middleware.ForcePasswordChangeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "financial_helpdesk.urls"

# Shared templates receive request, authentication, messages, and role navigation.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "requisitions.context_processors.role_navigation",
            ],
        },
    },
]

# Expose both deployment entry points for servers using either protocol.
WSGI_APPLICATION = "financial_helpdesk.wsgi.application"
ASGI_APPLICATION = "financial_helpdesk.asgi.application"


def _build_postgres_options() -> dict:
    options = {"connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10"))}
    sslmode = os.getenv("DB_SSLMODE", "").strip()
    if sslmode:
        options["sslmode"] = sslmode
    return options


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_ENGINE = os.getenv("DB_ENGINE", "").strip()
DB_CONN_MAX_AGE = int(os.getenv("DB_CONN_MAX_AGE", "60"))
DB_CONN_HEALTH_CHECKS = os.getenv("DB_CONN_HEALTH_CHECKS", "True").lower() in {
    "1",
    "true",
    "yes",
}

# Support DATABASE_URL (postgres:// or sqlite://), explicit DB_ENGINE, or local SQLite.
if DATABASE_URL:
    parsed_db = urlparse(DATABASE_URL)
    if parsed_db.scheme in ("postgres", "postgresql", "pgsql"):
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": unquote(parsed_db.path.lstrip("/")) or "financial_helpdesk",
                "USER": unquote(parsed_db.username or ""),
                "PASSWORD": unquote(parsed_db.password or ""),
                "HOST": parsed_db.hostname or "localhost",
                "PORT": str(parsed_db.port or 5432),
                "CONN_MAX_AGE": DB_CONN_MAX_AGE,
                "CONN_HEALTH_CHECKS": DB_CONN_HEALTH_CHECKS,
                "OPTIONS": _build_postgres_options(),
            }
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": unquote(parsed_db.path.lstrip("/")) or str(BASE_DIR / "db.sqlite3"),
            }
        }
elif DATABASE_ENGINE:
    if DATABASE_ENGINE == "django.db.backends.sqlite3":
        DATABASES = {
            "default": {
                "ENGINE": DATABASE_ENGINE,
                "NAME": os.getenv("DB_NAME", str(BASE_DIR / "db.sqlite3")),
            }
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": DATABASE_ENGINE,
                "NAME": os.getenv("DB_NAME", "financial_helpdesk"),
                "USER": os.getenv("DB_USER", ""),
                "PASSWORD": os.getenv("DB_PASSWORD", ""),
                "HOST": os.getenv("DB_HOST", ""),
                "PORT": os.getenv("DB_PORT", ""),
                "CONN_MAX_AGE": DB_CONN_MAX_AGE,
                "CONN_HEALTH_CHECKS": DB_CONN_HEALTH_CHECKS,
                "OPTIONS": _build_postgres_options()
                if "postgresql" in DATABASE_ENGINE
                else {},
            }
        }
elif os.getenv("DB_NAME") and os.getenv("DB_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "financial_helpdesk"),
            "USER": os.getenv("DB_USER", ""),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "CONN_MAX_AGE": DB_CONN_MAX_AGE,
            "CONN_HEALTH_CHECKS": DB_CONN_HEALTH_CHECKS,
            "OPTIONS": _build_postgres_options(),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Caching configuration (Redis in production when REDIS_URL is set, LocMemCache otherwise).
CACHE_DEFAULT_TIMEOUT = int(os.getenv("CACHE_DEFAULT_TIMEOUT", "300"))
REDIS_URL = os.getenv("REDIS_URL", os.getenv("CACHE_URL", "")).strip()
CACHE_BACKEND = os.getenv("CACHE_BACKEND", "").strip()
if CACHE_BACKEND:
    CACHES = {
        "default": {
            "BACKEND": CACHE_BACKEND,
            "LOCATION": os.getenv("CACHE_LOCATION", REDIS_URL or "financial-helpdesk-cache"),
            "TIMEOUT": CACHE_DEFAULT_TIMEOUT,
        }
    }
elif REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": CACHE_DEFAULT_TIMEOUT,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "financial-helpdesk-local",
            "TIMEOUT": CACHE_DEFAULT_TIMEOUT,
        }
    }

SESSION_ENGINE = os.getenv(
    "SESSION_ENGINE",
    "django.contrib.sessions.backends.cached_db"
    if REDIS_URL
    else "django.contrib.sessions.backends.db",
)

# Use the workflow-aware account model and Django's standard password policy.
AUTH_USER_MODEL = "requisitions.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static and shared/object media storage configuration.
STATIC_URL = os.getenv("STATIC_URL", "static/")
STATIC_ROOT = Path(os.getenv("STATIC_ROOT", str(BASE_DIR / "staticfiles")))
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(BASE_DIR / "media")))
MEDIA_URL = os.getenv("MEDIA_URL", "media/")

AWS_STORAGE_BUCKET_NAME = os.getenv(
    "AWS_STORAGE_BUCKET_NAME", os.getenv("S3_BUCKET_NAME", "")
).strip()
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "us-east-1").strip()
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL", "").strip() or None
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "").strip() or None
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip() or None
AWS_LOCATION = os.getenv("AWS_LOCATION", "media").strip("/")
AWS_S3_CUSTOM_DOMAIN = os.getenv("AWS_S3_CUSTOM_DOMAIN", "").strip()
AWS_QUERYSTRING_AUTH = os.getenv("AWS_QUERYSTRING_AUTH", "True").lower() in {
    "1",
    "true",
    "yes",
}
AWS_QUERYSTRING_EXPIRE = int(os.getenv("AWS_QUERYSTRING_EXPIRE", "300"))
AWS_S3_ENCRYPT = os.getenv("AWS_S3_ENCRYPT", "True").lower() in {"1", "true", "yes"}
STORAGE_USE_SIGNED_REDIRECT = os.getenv("STORAGE_USE_SIGNED_REDIRECT", "False").lower() in {
    "1",
    "true",
    "yes",
}
STORAGES = build_storages_setting(debug=DEBUG)

# Background task queue configuration ("eager", "thread", or "db_queue").
BACKGROUND_TASK_MODE = os.getenv("BACKGROUND_TASK_MODE", "eager").strip().lower()
WORKFLOW_EMAIL_NOTIFICATIONS = os.getenv(
    "WORKFLOW_EMAIL_NOTIFICATIONS", "False"
).lower() in {"1", "true", "yes"}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Browser sessions support the UI; Basic auth is available to API clients.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "requisitions.middleware.PasswordResetCompletedPermission",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
}

# Email and site URL configuration for user enrollment invitations.
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:8000")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in {"1", "true", "yes"}
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() in {"1", "true", "yes"}
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@financedesk.local")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# Browsers must send session and CSRF cookies over HTTPS outside development.
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "False").lower() in {
        "1",
        "true",
        "yes",
    }
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Structured operational logging.
LOG_LEVEL = os.getenv("DJANGO_LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "requisitions": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}
