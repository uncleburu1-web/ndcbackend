from django.urls import path

from . import views as v

urlpatterns = [
    path("communities/", v.CommunityList.as_view(), name="communities"),
    path("communities/lookup/", v.CommunityLookup.as_view(), name="community-lookup"),
    path("communities/<uuid:pk>/", v.CommunityDetail.as_view(), name="community"),
    path("communities/<uuid:pk>/posts/", v.CommunityPosts.as_view(), name="community-posts"),
    path("community-posts/<uuid:pk>/", v.PostDetail.as_view(), name="community-post"),
    path("community-posts/<uuid:pk>/comments/", v.PostComments.as_view(), name="community-post-comments"),
    path("community-posts/<uuid:pk>/reaction/", v.PostReaction.as_view(), name="community-post-reaction"),
    path("community-posts/<uuid:pk>/report/", v.PostReport.as_view(), name="community-post-report"),
    path("community-comments/<uuid:pk>/", v.CommentDetail.as_view(), name="community-comment"),
    path("community-comments/<uuid:pk>/report/", v.CommentReport.as_view(), name="community-comment-report"),
    path("me/communities/", v.MyCommunities.as_view(), name="my-communities"),
    path("me/communities/join/", v.MyJoin.as_view(), name="my-communities-join"),
    path("me/communities/state/", v.MyState.as_view(), name="my-communities-state"),
    path("me/communities/rejoin/", v.MyRejoin.as_view(), name="my-communities-rejoin"),
    path("me/communities/<uuid:pk>/", v.MyCommunity.as_view(), name="my-community"),
    path("admin/communities/", v.AdminCommunities.as_view(), name="admin-communities"),
    path("admin/communities/<uuid:pk>/", v.AdminCommunity.as_view(), name="admin-community"),
]
