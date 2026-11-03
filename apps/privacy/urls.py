from django.urls import path

from . import views as v

REQUEST_ACTIONS = ["reply", "ask-info", "resume", "close", "actions"]
INCIDENT_ACTIONS = ["steps", "notifications", "notice-preview", "notice", "close"]

urlpatterns = [
    path("privacy/", v.PrivacyNotice.as_view(), name="privacy-notice"),
    path("me/privacy/", v.MyPrivacy.as_view(), name="my-privacy"),
    path("me/consents/<str:purpose>/", v.MyConsent.as_view(), name="my-consent"),
    path("me/consents/<str:purpose>/prompted/", v.MyConsentPrompted.as_view(), name="my-consent-prompted"),
    path("me/data-export/", v.MyDataExport.as_view(), name="my-data-export"),
    path("me/delete/", v.MyDelete.as_view(), name="my-delete"),
    path("me/privacy-requests/", v.MyPrivacyRequests.as_view(), name="my-privacy-requests"),
    path("me/privacy-requests/<uuid:pk>/answer/", v.MyPrivacyRequestAnswer.as_view(), name="my-privacy-request-answer"),
    path("admin/privacy/requests/", v.AdminRequests.as_view(), name="admin-privacy-requests"),
    path("admin/privacy/requests/<uuid:pk>/", v.AdminRequestDetail.as_view(), name="admin-privacy-request"),
    path("admin/privacy/requests/<uuid:pk>/export/", v.AdminRequestExport.as_view(), name="admin-privacy-request-export"),
    *[path(f"admin/privacy/requests/<uuid:pk>/{a}/", v.AdminRequestAction.as_view(action_name=a), name=f"admin-privacy-request-{a}")
      for a in REQUEST_ACTIONS],
    path("admin/privacy/incidents/", v.AdminIncidents.as_view(), name="admin-privacy-incidents"),
    path("admin/privacy/incidents/<uuid:pk>/", v.AdminIncidentDetail.as_view(), name="admin-privacy-incident"),
    *[path(f"admin/privacy/incidents/<uuid:pk>/{a}/", v.AdminIncidentAction.as_view(action_name=a), name=f"admin-privacy-incident-{a}")
      for a in INCIDENT_ACTIONS],
    path("admin/privacy/register/", v.AdminRegister.as_view(), name="admin-privacy-register"),
]
