from django.urls import path

from . import views

ACTIONS = ["verify", "unverify", "publish", "unpublish", "archive", "restore"]

urlpatterns = [
    # public
    path("candidates/", views.PublicCandidateList.as_view(), name="candidates"),
    path("candidates/positions/", views.PublicPositions.as_view(), name="candidate-positions"),
    path("candidates/<slug:slug>/", views.PublicCandidateDetail.as_view(), name="candidate-detail"),
    # admin
    path("admin/candidates/", views.AdminCandidateList.as_view(), name="admin-candidates"),
    path("admin/candidates/<uuid:pk>/", views.AdminCandidateDetail.as_view(), name="admin-candidate"),
    path("admin/candidates/<uuid:pk>/photo/", views.AdminCandidatePhoto.as_view(), name="admin-candidate-photo"),
    path("admin/candidates/<uuid:pk>/accounts/", views.AdminCandidateAccounts.as_view(), name="admin-candidate-accounts"),
    path("admin/candidates/<uuid:pk>/accounts/<uuid:user_id>/", views.AdminCandidateAccountDetail.as_view(),
         name="admin-candidate-account"),
    *[
        path(f"admin/candidates/<uuid:pk>/{a}/", views.AdminCandidateAction.as_view(action_name=a), name=f"admin-candidate-{a}")
        for a in ACTIONS
    ],
]
