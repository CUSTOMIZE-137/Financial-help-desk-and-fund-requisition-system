"""Middleware enforcing mandatory password reset on first login."""

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from rest_framework import permissions


class PasswordResetCompletedPermission(permissions.BasePermission):
    """Block API access until a user with temporary credentials resets their password."""

    message = "You must change your temporary password before accessing the system."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and not getattr(request.user, "must_change_password", False)
        )


class ForcePasswordChangeMiddleware:
    """Redirect authenticated users with temporary credentials to the password reset page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        # Intercept authenticated sessions whose account is flagged for first-login password reset.
        if user and user.is_authenticated and getattr(user, "must_change_password", False):
            # Allow only health probes, sign-in/sign-out routes, and the password reset endpoints.
            allowed_paths = {
                reverse("favicon"),
                reverse("healthz"),
                reverse("readyz"),
                reverse("password-change"),
                reverse("logout"),
                reverse("login"),
                reverse("microsoft-login"),
                reverse("microsoft-callback"),
                reverse("teams-tab"),
                reverse("teams-auth-start"),
                reverse("teams-auth-end"),
                reverse("api-login"),
                reverse("api-logout"),
                reverse("api-password-change"),
            }
            static_prefix = "/" + getattr(settings, "STATIC_URL", "static/").lstrip("/")
            path = request.path
            if path not in allowed_paths and not path.startswith(static_prefix):
                # API callers receive an explicit 403 JSON payload; browsers are redirected to the reset form.
                if path.startswith("/api/"):
                    return JsonResponse(
                        {
                            "detail": "You must change your temporary password before accessing the system.",
                            "must_change_password": True,
                        },
                        status=403,
                    )
                return redirect("password-change")
        return self.get_response(request)


class TeamsFrameAncestorsMiddleware:
    """Allow Microsoft Teams tab embedding via strict CSP frame-ancestors when enabled."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Only Teams entry points need to be embeddable. Keep normal pages protected
        # by X-Frame-Options and the default clickjacking policy.
        if getattr(settings, "MICROSOFT_TEAMS_ENABLED", False) and request.path.startswith(
            "/teams/"
        ):
            ancestors = getattr(
                settings,
                "MICROSOFT_TEAMS_FRAME_ANCESTORS",
                "https://teams.microsoft.com https://*.teams.microsoft.com https://*.skype.com https://*.office.com",
            ).strip()
            frame_policy = f"frame-ancestors 'self' {ancestors}".strip()
            existing_csp = response.get("Content-Security-Policy", "").strip()
            if not existing_csp:
                response["Content-Security-Policy"] = frame_policy
            elif "frame-ancestors" not in existing_csp:
                response["Content-Security-Policy"] = f"{existing_csp}; {frame_policy}"
            # Modern browsers enforce CSP frame-ancestors over X-Frame-Options, but removing
            # X-Frame-Options: DENY prevents legacy WebView blocks inside Teams clients.
            if "X-Frame-Options" in response:
                del response["X-Frame-Options"]
            response.xframe_options_exempt = True
        return response
