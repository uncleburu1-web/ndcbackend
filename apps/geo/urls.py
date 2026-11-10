from django.urls import path

from . import views

urlpatterns = [
    # public (spec 19 §13.1)
    path("geo/states/", views.GeoStates.as_view(), name="geo-states"),
    path("geo/states/<str:state>/lgas/", views.GeoStateLgas.as_view(), name="geo-state-lgas"),
    path("geo/lgas/<int:pk>/wards/", views.GeoLgaWards.as_view(), name="geo-lga-wards"),
    path("geo/wards/<int:pk>/polling-units/", views.GeoWardUnits.as_view(), name="geo-ward-units"),
    path("geo/polling-units/", views.GeoUnitFinder.as_view(), name="geo-unit-finder"),
    path("geo/polling-units/<str:code>/", views.GeoUnitByCode.as_view(), name="geo-unit"),
    path("geo/constituencies/", views.GeoConstituencies.as_view(), name="geo-constituencies"),
    path("geo/about/", views.GeoAbout.as_view(), name="geo-about"),
    path("states/<str:state>/lgas/<str:lga>/", views.LgaPage.as_view(), name="lga-page"),
    # signed in (§13.2)
    path("me/geo-confirm/", views.MyGeoConfirm.as_view(), name="me-geo-confirm"),
    path("me/geo-confirm/<str:kind>/", views.MyGeoConfirmKind.as_view(), name="me-geo-confirm-kind"),
    # admin (§13.3)
    path("admin/geography/datasets/", views.AdminDatasets.as_view(), name="admin-geo-datasets"),
    path("admin/geography/datasets/<int:pk>/", views.AdminDatasetDetail.as_view(), name="admin-geo-dataset"),
    path("admin/geography/datasets/<int:pk>/apply/", views.AdminDatasetApply.as_view(), name="admin-geo-dataset-apply"),
    path("admin/geography/datasets/<int:pk>/changes/", views.AdminDatasetChanges.as_view(), name="admin-geo-dataset-changes"),
    path("admin/geography/review/", views.AdminReview.as_view(), name="admin-geo-review"),
    path("admin/geography/review/<str:kind>/<uuid:pk>/", views.AdminReviewAction.as_view(), name="admin-geo-review-action"),
    path("admin/geography/aliases/", views.AdminAliases.as_view(), name="admin-geo-aliases"),
    path("admin/geography/aliases/<int:pk>/", views.AdminAlias.as_view(), name="admin-geo-alias"),
]
