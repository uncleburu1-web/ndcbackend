from django.urls import path

from . import views

urlpatterns = [
    path("admin/team/", views.TeamView.as_view(), name="admin-team"),
    path("admin/team/review/", views.TeamReviewView.as_view(), name="admin-team-review"),
    path("admin/team/<uuid:pk>/revoke/", views.TeamRevokeView.as_view(), name="admin-team-revoke"),
]
