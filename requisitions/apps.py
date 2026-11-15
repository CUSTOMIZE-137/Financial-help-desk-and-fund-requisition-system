"""Django app configuration for requisition management."""

from django.apps import AppConfig


class RequisitionsConfig(AppConfig):
    """Register the requisitions app and its default primary-key type."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "requisitions"

    def ready(self):
        from requisitions.cache import register_cache_signals

        register_cache_signals()
