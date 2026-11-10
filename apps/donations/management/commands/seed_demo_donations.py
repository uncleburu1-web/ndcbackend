"""
python manage.py seed_demo_donations [--count 1500] [--total 557000000] [--remove]

DEMO DATA for a local or staging preview of the Finance pages: anonymous "successful" donations that never went
through a payment provider. Refuses to run unless DEBUG is on AND the database is on this machine, so it can never
write to production (the live database is remote, even when you point your laptop at it).

Every demo row has a reference starting NDC-DEMO and an @example.invalid email; --remove deletes exactly those.
No DonationEvent rows are written (that table is append-only), so removal is clean.
"""
import random
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.candidates.models import Candidate, CandidateStatus
from apps.candidates.reference import State
from apps.donations.models import Designation, DisplayChoice, Donation, DonationStatus
from apps.donations.services import ANON_LABEL

PREFIX = "NDC-DEMO"
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
MIN_NAIRA, MAX_NAIRA = 1_000, 5_000_000  # the per-payment ceiling (DONATION_MAX_PER_PAYMENT_KOBO default)


def _guard():
    host = (connection.settings_dict.get("HOST") or "").strip()
    if not settings.DEBUG:
        raise CommandError("Demo data only runs with DEBUG on (local development). Nothing was written.")
    if host not in LOCAL_HOSTS:
        raise CommandError(f"Demo data only goes into a database on this machine, not {host}. Nothing was written.")


def amounts(count: int, total_naira: int, rng: random.Random) -> list[int]:
    """`count` whole-naira amounts, each within the per-payment limits, summing exactly to `total_naira`."""
    if not count * MIN_NAIRA <= total_naira <= count * MAX_NAIRA:
        raise CommandError("That total can't be split into that many donations within ₦1,000–₦5,000,000 each.")
    weights = [rng.lognormvariate(0, 1.4) for _ in range(count)]
    scale = total_naira / sum(weights)
    out = [min(MAX_NAIRA, max(MIN_NAIRA, round(w * scale / 100) * 100)) for w in weights]
    diff = total_naira - sum(out)
    i = 0
    while diff:  # spread the rounding gap without breaking the limits
        j = i % count
        step = max(-(out[j] - MIN_NAIRA), min(MAX_NAIRA - out[j], diff))
        out[j] += step
        diff -= step
        i += 1
    rng.shuffle(out)
    return out


class Command(BaseCommand):
    help = "Local/staging only: add (or --remove) anonymous demo donations to preview the Finance pages."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1500)
        parser.add_argument("--total", type=int, default=557_000_000, help="Total in naira (default 557,000,000).")
        parser.add_argument("--days", type=int, default=120, help="Spread over the last N days.")
        parser.add_argument("--seed", type=int, default=None, help="Random seed, for the same data every time.")
        parser.add_argument("--remove", action="store_true", help="Delete every demo donation instead.")

    def handle(self, *args, **o):
        _guard()
        if o["remove"]:
            n, _ = Donation.objects.filter(reference__startswith=PREFIX).delete()
            self.stdout.write(f"Removed {n} demo donations.")
            return
        rng = random.Random(o["seed"])
        now = timezone.now()
        candidates = list(Candidate.objects.filter(status=CandidateStatus.PUBLISHED).only("id", "election_year"))
        states = list(State.values)
        rows = []
        for i, naira in enumerate(amounts(o["count"], o["total"], rng)):
            cand = rng.choice(candidates) if candidates and rng.random() < 0.3 else None
            paid = now - timedelta(seconds=rng.randint(60, o["days"] * 86400))
            rows.append(Donation(
                reference=f"{PREFIX}{secrets.token_hex(6).upper()}", provider="fake", provider_reference=f"demo-{i}",
                amount_kobo=naira * 100, designation=Designation.CANDIDATE if cand else Designation.PARTY,
                candidate=cand, election_year=cand.election_year if cand else None,
                donor_name="Demo donor", donor_email=f"demo{i}@example.invalid", donor_phone="+2348000000000",
                donor_state=rng.choice(states), donor_address="Demo data — not a real donor",
                display_choice=DisplayChoice.ANONYMOUS, public_label=ANON_LABEL,
                declarations={"demo": True}, status=DonationStatus.SUCCESSFUL, channel="demo",
                paid_at=paid, last_verified_at=paid,
            ))
        with transaction.atomic():
            made = Donation.objects.bulk_create(rows, batch_size=500)
            for d in made:  # created_at is set on insert; line it up with paid_at for realistic lists
                d.created_at = d.paid_at - timedelta(minutes=rng.randint(1, 10))
            Donation.objects.bulk_update(made, ["created_at"], batch_size=500)
        total = sum(r.amount_kobo for r in rows) // 100
        self.stdout.write(f"Added {len(rows)} anonymous demo donations, ₦{total:,} in total "
                          f"({sum(1 for r in rows if r.candidate_id)} to candidates). Remove with --remove.")
