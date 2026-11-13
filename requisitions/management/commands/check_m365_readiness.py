"""Management command to verify production readiness for Microsoft 365 SSO, Teams, and HTTPS cutover."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from requisitions.entra_auth import MULTI_TENANT_ALIASES, _normalize_role_code
from requisitions.teams import build_teams_manifest


class Command(BaseCommand):
    help = "Verify HTTPS, Entra ID OIDC SSO, role mapping, and Microsoft Teams readiness before production cutover."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit with a non-zero status code if any readiness warning or error is found.",
        )

    def handle(self, *args, **options):
        strict = bool(options["strict"])
        errors: list[str] = []
        warnings: list[str] = []

        # 1. HTTPS SITE_URL, ALLOWED_HOSTS, and CSRF_TRUSTED_ORIGINS check
        site_url = getattr(settings, "SITE_URL", "").strip()
        parsed_site = urlparse(site_url)
        if parsed_site.scheme.lower() != "https":
            warnings.append(
                f"SITE_URL ({site_url!r}) does not use https://. Microsoft Entra ID and Teams require HTTPS in production."
            )
        if parsed_site.hostname:
            allowed_hosts = set(getattr(settings, "ALLOWED_HOSTS", ()))
            if "*" not in allowed_hosts and parsed_site.hostname not in allowed_hosts:
                errors.append(
                    f"SITE_URL hostname '{parsed_site.hostname}' is not listed in DJANGO_ALLOWED_HOSTS."
                )
            origin = f"{parsed_site.scheme}://{parsed_site.netloc}"
            trusted_origins = set(getattr(settings, "CSRF_TRUSTED_ORIGINS", ()))
            if parsed_site.scheme.lower() == "https" and origin not in trusted_origins:
                warnings.append(
                    f"Origin '{origin}' should be included in CSRF_TRUSTED_ORIGINS for HTTPS POST requests."
                )

        # 2. Microsoft Entra ID OIDC SSO configuration
        if getattr(settings, "MICROSOFT_AUTH_ENABLED", False):
            tenant_id = getattr(settings, "MICROSOFT_TENANT_ID", "").strip()
            client_id = getattr(settings, "MICROSOFT_CLIENT_ID", "").strip()
            client_secret = getattr(settings, "MICROSOFT_CLIENT_SECRET", "").strip()
            allowed_tenants = set(getattr(settings, "MICROSOFT_ALLOWED_TENANTS", ()))

            if not tenant_id:
                errors.append("MICROSOFT_TENANT_ID is empty while MICROSOFT_AUTH_ENABLED=True.")
            elif tenant_id.lower() in MULTI_TENANT_ALIASES and not allowed_tenants:
                errors.append(
                    f"MICROSOFT_TENANT_ID={tenant_id!r} requires MICROSOFT_ALLOWED_TENANTS to enforce tenant isolation."
                )
            if not client_id:
                errors.append("MICROSOFT_CLIENT_ID is empty while MICROSOFT_AUTH_ENABLED=True.")
            if not client_secret:
                errors.append("MICROSOFT_CLIENT_SECRET is empty while MICROSOFT_AUTH_ENABLED=True.")
        else:
            warnings.append("MICROSOFT_AUTH_ENABLED is currently False (SSO is disabled).")

        # 3. Role Mapping validation
        role_mapping = getattr(settings, "MICROSOFT_ROLE_MAPPING", {}) or {}
        for group_key, spec in role_mapping.items():
            try:
                role_val = spec.get("role", "") if isinstance(spec, dict) else str(spec)
                _normalize_role_code(str(role_val))
            except Exception as exc:
                errors.append(f"Invalid MICROSOFT_ROLE_MAPPING entry for '{group_key}': {exc}")

        # 4. Microsoft Teams configuration & manifest validation
        if getattr(settings, "MICROSOFT_TEAMS_ENABLED", False):
            try:
                build_teams_manifest(require_https=(parsed_site.scheme.lower() == "https"))
            except ValidationError as exc:
                errors.append(f"Microsoft Teams manifest validation failed: {'; '.join(exc.messages)}")

        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"[WARN] {warning}"))
        for error in errors:
            self.stdout.write(self.style.ERROR(f"[FAIL] {error}"))

        if errors or (strict and warnings):
            raise CommandError(
                f"Microsoft 365 readiness check failed ({len(errors)} error(s), {len(warnings)} warning(s))."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Microsoft 365 readiness check passed ({len(warnings)} warning(s), 0 errors)."
            )
        )
