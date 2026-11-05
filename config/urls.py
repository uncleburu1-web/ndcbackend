from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

api_v1 = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.candidates.urls")),
    path("", include("apps.follows.urls")),
    path("", include("apps.donations.urls")),
    path("", include("apps.payments.urls")),
    path("", include("apps.pvc.urls")),
    path("", include("apps.membership.urls")),
    path("", include("apps.news.urls")),
    path("", include("apps.notifications.urls")),
    path("", include("apps.posts.urls")),
    path("", include("apps.access.urls")),
    path("", include("apps.audit.urls")),
    path("", include("apps.dashboard.urls")),
    path("", include("apps.search.urls")),
    path("", include("apps.moderation.urls")),
    path("", include("apps.privacy.urls")),
    path("", include("apps.integrations.urls")),
    path("", include("apps.media.urls")),
    path("", include("apps.communities.urls")),  # spec 17
    path("", include("apps.events.urls")),  # spec 18
    path("", include("apps.volunteers.urls")),  # spec 18
    path("", include("apps.core.schema_urls")),
]

urlpatterns = [path("api/v1/", include(api_v1))]

if settings.DEBUG:  # development only; production serves media from the storage provider
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
