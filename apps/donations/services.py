"""
Donation rules — spec 05 §6. Every status change happens here, under a row lock.

- A donation becomes `successful` ONLY from a provider verification whose amount equals
  `amount_kobo` and whose currency is NGN (FR-042). Anything else → `review`.
- Webhooks are never trusted on their own: every handled event triggers `verify`.
- Repeated events are no-ops (idempotent state machine).
"""
import hashlib
import hmac
import logging
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.candidates.models import Candidate, CandidateStatus
from apps.core.exceptions import ServiceError

from .models import Designation, DisplayChoice, Donation, DonationEvent, DonationStatus as S
from .providers import ProviderError, get_provider
from .providers.paystack import HANDLED_EVENTS as PAYSTACK_EVENTS

logger = logging.getLogger(__name__)

ANON_LABEL = "Anonymous Donor"
GUEST_LABEL = "Guest"
FAKE_EVENTS = {"charge.success", "charge.failed", "refund.processed"}
OPEN_STATES = (S.INITIATED, S.PENDING)


# ------------------------------------------------------------------ gate (§6.6)
def gate_status() -> dict[str, bool]:
    try:
        provider = get_provider()
        provider_ok = provider.configured()
    except Exception:  # unknown provider, or fake outside DEBUG/tests
        provider_ok = False
    limits_ok = all(isinstance(getattr(settings, k, None), int) and getattr(settings, k) > 0 for k in (
        "DONATION_MIN_KOBO", "DONATION_MAX_PER_PAYMENT_KOBO", "DONATION_CANDIDATE_DONOR_CAP_KOBO", "DONATION_PARTY_DONOR_CAP_KOBO"))
    limits_ok = limits_ok and settings.DONATION_MIN_KOBO <= settings.DONATION_MAX_PER_PAYMENT_KOBO
    return {
        "DONATIONS_ENABLED": bool(settings.DONATIONS_ENABLED),
        "payment provider configured": provider_ok,
        "donation limits set": limits_ok,
        "DONATIONS_COMPLIANCE_APPROVED": bool(settings.DONATIONS_COMPLIANCE_APPROVED),
    }


def donations_open() -> bool:
    return all(gate_status().values())


def config_payload() -> dict:
    is_open = donations_open()
    return {
        "open": is_open,
        "min_kobo": settings.DONATION_MIN_KOBO if is_open else None,
        "max_per_payment_kobo": settings.DONATION_MAX_PER_PAYMENT_KOBO if is_open else None,
        "presets_kobo": list(settings.DONATION_PRESETS_KOBO) if is_open else [],
        "designations": {"party": True, "candidate": True} if is_open else {},
    }


# ----------------------------------------------------------------- helpers
def published_candidate(slug: str) -> Candidate | None:
    if not slug:
        return None
    return Candidate.objects.filter(slug=slug, status=CandidateStatus.PUBLISHED, verified_at__isnull=False).first()


def ip_hash(ip: str) -> str:
    if not ip:
        return ""
    return hmac.new(settings.SECRET_KEY.encode(), ip.encode(), hashlib.sha256).hexdigest()[:32]


def public_label_for(d: Donation) -> str:
    if d.display_choice == DisplayChoice.GUEST:
        return GUEST_LABEL
    if d.display_choice == DisplayChoice.NAME and d.donor_user_id and d.donor_user and d.donor_user.is_active:
        return (d.donor_user.full_name or d.donor_name).strip() or ANON_LABEL
    return ANON_LABEL


def _identity_q(user, email: str, phone: str) -> Q:
    q = Q(donor_email__iexact=email) | Q(donor_phone=phone)
    if user is not None:
        q |= Q(donor_user=user)
    return q


def _period_q(designation: str, candidate, election_year, year: int) -> Q:
    if designation == Designation.CANDIDATE:
        return Q(designation=Designation.CANDIDATE, candidate=candidate, election_year=election_year)
    return Q(designation=Designation.PARTY, created_at__year=year)


def _cap(designation: str) -> int:
    return settings.DONATION_CANDIDATE_DONOR_CAP_KOBO if designation == Designation.CANDIDATE else settings.DONATION_PARTY_DONOR_CAP_KOBO


def committed_kobo(*, user, email, phone, designation, candidate, election_year, year, include_pending=True, exclude_id=None) -> int:
    """Successful (net) + review (+ pending at start) for the same donor in the same limit period (§6.3)."""
    states = [S.SUCCESSFUL, S.REVIEW] + ([S.PENDING, S.INITIATED] if include_pending else [])
    qs = Donation.objects.filter(_identity_q(user, email, phone), _period_q(designation, candidate, election_year, year), status__in=states)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    agg = qs.aggregate(a=Sum("amount_kobo"), r=Sum("refunded_kobo"))
    return (agg["a"] or 0) - (agg["r"] or 0)


def _over_limit_at_verify(d: Donation) -> bool:
    used = committed_kobo(user=d.donor_user, email=d.donor_email, phone=d.donor_phone, designation=d.designation,
                          candidate=d.candidate, election_year=d.election_year, year=d.created_at.year,
                          include_pending=False, exclude_id=d.pk)
    return used + d.amount_kobo > _cap(d.designation)


def _event(d, *, source, event="", status_from="", status_to="", result=None, note="", payload_sha256=""):
    DonationEvent.objects.create(
        donation=d, source=source, event=event[:48], status_from=status_from, status_to=status_to,
        amount_kobo=getattr(result, "amount_kobo", None), currency=getattr(result, "currency", "")[:3],
        note=note[:120], payload_sha256=payload_sha256,
    )


# ------------------------------------------------------------- start (§6.2)
def start_donation(*, data: dict, user, request) -> Donation:
    if not donations_open():
        raise ServiceError("Donations aren't open yet.", code="donations_unavailable", status_code=503)

    amount = data["amount_kobo"]
    if not settings.DONATION_MIN_KOBO <= amount <= settings.DONATION_MAX_PER_PAYMENT_KOBO:
        lo, hi = settings.DONATION_MIN_KOBO // 100, settings.DONATION_MAX_PER_PAYMENT_KOBO // 100
        raise ServiceError(f"Give between ₦{lo:,} and ₦{hi:,} in one payment.", code="amount_out_of_range", status_code=400)

    candidate = None
    if data["designation"] == Designation.CANDIDATE:
        candidate = published_candidate(data.get("candidate_slug", ""))
        if candidate is None:
            raise ServiceError("That candidate isn't available for donations. Choose another, or give to the NDC general fund.",
                               code="candidate_unavailable", status_code=400)

    signed_in = bool(user and user.is_authenticated)
    display = DisplayChoice.GUEST if not signed_in else (
        DisplayChoice.ANONYMOUS if data.get("display_choice") == DisplayChoice.ANONYMOUS else DisplayChoice.NAME)
    election_year = candidate.election_year if candidate else None
    year = timezone.now().year
    donor_user = user if signed_in else None

    with transaction.atomic():
        # Serialise limit checks per donor (email) so two simultaneous starts can't both pass —
        # a transaction-scoped advisory lock also covers a donor's very first donation.
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"donor:{data['donor_email'].strip().lower()}"])
        used = committed_kobo(user=donor_user, email=data["donor_email"], phone=data["donor_phone"], designation=data["designation"],
                              candidate=candidate, election_year=election_year, year=year)
        remaining = _cap(data["designation"]) - used
        if amount > remaining:
            raise ServiceError(
                f"This would take you over the legal donation limit. You can give up to ₦{max(remaining, 0) // 100:,} more.",
                code="over_limit", status_code=400)

        provider = get_provider()
        d = Donation.objects.create(
            provider=provider.name, amount_kobo=amount, designation=data["designation"], candidate=candidate,
            election_year=election_year, donor_user=donor_user, donor_name=data["donor_name"].strip(),
            donor_email=data["donor_email"].strip().lower(), donor_phone=data["donor_phone"], donor_state=data["donor_state"],
            donor_address=data["donor_address"].strip(), display_choice=display,
            declarations={"own_funds": True, "nigerian_source": True, "voluntary": True, "accepted_at": timezone.now().isoformat()},
            initiated_ip_hash=ip_hash(_client_ip(request)),
        )

    callback = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/donate/complete?reference={d.reference}"
    try:
        init = get_provider(d.provider).initialize(d, callback_url=callback, base_url=request.build_absolute_uri("/"))
    except ProviderError as exc:
        logger.error("Donation %s: provider initialize failed: %s", d.reference, exc)
        _transition(d.pk, S.FAILED, source=DonationEvent.Source.SYSTEM, note="initialize failed")
        raise ServiceError("We couldn't reach the payment provider. No money was taken. Try again.",
                           code="payment_provider_unavailable", status_code=502) from None
    with transaction.atomic():
        locked = Donation.objects.select_for_update().get(pk=d.pk)
        if init.provider_reference:
            locked.provider_reference = init.provider_reference
        prev = locked.status
        if locked.status == S.INITIATED:
            locked.status = S.PENDING
        locked.save()
        _event(locked, source=DonationEvent.Source.SYSTEM, event="initialized", status_from=prev, status_to=locked.status)
    d.checkout_url = init.checkout_url
    return d


def _client_ip(request) -> str:
    from rest_framework.throttling import SimpleRateThrottle

    try:
        return SimpleRateThrottle().get_ident(request) or ""
    except Exception:
        return request.META.get("REMOTE_ADDR", "")


def _transition(pk, to, *, source, note=""):
    with transaction.atomic():
        d = Donation.objects.select_for_update().get(pk=pk)
        prev = d.status
        d.status = to
        d.save(update_fields=["status", "updated_at"])
        _event(d, source=source, status_from=prev, status_to=to, note=note)


# ------------------------------------------------------- verification (§6.1)
def apply_result(donation_id, result, *, source, event="", payload_sha256="", allow_review_clear=False) -> Donation:
    with transaction.atomic():
        d = Donation.objects.select_for_update(of=("self",)).select_related("donor_user", "candidate").get(pk=donation_id)
        prev, prev_refunded = d.status, d.refunded_kobo
        now = timezone.now()
        d.last_verified_at = now
        if result.provider_reference and not d.provider_reference:
            d.provider_reference = result.provider_reference[:64]
        if result.channel:
            d.channel = result.channel[:32]

        if result.status == "pending":
            if d.status == S.INITIATED:
                d.status = S.PENDING
        elif result.status in ("failed", "abandoned"):
            if d.status in OPEN_STATES:
                d.status = S.FAILED if result.status == "failed" else S.ABANDONED
        elif result.status in ("success", "reversed"):
            if result.amount_kobo != d.amount_kobo or result.currency != "NGN":
                if d.status != S.REFUNDED:
                    d.status, d.review_reason = S.REVIEW, "amount_or_currency_mismatch"
            else:
                becoming_successful = d.status in OPEN_STATES or (
                    d.status == S.REVIEW and allow_review_clear and d.review_reason != "over_limit")
                if becoming_successful:
                    if d.status in OPEN_STATES and _over_limit_at_verify(d):
                        d.status, d.review_reason = S.REVIEW, "over_limit"
                    else:
                        d.status, d.review_reason = S.SUCCESSFUL, ""
                        d.paid_at = result.paid_at or d.paid_at or now
                        d.public_label = public_label_for(d)
                refunded = result.refunded_kobo
                if result.status == "reversed" and refunded == 0:
                    refunded = d.amount_kobo
                refunded = min(max(refunded, d.refunded_kobo), d.amount_kobo)
                if refunded != d.refunded_kobo:
                    d.refunded_kobo = refunded
                    d.refunded_at = now
                if d.status == S.SUCCESSFUL and d.refunded_kobo >= d.amount_kobo:
                    d.status = S.REFUNDED

        d.save()
        if d.status != prev or d.refunded_kobo != prev_refunded:
            note = d.review_reason if d.status == S.REVIEW else (f"refunded {d.refunded_kobo}" if d.refunded_kobo != prev_refunded else "")
            _event(d, source=source, event=event, status_from=prev, status_to=d.status, result=result, note=note,
                   payload_sha256=payload_sha256)
        return d


def verify_now(d: Donation, *, source, event="", check_refunds=False, payload_sha256="", allow_review_clear=False) -> Donation:
    result = get_provider(d.provider).verify(d, check_refunds=check_refunds)
    return apply_result(d.pk, result, source=source, event=event, payload_sha256=payload_sha256, allow_review_clear=allow_review_clear)


def maybe_reverify(d: Donation, *, min_age_seconds=5) -> Donation:
    """Receipt/status reads re-check pending donations, at most every few seconds (§7.1)."""
    if d.status not in OPEN_STATES:
        return d
    if d.last_verified_at and (timezone.now() - d.last_verified_at).total_seconds() < min_age_seconds:
        return d
    try:
        return verify_now(d, source=DonationEvent.Source.VERIFY)
    except ProviderError as exc:
        logger.warning("Donation %s: verify failed: %s", d.reference, exc)
        return d


# ------------------------------------------------------------- webhooks (§6.4)
def handle_webhook(provider_name: str, raw_body: bytes, headers) -> None:
    provider = get_provider(provider_name)
    event = provider.parse_webhook(raw_body, headers)  # raises InvalidSignature
    handled = PAYSTACK_EVENTS if provider_name == "paystack" else FAKE_EVENTS
    if event.name not in handled:
        return
    d = Donation.objects.filter(reference=event.reference, provider=provider_name).first()
    if d is None:
        logger.warning("Payment webhook %s for unknown reference %s", event.name, event.reference[:40])
        return
    verify_now(d, source=DonationEvent.Source.WEBHOOK, event=event.name, check_refunds=event.name.startswith("refund."),
               payload_sha256=event.payload_sha256)


# --------------------------------------------------------------- admin (§4.6)
def recheck(d: Donation, *, actor, request=None) -> Donation:
    try:
        updated = verify_now(d, source=DonationEvent.Source.ADMIN_RECHECK, check_refunds=True, allow_review_clear=True)
    except ProviderError as exc:
        raise ServiceError("The payment provider didn't answer. Try again in a minute.", code="payment_provider_unavailable",
                           status_code=502) from exc
    record(AuditLog.Action.DONATION_RECHECKED, target=d, actor=actor, request=request, status=updated.status)
    return updated


# ---------------------------------------------------------------- sweep (§6.5)
def sweep(*, older_than_minutes=10, abandon_after_hours=24) -> dict:
    now = timezone.now()
    counts = {"rechecked": 0, "abandoned": 0, "ip_hashes_cleared": 0}
    for d in Donation.objects.filter(status__in=OPEN_STATES, created_at__lte=now - timedelta(minutes=older_than_minutes)):
        try:
            d = verify_now(d, source=DonationEvent.Source.SWEEP)
            counts["rechecked"] += 1
        except ProviderError as exc:
            logger.warning("Sweep: verify failed for %s: %s", d.reference, exc)
        if d.status in OPEN_STATES and d.created_at <= now - timedelta(hours=abandon_after_hours):
            _transition(d.pk, S.ABANDONED, source=DonationEvent.Source.SWEEP, note="no success after 24 h")
            counts["abandoned"] += 1
    counts["ip_hashes_cleared"] = (Donation.objects.filter(created_at__lte=now - timedelta(days=90)).exclude(initiated_ip_hash="")
                                   .update(initiated_ip_hash=""))
    return counts


# ------------------------------------------------------- account deletion (§6.7)
def anonymise_for_deleted_user(user) -> int:
    """Public labels → "Anonymous Donor"; finance record (name/address/contacts) kept for party returns (s. 93)."""
    qs = Donation.objects.filter(donor_user=user)
    n = qs.count()
    if not n:
        return 0
    qs.filter(display_choice=DisplayChoice.NAME).update(display_choice=DisplayChoice.ANONYMOUS)
    Donation.objects.filter(donor_user=user, status__in=[S.SUCCESSFUL, S.REFUNDED, S.REVIEW]).update(public_label=ANON_LABEL)
    qs.update(donor_user=None)
    record(AuditLog.Action.DONATION_LABEL_ANONYMISED, target=user, actor_label="clerk:webhook", donations=n)
    return n
