from django.urls import path

from . import views

urlpatterns = [
    path("search/", views.SearchView.as_view(), name="search"),
    path("search/suggest/", views.SuggestView.as_view(), name="search-suggest"),
    path("states/", views.StatesView.as_view(), name="states"),
    path("states/<str:state>/", views.StateDetailView.as_view(), name="state-detail"),
]
