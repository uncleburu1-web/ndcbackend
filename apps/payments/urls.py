from django.urls import path

from . import views

urlpatterns = [
    path("webhooks/payments/<str:provider>/", views.payment_webhook, name="payment-webhook"),
    path("dev/fake-checkout/<str:reference>/", views.fake_checkout, name="fake-checkout"),
]
