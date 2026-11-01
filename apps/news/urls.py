from django.urls import path

from . import views

urlpatterns = [
    path("news/", views.NewsList.as_view(), name="news-list"),
    path("news/<uuid:pk>/", views.NewsDetail.as_view(), name="news-detail"),
    path("news/<uuid:pk>/report/", views.NewsReport.as_view(), name="news-report"),
    path("me/news/", views.MyNews.as_view(), name="my-news"),
    path("me/news/<uuid:pk>/", views.MyNewsDetail.as_view(), name="my-news-detail"),
    path("me/news/<uuid:pk>/image/", views.MyNewsImage.as_view(), name="my-news-image"),
    path("me/news/<uuid:pk>/withdraw/", views.MyNewsWithdraw.as_view(), name="my-news-withdraw"),
    path("admin/news/", views.AdminNewsList.as_view(), name="admin-news"),
    path("admin/news/reports/<uuid:pk>/dismiss/", views.AdminReportDismiss.as_view(), name="admin-news-report-dismiss"),
    path("admin/news/<uuid:pk>/", views.AdminNewsDetail.as_view(), name="admin-news-detail"),
    *[path(f"admin/news/<uuid:pk>/{a}/", views.AdminNewsAction.as_view(action_name=a), name=f"admin-news-{a}")
      for a in ("approve", "reject", "remove", "restore")],
    path("admin/announcements/", views.AdminAnnouncementList.as_view(), name="admin-announcements"),
    path("admin/announcements/notify-count/", views.AnnouncementNotifyCount.as_view(), name="admin-announcements-notify-count"),
    path("admin/announcements/<uuid:pk>/", views.AdminAnnouncementDetail.as_view(), name="admin-announcement-detail"),
    path("admin/announcements/<uuid:pk>/image/", views.AnnouncementImage.as_view(), name="admin-announcement-image"),
    *[path(f"admin/announcements/<uuid:pk>/{a}/", views.AnnouncementAction.as_view(action_name=a), name=f"admin-announcement-{a}")
      for a in ("pin", "unpin", "unpublish", "restore")],
]
