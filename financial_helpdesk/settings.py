"""Project configuration, with local SQLite defaults and environment overrides."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

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

DATABASE_ENGINE = os.getenv("DB_ENGINE", "")
# An explicit engine takes precedence; otherwise support the legacy PostgreSQL
# environment variables or use SQLite for a zero-configuration local checkout.
if DATABASE_ENGINE:
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
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

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

# Keep collected assets separate from uploaded evidence files.
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "media/"

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
    ],
}

# Browsers must send session and CSRF cookies over HTTPS outside development.
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
