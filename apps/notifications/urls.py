from django.urls import path

from . import views

urlpatterns = [
    path("me/notifications/", views.NotificationList.as_view(), name="my-notifications"),
    path("me/notifications/unread-count/", views.UnreadCount.as_view(), name="my-notifications-unread"),
    path("me/notifications/read-all/", views.MarkAllRead.as_view(), name="my-notifications-read-all"),
    path("me/notifications/preferences/", views.Preferences.as_view(), name="my-notifications-preferences"),
    path("me/notifications/<uuid:pk>/read/", views.MarkRead.as_view(), name="my-notification-read"),
]
