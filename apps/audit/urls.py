from django.urls import path

from . import views

urlpatterns = [
    path("admin/audit/", views.AuditSearch.as_view(), name="admin-audit"),
    path("admin/audit/actions/", views.AuditActions.as_view(), name="admin-audit-actions"),
]
