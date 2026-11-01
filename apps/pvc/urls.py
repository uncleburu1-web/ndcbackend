from django.urls import path

from . import views

urlpatterns = [
    path("me/pvc/", views.MyPvc.as_view(), name="my-pvc"),
    path("admin/pvc/", views.AdminPvcList.as_view(), name="admin-pvc"),
    *[path(f"admin/pvc/<uuid:pk>/{a}/", views.AdminPvcAction.as_view(action_name=a), name=f"admin-pvc-{a}")
      for a in ("verify", "reject", "reveal")],
]
