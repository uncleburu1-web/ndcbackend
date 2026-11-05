"""Spec 18 §19.1 — events."""
import threading
from datetime import timedelta

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access.models import Role, ScopeType
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.candidates.models import Candidate
from apps.core.integrity import _event_counts_drift, _events_over_capacity, _waitlist_with_free_places
from apps.events import services as S
from apps.events.models import Event, EventChange, EventRegistration
from apps.follows.models import CandidateFollow
from apps.moderation import services as M
from apps.moderation.models import Case
from apps.notifications.models import Notification
from apps.privacy import consent
from apps.search.models import SearchEntry

NOW = timezone.now()
PERIODS = [{"positions": ["governor"], "start": (NOW - timedelta(days=30)).isoformat(), "end": (NOW + timedelta(days=60)).isoformat()}]
DECL = {"no_inducements": True, "lawful_venue": True, "police_notified": True}


def mk_user(cid, *roles, consent_given=True, name=None):
    then = timezone.now() - timedelta(days=3)
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=name or f"Bola {cid.title()}", phone_number="+2348031234567",
                                 profile_completed_at=then, adult_confirmed_at=then)
    for r in roles:
        grant_role(user=u, role=r)
    if consent_given:
        consent.grant(u, S.PURPOSE)
    return u


def mk_candidate(slug="amina", position="governor", state="lagos"):
    return Candidate.objects.create(slug=slug, full_name="Amina Bello", position=position, election_year=2027, state=state,
                                    status="published", verified_at=timezone.now(), published_at=timezone.now())


def drain():
    """Run queued outbox jobs (fan-out, reminders) the way process_outbox does."""
    from apps.core import outbox

    outbox.process(now=timezone.now() + timedelta(seconds=1))


def client(user=None):
    cl = APIClient()
    if user:
        cl.force_authenticate(user)
    return cl


def data(**over):
    start = timezone.now() + timedelta(days=5)
    d = {"type": "rally", "mode": "in_person", "title": "Ikeja rally for jobs", "description": "Come and hear the plan for jobs in Lagos. " * 2,
         "starts_at": start, "ends_at": start + timedelta(hours=3), "venue_name": "Ikeja Grammar School field", "venue_address": "Ikeja, Lagos",
         "venue_type": "open_ground", "state": "lagos"}
    d.update(over)
    return d


@override_settings(CAMPAIGN_PERIODS=PERIODS, CAMPAIGN_BLACKOUTS=[], PRIVACY_CONSENT_REQUIRED=True)
class Base(TestCase):
    def setUp(self):
        self.cand = mk_candidate()
        self.camp = mk_user("u_camp")
        grant_role(user=self.camp, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=self.cand.pk)
        self.sa = mk_user("u_sa", Role.SYSTEM_ADMIN)
        self.ada = mk_user("u_ada", name="Ada Obi")
        self.bo = mk_user("u_bo", name="Bode Ayo")

    def event(self, publish=True, **over):
        e = S.create(self.camp, self.cand, data(**over))
        return S.publish(self.camp, e, DECL) if publish else e

    def code(self, fn):
        with self.assertRaises(Exception) as cm:
            fn()
        return cm.exception.get_codes()


# ---------------------------------------------------------------- organisers and rules
class OrganiserTests(Base):
    def test_only_linked_accounts_of_published_candidates_and_admins(self):
        self.assertTrue(S.can_organise(self.camp, self.cand))
        self.assertFalse(S.can_organise(self.ada, self.cand))
        self.assertTrue(S.can_organise(self.sa, None))
        self.assertFalse(S.can_organise(self.camp, None))
        r = client(self.ada).post(reverse("campaign-events", kwargs={"candidate_id": self.cand.pk}), data(), format="json")
        self.assertEqual(r.status_code, 404)
        r = client(self.camp).post(reverse("campaign-events", kwargs={"candidate_id": self.cand.pk}), data(), format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["organiser"]["label"], "Amina Bello's campaign")
        self.assertEqual(r.data["status"], "draft")
        Candidate.objects.filter(pk=self.cand.pk).update(status="unpublished")
        r = client(self.camp).post(reverse("campaign-events", kwargs={"candidate_id": self.cand.pk}), data(), format="json")
        self.assertEqual(r.data["error"]["code"], "candidate_not_published")

    def test_party_events_by_system_admins(self):
        r = client(self.sa).post(reverse("admin-party-events"), {**data(type="meeting"), "members_only": True}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["organiser"]["label"], "NDC")
        self.assertEqual(client(self.camp).post(reverse("admin-party-events"), data(), format="json").status_code, 403)
        r = client(self.camp).post(reverse("campaign-events", kwargs={"candidate_id": self.cand.pk}), {**data(), "members_only": True},
                                   format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("members_only", r.data["error"]["fields"])

    def test_campaign_period_for_rallies_and_canvassing_only(self):
        late = timezone.now() + timedelta(days=90)
        self.assertEqual(self.code(lambda: S.create(self.camp, self.cand, data(starts_at=late, ends_at=late + timedelta(hours=2)))),
                         "outside_campaign_period")
        self.assertEqual(self.code(lambda: S.create(self.camp, self.cand, data(type="canvass", starts_at=late,
                                                                                   ends_at=late + timedelta(hours=2)))),
                         "outside_campaign_period")
        e = S.create(self.camp, self.cand, data(type="town_hall", starts_at=late, ends_at=late + timedelta(hours=2)))  # any time
        self.assertEqual(e.status, "draft")
        with override_settings(CAMPAIGN_PERIODS=[]):  # an office with no period configured isn't checked
            S.create(self.camp, self.cand, data(starts_at=late, ends_at=late + timedelta(hours=2)))

    def test_blackout_refuses_starts_inside_and_pauses_writing(self):
        start = timezone.now() + timedelta(days=5)
        window = [{"positions": ["governor"], "start": (start - timedelta(hours=1)).isoformat(), "end": (start + timedelta(days=1)).isoformat()}]
        with override_settings(CAMPAIGN_BLACKOUTS=window):
            self.assertEqual(self.code(lambda: S.create(self.camp, self.cand, data(type="meeting"))), "publishing_paused")
        e = self.event()
        live = [{"positions": ["governor"], "start": (timezone.now() - timedelta(hours=1)).isoformat(),
                 "end": (timezone.now() + timedelta(hours=1)).isoformat()}]
        with override_settings(CAMPAIGN_BLACKOUTS=live):
            r = client(self.camp).patch(reverse("campaign-event", kwargs={"candidate_id": self.cand.pk, "pk": e.pk}), {"title": "A new title here"},
                                        format="json")
            self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "publishing_paused"))
            self.assertIn("until", r.data["error"])
        # a window added later over a published event is flagged, never cancelled
        with override_settings(CAMPAIGN_BLACKOUTS=window):
            out = S.tick()
        drain()
        e.refresh_from_db()
        self.assertEqual((out["flagged"], e.status), (1, "published"))
        self.assertIsNotNone(e.flagged_blackout_at)
        self.assertTrue(Notification.objects.filter(recipient=self.camp, kind="event_update", decision="blackout").exists())

    def test_venue_types_and_declarations(self):
        r = client(self.camp).post(reverse("campaign-events", kwargs={"candidate_id": self.cand.pk}), data(venue_type="church"),
                                   format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("s. 96", r.data["error"]["fields"]["venue_type"][0])
        e = self.event(publish=False)
        r = client(self.camp).post(reverse("campaign-event-publish", kwargs={"candidate_id": self.cand.pk, "pk": e.pk}),
                                   {"declarations": {"no_inducements": True, "lawful_venue": True}}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "declaration_required"))
        self.assertEqual(r.data["error"]["missing"], ["police_notified"])
        r = client(self.camp).post(reverse("campaign-event-publish", kwargs={"candidate_id": self.cand.pk, "pk": e.pk}),
                                   {"declarations": DECL}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(set(r.data["declarations"]), set(DECL))
        # private premises and online events don't need the police notice
        e2 = S.create(self.camp, self.cand, data(type="meeting", mode="online", online_url="https://meet.example.com/abc"))
        self.assertEqual(S.required_declarations(e2), ["no_inducements"])
        e3 = S.create(self.camp, self.cand, data(venue_type="private_premises"))
        self.assertNotIn("police_notified", S.required_declarations(e3))

    def test_field_rules_and_constraints(self):
        bad = [{"title": "Short"}, {"description": "too short"}, {"ends_at": timezone.now() + timedelta(days=4)},
               {"ends_at": timezone.now() + timedelta(days=10)}, {"mode": "online", "online_url": "http://x.example"},
               {"capacity": 0}, {"venue_name": ""}, {"starts_at": timezone.now() + timedelta(days=400),
                                                     "ends_at": timezone.now() + timedelta(days=400, hours=1)}]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(Exception):
                S.create(self.camp, self.cand, data(type="meeting", **b))
        e = self.event()
        for kw in ({"ends_at": e.starts_at}, {"organiser_kind": "party"}, {"capacity": 0}, {"status": "cancelled"},
                   {"members_only": True}, {"venue_type": "church"}):
            with self.subTest(kw=kw), transaction.atomic(), self.assertRaises(IntegrityError):
                Event.objects.filter(pk=e.pk).update(**kw)

    def test_lifecycle_edits_cancel_and_no_delete_after_publish(self):
        e = self.event(publish=False)
        S.delete_draft(self.camp, e)
        self.assertFalse(Event.objects.filter(pk=e.pk).exists())
        e = self.event()
        self.assertEqual(self.code(lambda: S.delete_draft(self.camp, e)), "invalid_transition")
        with self.assertRaises(Exception):
            S.cancel(self.camp, e, "short")
        S.cancel(self.camp, e, "Rain has flooded the field, sorry.")
        e.refresh_from_db()
        self.assertEqual((e.status, e.cancel_reason), ("cancelled", "Rain has flooded the field, sorry."))
        self.assertTrue(client().get(reverse("event", kwargs={"pk": e.pk})).data["cancelled_reason"])
        kinds = list(EventChange.objects.filter(event=e).values_list("kind", flat=True))
        self.assertEqual(kinds, ["created", "published", "cancelled"])
        self.assertTrue(AuditLog.objects.filter(action="event.cancelled").exists())

    def test_history_is_append_only(self):
        e = self.event()
        ch = EventChange.objects.filter(event=e).first()
        with transaction.atomic(), self.assertRaises(Exception):
            EventChange.objects.filter(pk=ch.pk).update(note="changed")


# ---------------------------------------------------------------- public pages and privacy
class PublicTests(Base):
    def test_list_detail_filters_and_404s(self):
        e = self.event()
        draft = self.event(publish=False, title="Draft rally not shown")
        other = self.event(state="kano", title="Kano rally for farmers")
        r = client().get(reverse("events"), {"state": "lagos"})
        self.assertEqual([x["id"] for x in r.data["results"]], [str(e.pk)])
        self.assertIn("public", r["Cache-Control"])
        self.assertEqual(r.data["results"][0]["going"], "Fewer than 10")
        self.assertEqual(client().get(reverse("event", kwargs={"pk": draft.pk})).status_code, 404)
        self.assertEqual(client().get(reverse("events"), {"organiser": "party"}).data["results"], [])
        self.assertEqual(len(client().get(reverse("events")).data["results"]), 2)
        self.assertEqual(client().get(reverse("events"), {"candidate": "amina", "type": "rally"}).status_code, 200)
        del other

    def test_online_link_and_private_address_only_for_registered_and_organisers(self):
        e = self.event(type="meeting", mode="hybrid", online_url="https://meet.example.com/secret", address_visibility="registered")
        pub = client().get(reverse("event", kwargs={"pk": e.pk})).data
        self.assertIsNone(pub["online_url"])
        self.assertIsNone(pub["venue_address"])
        self.assertEqual(pub["place"], "Address shown when you register")
        self.assertNotIn("secret", client().get(reverse("event-calendar", kwargs={"pk": e.pk})).content.decode())
        me = client(self.ada).get(reverse("event-me", kwargs={"pk": e.pk})).data
        self.assertIsNone(me["online_url"])
        client(self.ada).post(reverse("event-registration", kwargs={"pk": e.pk}))
        me = client(self.ada).get(reverse("event-me", kwargs={"pk": e.pk})).data
        self.assertEqual((me["online_url"], me["venue_address"]), ("https://meet.example.com/secret", "Ikeja, Lagos"))
        self.assertEqual(client(self.camp).get(reverse("event-me", kwargs={"pk": e.pk})).data["online_url"], "https://meet.example.com/secret")

    def test_calendar_file(self):
        e = self.event()
        r = client().get(reverse("event-calendar", kwargs={"pk": e.pk}))
        body = r.content.decode()
        self.assertEqual(r["Content-Type"], "text/calendar; charset=utf-8")
        self.assertIn("BEGIN:VEVENT", body)
        self.assertIn("SUMMARY:Ikeja rally for jobs", body)
        self.assertIn("LOCATION:Ikeja Grammar School field\\, Ikeja\\, Lagos", body)

    def test_nobody_lists_registrants(self):
        e = self.event()
        S.register(self.ada, e)
        for path in (reverse("event", kwargs={"pk": e.pk}), reverse("events"), reverse("admin-events")):
            body = client(self.sa).get(path).content.decode()
            self.assertNotIn("Ada", body)
            self.assertNotIn(str(self.ada.pk), body)
        r = client(self.camp).get(reverse("campaign-event", kwargs={"candidate_id": self.cand.pk, "pk": e.pk}))
        self.assertEqual(r.data["counts"]["registered"], 1)
        self.assertNotIn("Ada", r.content.decode())

    def test_search_profiles_states_and_communities(self):
        e = self.event()
        self.assertTrue(SearchEntry.objects.filter(kind="event", object_id=e.pk).exists())
        self.assertEqual([x.pk for x in S.upcoming(state="lagos")], [e.pk])
        self.assertEqual(client().get(reverse("state-detail", kwargs={"state": "lagos"})).data["events"][0]["id"], str(e.pk))
        r = client().get(reverse("search"), {"q": "rally", "type": "events"})
        self.assertEqual(r.data["results"][0]["url"], f"/events/{e.pk}")
        S.cancel(self.camp, e, "The venue is no longer available.")
        self.assertFalse(SearchEntry.objects.filter(kind="event", object_id=e.pk).exists())
        self.assertEqual(S.upcoming(state="lagos"), [])

    def test_typed_lga_links_to_the_geography_for_community_strips(self):
        from pathlib import Path

        from django.conf import settings

        from apps.communities import sync
        from apps.communities.models import Community
        from apps.geo import loading

        loading.load((Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv").read_bytes(), source="sample")
        sync.sync()
        e = self.event(lga_text="ikeja ")
        self.assertIsNotNone(e.geo_lga_id)
        self.assertEqual(e.lga_text, "ikeja")
        other = self.event(title="Agege rally for jobs", lga_text="Somewhere else")
        self.assertIsNone(other.geo_lga_id)
        ikeja = Community.objects.get(path="lagos/ikeja")
        pu = Community.objects.filter(path__startswith="lagos/ikeja/", level="polling_unit").first()
        for c in (ikeja, pu):
            ids = [x["id"] for x in client().get(reverse("events"), {"community": str(c.pk)}).data["results"]]
            self.assertEqual(ids, [str(e.pk)], c.path)
        lagos = Community.objects.get(path="lagos")
        self.assertEqual(len(client().get(reverse("events"), {"community": str(lagos.pk)}).data["results"]), 2)
        S.edit(self.camp, Event.objects.get(pk=e.pk), {"lga_text": ""})
        self.assertIsNone(Event.objects.get(pk=e.pk).geo_lga_id)


# ---------------------------------------------------------------- registration
class RegistrationTests(Base):
    def test_consent_profile_and_members_only(self):
        e = self.event()
        nc = mk_user("u_nc", consent_given=False)
        r = client(nc).post(reverse("event-registration", kwargs={"pk": e.pk}))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "consent_required"))
        self.assertEqual(r.data["error"]["purpose"], "event_registration")
        User.objects.filter(pk=self.ada.pk).update(profile_completed_at=None)
        self.ada.refresh_from_db()
        self.assertEqual(client(self.ada).post(reverse("event-registration", kwargs={"pk": e.pk})).data["error"]["code"], "profile_incomplete")
        party = S.publish(self.sa, S.create(self.sa, None, {**data(type="meeting"), "members_only": True}), DECL)
        r = client(self.bo).post(reverse("event-registration", kwargs={"pk": party.pk}))
        self.assertEqual(r.data["error"]["code"], "members_only")
        r = client(self.camp).post(reverse("event-registration", kwargs={"pk": e.pk}))
        self.assertEqual(r.status_code, 403)  # organisers don't register

    def test_ticket_codes_waitlist_and_promotion(self):
        e = self.event(capacity=1)
        r1 = S.register(self.ada, e)
        r2 = S.register(self.bo, e)
        self.assertEqual((r1.status, r2.status, r2.waitlist_position), ("registered", "waitlisted", 1))
        self.assertRegex(r1.ticket_code, r"^[A-HJ-NP-Z2-9]{6}$")
        self.assertEqual(self.code(lambda: S.register(self.ada, e)), "already_registered")
        cy = mk_user("u_cy")
        self.assertEqual(self.code(lambda: S.register(cy, e)), "registration_closed")  # waitlist capped at capacity
        S.cancel_registration(self.ada, e)
        r2.refresh_from_db()
        e.refresh_from_db()
        self.assertEqual((r2.status, e.registered_count, e.waitlist_count), ("registered", 1, 0))
        self.assertTrue(Notification.objects.filter(recipient=self.bo, kind="event_waitlist").exists())

    def test_reregister_cap_limits_and_deadlines(self):
        e = self.event()
        for _ in range(3):
            S.register(self.ada, e)
            S.cancel_registration(self.ada, e)
        self.assertEqual(self.code(lambda: S.register(self.ada, e)), "too_many_registrations")
        e2 = self.event(title="Closing rally in Ikeja", registration_closes_at=timezone.now() + timedelta(minutes=1))
        Event.objects.filter(pk=e2.pk).update(registration_closes_at=timezone.now() - timedelta(minutes=1))
        e2.refresh_from_db()
        self.assertEqual(self.code(lambda: S.register(self.bo, e2)), "registration_closed")
        S.close_registration(self.camp, e)
        e.refresh_from_db()
        self.assertEqual(self.code(lambda: S.register(self.bo, e)), "registration_closed")

    def test_material_change_notifies_registered_and_waitlisted(self):
        e = self.event(capacity=1)
        S.register(self.ada, e)
        S.register(self.bo, e)
        new = e.starts_at + timedelta(hours=2)
        r = client(self.camp).patch(reverse("campaign-event", kwargs={"candidate_id": self.cand.pk, "pk": e.pk}),
                                    {"starts_at": new.isoformat(), "ends_at": (new + timedelta(hours=3)).isoformat()}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["material_change"]["kind"], "time")
        drain()
        got = set(Notification.objects.filter(kind="event_update", event=e).values_list("recipient_id", flat=True))
        self.assertEqual(got, {self.ada.pk, self.bo.pk})
        # a plain edit doesn't notify
        client(self.camp).patch(reverse("campaign-event", kwargs={"candidate_id": self.cand.pk, "pk": e.pk}),
                                {"title": "Ikeja rally for jobs and roads"}, format="json")
        self.assertEqual(Notification.objects.filter(kind="event_update", event=e).count(), 2)

    def test_updates_limit_and_capacity_floor(self):
        e = self.event(capacity=5)
        S.register(self.ada, e)
        S.register(self.bo, e)
        for i in range(3):
            S.add_update(self.camp, e, f"Gates open at {8 + i} am, bring water.")
        self.assertEqual(self.code(lambda: S.add_update(self.camp, e, "One more update please.")), "update_limit")
        self.assertEqual(self.code(lambda: S.edit(self.camp, e, {"capacity": 1})), "capacity_below_registrations")
        me = client(self.ada).get(reverse("event-me", kwargs={"pk": e.pk})).data
        self.assertEqual(len(me["updates"]), 3)

    def test_reminders_once_and_not_for_new_or_cancelled(self):
        e = self.event()
        S.register(self.ada, e)
        S.register(self.bo, e)
        S.cancel_registration(self.bo, e)
        EventRegistration.objects.filter(user=self.ada).update(updated_at=timezone.now() - timedelta(hours=3))
        Event.objects.filter(pk=e.pk).update(starts_at=timezone.now() + timedelta(hours=23), ends_at=timezone.now() + timedelta(hours=26))
        S.tick()
        S.tick()
        drain()
        rem = Notification.objects.filter(kind="event_reminder", event=e)
        self.assertEqual(list(rem.values_list("recipient_id", flat=True)), [self.ada.pk])

    def test_withdrawing_consent_cancels_upcoming(self):
        e = self.event(capacity=1)
        S.register(self.ada, e)
        S.register(self.bo, e)
        consent.withdraw(self.ada, S.PURPOSE)
        self.assertFalse(EventRegistration.objects.filter(user=self.ada).exists())
        self.assertEqual(EventRegistration.objects.get(user=self.bo).status, "registered")


@override_settings(CAMPAIGN_PERIODS=PERIODS, CAMPAIGN_BLACKOUTS=[], PRIVACY_CONSENT_REQUIRED=False)
class ConcurrencyTests(TransactionTestCase):
    def test_capacity_never_exceeded(self):
        cand = mk_candidate()
        camp = mk_user("u_camp2", consent_given=False)
        grant_role(user=camp, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=cand.pk)
        e = S.publish(camp, S.create(camp, cand, data(capacity=2)), DECL)
        people = [mk_user(f"u_p{i}", consent_given=False) for i in range(6)]
        errors = []

        def go(u):
            try:
                S.register(u, Event.objects.get(pk=e.pk))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=go, args=(u,)) for u in people]
        [t.start() for t in threads]
        [t.join() for t in threads]
        e.refresh_from_db()
        self.assertEqual(EventRegistration.objects.filter(event=e, status="registered").count(), 2)
        self.assertEqual(e.registered_count, 2)
        self.assertEqual(e.waitlist_count, 2)
        self.assertEqual(len(errors), 2)  # the waitlist is capped at the capacity


# ---------------------------------------------------------------- check-in
class CheckinTests(Base):
    def test_window_short_names_and_audit(self):
        e = self.event()
        reg = S.register(self.ada, e)
        url = reverse("campaign-event-checkin", kwargs={"candidate_id": self.cand.pk, "pk": e.pk})
        r = client(self.camp).get(url)
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "checkin_window_closed"))
        Event.objects.filter(pk=e.pk).update(starts_at=timezone.now() + timedelta(hours=2), ends_at=timezone.now() + timedelta(hours=5))
        r = client(self.camp).get(url)
        self.assertEqual(r.data["results"], [{"ticket_code": reg.ticket_code, "name": "Ada O.", "checked_in": False}])
        self.assertNotIn("ada@example.com", r.content.decode())
        self.assertTrue(AuditLog.objects.filter(action="event.checkin_list_opened").exists())
        r = client(self.camp).post(url, {"code": reg.ticket_code.lower()}, format="json")
        self.assertEqual(r.data["checked_in"], True)
        self.assertEqual(client(self.camp).post(url, {"code": "ZZZZZZ"}, format="json").status_code, 404)
        self.assertEqual(client(self.ada).get(url).status_code, 404)
        e.refresh_from_db()
        self.assertEqual(e.checked_in_count, 1)


# ---------------------------------------------------------------- notifications
class NotificationTests(Base):
    def test_candidate_event_to_bell_on_followers_only(self):
        CandidateFollow.objects.create(user=self.ada, candidate=self.cand, notify=True, notify_changed_at=timezone.now() - timedelta(days=1))
        CandidateFollow.objects.create(user=self.bo, candidate=self.cand, notify=False)
        e = self.event()
        drain()
        got = list(Notification.objects.filter(kind="candidate_event", event=e).values_list("recipient_id", flat=True))
        self.assertEqual(got, [self.ada.pk])
        self.assertFalse(AuditLog.objects.filter(action__startswith="follow").exists())

    def test_preferences_and_payload(self):
        r = client(self.ada).patch(reverse("my-notifications-preferences"), {"event_reminders": False}, format="json")
        self.assertEqual(r.data["event_reminders"], False)
        e = self.event()
        S.register(self.ada, e)
        S.cancel(self.camp, e, "Postponed until after the holidays.")
        drain()
        items = client(self.ada).get(reverse("my-notifications")).data["results"]
        ev = [i for i in items if i["kind"] == "event_update"][0]
        self.assertEqual((ev["decision"], ev["event"]["title"]), ("cancelled", "Ikeja rally for jobs"))


# ---------------------------------------------------------------- moderation
class ModerationTests(Base):
    def setUp(self):
        super().setUp()
        self.mod = mk_user("u_mod")
        grant_role(user=self.mod, role=Role.MODERATOR)
        self.mod2 = mk_user("u_mod2")
        grant_role(user=self.mod2, role=Role.MODERATOR)

    def test_report_hide_remove_appeal(self):
        e = self.event()
        S.register(self.ada, e)
        r = client(self.bo).post(reverse("event-report", kwargs={"pk": e.pk}), {"reason": "inducement", "details": "₦2,000 each"},
                                 format="json")
        self.assertEqual(r.status_code, 201, r.content)
        case = Case.objects.get(target_kind="event", target_id=e.pk)
        self.assertEqual(case.priority, "high")
        self.assertFalse(M.can_view(self.camp, case))  # no self-moderation
        M.claim(case, self.mod)
        M.hide(case, self.mod, "checking")
        e.refresh_from_db()
        self.assertEqual(e.status, "hidden")
        self.assertEqual(client().get(reverse("event", kwargs={"pk": e.pk})).status_code, 404)
        M.remove(case, self.mod, "R11", "No inducements at events.")
        drain()
        e.refresh_from_db()
        self.assertEqual((e.status, e.removed_rule), ("removed", "R11"))
        pub = client().get(reverse("event", kwargs={"pk": e.pk})).data
        self.assertEqual(pub["removed"]["rule"], "R11")
        self.assertEqual(pub["description"], "")
        self.assertTrue(Notification.objects.filter(recipient=self.ada, kind="event_update", decision="removed").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.camp, kind="content_removed").exists())
        self.assertFalse(SearchEntry.objects.filter(kind="event", object_id=e.pk).exists())
        a = M.open_appeal(self.camp, "It was a refreshment table, not cash.", case=Case.objects.get(pk=case.pk))
        self.assertEqual(self.code(lambda: M.decide_appeal(a, self.mod, True, "x")), "same_decider")
        M.decide_appeal(a, self.mod2, True, "Refreshments aren't an inducement.")
        e.refresh_from_db()
        self.assertEqual(e.status, "published")

    def test_new_reasons_and_queue_filter(self):
        e = self.event()
        bad = client(self.bo).post(reverse("event-report", kwargs={"pk": e.pk}), {"reason": "nonsense"}, format="json")
        self.assertEqual(bad.status_code, 400)
        client(self.bo).post(reverse("event-report", kwargs={"pk": e.pk}), {"reason": "banned_venue"}, format="json")
        r = client(self.mod).get(reverse("admin-moderation-cases"), {"type": "events"})
        self.assertEqual([x["target_kind"] for x in r.data["results"]], ["event"])
        d = client(self.mod).get(reverse("admin-moderation-case", kwargs={"pk": r.data["results"][0]["id"]})).data
        self.assertEqual(d["item"]["author"]["label"], "Amina Bello's campaign")
        self.assertEqual(d["reports"][0]["reason_label"], "At a place of worship, police station or public office")


# ---------------------------------------------------------------- candidate, privacy, retention, integrity
@override_settings(CAMPAIGN_PERIODS=PERIODS, CAMPAIGN_BLACKOUTS=[], PRIVACY_CONSENT_REQUIRED=False)
class CandidateActionsOutsideATransaction(TransactionTestCase):
    """The live check found unpublish outside a transaction (500): TestCase's wrapping transaction hid it."""

    def test_unpublish_and_archive_through_the_api(self):
        cand = mk_candidate()
        camp = mk_user("u_camp2", consent_given=False)
        grant_role(user=camp, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=cand.pk)
        sa = mk_user("u_sa2", Role.SYSTEM_ADMIN, consent_given=False)
        e = S.publish(camp, S.create(camp, cand, data()), DECL)
        r = client(sa).post(reverse("admin-candidate-unpublish", kwargs={"pk": cand.pk}), {"reason": "Withdrew from the race."}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        e.refresh_from_db()
        self.assertEqual(e.registration, "closed")
        r = client(sa).post(reverse("admin-candidate-archive", kwargs={"pk": cand.pk}), {"reason": "Left the race."}, format="json")
        self.assertEqual(r.status_code, 200, r.content)


class LifecycleTests(Base):
    def test_unpublished_candidate_closes_registration(self):
        from apps.candidates import services as C

        e = self.event()
        C.unpublish(candidate=self.cand, reason="Withdrew from the race.", actor=self.sa)
        e.refresh_from_db()
        self.assertEqual(e.registration, "closed")
        self.assertEqual(client().get(reverse("event", kwargs={"pk": e.pk})).status_code, 404)

    def test_download_and_deletion(self):
        from apps.accounts.services import mark_deleted
        from apps.events.privacy import export_for

        e = self.event(capacity=1)
        S.register(self.ada, e)
        S.register(self.bo, e)
        self.assertEqual(export_for(self.ada)["registrations"][0]["event"], "Ikeja rally for jobs")
        mark_deleted(self.ada.clerk_user_id)
        self.assertFalse(EventRegistration.objects.filter(user=self.ada).exists())
        e.refresh_from_db()
        self.assertEqual((e.registered_count, EventRegistration.objects.get(user=self.bo).status), (1, "registered"))
        mark_deleted(self.camp.clerk_user_id)
        e.refresh_from_db()
        self.assertIsNone(e.created_by_id)

    def test_retention(self):
        e = self.event()
        S.register(self.ada, e)
        Event.objects.filter(pk=e.pk).update(starts_at=timezone.now() - timedelta(days=100), ends_at=timezone.now() - timedelta(days=99))
        out = S.prune()
        self.assertEqual((out["registrations"], out["events"]), (1, 0))
        Event.objects.filter(pk=e.pk).update(starts_at=timezone.now() - timedelta(days=800), ends_at=timezone.now() - timedelta(days=799))
        self.assertEqual(S.prune()["events"], 1)
        call_command("prune_events")

    def test_integrity_checks(self):
        e = self.event(capacity=2)
        S.register(self.ada, e)
        Event.objects.filter(pk=e.pk).update(registered_count=9)
        self.assertEqual(_event_counts_drift(), 1)
        e.refresh_from_db()
        self.assertEqual(e.registered_count, 1)
        self.assertEqual(_events_over_capacity(), 0)
        EventRegistration.objects.create(event=e, user=self.bo, status="waitlisted", waitlist_position=1, ticket_code="ABCDEF")
        self.assertEqual(_waitlist_with_free_places(timezone.now()), 1)

    def test_commands_and_dashboard(self):
        call_command("events_tick")
        r = client(self.sa).get(reverse("admin-overview"))
        keys = [c["key"] for c in r.data["attention"]]
        self.assertIn("events_reported", keys)
        labels = [x["label"] for g in r.data["readiness"] for x in g["items"]] if isinstance(r.data["readiness"], list) else []
        if labels:
            self.assertIn("Campaign periods", labels)
