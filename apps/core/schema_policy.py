"""
The schema rules the generated dictionary checks (spec 16 §3.2, §5.1). A test fails when a model has no docstring, a
JSON field has no reason here, or a foreign key's delete rule doesn't follow the policy and isn't listed with a reason.
"""
from django.db import models

# Why each JSON column isn't a table (§3.2 rule 6).
JSON_REASONS = {
    "core.OutboxJob.payload": "IDs a background task needs; read only by its handler",
    "audit.AuditLog.metadata": "per-action details from an allow-list; displayed, never joined",
    "candidates.Candidate.priorities": "a short ordered list of the candidate's own words, always read with the profile",
    "candidates.Candidate.links": "a few labelled links, always read with the profile",
    "donations.Donation.declarations": "the ticked declarations at the time of giving, kept as evidence",
    "membership.Membership.declarations": "the ticked declarations at the time of applying, kept as evidence",
    "news.Article.declarations": "the ticked declarations at submission, kept as evidence",
    "moderation.ComplianceYear.figures": "a frozen yearly report; never queried by field",
    "privacy.Incident.data_categories": "a few category keys chosen by the officer, shown with the incident",
    "media.MediaAsset.local_files": "variant → storage name for local files; read only by the media service",
    "events.Event.declarations": "the declarations confirmed at publishing, with the time, kept as evidence (spec 18 rules 3–5)",
    "volunteers.VolunteerOpening.roles": "a few role keys from a fixed list, always read with the opening",
    "volunteers.VolunteerOpening.states": "a few state slugs, always read with the opening",
    "volunteers.VolunteerApplication.roles": "the role keys the person offered, from the opening's list",
    "volunteers.VolunteerApplication.availability": "up to three availability keys, shown with the application",
    "geo.Dataset.preview": "the preview report of a geography file: counts and lists shown together, never queried (spec 19 §11.1)",
}

# Owner-of-private-data links to a person (CASCADE is the rule; §5.1 row 2).
USER_OWNER_LINKS = {
    "access.RoleAssignment.user", "follows.CandidateFollow.user", "notifications.Notification.recipient",
    "notifications.NotificationPreference.user", "privacy.ConsentRecord.user", "pvc.PvcVerification.user", "pvc.PvcEvent.user",
    "moderation.Restriction.user",
    "communities.CommunityMembership.user", "communities.CommunitySettings.user", "communities.CommunityReaction.user",  # spec 17
    "events.EventRegistration.user", "volunteers.VolunteerApplication.user",  # spec 18
}
USER_LEGAL_RECORDS = {"membership.Membership.user"}  # PROTECT (§5.1 row 3)
MONEY_PARENTS = {"donations.Donation", "membership.Membership", "membership.MembershipPayment"}

# Deliberate exceptions, each with its reason (§5.2).
EXCEPTIONS = {
    "moderation.Appeal.case": "an appeal has no meaning without its case",
    "moderation.Appeal.restriction": "an appeal has no meaning without its pause",
    "moderation.LegalNoticeItem.case": "the notice keeps its own record",
    "moderation.LegalNoticeItem.notice": "items belong to their notice",
    "moderation.Report.case": "reports are grouped by their case and pruned with it",
    "moderation.Restriction.case": "a pause outlives the case that caused it",
    "notifications.Notification.membership": "a membership notice survives the record as 'your membership'",
    "notifications.Notification.donation": "a receipt survives an erased record as 'your donation'",
    "moderation.Restriction.community": "a community pause outlives a community that closed",
}


def _name(f):
    return f"{f.model._meta.label}.{f.name}"


def expected(f) -> tuple[set, str]:
    """(allowed on_delete functions, why)."""
    name, target = _name(f), f.related_model._meta.label
    if name in EXCEPTIONS:
        return {f.remote_field.on_delete}, EXCEPTIONS[name]
    if target == "accounts.User":
        if name in USER_OWNER_LINKS:
            return {models.CASCADE}, "owner of private data; erased through the owning service"
        if name in USER_LEGAL_RECORDS:
            return {models.PROTECT}, "a legal record; erased deliberately, never as a side effect"
        return {models.SET_NULL}, "a person as actor; the record outlives the account"
    if f.model.__name__.endswith("Event") and target in MONEY_PARENTS:
        return {models.PROTECT, models.CASCADE}, "money history: kept with its record, removed only by the record's own erasure"
    if f.model.__name__.endswith("Event") and f.model._meta.label != "events.Event":  # events.Event is content (spec 18)
        return {models.CASCADE}, "history goes when retention deletes its parent"
    if target == "candidates.Candidate":
        return {models.PROTECT}, "candidates are archived, never deleted"
    if target in MONEY_PARENTS:
        return {models.PROTECT}, "money history can't disappear"
    if target == "media.MediaAsset":
        return {models.SET_NULL}, "assets are released through media.services"
    if target.startswith("geo."):
        return {models.PROTECT}, "reference data is deactivated, never deleted"
    if f.model._meta.label == "notifications.Notification":
        return {models.CASCADE}, "a notice about deleted content is meaningless"
    if target == "communities.Community":
        return {models.PROTECT}, "communities are deactivated, never deleted"
    if target == "communities.CommunityPost":
        return {models.CASCADE}, "comments and likes go with their post (retention only)"
    if target == "events.Event":
        return {models.CASCADE}, "registrations, updates and history go with their event (retention only)"
    if target in ("volunteers.VolunteerApplication", "volunteers.VolunteerUpdate"):
        return {models.CASCADE}, "history and notices go with their application or update (retention only)"
    return set(), "not covered by the policy: add a reason to EXCEPTIONS"


def check_model(m) -> list[str]:
    problems = []
    doc = m.__dict__.get("__doc__") or ""
    if not doc.strip() or doc.startswith(f"{m.__name__}("):
        problems.append(f"{m._meta.label}: no docstring")
    for f in m._meta.concrete_fields:
        n = f"{m._meta.label}.{f.name}"
        if isinstance(f, models.JSONField) and n not in JSON_REASONS:
            problems.append(f"{n}: JSON field without a reason in JSON_REASONS")
        if f.is_relation and f.many_to_one or getattr(f, "one_to_one", False):
            allowed, why = expected(f)
            if f.remote_field.on_delete not in allowed:
                problems.append(f"{n}: on_delete={f.remote_field.on_delete.__name__} breaks the policy ({why})")
    return problems
