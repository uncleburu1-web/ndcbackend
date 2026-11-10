"""
python manage.py seed_demo_donations [--count 1500] [--total 557000000] [--days 120 | --start YYYY-MM-DD --end YYYY-MM-DD]
                                     [--anonymous-share 0.4] [--candidate-share 0.3] [--seed 557] [--replace | --remove]

SIMULATED donations for previewing the Finance pages locally. Never real contributions:
- every row has is_demo=True (a database check also requires an NDC-DEMO- reference and provider "demo");
- real totals, donor limits, exports, the admin dashboard and reports exclude them; the Finance pages label them DEMO;
- written straight to the table, so no payment provider, webhook, receipt or notification is ever involved;
- refuses to run unless DEBUG is on and the database is on this machine (production is remote, even from your laptop).

Idempotent: running it again with the same count and total does nothing; different numbers need --replace (or --remove).
"""
import random
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from apps.candidates.models import Candidate, CandidateStatus
from apps.donations.models import (
    DEMO_PREFIX,
    Designation,
    DisplayChoice,
    Donation,
    DonationStatus,
)
from apps.donations.services import ANON_LABEL

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
MIN_NAIRA, MAX_NAIRA = 500, 5_000_000
EMAIL_DOMAINS = ("example.com", "example.org", "example.net")  # reserved for examples (RFC 2606)
DEMO_PHONE_PREFIX = "+23480000"  # 0800-000-xxxxx: a toll-free block, never a mobile line; demo use only

FIRST = ("Adaeze", "Bola", "Chidi", "Damilola", "Emeka", "Fatima", "Garba", "Halima", "Ifeoma", "Jide", "Kemi", "Lami", "Musa",
         "Ngozi", "Obinna", "Patience", "Rukayat", "Segun", "Tunde", "Uche", "Victoria", "Wale", "Yetunde", "Zainab", "Aisha",
         "Bashir", "Chiamaka", "Dauda", "Ebere", "Funke", "Gbenga", "Hauwa", "Ibrahim", "Jumoke", "Kelechi", "Ladi", "Mfon",
         "Nkechi", "Oluwaseun", "Precious")
LAST = ("Adebayo", "Bello", "Chukwu", "Danjuma", "Eze", "Fashola", "Gambo", "Hassan", "Ibe", "Jatau", "Kalu", "Lawal", "Mohammed",
        "Nwosu", "Obi", "Ogunleye", "Pam", "Quadri", "Raji", "Sani", "Tanko", "Udo", "Usman", "Wike", "Yakubu", "Zubairu", "Akpan",
        "Bassey", "Etim", "Okon", "Effiong", "Ibrahim", "Aliyu", "Oyelaran", "Nnamdi", "Okafor", "Ekwueme", "Ahmed", "Oladipo", "Garuba")
STREETS = ("Sample", "Example", "Placeholder", "Demo", "Model", "Preview", "Mock", "Specimen")
SUFFIXES = ("Close", "Street", "Avenue", "Crescent", "Road", "Lane", "Drive", "Way")
TOWNS = {  # state slug → capital (state capitals are public places; the street part is invented)
    "abia": "Umuahia", "adamawa": "Yola", "akwa_ibom": "Uyo", "anambra": "Awka", "bauchi": "Bauchi", "bayelsa": "Yenagoa",
    "benue": "Makurdi", "borno": "Maiduguri", "cross_river": "Calabar", "delta": "Asaba", "ebonyi": "Abakaliki", "edo": "Benin City",
    "ekiti": "Ado-Ekiti", "enugu": "Enugu", "gombe": "Gombe", "imo": "Owerri", "jigawa": "Dutse", "kaduna": "Kaduna", "kano": "Kano",
    "katsina": "Katsina", "kebbi": "Birnin Kebbi", "kogi": "Lokoja", "kwara": "Ilorin", "lagos": "Ikeja", "nasarawa": "Lafia",
    "niger": "Minna", "ogun": "Abeokuta", "ondo": "Akure", "osun": "Osogbo", "oyo": "Ibadan", "plateau": "Jos", "rivers": "Port Harcourt",
    "sokoto": "Sokoto", "taraba": "Jalingo", "yobe": "Damaturu", "zamfara": "Gusau", "fct": "Abuja",
}


def guard():
    host = (connection.settings_dict.get("HOST") or "").strip()
    if not settings.DEBUG:
        raise CommandError("Demo data only runs with DEBUG on (local development). Nothing was written.")
    if host not in LOCAL_HOSTS:
        raise CommandError(f"Demo data only goes into a database on this machine, not {host}. Nothing was written.")


def amounts(count: int, total_naira: int, rng: random.Random) -> list[int]:
    """`count` whole-naira amounts in ₦500–₦5,000,000, summing exactly to `total_naira`."""
    if count < 1:
        raise CommandError("--count must be at least 1.")
    if not count * MIN_NAIRA <= total_naira <= count * MAX_NAIRA:
        raise CommandError(f"₦{total_naira:,} can't be split into {count:,} donations of ₦{MIN_NAIRA:,}–₦{MAX_NAIRA:,} each.")
    weights = [rng.lognormvariate(0, 1.4) for _ in range(count)]
    scale = total_naira / sum(weights)
    out = [min(MAX_NAIRA, max(MIN_NAIRA, round(w * scale / 100) * 100)) for w in weights]
    diff, i = total_naira - sum(out), 0
    while diff:  # close the rounding gap without leaving the limits
        j = i % count
        step = max(MIN_NAIRA - out[j], min(MAX_NAIRA - out[j], diff))
        out[j] += step
        diff -= step
        i += 1
    rng.shuffle(out)
    return out


def _totals(qs):
    a = qs.aggregate(n=Count("id"), k=Sum("amount_kobo"))
    return a["n"], a["k"] or 0


class Command(BaseCommand):
    help = "Local only: seed (or --remove) simulated, clearly labelled demo donations to preview the Finance pages."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1500)
        parser.add_argument("--total", type=int, default=557_000_000, help="Total in naira (default 557,000,000).")
        parser.add_argument("--days", type=int, default=120, help="Spread over the last N days (ignored with --start).")
        parser.add_argument("--start", help="First day of the demo period, YYYY-MM-DD.")
        parser.add_argument("--end", help="Last day of the demo period, YYYY-MM-DD (default today).")
        parser.add_argument("--anonymous-share", type=float, default=0.4, help="Share shown as 'Anonymous Donor' (default 0.4).")
        parser.add_argument("--candidate-share", type=float, default=0.3, help="Share given to candidates (default 0.3).")
        parser.add_argument("--seed", type=int, default=557, help="Random seed: the same data every time.")
        parser.add_argument("--replace", action="store_true", help="Remove existing demo data first (one transaction).")
        parser.add_argument("--remove", action="store_true", help="Delete all demo donations and stop.")

    # ------------------------------------------------------------------------------------------------------------------
    def handle(self, *args, **o):
        guard()
        demo_qs, real_qs = Donation.objects.filter(is_demo=True), Donation.objects.filter(is_demo=False)
        real_before = _totals(real_qs)
        if o["remove"]:
            with transaction.atomic():
                n, _ = demo_qs.delete()
                if _totals(real_qs) != real_before:
                    raise CommandError("Real donations changed; rolled back.")
            self.stdout.write(f"Removed {n:,} demo donations. Real donations untouched ({real_before[0]:,}).")
            return

        count, total = o["count"], o["total"]
        target_kobo = total * 100
        existing = _totals(demo_qs)
        if existing[0] and not o["replace"]:
            if existing == (count, target_kobo):
                self.stdout.write(f"Already seeded: {count:,} demo donations, ₦{total:,}. Nothing to do.")
                return
            raise CommandError(f"{existing[0]:,} demo donations (₦{existing[1] // 100:,}) already exist. "
                               "Use --replace to swap them, or --remove first.")
        for share in ("anonymous_share", "candidate_share"):
            if not 0 <= o[share] <= 1:
                raise CommandError(f"--{share.replace('_', '-')} must be between 0 and 1.")
        start, end = self._period(o)
        rng = random.Random(o["seed"])
        money = amounts(count, total, rng)
        candidates = list(Candidate.objects.filter(status=CandidateStatus.PUBLISHED).only("id", "election_year"))
        rows = [self._row(i, naira, rng, candidates, o, start, end) for i, naira in enumerate(money, 1)]

        with transaction.atomic():
            if o["replace"]:
                demo_qs.delete()
            made = Donation.objects.bulk_create(rows, batch_size=500)
            for d in made:  # created_at is stamped on insert: line it up with the demo payment time
                d.created_at = d.paid_at - timedelta(minutes=rng.randint(1, 15))
            Donation.objects.bulk_update(made, ["created_at"], batch_size=500)
            got = _totals(demo_qs)
            if got != (count, target_kobo):  # reconcile in the database before committing
                raise CommandError(f"Reconciliation failed (count {got[0]}, {got[1]} kobo); rolled back.")
            if _totals(real_qs) != real_before:
                raise CommandError("Real donations changed; rolled back.")
        self._summary(demo_qs, real_before, start, end)

    def _period(self, o):
        tz = timezone.get_current_timezone()
        try:
            end_day = date.fromisoformat(o["end"]) if o["end"] else timezone.localdate()
            start_day = date.fromisoformat(o["start"]) if o["start"] else end_day - timedelta(days=o["days"])
        except ValueError as exc:
            raise CommandError("Dates look like 2026-07-01.") from exc
        if start_day > end_day:
            raise CommandError("--start must be on or before --end.")
        start = timezone.make_aware(datetime.combine(start_day, time.min), tz)
        end = min(timezone.make_aware(datetime.combine(end_day, time.max), tz), timezone.now())
        if end <= start:
            raise CommandError("The demo period must include some time before now.")
        return start, end

    def _row(self, i, naira, rng, candidates, o, start, end):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        state = rng.choice(list(TOWNS))
        cand = rng.choice(candidates) if candidates and rng.random() < o["candidate_share"] else None
        anonymous = rng.random() < o["anonymous_share"]
        paid = start + timedelta(seconds=rng.uniform(0, (end - start).total_seconds()))
        return Donation(
            is_demo=True, reference=f"{DEMO_PREFIX}{i:05d}", provider="demo", provider_reference=f"demo-{i:05d}", channel="demo",
            amount_kobo=naira * 100, currency="NGN", status=DonationStatus.SUCCESSFUL, paid_at=paid, last_verified_at=paid,
            designation=Designation.CANDIDATE if cand else Designation.PARTY, candidate=cand,
            election_year=cand.election_year if cand else None,
            donor_name=f"{first} {last} (demo)", donor_email=f"{first}.{last}.{i}@{rng.choice(EMAIL_DOMAINS)}".lower(),
            donor_phone=f"{DEMO_PHONE_PREFIX}{i:05d}", donor_state=state,
            donor_address=f"{rng.randint(1, 120)} {rng.choice(STREETS)} {rng.choice(SUFFIXES)}, {TOWNS[state]} (demo address)",
            display_choice=DisplayChoice.ANONYMOUS if anonymous else DisplayChoice.NAME,
            public_label=ANON_LABEL if anonymous else f"{first} {last[0]}.",
            declarations={"demo": True, "note": "Simulated for a local preview. Not a real contribution."},
        )

    def _summary(self, demo_qs, real_before, start, end):
        agg = demo_qs.aggregate(n=Count("id"), k=Sum("amount_kobo"), lo=Min("amount_kobo"), hi=Max("amount_kobo"),
                                anon=Count("id", filter=Q(display_choice=DisplayChoice.ANONYMOUS)),
                                cand=Count("id", filter=Q(designation=Designation.CANDIDATE)))
        w = self.stdout.write
        w(self.style.SUCCESS("Demo donations seeded (simulated; not real contributions)."))
        w(f"  COUNT          {agg['n']:,}")
        w(f"  SUM(amount)    {agg['k']:,} kobo = ₦{agg['k'] // 100:,}")
        w(f"  Range          ₦{agg['lo'] // 100:,} – ₦{agg['hi'] // 100:,}")
        w(f"  Shown as       {agg['n'] - agg['anon']:,} named (demo names), {agg['anon']:,} 'Anonymous Donor'")
        w(f"  Given to       {agg['n'] - agg['cand']:,} NDC general fund, {agg['cand']:,} candidates")
        w(f"  Period         {start:%d %b %Y} – {end:%d %b %Y}")
        w(f"  Real donations untouched: {real_before[0]:,} (₦{real_before[1] // 100:,})")
        w("  Remove with: python manage.py seed_demo_donations --remove")

