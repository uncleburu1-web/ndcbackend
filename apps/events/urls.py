from django.urls import path

from . import views as v

ACTIONS = ["publish", "cancel", "close-registration", "updates"]

urlpatterns = [
    path("events/", v.EventList.as_view(), name="events"),
    path("events/<uuid:pk>/", v.EventDetail.as_view(), name="event"),
    path("events/<uuid:pk>/calendar.ics", v.EventCalendar.as_view(), name="event-calendar"),
    path("events/<uuid:pk>/me/", v.EventMe.as_view(), name="event-me"),
    path("events/<uuid:pk>/registration/", v.EventRegistrationView.as_view(), name="event-registration"),
    path("events/<uuid:pk>/report/", v.EventReport.as_view(), name="event-report"),
    path("me/events/", v.MyEvents.as_view(), name="my-events"),
    # campaign events (09's linked accounts)
    path("campaign/<uuid:candidate_id>/events/", v.OrganiserEvents.as_view(), name="campaign-events"),
    path("campaign/<uuid:candidate_id>/events/<uuid:pk>/", v.OrganiserEventDetail.as_view(), name="campaign-event"),
    *[path(f"campaign/<uuid:candidate_id>/events/<uuid:pk>/{a}/", v.OrganiserEventAction.as_view(action_name=a),
           name=f"campaign-event-{a}") for a in ACTIONS],
    path("campaign/<uuid:candidate_id>/events/<uuid:pk>/checkin/", v.OrganiserCheckin.as_view(), name="campaign-event-checkin"),
    path("campaign/<uuid:candidate_id>/events/<uuid:pk>/image/", v.OrganiserEventImage.as_view(), name="campaign-event-image"),
    # party events and the full list (system admins)
    path("admin/events/", v.AdminAllEvents.as_view(), name="admin-events"),
    path("admin/events/party/", v.AdminPartyEvents.as_view(), name="admin-party-events"),
    path("admin/events/party/<uuid:pk>/", v.AdminPartyEventDetail.as_view(), name="admin-party-event"),
    *[path(f"admin/events/party/<uuid:pk>/{a}/", v.AdminPartyEventAction.as_view(action_name=a), name=f"admin-party-event-{a}")
      for a in ACTIONS],
    path("admin/events/party/<uuid:pk>/checkin/", v.AdminPartyCheckin.as_view(), name="admin-party-event-checkin"),
    path("admin/events/party/<uuid:pk>/image/", v.AdminPartyEventImage.as_view(), name="admin-party-event-image"),
]
