"""Spec 18 §19.1 — volunteering (FR-047–FR-050)."""
from datetime import timedelta

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.access.models import Role, ScopeType
from apps.access.services import grant_role
from apps.audit.models import AuditLog
from apps.candidates.models import Candidate
from apps.core.integrity import _volunteers_unpublished
from apps.events.tests.test_events import client, drain, mk_candidate, mk_user
from apps.notifications.models import Notification
from apps.privacy import consent
from apps.volunteers import services as V
from apps.volunteers.models import VolunteerApplication, VolunteerChange

APPLY = {"roles": ["canvassing"], "state": "lagos", "lga_text": "Ikeja", "availability": ["weekends"], "note": "I know Ikeja well.",
         "share_phone": True}


@override_settings(PRIVACY_CONSENT_REQUIRED=True)
class Base(TestCase):
    def setUp(self):
        self.cand = mk_candidate()
        self.camp = mk_user("v_camp", consent_given=False)
        grant_role(user=self.camp, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=self.cand.pk)
        self.ada = mk_user("v_ada", consent_given=False, name="Ada Obi")
        consent.grant(self.ada, V.PURPOSE)
        self.bo = mk_user("v_bo", consent_given=False, name="Bode Ayo")
        consent.grant(self.bo, V.PURPOSE)

    def open(self, **over):
        return V.set_opening(self.camp, self.cand, {"open": True, "intro": "Help us knock on doors across Lagos this season.",
                                                    "roles": ["canvassing", "events"], "states": ["lagos"], **over})

    def url(self, name, **kw):
        return reverse(name, kwargs={"candidate_id": self.cand.pk, **kw})


class OpeningTests(Base):
    def test_off_by_default_then_open(self):
        r = client().get(reverse("candidate-volunteering", kwargs={"slug": "amina"}))
        self.assertFalse(r.data["open"])
        self.assertEqual(client(self.ada).post(reverse("candidate-volunteering-apply", kwargs={"slug": "amina"}), APPLY, format="json")
                         .data["error"]["code"], "volunteering_closed")
        r = client(self.camp).patch(self.url("campaign-volunteering"), {"open": True, "intro": "short"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.open()
        r = client().get(reverse("candidate-volunteering", kwargs={"slug": "amina"}))
        self.assertTrue(r.data["open"])
        self.assertEqual([x["value"] for x in r.data["roles"]], ["canvassing", "events"])
        self.assertEqual(client(self.ada).patch(self.url("campaign-volunteering"), {"open": False}, format="json").status_code, 404)
        self.assertTrue(AuditLog.objects.filter(action="volunteer.opening_changed").exists())


class ApplyTests(Base):
    def test_consent_minimum_data_and_one_open_application(self):
        self.open()
        nc = mk_user("v_nc", consent_given=False)
        r = client(nc).post(reverse("candidate-volunteering-apply", kwargs={"slug": "amina"}), APPLY, format="json")
        self.assertEqual((r.status_code, r.data["error"]["purpose"]), (403, "volunteering"))
        r = client(self.ada).post(reverse("candidate-volunteering-apply", kwargs={"slug": "amina"}), {**APPLY, "roles": ["logistics"]},
                                  format="json")
        self.assertIn("roles", r.data["error"]["fields"])  # not offered
        r = client(self.ada).post(reverse("candidate-volunteering-apply", kwargs={"slug": "amina"}), {**APPLY, "state": "kano"}, format="json")
        self.assertIn("state", r.data["error"]["fields"])  # outside the states they need
        r = client(self.ada).post(reverse("candidate-volunteering-apply", kwargs={"slug": "amina"}), APPLY, format="json")
        self.assertEqual((r.status_code, r.data["status"]), (201, "pending"))
        r = client(self.ada).post(reverse("candidate-volunteering-apply", kwargs={"slug": "amina"}), APPLY, format="json")
        self.assertEqual(r.data["error"]["code"], "already_applied")
        self.assertEqual(client(self.camp).post(reverse("candidate-volunteering-apply", kwargs={"slug": "amina"}), APPLY,
                                                format="json").status_code, 403)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            VolunteerApplication.objects.filter(user=self.ada).update(roles=[])


class CampaignTests(Base):
    def test_short_names_decide_contact_audited(self):
        self.open()
        a = V.apply(self.ada, self.cand, APPLY)
        b = V.apply(self.bo, self.cand, {**APPLY, "share_phone": False})
        r = client(self.camp).get(self.url("campaign-volunteers"))
        self.assertEqual([x["name"] for x in r.data["results"]], ["Ada O.", "Bode A."])
        self.assertNotIn("+234", r.content.decode())
        self.assertNotIn("v_ada@example.com", r.content.decode())
        self.assertEqual(client(self.camp).get(self.url("campaign-volunteer-contact", pk=a.pk)).status_code, 409)  # not accepted yet
        client(self.camp).post(self.url("campaign-volunteer-decide", pk=a.pk), {"accept": True}, format="json")
        client(self.camp).post(self.url("campaign-volunteer-decide", pk=b.pk), {"accept": True}, format="json")
        r = client(self.camp).get(self.url("campaign-volunteer-contact", pk=a.pk))
        self.assertEqual(r.data, {"full_name": "Ada Obi", "phone_number": "+2348031234567"})
        self.assertIsNone(client(self.camp).get(self.url("campaign-volunteer-contact", pk=b.pk)).data["phone_number"])
        self.assertTrue(AuditLog.objects.filter(action="volunteer.contact_viewed").exists())
        self.assertTrue(VolunteerChange.objects.filter(application=a, kind="phone_viewed").exists())
        self.assertEqual(Notification.objects.filter(recipient=self.ada, kind="volunteer_decision", decision="accepted").count(), 1)
        self.assertEqual(client(self.bo).get(self.url("campaign-volunteers")).status_code, 404)

    def test_decline_with_reason_and_withdraw_hides_contact(self):
        self.open()
        a = V.apply(self.ada, self.cand, APPLY)
        r = client(self.camp).post(self.url("campaign-volunteer-decide", pk=a.pk), {"accept": False, "reason": "We're full in Ikeja."},
                                   format="json")
        self.assertEqual(r.data["status"], "declined")
        mine = client(self.ada).get(reverse("my-volunteering")).data["results"][0]
        self.assertEqual((mine["status"], mine["decline_reason"]), ("declined", "We're full in Ikeja."))
        b = V.apply(self.bo, self.cand, APPLY)
        V.decide(self.camp, b, True)
        r = client(self.bo).post(reverse("my-volunteering-withdraw", kwargs={"pk": b.pk}))
        self.assertEqual(r.data["status"], "withdrawn")
        self.assertEqual(client(self.camp).get(self.url("campaign-volunteer-contact", pk=b.pk)).status_code, 409)

    def test_updates_to_accepted_volunteers_with_limits(self):
        self.open()
        a = V.apply(self.ada, self.cand, APPLY)
        V.apply(self.bo, self.cand, {**APPLY, "roles": ["events"]})
        V.decide(self.camp, a, True)
        r = client(self.camp).post(self.url("campaign-volunteer-updates"), {"text": "Meet at Ikeja roundabout 9 am Saturday.",
                                                                           "role": "canvassing"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        drain()
        got = list(Notification.objects.filter(kind="volunteer_update").values_list("recipient_id", flat=True))
        self.assertEqual(got, [self.ada.pk])
        self.assertEqual(client(self.ada).get(reverse("my-volunteering")).data["updates"][0]["text"], "Meet at Ikeja roundabout 9 am Saturday.")
        for i in range(4):
            V.send_update(self.camp, self.cand, f"Update number {i} for the team.")
        with self.assertRaises(Exception) as cm:
            V.send_update(self.camp, self.cand, "One update too many this week.")
        self.assertEqual(cm.exception.get_codes(), "update_limit")


class EndingTests(Base):
    def test_closing_and_unpublishing_end_open_applications(self):
        from apps.candidates import services as C

        self.open()
        V.apply(self.ada, self.cand, APPLY)
        V.set_opening(self.camp, self.cand, {"open": False})
        self.assertEqual(VolunteerApplication.objects.get(user=self.ada).status, "ended")
        self.assertTrue(Notification.objects.filter(recipient=self.ada, kind="volunteer_decision", decision="ended").exists())
        self.open()
        V.apply(self.bo, self.cand, APPLY)
        sa = mk_user("v_sa", Role.SYSTEM_ADMIN, consent_given=False)
        C.unpublish(candidate=self.cand, reason="Withdrew from the race.", actor=sa)
        self.assertEqual(VolunteerApplication.objects.get(user=self.bo).status, "ended")
        self.assertEqual(_volunteers_unpublished(), 0)

    def test_consent_withdrawal_download_deletion_retention(self):
        from apps.accounts.services import mark_deleted
        from apps.volunteers.privacy import export_for

        self.open()
        a = V.apply(self.ada, self.cand, APPLY)
        V.decide(self.camp, a, True)
        self.assertEqual(export_for(self.ada)["applications"][0]["campaign"], "Amina Bello")
        consent.withdraw(self.ada, V.PURPOSE)
        self.assertEqual(VolunteerApplication.objects.get(pk=a.pk).status, "withdrawn")
        VolunteerApplication.objects.filter(pk=a.pk).update(updated_at=timezone.now() - timedelta(days=91))
        self.assertEqual(V.prune()["applications"], 1)
        b = V.apply(self.bo, self.cand, APPLY)
        mark_deleted(self.bo.clerk_user_id)
        self.assertFalse(VolunteerApplication.objects.filter(pk=b.pk).exists())
        call_command("prune_volunteers")
        Candidate.objects.filter(pk=self.cand.pk).update(status="published")


class LgaPickerTests(Base):
    """Spec 19 §7.2: the volunteer form sends the LGA's ID; the text is written from it."""

    def test_lga_id_writes_the_text_and_is_checked(self):
        from pathlib import Path

        from django.conf import settings

        from apps.geo import loading
        from apps.geo.models import Lga

        loading.load((Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv").read_bytes(), source="sample")
        self.open()
        ikeja = Lga.objects.get(state__slug="lagos", code="07")
        url = reverse("candidate-volunteering-apply", kwargs={"slug": "amina"})
        r = client(self.ada).post(url, {**APPLY, "lga_text": "typed", "geo_lga": ikeja.pk}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        app = VolunteerApplication.objects.get(user=self.ada)
        self.assertEqual((app.lga_text, app.geo_lga_id), ("Ikeja", ikeja.pk))
        fct = Lga.objects.get(state__slug="fct")
        r = client(self.bo).post(url, {**APPLY, "geo_lga": fct.pk}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(client(self.bo).post(url, {**APPLY, "geo_lga": "abc"}, format="json").status_code, 400)
