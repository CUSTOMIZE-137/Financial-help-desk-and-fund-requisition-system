"""Browser routes for submitting and reviewing requisitions."""

from django.urls import path

from requisitions import views

urlpatterns = [
    path("new/", views.requisition_create_view, name="requisition-create"),
    path("<int:pk>/", views.requisition_detail_view, name="requisition-detail"),
    path("<int:pk>/edit/", views.requisition_edit_view, name="requisition-edit"),
    path("<int:pk>/action/", views.requisition_action_view, name="requisition-action"),
    path(
        "<int:pk>/accountability/",
        views.accountability_create_view,
        name="accountability-create",
    ),
    path("files/<str:kind>/<int:pk>/", views.protected_file_view, name="protected-file"),
]
