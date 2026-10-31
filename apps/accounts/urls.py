from django.urls import path

from .views import MeView
from .webhooks import clerk_webhook

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("webhooks/clerk/", clerk_webhook, name="clerk-webhook"),
]
