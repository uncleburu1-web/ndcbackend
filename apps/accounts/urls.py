from django.urls import path

from . import admin_views
from . import settings_views as sv
from .views import MeView
from .webhooks import clerk_webhook

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    # spec 20: settings (private, no-store)
    path("me/settings/", sv.MySettings.as_view(), name="my-settings"),
    path("me/settings/profile/", sv.MyProfileSettings.as_view(), name="my-settings-profile"),
    path("me/settings/public-profile/", sv.MyPublicProfile.as_view(), name="my-settings-public-profile"),
    path("me/settings/language/", sv.MyLanguage.as_view(), name="my-settings-language"),
    path("me/settings/accessibility/", sv.MyAccessibility.as_view(), name="my-settings-accessibility"),
    path("me/settings/notifications/", sv.MyNotificationSettings.as_view(), name="my-settings-notifications"),
    path("me/settings/privacy/", sv.MyPrivacySettings.as_view(), name="my-settings-privacy"),
    path("me/settings/security/sign-out-others/", sv.MySignOutOthers.as_view(), name="my-settings-sign-out-others"),
    path("me/settings/import-device/", sv.MyImportDevice.as_view(), name="my-settings-import-device"),
    path("me/pause/", sv.MyPause.as_view(), name="my-pause"),
    path("me/reactivate/", sv.MyReactivate.as_view(), name="my-reactivate"),
    path("people/<str:handle>/", sv.PublicProfileView.as_view(), name="public-profile"),
    path("people/<str:handle>/report/", sv.PublicProfileReport.as_view(), name="public-profile-report"),
    path("admin/settings/defaults/", sv.AdminSettingsDefaults.as_view(), name="admin-settings-defaults"),
    path("webhooks/clerk/", clerk_webhook, name="clerk-webhook"),
    # spec 11: admin users
    path("admin/users/", admin_views.UsersView.as_view(), name="admin-users"),
    path("admin/users/<uuid:pk>/", admin_views.UserDetailView.as_view(), name="admin-user"),
    path("admin/users/<uuid:pk>/phone/", admin_views.UserPhoneView.as_view(), name="admin-user-phone"),
    path("admin/users/<uuid:pk>/deactivate/", admin_views.UserActionView.as_view(action="deactivate"), name="admin-user-deactivate"),
    path("admin/users/<uuid:pk>/reactivate/", admin_views.UserActionView.as_view(action="reactivate"), name="admin-user-reactivate"),
]
