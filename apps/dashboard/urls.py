from django.urls import path

from . import views

urlpatterns = [path("admin/overview/", views.Overview.as_view(), name="admin-overview")]
