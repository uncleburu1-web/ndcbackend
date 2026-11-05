from django.urls import path

from . import views as v

urlpatterns = [
    path("candidates/<slug:slug>/volunteering/", v.CandidateVolunteering.as_view(), name="candidate-volunteering"),
    path("candidates/<slug:slug>/volunteering/apply/", v.CandidateVolunteerApply.as_view(), name="candidate-volunteering-apply"),
    path("me/volunteering/", v.MyVolunteering.as_view(), name="my-volunteering"),
    path("me/volunteering/<uuid:pk>/withdraw/", v.MyVolunteerWithdraw.as_view(), name="my-volunteering-withdraw"),
    path("campaign/<uuid:candidate_id>/volunteering/", v.CampaignVolunteering.as_view(), name="campaign-volunteering"),
    path("campaign/<uuid:candidate_id>/volunteers/", v.CampaignVolunteers.as_view(), name="campaign-volunteers"),
    path("campaign/<uuid:candidate_id>/volunteers/updates/", v.CampaignVolunteerUpdates.as_view(), name="campaign-volunteer-updates"),
    path("campaign/<uuid:candidate_id>/volunteers/<uuid:pk>/decide/", v.CampaignVolunteerDecide.as_view(), name="campaign-volunteer-decide"),
    path("campaign/<uuid:candidate_id>/volunteers/<uuid:pk>/contact/", v.CampaignVolunteerContact.as_view(),
         name="campaign-volunteer-contact"),
]
