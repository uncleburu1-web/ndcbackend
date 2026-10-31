from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

api_v1 = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.candidates.urls")),
    path("", include("apps.follows.urls")),
]

urlpatterns = [path("api/v1/", include(api_v1))]

if settings.DEBUG:  # development only; production serves media from the storage provider
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
