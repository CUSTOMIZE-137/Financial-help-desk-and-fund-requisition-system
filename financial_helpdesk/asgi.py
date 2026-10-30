"""ASGI entry point for serving the Django project asynchronously."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_helpdesk.settings")

application = get_asgi_application()
