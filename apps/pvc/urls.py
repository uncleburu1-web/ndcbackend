from django.urls import path

from . import views

urlpatterns = [
    path("pvc/config/", views.PvcConfig.as_view(), name="pvc-config"),
    path("me/pvc/", views.MyPvc.as_view(), name="my-pvc"),
    path("me/pvc/badge/", views.MyPvcBadge.as_view(), name="my-pvc-badge"),
    path("admin/pvc/", views.AdminPvcList.as_view(), name="admin-pvc"),
    path("admin/pvc/settings/", views.AdminPvcSettings.as_view(), name="admin-pvc-settings"),
    *[path(f"admin/pvc/<uuid:pk>/{a}/", views.AdminPvcAction.as_view(action_name=a), name=f"admin-pvc-{a}")
      for a in ("verify", "reject", "reveal")],
]
