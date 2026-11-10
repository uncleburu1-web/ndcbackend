"""Spec 19 §19.1 — states and geography: loading, constituencies, the API, pickers, confirmation, review, notices, search."""
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.candidates.models import Candidate
from apps.core.exceptions import ServiceError
from apps.core.models import OutboxJob
from apps.geo import loading, records, services
from apps.geo.models import Alias, Change, Constituency, Dataset, Lga, PollingUnit, Ward
from apps.membership.models import Membership
from apps.notifications.models import Notification
from apps.privacy import consent
from apps.pvc.models import PvcVerification

DATA = Path(settings.BASE_DIR) / "data" / "geo"
SAMPLE, V2, BROKEN = (DATA / "sample.csv").read_bytes(), (DATA / "sample_v2.csv").read_bytes(), (DATA / "sample_broken.csv").read_bytes()
KINDS = {"senatorial_districts": "sample_senatorial_districts.csv", "federal_constituencies": "sample_federal_constituencies.csv",
         "state_constituencies": "sample_state_constituencies.csv"}


def mk_user(cid, *roles):
    then = timezone.now() - timedelta(days=3)
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=f"Ada {cid.title()}", phone_number="+2348031234567",
                                 profile_completed_at=then, adult_confirmed_at=then)
    for r in roles:
        grant_role(user=u, role=r)
    for p in ("membership", "pvc_check", "community_membership"):
        consent.grant(u, p)
    return u


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def drain():
    from apps.core import outbox

    outbox.process(now=timezone.now() + timedelta(seconds=1))


def pu(code):
    return PollingUnit.objects.select_related("ward__lga__state").get(delimitation=code)


def load_constituencies():
    for kind, name in KINDS.items():
        loading.apply(loading.preview((DATA / name).read_bytes(), kind=kind, source="sample"), None)


class PreviewApplyTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")

    def test_preview_reports_each_change_and_writes_nothing(self):
        ds = loading.preview(V2, source="sample v2")
        rep = ds.preview
        self.assertEqual(ds.status, "previewed")
        self.assertEqual(rep["counts"]["added"], {"polling_unit": 1})
        self.assertEqual(rep["counts"]["renamed"], {"ward": 1})
        self.assertEqual(rep["counts"]["retired"], {"polling_unit": 1})
        self.assertEqual(rep["renamed"]["ward"], [{"code": "24-07-06", "old": "Alausa / Oregun", "new": "Alausa / Oregun / Olusosun"}])
        self.assertEqual(rep["retired"]["polling_unit"][0]["code"], "24-07-05-002")
        self.assertEqual(rep["error_count"], 0)
        self.assertTrue(PollingUnit.objects.get(delimitation="24-07-05-002").active)  # nothing applied
        self.assertTrue(AuditLog.objects.filter(action="geo.dataset_previewed").exists())

    def test_preview_counts_people_linked_to_retired_units(self):
        Membership.objects.create(user=mk_user("u_a"), status="pending_approval", state="lagos", full_name="A",
                                  geo_polling_unit=pu("24-07-05-002"))
        self.assertEqual(loading.preview(V2, source="v2").preview["affected"], {"memberships": 1, "pvc_records": 0})

    def test_apply_writes_changes_aliases_and_queues_the_job_then_changes_nothing(self):
        ds = loading.apply(loading.preview(V2, source="v2"), None)
        self.assertEqual(ds.status, "applied")
        self.assertFalse(pu("24-07-05-002").active)
        self.assertEqual(Ward.objects.get(lga__code="07", code="06").name, "Alausa / Oregun / Olusosun")
        self.assertEqual(set(Change.objects.filter(dataset=ds).values_list("level", "action", "code")),
                         {("ward", "renamed", "24-07-06"), ("polling_unit", "added", "24-07-06-002"),
                          ("polling_unit", "retired", "24-07-05-002")})
        self.assertTrue(Alias.objects.filter(ward__code="06", name="Alausa / Oregun", source="rename").exists())
        self.assertEqual(OutboxJob.objects.filter(kind="geo_dataset_applied", payload={"dataset": ds.pk}).count(), 1)
        again = loading.apply(loading.preview(V2, source="v2 again"), None)
        self.assertEqual(Change.objects.filter(dataset=again).count(), 0)
        self.assertEqual(again.preview["changes"], 0)
        with self.assertRaises(ServiceError):
            loading.apply(ds, None)  # already applied

    def test_change_rows_are_append_only(self):
        from django.db import DatabaseError, transaction

        ds = loading.apply(loading.preview(V2, source="v2"), None)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Change.objects.filter(dataset=ds).update(new_value="x")

    def test_retired_unit_comes_back(self):
        loading.apply(loading.preview(V2, source="v2"), None)
        ds = loading.apply(loading.preview(SAMPLE, source="sample again"), None)
        self.assertTrue(pu("24-07-05-002").active)
        self.assertIn(("polling_unit", "restored", "24-07-05-002"), set(Change.objects.filter(dataset=ds).values_list("level", "action", "code")))

    def test_moved_unit(self):
        moved = SAMPLE.replace(b"24,LAGOS,07,Ikeja,05,Anifowoshe / Ikeja,002,Sample Primary School II",
                               b"24,LAGOS,07,Ikeja,06,Alausa / Oregun,009,Sample Primary School II")
        rep = loading.preview(moved, source="moved").preview
        self.assertEqual(rep["moved"], [{"from": "24-07-05-002", "to": "24-07-06-009", "name": "Sample Primary School II"}])
        self.assertEqual(rep["counts"]["added"], {})
        self.assertEqual(rep["counts"]["retired"], {})

    def test_bad_files_are_refused(self):
        rep = loading.preview(BROKEN, source="broken").preview
        self.assertEqual(rep["error_count"], 1)
        self.assertIn("Line 3", rep["errors"][0])
        with self.assertRaises(loading.GeographyFileError):
            loading.apply(Dataset.objects.get(source="broken"), None)
        dup = SAMPLE + b"24,LAGOS,07,Ikeja,05,Anifowoshe / Ikeja,001,Sample Primary School I\n"
        self.assertIn("appears twice", loading.preview(dup, source="dup").preview["errors"][0])
        clash = SAMPLE + b"24,LAGOS,07,Ikeja Two,05,Anifowoshe / Ikeja,003,X\n"
        self.assertIn("two names", loading.preview(clash, source="clash").preview["errors"][0])
        wrong_state = SAMPLE + b"25,LAGOS,09,Other,01,W,001,X\n"
        self.assertTrue(any("already has state code" in e for e in loading.preview(wrong_state, source="ws").preview["errors"]))
        with self.assertRaises(loading.GeographyFileError):
            loading.preview(b"state,lga\nlagos,ikeja\n")

    @override_settings(GEO_SECOND_ADMIN_MIN_UNITS=1)
    def test_ninety_five_percent_rule_needs_a_second_admin(self):
        a, b = mk_user("u_sa1", Role.SYSTEM_ADMIN), mk_user("u_sa2", Role.SYSTEM_ADMIN)
        half = b"\n".join(SAMPLE.splitlines()[:3]) + b"\n"  # 2 of 5 units
        ds = loading.preview(half, source="half")
        self.assertTrue(ds.preview["needs_second_admin"])
        url = reverse("admin-geo-dataset-apply", kwargs={"pk": ds.pk})
        r = client(a).post(url, {}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "geo_second_admin_required"))
        ds.refresh_from_db()
        self.assertEqual((ds.status, ds.first_approval_by_id), ("previewed", a.pk))
        self.assertEqual(client(a).post(url, {"confirm_second": True}, format="json").status_code, 409)  # the same admin twice
        self.assertEqual(client(b).post(url, {}, format="json").status_code, 409)  # the second admin must confirm
        r = client(b).post(url, {"confirm_second": True}, format="json")
        self.assertEqual((r.status_code, r.data["status"]), (200, "applied"))
        self.assertEqual(PollingUnit.objects.filter(active=True).count(), 2)

    @override_settings(GEO_SECOND_ADMIN_MIN_UNITS=1)
    def test_cli_counts_as_one_admin_and_needs_confirm_by(self):
        half = DATA.parent.parent / "private_media_test_half.csv"
        half.write_bytes(b"\n".join(SAMPLE.splitlines()[:3]) + b"\n")
        self.addCleanup(half.unlink)
        with self.assertRaises(CommandError):
            call_command("load_geography", str(half), "--apply", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("load_geography", str(half), "--apply", "--confirm-by", "nobody@example.com", stdout=StringIO())
        mk_user("u_sa3", Role.SYSTEM_ADMIN)
        call_command("load_geography", str(half), "--apply", "--confirm-by", "u_sa3@example.com", stdout=StringIO())
        self.assertEqual(PollingUnit.objects.filter(active=True).count(), 2)


class AdminDatasetTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")
        self.sa = mk_user("u_sa", Role.SYSTEM_ADMIN)

    def test_upload_preview_apply_changes(self):
        c = client(self.sa)
        r = c.post(reverse("admin-geo-datasets"), {"file": SimpleUploadedFile("v2.csv", V2, content_type="text/csv"),
                                                   "source": "INEC 2026", "notes": "Checked by A. Bello"}, format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        ds = Dataset.objects.get(pk=r.data["id"])
        self.assertTrue(Path(settings.PRIVATE_MEDIA_ROOT, ds.file_name).exists())
        self.assertEqual(r.data["preview"]["counts"]["added"], {"polling_unit": 1})
        self.assertEqual(c.post(reverse("admin-geo-dataset-apply", kwargs={"pk": ds.pk}), {}, format="json").status_code, 200)
        ch = c.get(reverse("admin-geo-dataset-changes", kwargs={"pk": ds.pk})).data
        self.assertEqual(ch["count"], 3)
        self.assertEqual(c.get(reverse("admin-geo-datasets")).data["results"][0]["status"], "applied")

    def test_only_system_admins_and_csv_only(self):
        off = mk_user("u_off", Role.PVC_OFFICER)
        self.assertEqual(client(off).get(reverse("admin-geo-datasets")).status_code, 403)
        r = client(self.sa).post(reverse("admin-geo-datasets"), {"file": SimpleUploadedFile("x.txt", V2)}, format="multipart")
        self.assertEqual(r.data["error"]["code"], "geo_file_invalid")
        r = client(self.sa).post(reverse("admin-geo-datasets"), {"file": SimpleUploadedFile("x.csv", b"a,b\n1,2\n")}, format="multipart")
        self.assertEqual(r.data["error"]["code"], "geo_file_invalid")

    def test_aliases(self):
        c = client(self.sa)
        ward = Ward.objects.get(lga__code="07", code="05")
        r = c.post(reverse("admin-geo-aliases"), {"level": "ward", "target_id": ward.pk, "name": "Anifowose"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(c.get(reverse("admin-geo-aliases"), {"q": "anifowose"}).data["results"][0]["target_id"], ward.pk)
        hits = services.children("ward", ward.lga_id, "anifowose")
        self.assertEqual(hits[0].pk, ward.pk)
        self.assertEqual(c.delete(reverse("admin-geo-alias", kwargs={"pk": r.data["id"]})).status_code, 204)
        self.assertEqual(AuditLog.objects.filter(action="geo.alias_changed").count(), 2)


class ConstituencyTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")
        load_constituencies()

    def test_load_with_lga_and_ward_areas(self):
        self.assertEqual(Constituency.objects.count(), 9)
        sc = Constituency.objects.get(code="SC-24-01")
        self.assertEqual([a.ward.code for a in sc.areas.all()], ["05"])
        ikeja = Lga.objects.get(state__slug="lagos", code="07")
        self.assertEqual({c.code for c in services.covering(ikeja)}, {"SD-24-02", "FC-24-01", "SC-24-01", "SC-24-02"})
        r = client().get(reverse("geo-constituencies"), {"state": "lagos", "kind": "state_constituency"})
        self.assertEqual([c["code"] for c in r.data["results"]], ["SC-24-01", "SC-24-02", "SC-24-03"])

    def test_unknown_lga_refused(self):
        bad = b"state_code,code,name,lga_codes,ward_codes\n24,SD-X,Bad,99,\n"
        self.assertIn("Unknown LGA", loading.preview(bad, kind="senatorial_districts").preview["errors"][0])

    def test_lga_page_lists_the_right_candidates(self):
        sa = mk_user("u_sa", Role.SYSTEM_ADMIN)
        ikeja = Lga.objects.get(state__slug="lagos", code="07")
        ward = Ward.objects.get(lga=ikeja, code="05")
        con = Constituency.objects.get(code="FC-24-01")
        c = client(sa)
        base = {"full_name": "Tunde Ade", "election_year": 2027, "state": "lagos"}
        r1 = c.post(reverse("admin-candidates"), {**base, "position": "lga_chairman", "lga_id": ikeja.pk}, format="json")
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = c.post(reverse("admin-candidates"), {**base, "full_name": "Ngozi Eze", "position": "house_of_reps", "constituency_id": con.pk},
                    format="json")
        r3 = c.post(reverse("admin-candidates"), {**base, "full_name": "Musa Ali", "position": "councillor", "lga_id": ikeja.pk,
                                                  "ward_id": ward.pk}, format="json")
        bad = c.post(reverse("admin-candidates"), {**base, "full_name": "X", "position": "senator", "constituency_id": con.pk}, format="json")
        self.assertEqual(bad.data["error"]["code"], "geo_mismatch")
        for r in (r1, r2, r3):
            Candidate.objects.filter(pk=r.data["id"]).update(status="published", verified_at=timezone.now(), published_at=timezone.now())
        self.assertEqual(Candidate.objects.get(pk=r2.data["id"]).area_name, "Ikeja (sample)")
        self.assertEqual(Candidate.objects.get(pk=r3.data["id"]).ward_name, "Anifowoshe / Ikeja")
        page = client().get(reverse("lga-page", kwargs={"state": "lagos", "lga": ikeja.slug})).data
        self.assertEqual([(g["key"], g["total"]) for g in page["groups"]],
                         [("lga_chairman", 1), ("councillor", 1), (con.slug, 1)])
        self.assertEqual([w["name"] for w in page["wards"]], ["Alausa / Oregun", "Anifowoshe / Ikeja"])
        self.assertEqual(page["wards"][1]["polling_units"], 2)
        self.assertFalse(page["area_council"])
        lagos = client().get(reverse("state-detail", kwargs={"state": "lagos"})).data
        self.assertEqual({x["name"]: x["candidates"] for x in lagos["lgas"]}, {"Agege": 0, "Ikeja": 3})
        self.assertEqual(lagos["capital"], "Ikeja")
        self.assertEqual(client().get(reverse("candidates"), {"lga": ikeja.pk}).data["count"], 2)


class PublicApiTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample", published_on=timezone.localdate())
        self.ikeja = Lga.objects.get(state__slug="lagos", code="07")

    def test_endpoints(self):
        c = client()
        states = c.get(reverse("geo-states")).data["results"]
        self.assertEqual({s["slug"]: s["lgas"] for s in states}, {"fct": 1, "lagos": 2})
        lgas = c.get(reverse("geo-state-lgas", kwargs={"state": "lagos"}), {"q": "ike"}).data["results"]
        self.assertEqual([x["name"] for x in lgas], ["Ikeja"])
        wards = c.get(reverse("geo-lga-wards", kwargs={"pk": self.ikeja.pk})).data["results"]
        self.assertEqual({w["code"]: w["polling_units"] for w in wards}, {"05": 2, "06": 1})
        units = c.get(reverse("geo-ward-units", kwargs={"pk": wards[1]["id"] if wards[1]["code"] == "05" else wards[0]["id"]})).data
        self.assertEqual(len(units["results"]), 2)
        one = c.get(reverse("geo-unit", kwargs={"code": "24-07-05-001"})).data
        self.assertEqual((one["ward"]["code"], one["lga"]["slug"], one["state"]["slug"], one["active"]), ("05", "ikeja", "lagos", True))
        self.assertTrue(one["inec_url"].startswith("https://"))
        self.assertEqual(len(c.get(reverse("geo-unit-finder"), {"code": "24-07"}).data["results"]), 3)
        self.assertEqual(c.get(reverse("geo-unit-finder"), {"code": "2"}).status_code, 400)
        about = c.get(reverse("geo-about")).data
        self.assertEqual(about["datasets"][0]["polling_units"], 5)
        self.assertEqual(c.get(reverse("geo-unit", kwargs={"code": "99-99-99-999"})).status_code, 404)
        self.assertEqual(c.get(reverse("geo-state-lgas", kwargs={"state": "nowhere"})).status_code, 404)
        # a real state whose INEC list isn't loaded yet: an empty list, not an error (the forms fall back to typing)
        r = c.get(reverse("geo-state-lgas", kwargs={"state": "kebbi"}))
        self.assertEqual((r.status_code, r.data["results"]), (200, []))
        self.assertEqual(c.get(reverse("lga-page", kwargs={"state": "lagos", "lga": "nowhere"})).status_code, 404)

    def test_inactive_unit_flagged(self):
        loading.load(b"\n".join(line for line in SAMPLE.splitlines() if b",002," not in line) + b"\n", source="newer")
        self.assertFalse(client().get(reverse("geo-unit", kwargs={"code": "24-07-05-002"})).data["active"])
        ward = Ward.objects.get(lga=self.ikeja, code="05")
        self.assertEqual(len(client().get(reverse("geo-ward-units", kwargs={"pk": ward.pk})).data["results"]), 1)

    def test_cache_headers_and_etag(self):
        r = client().get(reverse("geo-states"))
        self.assertIn("max-age=3600", r["Cache-Control"])
        self.assertIn("public", r["Cache-Control"])
        tag = r["ETag"]
        self.assertEqual(client().get(reverse("geo-states"), HTTP_IF_NONE_MATCH=tag).status_code, 304)
        loading.load(V2, source="v2")
        self.assertNotEqual(client().get(reverse("geo-states"))["ETag"], tag)  # a new dataset changes it
        page = client().get(reverse("lga-page", kwargs={"state": "lagos", "lga": "ikeja"}))
        self.assertEqual(client().get(reverse("lga-page", kwargs={"state": "lagos", "lga": "ikeja"}),
                                      HTTP_IF_NONE_MATCH=page["ETag"]).status_code, 304)

    def test_throttle_scope_is_catalogued(self):
        from apps.core.throttling import CATALOGUE

        self.assertIn("geo_lookup", CATALOGUE)
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["geo_lookup"], "10000/min")  # generous in tests

    def test_public_endpoints_hold_nothing_personal(self):
        u = mk_user("u_priv")
        Membership.objects.create(user=u, status="pending_approval", state="lagos", full_name="Secret Person", geo_polling_unit=pu("24-07-05-001"))
        for name, kw in (("geo-states", {}), ("geo-state-lgas", {"state": "lagos"}), ("geo-about", {}),
                         ("geo-unit", {"code": "24-07-05-001"}), ("lga-page", {"state": "lagos", "lga": "ikeja"})):
            body = client().get(reverse(name, kwargs=kw)).content.decode()
            self.assertNotIn("Secret Person", body)
            self.assertNotIn(str(u.pk), body)


class PickerTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")
        self.u = mk_user("u_pick")
        self.unit = pu("24-07-05-001")

    def ids(self, p=None):
        p = p or self.unit
        return {"lga_id": p.ward.lga_id, "ward_id": p.ward_id, "polling_unit_id": p.pk}

    def test_membership_saves_ids_and_writes_text(self):
        r = client(self.u).put(reverse("my-membership"), {"state": "lagos", "lga": "ikj", **self.ids()}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        m = Membership.objects.get(user=self.u)
        self.assertEqual((m.lga, m.ward, m.polling_unit, m.geo_source), ("Ikeja", "Anifowoshe / Ikeja", "Sample Primary School I", "chosen"))
        self.assertEqual(r.data["place"]["polling_unit"]["code"], "24-07-05-001")

    def test_chain_mismatch_and_inactive(self):
        other = pu("24-01-01-001")
        r = client(self.u).put(reverse("my-membership"), {"state": "lagos", **self.ids(), "ward_id": other.ward_id}, format="json")
        self.assertEqual((r.data["error"]["code"], r.data["error"]["field"]), ("geo_mismatch", "polling_unit"))
        r = client(self.u).put(reverse("my-membership"), {"state": "kano", **self.ids()}, format="json")
        self.assertEqual(r.data["error"]["code"], "geo_mismatch")
        PollingUnit.objects.filter(pk=self.unit.pk).update(active=False)
        r = client(self.u).put(reverse("my-membership"), {"state": "lagos", **self.ids()}, format="json")
        self.assertEqual(r.data["error"]["code"], "geo_inactive")

    def test_not_listed_goes_to_review(self):
        client(self.u).put(reverse("my-membership"), {"state": "lagos", "lga": "Ikeja", "ward": "New ward", "polling_unit": "New school",
                                                      "geo_not_listed": True}, format="json")
        m = Membership.objects.get(user=self.u)
        self.assertEqual((m.geo_source, m.polling_unit, m.geo_lga_id), ("review", "New school", None))
        Membership.objects.filter(pk=m.pk).update(status="pending_approval")  # drafts aren't queued
        self.assertIn(m, records.review_queue("membership"))

    def test_pvc_chosen_link_does_not_place_until_an_officer_confirms(self):
        from apps.communities.services import memberships, place
        from apps.communities.sync import sync
        from apps.pvc.services import verified_location

        sync()
        r = client(self.u).put(reverse("my-pvc"), {"vin": "90F5B0F8E0295612345", "surname": "Obi", "state": "lagos", "consent": True,
                                                   **self.ids()}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        rec = PvcVerification.objects.get(user=self.u)
        self.assertEqual((rec.lga, rec.geo_source), ("Ikeja", "chosen"))
        PvcVerification.objects.filter(pk=rec.pk).update(status="successful", reviewed_at=timezone.now())
        self.assertEqual(verified_location(self.u), ("lagos", None, None, None))
        place(self.u)
        self.assertNotIn("polling_unit", {m.level for m in memberships(self.u)})
        PvcVerification.objects.filter(pk=rec.pk).update(status="pending", reviewed_at=None)
        officer = mk_user("u_off", Role.PVC_OFFICER)
        r = client(officer).post(reverse("admin-pvc-verify", kwargs={"pk": rec.pk}), self.ids(), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        rec.refresh_from_db()
        self.assertEqual(rec.geo_source, "officer")
        self.assertEqual(verified_location(self.u)[3], self.unit.pk)
        place(self.u)
        self.assertIn("polling_unit", {m.level for m in memberships(self.u)})

    def test_officer_must_give_the_unit(self):
        officer = mk_user("u_off2", Role.PVC_OFFICER)
        rec = PvcVerification.objects.create(user=self.u, vin_last4="1", surname="S", state="lagos", lga="Ikeja", consent_at=timezone.now())
        r = client(officer).post(reverse("admin-pvc-verify", kwargs={"pk": rec.pk}), {"lga_id": self.unit.ward.lga_id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_events_and_volunteers_take_lga_ids(self):
        from apps.events import services as ES

        sa = mk_user("u_sa", Role.SYSTEM_ADMIN)
        ikeja = self.unit.ward.lga
        now = timezone.now()
        r = client(sa).post(reverse("admin-party-events"), {
            "type": "meeting", "mode": "in_person", "title": "Ward meeting", "description": "Come along and meet the ward team. " * 2,
            "venue_name": "Hall", "venue_address": "1 Road", "venue_type": "open_ground", "state": "lagos", "lga_text": "typed", "geo_lga": ikeja.pk,
            "starts_at": (now + timedelta(days=3)).isoformat(), "ends_at": (now + timedelta(days=3, hours=2)).isoformat()}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["lga_text"], r.data["geo_lga"]), ("Ikeja", str(ikeja.pk)))
        self.assertTrue(ES)
        from apps.volunteers import services as VS

        self.assertTrue(VS)


class ConfirmTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")
        self.u = mk_user("u_conf")
        self.m = Membership.objects.create(user=self.u, status="pending_approval", state="lagos", full_name="C", lga="IKEJA",
                                           ward="anifowoshe / ikeja", polling_unit="Sample Primary School I")
        from apps.geo.matching import match_all

        match_all()

    def test_matched_link_is_offered_and_can_be_changed(self):
        self.m.refresh_from_db()
        self.assertEqual(self.m.geo_source, "matched")
        items = client(self.u).get(reverse("me-geo-confirm")).data["results"]
        self.assertEqual(items[0]["kind"], "membership")
        self.assertEqual(items[0]["suggestion"]["polling_unit"]["code"], "24-07-05-001")
        other = pu("24-07-05-002")
        r = client(self.u).post(reverse("me-geo-confirm-kind", kwargs={"kind": "membership"}),
                                {"lga_id": other.ward.lga_id, "ward_id": other.ward_id, "polling_unit_id": other.pk}, format="json")
        self.assertEqual((r.status_code, r.data["geo_source"]), (200, "chosen"))
        self.m.refresh_from_db()
        self.assertEqual((self.m.polling_unit, self.m.geo_polling_unit_id), ("Sample Primary School II", other.pk))
        self.assertEqual(client(self.u).get(reverse("me-geo-confirm")).data["results"], [])
        log = AuditLog.objects.get(action="geo.link_changed")
        self.assertNotIn("Sample", str(log.metadata))

    def test_not_asked_where_the_state_has_no_inec_list(self):
        other = mk_user("u_kebbi")
        Membership.objects.create(user=other, status="pending_approval", state="kebbi", full_name="K", lga="Argungu")
        self.assertEqual(client(other).get(reverse("me-geo-confirm")).data["results"], [])
        from apps.geo.records import notify_unconfirmed

        notify_unconfirmed(limit=50)
        self.assertFalse(Membership.objects.filter(user=other, geo_confirm_asked_at__isnull=False).exists())

    def test_verified_pvc_choice_waits_for_an_officer(self):
        from apps.pvc.services import verified_location

        p = pu("24-07-05-001")
        rec = PvcVerification.objects.create(user=self.u, vin_last4="1", surname="S", state="lagos", lga="Ikeja", polling_unit=p.name,
                                             status="successful", reviewed_at=timezone.now(), consent_at=timezone.now(),
                                             geo_lga=p.ward.lga, geo_ward=p.ward, geo_polling_unit=p, geo_source="matched")
        other = pu("24-07-06-001")
        r = client(self.u).post(reverse("me-geo-confirm-kind", kwargs={"kind": "pvc"}),
                                {"lga_id": other.ward.lga_id, "ward_id": other.ward_id, "polling_unit_id": other.pk}, format="json")
        self.assertTrue(r.data["waits_for_officer"])
        self.assertEqual(verified_location(self.u)[3], p.pk)  # placement unchanged
        officer = mk_user("u_off", Role.PVC_OFFICER)
        q = client(officer).get(reverse("admin-geo-review")).data
        self.assertEqual(q["kinds"], ["pvc"])
        self.assertEqual([x["why"] for x in q["results"]], ["person_choice"])
        r = client(officer).post(reverse("admin-geo-review-action", kwargs={"kind": "pvc", "pk": rec.pk}), {"action": "accept"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(verified_location(self.u)[3], other.pk)
        rec.refresh_from_db()
        self.assertEqual(rec.geo_source, "officer")

    def test_notify_unconfirmed_once(self):
        call_command("geo_notify_unconfirmed", stdout=StringIO())
        call_command("geo_notify_unconfirmed", stdout=StringIO())
        n = Notification.objects.filter(recipient=self.u, kind="geo_confirm")
        self.assertEqual([x.decision for x in n], ["mem"])
        item = [x for x in client(self.u).get(reverse("my-notifications")).data["results"] if x["kind"] == "geo_confirm"][0]
        self.assertEqual(item["geo"], {"record": "membership", "change": ""})


class ReviewQueueTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")
        self.mem_rec = Membership.objects.create(user=mk_user("u_m"), status="pending_approval", state="lagos", full_name="M", lga="Ikejaa")
        self.pvc_rec = PvcVerification.objects.create(user=mk_user("u_p"), vin_last4="1", surname="S", state="lagos", lga="Ikejaa",
                                                      consent_at=timezone.now())

    def test_officers_see_only_their_kinds(self):
        mo, po, sa = mk_user("u_mo", Role.MEMBERSHIP_OFFICER), mk_user("u_po", Role.PVC_OFFICER), mk_user("u_sa", Role.SYSTEM_ADMIN)
        self.assertEqual([x["kind"] for x in client(po).get(reverse("admin-geo-review")).data["results"]], ["pvc"])
        self.assertEqual({x["kind"] for x in client(sa).get(reverse("admin-geo-review")).data["results"]}, {"membership", "pvc"})
        self.assertEqual(client(po).get(reverse("admin-geo-review"), {"kind": "membership"}).status_code, 403)
        self.assertEqual(client(mk_user("u_plain")).get(reverse("admin-geo-review")).status_code, 403)
        mem_items = client(mo).get(reverse("admin-geo-review")).data["results"]
        self.assertEqual(mem_items[0]["suggestion"]["lga"]["name"], "Ikeja")  # trigram ≥ 0.6
        self.assertNotIn("full_name", mem_items[0])

    def test_link_and_ask_are_audited_without_place_names(self):
        po = mk_user("u_po", Role.PVC_OFFICER)
        p = pu("24-07-05-001")
        url = reverse("admin-geo-review-action", kwargs={"kind": "pvc", "pk": self.pvc_rec.pk})
        r = client(po).post(url, {"action": "link", "lga_id": p.ward.lga_id, "ward_id": p.ward_id, "polling_unit_id": p.pk}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.pvc_rec.refresh_from_db()
        self.assertEqual((self.pvc_rec.geo_source, self.pvc_rec.lga), ("officer", "Ikeja"))
        client(po).post(url, {"action": "ask"}, format="json")
        self.assertTrue(Notification.objects.filter(recipient=self.pvc_rec.user, kind="geo_confirm", decision="pvc").exists())
        for log in AuditLog.objects.filter(action="geo.link_changed"):
            self.assertNotIn("Ikeja", str(log.metadata))
            self.assertEqual(log.metadata["record"], "pvc")


class NoticeAndSearchTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")

    def test_retired_unit_sends_one_notice_per_record(self):
        retired, kept = pu("24-07-05-002"), pu("24-07-05-001")
        a, b = mk_user("u_a"), mk_user("u_b")
        Membership.objects.create(user=a, status="pending_approval", state="lagos", full_name="A", lga="Ikeja", geo_lga=retired.ward.lga,
                                  geo_ward=retired.ward, geo_polling_unit=retired, geo_source="chosen")
        Membership.objects.create(user=b, status="pending_approval", state="lagos", full_name="B", lga="Ikeja", geo_lga=kept.ward.lga,
                                  geo_ward=kept.ward, geo_polling_unit=kept, geo_source="chosen")
        loading.apply(loading.preview(V2, source="v2"), None)
        drain()
        drain()
        self.assertEqual(list(Notification.objects.filter(kind="geo_unit_changed").values_list("recipient_id", "decision")),
                         [(a.pk, "mem.ret")])
        self.assertTrue(records.needs_confirmation("membership", Membership.objects.get(user=a)))
        self.assertIn(Membership.objects.get(user=a), records.review_queue("membership"))

    def test_search_finds_states_and_lgas_after_an_apply(self):
        loading.apply(loading.preview(V2, source="v2"), None)
        drain()
        r = client().get(reverse("search"), {"q": "Ikeja"}).data
        self.assertEqual(r["places"]["results"][0], {"kind": "lga", "title": "Ikeja, Lagos State", "state": "lagos",
                                                     "url": "/states/lagos/ikeja"})
        sug = client().get(reverse("search-suggest"), {"q": "lagos"}).data
        self.assertEqual(sug["places"][0]["url"], "/states/lagos")

    def test_privacy_download_includes_the_chosen_place(self):
        from apps.membership.privacy import export_for

        p = pu("24-07-05-001")
        u = mk_user("u_dl")
        Membership.objects.create(user=u, status="pending_approval", state="lagos", full_name="D", geo_lga=p.ward.lga, geo_ward=p.ward,
                                  geo_polling_unit=p, geo_source="chosen")
        place = export_for(u)["membership"]["chosen_place"]
        self.assertEqual(place["polling_unit"], {"name": "Sample Primary School I", "code": "24-07-05-001"})

    def test_integrity_checks_report_planted_problems(self):
        from apps.core import integrity

        p, other = pu("24-07-05-001"), pu("24-01-01-001")
        Membership.objects.create(user=mk_user("u_i"), status="pending_approval", state="lagos", full_name="I", geo_lga=p.ward.lga,
                                  geo_ward=other.ward)
        PvcVerification.objects.create(user=mk_user("u_j"), vin_last4="1", surname="S", state="lagos", lga="Ikeja", geo_source="officer",
                                       consent_at=timezone.now())
        Ward.objects.filter(pk=p.ward_id).update(active=False)
        counts = integrity.run()["counts"]
        self.assertEqual(counts["geo_chain_broken"], 1)
        self.assertEqual(counts["geo_officer_without_unit"], 1)
        self.assertEqual(counts["geo_active_under_inactive"], 2)

    def test_readiness_lines(self):
        from apps.dashboard.services import geo_readiness

        lines = {x["label"]: x for x in geo_readiness(timezone.now())}
        self.assertIn("Only the sample is loaded", lines["Geography"]["detail"])
        self.assertEqual(lines["Constituencies"]["state"], "warning")


class EventsByLgaTests(TestCase):
    def test_events_list_filters_by_lga_id(self):
        loading.load(SAMPLE, source="sample")
        ikeja = Lga.objects.get(state__slug="lagos", code="07")
        self.assertEqual(client().get(reverse("events"), {"lga": ikeja.pk}).status_code, 200)
        self.assertEqual(client().get(reverse("events"), {"lga": "nope"}).data["results"], [])
