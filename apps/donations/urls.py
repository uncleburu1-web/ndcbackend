from django.urls import path

from . import views

urlpatterns = [
    path("donations/config/", views.DonationConfigView.as_view(), name="donation-config"),
    path("donations/", views.StartDonationView.as_view(), name="donations"),
    path("donations/<str:reference>/", views.DonationStatusView.as_view(), name="donation-status"),
    path("me/donations/", views.MyDonationsView.as_view(), name="my-donations"),
    path("finance/donations/", views.FinanceDonationsView.as_view(), name="finance-donations"),
    path("finance/summary/", views.FinanceSummaryView.as_view(), name="finance-summary"),
    path("webhooks/payments/<str:provider>/", views.payment_webhook, name="payment-webhook"),
    path("dev/fake-checkout/<str:reference>/", views.fake_checkout, name="fake-checkout"),
    path("admin/donations/", views.AdminDonationList.as_view(), name="admin-donations"),
    path("admin/donations/export.csv", views.AdminDonationExport.as_view(), name="admin-donations-export"),
    path("admin/donations/<uuid:pk>/", views.AdminDonationDetail.as_view(), name="admin-donation"),
    path("admin/donations/<uuid:pk>/recheck/", views.AdminDonationRecheck.as_view(), name="admin-donation-recheck"),
]
