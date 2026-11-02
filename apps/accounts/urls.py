from django.urls import path

from . import admin_views
from .views import MeView
from .webhooks import clerk_webhook

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("webhooks/clerk/", clerk_webhook, name="clerk-webhook"),
    # spec 11: admin users
    path("admin/users/", admin_views.UsersView.as_view(), name="admin-users"),
    path("admin/users/<uuid:pk>/", admin_views.UserDetailView.as_view(), name="admin-user"),
    path("admin/users/<uuid:pk>/phone/", admin_views.UserPhoneView.as_view(), name="admin-user-phone"),
    path("admin/users/<uuid:pk>/deactivate/", admin_views.UserActionView.as_view(action="deactivate"), name="admin-user-deactivate"),
    path("admin/users/<uuid:pk>/reactivate/", admin_views.UserActionView.as_view(action="reactivate"), name="admin-user-reactivate"),
]
