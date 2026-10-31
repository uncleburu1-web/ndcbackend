from django.urls import path

from . import views

urlpatterns = [
    path("me/follows/", views.MyFollows.as_view(), name="my-follows"),
    # <str:> rather than <slug:>: the view validates the slug itself so every bad value gets the same answer (§9).
    path("me/follows/<str:slug>/", views.MyFollowDetail.as_view(), name="my-follow"),
]
