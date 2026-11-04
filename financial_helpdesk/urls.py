"""Top-level routes for the web interface, admin site, and REST API."""

from django.contrib import admin
from django.urls import include, path

from requisitions import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.dashboard_view, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("signature/", views.signature_settings_view, name="signature-settings"),
    path("approval-queue/", views.approval_queue_view, name="approval-queue"),
    path("requisitions/", include("requisitions.web_urls")),
    path("api/", include("requisitions.urls")),
]
