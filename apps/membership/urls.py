from django.urls import path

from . import views

ACTIONS = ["approve", "reject", "cancel", "reveal-nin", "recheck-payment"]

urlpatterns = [
    path("membership/config/", views.MembershipConfigView.as_view(), name="membership-config"),
    path("membership/payments/<str:reference>/", views.PaymentStatusView.as_view(), name="membership-payment-status"),
    path("me/membership/", views.MyMembership.as_view(), name="my-membership"),
    path("me/membership/photo/", views.MyMembershipPhoto.as_view(), name="my-membership-photo"),
    path("me/membership/pay/", views.MyMembershipPay.as_view(), name="my-membership-pay"),
    path("me/membership/cancel/", views.MyMembershipCancel.as_view(), name="my-membership-cancel"),
    path("me/membership/card/", views.MyMembershipCard.as_view(), name="my-membership-card"),
    path("admin/membership/", views.AdminMembershipList.as_view(), name="admin-membership"),
    path("admin/membership/settings/", views.AdminMembershipSettingsView.as_view(), name="admin-membership-settings"),
    path("admin/membership/payments/", views.AdminMembershipPayments.as_view(), name="admin-membership-payments"),
    path("admin/membership/register.csv", views.AdminRegisterCsv.as_view(), name="admin-membership-register"),
    path("admin/membership/register-photos.zip", views.AdminRegisterPhotos.as_view(), name="admin-membership-register-photos"),
    path("admin/membership/<uuid:pk>/", views.AdminMembershipDetail.as_view(), name="admin-membership-detail"),
    path("admin/membership/<uuid:pk>/photo/", views.AdminMembershipPhoto.as_view(), name="admin-membership-photo"),
    *[path(f"admin/membership/<uuid:pk>/{a}/", views.AdminMembershipAction.as_view(action_name=a), name=f"admin-membership-{a}")
      for a in ACTIONS],
]
