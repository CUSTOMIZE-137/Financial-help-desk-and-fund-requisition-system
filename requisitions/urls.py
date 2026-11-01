"""REST API routes for users, budgets, requests, and accountability."""

from django.urls import path

from requisitions import views

urlpatterns = [
    path("auth/login/", views.LoginAPIView.as_view(), name="api-login"),
    path("auth/logout/", views.LogoutAPIView.as_view(), name="api-logout"),
    path(
        "files/<str:kind>/<int:pk>/",
        views.ProtectedFileAPIView.as_view(),
        name="api-protected-file",
    ),
    path("users/", views.UserListCreateView.as_view(), name="user-list-create"),

    path(
        "requisitions/",
        views.RequisitionListCreateView.as_view(),
        name="requisition-list-create",
    ),
    path(
        "requisitions/<int:pk>/",
        views.RequisitionDetailView.as_view(),
        name="requisition-detail-api",
    ),
    path(
        "requisitions/<int:pk>/transition/",
        views.RequisitionActionView.as_view(),
        name="requisition-transition",
    ),
    path(
        "requisition-logs/",
        views.RequisitionLogListView.as_view(),
        name="requisition-log-list",
    ),
    path(
        "accountability/",
        views.AccountabilitySubmissionListCreateView.as_view(),
        name="accountability-list-create",
    ),
    path(
        "attachments/",
        views.AttachmentListCreateView.as_view(),
        name="attachment-list-create",
    ),
]
