from django.urls import path

from . import views

urlpatterns = [
    path("candidates/<slug:slug>/posts/", views.PublicPostList.as_view(), name="candidate-posts"),
    path("candidates/<slug:slug>/posts/<uuid:pk>/", views.PublicPostDetail.as_view(), name="candidate-post"),
    path("candidates/<slug:slug>/posts/<uuid:pk>/report/", views.PostReportView.as_view(), name="candidate-post-report"),
    path("campaign/candidates/", views.CampaignCandidates.as_view(), name="campaign-candidates"),
    path("campaign/candidates/<uuid:candidate_id>/posts/", views.CampaignPosts.as_view(), name="campaign-posts"),
    path("campaign/candidates/<uuid:candidate_id>/posts/<uuid:post_id>/", views.CampaignPostDetail.as_view(), name="campaign-post"),
    path("campaign/candidates/<uuid:candidate_id>/posts/<uuid:post_id>/image/", views.CampaignPostImage.as_view(), name="campaign-post-image"),
    path("admin/posts/", views.AdminPosts.as_view(), name="admin-posts"),
    path("admin/posts/reports/<uuid:pk>/dismiss/", views.AdminReportDismiss.as_view(), name="admin-post-report-dismiss"),
    path("admin/posts/<uuid:pk>/", views.AdminPostDetail.as_view(), name="admin-post"),
    path("admin/posts/<uuid:pk>/remove/", views.AdminPostAction.as_view(action_name="remove"), name="admin-post-remove"),
    path("admin/posts/<uuid:pk>/restore/", views.AdminPostAction.as_view(action_name="restore"), name="admin-post-restore"),
    path("admin/candidates/<uuid:pk>/posts/", views.AdminCandidatePosts.as_view(), name="admin-candidate-posts"),
]
