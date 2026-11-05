"""
The data register (spec 14 §5.2): every category of personal data NDCConnect holds, why, on what
lawful basis, who can see it, how long it is kept and which job enforces that. Code, not database:
changes are reviewed like code. `/privacy` is rendered from it, and a test fails when a model that
holds personal data isn't listed here or in NOT_PERSONAL.
"""
from dataclasses import asdict, dataclass
from datetime import date

PRIVACY_VERSION = 1
PRIVACY_UPDATED = date(2026, 11, 4)
PRIVACY_DRAFT = True  # wording pending the DPO's and the legal team's review (spec 14 §15.1)
CHANGES = [(date(2026, 11, 3), "First published."),
           (date(2026, 11, 4), "Added public images and, when it's in use, Cloudinary as the image host.")]


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    models: tuple
    collected: str
    purpose: str
    lawful_basis: str
    visible_to: str
    retention: str
    on_deletion: str
    sensitive: bool = False
    job: str | None = None
    public: bool = True  # shown on /privacy


CATEGORIES = [
    Category("account", "Your account", ("accounts.User",),
             "Your name, email, phone number, an optional home state, that you confirmed you're 18 or older, and when you last used the admin "
             "(admin roles only).",
             "To run your account, show your name where you choose to, and contact you about your own requests.",
             "Contract (providing the service you signed up for), NDPA s. 25(1)(b).",
             "You. System administrators can open an account page (audited); a phone number needs an audited reveal.",
             "While your account exists.",
             "Name, email, phone and home state are erased; the record stays, anonymised, so history still adds up."),
    Category("roles", "Admin roles", ("access.RoleAssignment",),
             "Which admin or campaign roles you hold, who granted them and when.",
             "To control who can use the admin and to review access every 90 days.",
             "Legitimate interest in securing the platform, NDPA s. 25(1)(f).",
             "System administrators.",
             "Kept as part of the audit record (6 years).",
             "Roles are revoked; the grant history stays for the audit record."),
    Category("follows", "Who you follow", ("follows.CandidateFollow",),
             "The candidates you follow (Vote) and whether their notifications are on.",
             "To show your Following page and send you candidates' posts when you ask for them.",
             "Your explicit consent, NDPA s. 30(1)(a) (it shows your political interests).",
             "Only you. Admins see counts, never names.",
             "Until you unfollow or withdraw consent.",
             "Deleted.", sensitive=True),
    Category("donations", "Donations", ("donations.Donation", "donations.DonationEvent"),
             "Amount, the candidate or party you gave to, your name (or 'Anonymous Donor'), email and phone for the receipt, the payment "
             "reference and a hashed IP address.",
             "To take the payment, send your receipt, and keep the records the party must file under the Electoral Act 2026 (ss. 89–93).",
             "Contract and legal obligation, NDPA s. 25(1)(b)–(c); explicit consent for the political element, s. 30.",
             "You, and finance officers and system administrators (audited).",
             "Kept for the party's financial returns. IP hashes are cleared after 90 days.",
             "The public name becomes 'Anonymous Donor'; the finance record is kept.", sensitive=True, job="sweep_payments"),
    Category("membership", "NDC membership", ("membership.Membership", "membership.MembershipEvent", "membership.MembershipPayment",
                                              "membership.MembershipPaymentEvent"),
             "The register details the Electoral Act s. 77(2) asks for (name, sex, date of birth, address, state, LGA, ward, polling unit, NIN, "
             "photo), your declarations, membership number, status and fee payments. NIN is encrypted.",
             "To process your application, issue your card and keep the party register that goes to INEC.",
             "Your explicit consent, NDPA s. 30(1)(a), and the party's legal obligation under the Electoral Act s. 77.",
             "You, and membership officers and system administrators (NIN and photo only through audited reveals).",
             "While you're a member; cancelled applications keep only what the party needs for its records.",
             "NIN, date of birth and photo are erased and the membership cancelled; payments are kept.", sensitive=True),
    Category("pvc", "PVC check", ("pvc.PvcVerification", "pvc.PvcEvent"),
             "Your VIN (encrypted), surname, state, LGA and polling unit, the check's result, and whether you show the verified-voter badge.",
             "To check your voter details against INEC's public checker so you can pay for membership.",
             "Your explicit consent, NDPA s. 30(1)(a).",
             "You, and PVC and membership officers (the VIN only through an audited reveal).",
             "Rejected checks lose their VIN after 90 days.",
             "Deleted.", sensitive=True, job="prune_pvc"),
    Category("news", "Community news you submit", ("news.Article", "news.ArticleEvent"),
             "Your submissions, images, byline choice, declarations and the review history.",
             "To review and publish community news.",
             "Contract, NDPA s. 25(1)(b).",
             "Published stories are public (your name only if you choose); drafts: you and news reviewers.",
             "Rejected or withdrawn after 180 days; removed after 1 year.",
             "Drafts are cleared and your byline hidden; published stories stay as 'a member'.", job="prune_news"),
    Category("posts", "Candidate posts", ("posts.CandidatePost", "posts.PostEvent"),
             "Posts written by campaign accounts, and which account wrote or edited them.",
             "To publish candidates' posts.",
             "Contract with the campaign, NDPA s. 25(1)(b).",
             "Posts are public under the candidate's name; who wrote them: system administrators only.",
             "Deleted or removed posts after 1 year.",
             "Posts stay under the candidate; the author link is kept for the record.", job="prune_posts"),
    Category("candidates", "Candidate profiles", ("candidates.Candidate",),
             "Public candidate details (name, office, area, biography, photo, links) and which admins created or edited them.",
             "To publish verified candidate profiles.",
             "Legitimate interest and the candidate's agreement, NDPA s. 25(1)(f).",
             "Public once published; editing history: system administrators.",
             "While the candidacy is relevant; archived profiles are kept for the record.",
             "Not linked to a member account's deletion."),
    Category("images", "Images", ("media.MediaAsset",),
             "Images published with candidate profiles, campaign posts, community news and announcements, the image's size and type, "
             "and which account uploaded it. Location and camera details are removed before storage.",
             "To show the images with the content they belong to.",
             "Legitimate interest and the publisher's agreement, NDPA s. 25(1)(f).",
             "Public with their content; who uploaded: system administrators.",
             "While the content is published; removed when it's replaced, deleted or pruned.",
             "Images of your community news are deleted with it; the uploader link is cleared."),
    Category("notifications", "Notifications", ("notifications.Notification", "notifications.NotificationPreference"),
             "Your in-app notifications and settings.",
             "To tell you about announcements, decisions on your requests and posts you asked for.",
             "Contract, NDPA s. 25(1)(b).",
             "Only you.",
             "Read after 180 days, unread after 365 days.",
             "Deleted.", job="prune_notifications"),
    Category("moderation", "Reports and moderation", ("moderation.Case", "moderation.Report", "moderation.CaseEvent", "moderation.Restriction",
                                                      "moderation.Appeal", "moderation.LegalNotice", "moderation.LegalNoticeItem"),
             "Reports you make (reason, details, optional contact email, hashed IP), decisions about your content, warnings, pauses, "
             "appeals, and legal or copyright notices.",
             "To handle complaints under the NITDA Code of Practice and the Copyright Act 2022.",
             "Legal obligation and legitimate interest, NDPA s. 25(1)(c), (f).",
             "Reporters stay anonymous to authors; moderators see 'Signed-in member' or 'Guest'; only an audited system-admin reveal shows who.",
             "Closed cases after 1 year, unless under a legal hold.",
             "Your reports are detached from your account; decisions stay for the record.", job="prune_moderation"),
    Category("audit", "Security and audit records", ("audit.AuditLog",),
             "Sensitive admin actions (who did what, when, from which IP address) and security events.",
             "To protect your data and prove who accessed it.",
             "Legal obligation (NDPA s. 39) and legitimate interest, s. 25(1)(c), (f).",
             "System administrators (searches of the log are themselves audited).",
             "6 years.",
             "Kept; your name and email are removed from your account record.", job="prune_audit"),
    Category("privacy", "Your privacy choices and requests", ("privacy.ConsentRecord", "privacy.DataRequest", "privacy.DataRequestEvent"),
             "The consents you gave or withdrew (with versions and dates) and your privacy requests.",
             "To prove consent and answer your requests, as the NDPA requires.",
             "Legal obligation, NDPA ss. 26 and 34–38.",
             "You and privacy officers.",
             "6 years after withdrawal or closing.",
             "Kept as the record of what you asked and agreed; detached from contact details.", job="prune_audit"),
    Category("security_operations", "Incident and access records", ("privacy.Incident", "privacy.IncidentEvent", "privacy.AccessReview"),
             "The breach and incident register and admin access reviews. Counts, not personal details.",
             "To meet the NDPA s. 40 breach record and notification duties and the Cybercrimes Act s. 21 reporting duty.",
             "Legal obligation, NDPA s. 25(1)(c).",
             "Privacy officers and system administrators.",
             "6 years.",
             "Not affected.", job="prune_audit", public=False),
    Category("admin_records", "Settings changes", ("pvc.PvcSettings", "membership.MembershipSettings"),
             "Which admin last changed a setting.",
             "Accountability for configuration changes.",
             "Legitimate interest, NDPA s. 25(1)(f).",
             "System administrators.",
             "Until the next change.",
             "Not affected.", public=False),
]

# Models with no personal data, or only derived/public data, and why (the coverage test needs a reason).
NOT_PERSONAL = {
    "core.JobRun": "background job records: counts only (spec 11)",
    "integrations.WebhookReceipt": "webhook event IDs, types and body hashes; payloads are never stored (spec 15 §12.4)",
    "core.OutboxJob": "background jobs holding record IDs only (spec 15 §9.3)",
    "membership.MembershipNumberSequence": "a per-state counter",
    "search.SearchEntry": "an index of items that are already public",
    "moderation.ComplianceYear": "yearly aggregate counts",
    "privacy.AuditAnchor": "a hash, no personal data",
    "geo.State": "INEC reference data (spec 16 §8)",
    "geo.Lga": "INEC reference data (spec 16 §8)",
    "geo.Ward": "INEC reference data (spec 16 §8)",
    "geo.PollingUnit": "INEC reference data (spec 16 §8)",
    "geo.Dataset": "a record of each geography load (spec 16 §8)",
}


@dataclass(frozen=True)
class ConsentPurpose:
    key: str
    label: str
    version: int
    text: str
    withdraw_effect: str
    material_since: int = 1  # versions below this must be asked again
    notice_anchor: str = ""


CONSENT_PURPOSES = {
    "political_following": ConsentPurpose(
        "political_following", "Following candidates", 1,
        "Following a candidate tells NDCConnect your political interest. Only you can see who you follow; admins see counts only. "
        "You can withdraw this at any time from Your data, which removes every follow.",
        "Every follow and notification bell is removed.", notice_anchor="follows"),
    "membership": ConsentPurpose(
        "membership", "NDC membership application", 1,
        "Your application records your party membership and the register details the Electoral Act asks for, including your NIN and photo. "
        "The party sends the register to INEC before primaries.",
        "Your membership is cancelled and your NIN, date of birth and photo are erased. Payments are kept for the party's records.",
        notice_anchor="membership"),
    "pvc_check": ConsentPurpose(
        "pvc_check", "PVC check", 1,
        "An officer checks your VIN and details on INEC's public voter checker. Your VIN is encrypted and only its last 4 characters are shown.",
        "Your PVC details are deleted. Paying for membership needs a verified PVC again.", notice_anchor="pvc"),
    "verified_badge": ConsentPurpose(
        "verified_badge", "Public verified-voter badge", 1,
        "The badge shows next to your name on community news you publish with your name. It tells readers you're a verified voter.",
        "The badge is turned off.", notice_anchor="pvc"),
}


@dataclass(frozen=True)
class Processor:
    name: str
    role: str
    location: str
    transfer_basis: str
    data: str


def processors():
    from django.conf import settings

    hosting = settings.HOSTING_LOCATION or "to be confirmed (spec 14 §15.6)"
    return [
        Processor("Clerk", "Sign-in and account security", "United States", "Your consent at sign-up (NDPA s. 43(1)(a))",
                  "Email, sign-in methods, sessions"),
        Processor("Paystack", "Payments for donations and membership fees", "Nigeria", "Not a transfer abroad",
                  "Payment details you enter on Paystack's page; NDCConnect never sees card numbers"),
        *([Processor("Cloudinary", "Stores and delivers public images", "United States and its delivery network",
                      "Only images already published to everyone (to be confirmed, spec 15 §19.3)",
                      "Public images with metadata removed; random file names")]
          if getattr(settings, "MEDIA_BACKEND", "local") == "cloudinary" else []),
        Processor("Hosting provider", "Runs NDCConnect's servers, database and backups", hosting,
                  "Depends on the location (to be confirmed)" if not settings.HOSTING_LOCATION else "As stated on this page", "Everything above"),
    ]


RIGHTS = [
    ("access", "See what we hold", "Download your data from Your data, or ask the DPO for anything not in it."),
    ("rectification", "Correct it", "Edit your profile, membership or PVC details, or ask the DPO."),
    ("erasure", "Delete it", "Delete your account from Your data. Some records are kept where the law requires (listed above)."),
    ("withdraw", "Withdraw consent", "From Your data, at any time. It takes effect at once."),
    ("objection", "Object or restrict", "Ask the DPO from Your data."),
    ("portability", "Take it elsewhere", "The download is in a common format (JSON)."),
    ("complaint", "Complain", "To the Nigeria Data Protection Commission (NDPC) if you're not satisfied with our answer."),
]


def public_payload() -> dict:
    from django.conf import settings

    return {
        "version": PRIVACY_VERSION, "updated": PRIVACY_UPDATED.isoformat(), "draft": PRIVACY_DRAFT,
        "controller": settings.CONTROLLER_NAME, "contact": settings.PRIVACY_CONTACT_EMAIL, "security_contact": settings.SECURITY_CONTACT_EMAIL,
        "categories": [{k: v for k, v in asdict(c).items() if k not in ("models", "job", "public")} for c in CATEGORIES if c.public],
        "consents": [asdict(p) for p in CONSENT_PURPOSES.values()],
        "processors": [asdict(p) for p in processors()],
        "rights": [{"key": k, "title": t, "how": h} for k, t, h in RIGHTS],
        "changes": [{"date": d.isoformat(), "text": t} for d, t in CHANGES],
        "request_days": settings.PRIVACY_REQUEST_DAYS,
    }


def admin_payload() -> dict:
    return {"categories": [asdict(c) for c in CATEGORIES], "not_personal": NOT_PERSONAL}


def category_keys() -> list[str]:
    return [c.key for c in CATEGORIES]


def registered_models() -> set[str]:
    return {m for c in CATEGORIES for m in c.models}


