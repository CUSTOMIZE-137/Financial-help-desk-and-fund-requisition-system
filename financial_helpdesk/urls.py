"""Top-level routes for the web interface, admin site, and REST API."""

from django.contrib import admin
from django.urls import include, path

from requisitions import views

urlpatterns = [
    path("healthz/", views.healthz_view, name="healthz"),
    path("readyz/", views.readyz_view, name="readyz"),
    path("admin/", admin.site.urls),
    path("", views.dashboard_view, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password-change/", views.password_change_view, name="password-change"),
    path("users/enroll/", views.user_enrollment_view, name="user-enrollment"),
    path("signature/", views.signature_settings_view, name="signature-settings"),
    path("approval-queue/", views.approval_queue_view, name="approval-queue"),
    path("reports/internal-audit/", views.internal_audit_report_view, name="internal-audit-report"),
    path("requisitions/", include("requisitions.web_urls")),
    path("api/", include("requisitions.urls")),
]
