from django.urls import include, path

api_v1 = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
]

urlpatterns = [path("api/v1/", include(api_v1))]
