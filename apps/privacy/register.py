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
             "Your name, email, phone number, the profile photo you uploaded in Clerk (if any), an optional home state, that you confirmed "
             "you're 18 or older, and when you last used the admin (admin roles only).",
             "To run your account, show your name where you choose to, and contact you about your own requests.",
             "Contract (providing the service you signed up for), NDPA s. 25(1)(b).",
             "You. System administrators can open an account page (audited); a phone number needs an audited reveal.",
             "While your account exists.",
             "Name, email, phone and home state are erased; the record stays, anonymised, so history still adds up."),
    Category("settings", "Your settings and public profile", ("accounts.UserPreferences", "accounts.RetiredHandle"),
             "Your language, display and notification-category choices, the name style for your bylines, the default name on your "
             "donations, and, if you turn it on, your public profile: its address, bio, the location you chose to show and whether to show "
             "your photo.",
             "To show NDCConnect the way you asked, on every device, and to publish the profile you chose.",
             "Contract, NDPA s. 25(1)(b); your explicit consent for the public profile, s. 30(1)(a) (it shows that you use a party platform).",
             "Only you. The public profile, when on, is public; admins see counts only (for example how many people chose dark mode).",
             "While your account exists.",
             "Deleted. A public-profile address is retired so nobody else gets it."),
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
    Category("communities", "Communities", ("communities.CommunityMembership", "communities.CommunitySettings", "communities.CommunityPost",
                                            "communities.CommunityComment", "communities.CommunityReaction", "communities.CommunityEvent"),
             "Which communities you're in (state, LGA, ward and polling unit) and how you were placed there, what you post and comment, "
             "the posts you like, and the history of edits and moderation decisions on your posts.",
             "To let you discuss politics with people where you vote.",
             "Your explicit consent, NDPA s. 30(1)(a) (it shows your political opinions and where you vote).",
             "Members of a community see its posts and comments with your first name and last initial. Nobody, admins included, can see who "
             "is in a community or who liked a post. Moderators see items in their queue; system administrators can open an author's "
             "account through an audited view.",
             "Posts you delete: text erased after 30 days. Removed posts: 1 year. Ended memberships: 30 days.",
             "Memberships and likes are deleted; your posts and comments stay as 'Former member'.", sensitive=True, job="prune_communities"),
    Category("events", "Events", ("events.EventRegistration",),
             "The events you register for, your ticket code, whether you were waitlisted and when you were checked in.",
             "To give you a place at events and send you updates and reminders.",
             "Your explicit consent, NDPA s. 30(1)(a) (registering for a political event shows your political opinions).",
             "You. The event's organiser sees how many people registered and, from 24 hours before to 24 hours after the event, your "
             "first name and last initial with your ticket code for check-in. Nobody else, admins included, sees that you registered.",
             "Deleted 90 days after the event ends; counts are kept.",
             "Your registrations are deleted.", sensitive=True, job="prune_events"),
    Category("event_records", "Event listings", ("events.Event", "events.EventUpdate", "events.EventChange"),
             "Events published by campaigns and the party, their updates and their history, and which account created them.",
             "To publish campaign and party events.",
             "Legitimate interest in publishing public campaign activity, NDPA s. 25(1)(b).",
             "Published events are public under the campaign's or the party's name; who created them: system administrators only.",
             "2 years after the event ends (removed events: the moderation preservation period).",
             "Events stay under the organiser; the creator link is cleared.", job="prune_events"),
    Category("volunteering", "Volunteering", ("volunteers.VolunteerApplication", "volunteers.VolunteerChange",
                                              "volunteers.VolunteerUpdate", "volunteers.VolunteerOpening"),
             "Your volunteer applications (roles, state and LGA, availability, a note, whether to share your phone), the campaign's "
             "decision, the updates campaigns send their volunteers, and campaigns' volunteer settings.",
             "To let you volunteer for a campaign and let the campaign coordinate with you (FR-047–FR-050).",
             "Your explicit consent, NDPA s. 30(1)(a) (volunteering shows which candidate you support).",
             "You and the campaign you applied to: first name and last initial, state, LGA, roles, availability and note; if they accept "
             "you, your full name and, if you chose, your phone number (each view is recorded). Nobody else.",
             "Declined, withdrawn or ended: 90 days. Accepted: until 90 days after the candidate's election. Updates: 1 year.",
             "Your applications are deleted.", sensitive=True, job="prune_volunteers"),
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
    "accounts.LanguageReview": "a translation review sign-off: the reviewer's name as written by an admin, the catalogue version and "
                               "the admin who recorded it (spec 21 §14)",
    "moderation.StaffLanguage": "which languages a moderator or news reviewer reads, for their own queues (spec 21 §9)",
    "news.ArticleTranslation": "an official announcement in another language and the admins who wrote and reviewed it (spec 21 §10)",
    "accounts.SettingsDefaults": "platform defaults for new accounts and the enabled languages; who last changed them (spec 20 §14)",
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
    "geo.Dataset": "a record of each geography load (spec 16 §8); the uploaded file is reference data (spec 19 §17.1)",
    "geo.Constituency": "INEC reference data (spec 19 §5)",
    "geo.ConstituencyArea": "INEC reference data (spec 19 §5)",
    "geo.Alias": "other names for places (spec 19 §4.5)",
    "geo.Change": "what each geography file changed (spec 19 §4.4)",
    "communities.Community": "communities themselves are geography, with counts only (spec 17 §17.2)",
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
        "verified_badge", "Public verified-voter badge", 2,
        "The badge shows next to your name on community news you publish with your name, and on your community posts and comments. "
        "It tells readers you're a verified voter.",
        "The badge is turned off.", notice_anchor="pvc"),
    # v2 adds communities (spec 17 §17.1). Material for communities only: a v1 record keeps the badge on community news, and the
    # badge shows in communities once the person agrees to v2 (pvc.services.community_badge; consents_needed asks them).
    "event_registration": ConsentPurpose(
        "event_registration", "Event registration", 1,
        "Registering for an event tells NDCConnect, and the event's organiser, that you plan to attend a political event. The organiser "
        "sees your first name and last initial with your ticket code around the event, and how many people registered. Nobody else sees "
        "that you registered. You can cancel any time.",
        "Your upcoming registrations are cancelled and deleted.", notice_anchor="events"),
    "volunteering": ConsentPurpose(
        "volunteering", "Volunteering", 1,
        "Volunteering tells NDCConnect, and the campaign you apply to, that you support that candidate. The campaign sees your first name "
        "and last initial, state, LGA, roles, availability and note; if they accept you, your full name, and your phone number if you "
        "choose to share it. You can withdraw any time.",
        "Your open applications are withdrawn and the campaign can no longer see your contact details.", notice_anchor="volunteering"),
    "public_profile": ConsentPurpose(
        "public_profile", "Public profile", 1,
        "A public profile shows that you use NDCConnect, which says something about your politics. It shows only what you choose: your "
        "public name, a short bio, your state or LGA, your photo, the Verified voter badge if it's on, and community news you published "
        "under your name. It never shows who you follow, your communities, membership, PVC or donations. It isn't in search and asks "
        "search engines not to list it. You can turn it off at any time.",
        "Your public profile is turned off at once.", notice_anchor="settings"),
    "community_membership": ConsentPurpose(
        "community_membership", "Communities", 1,
        "Communities let you discuss politics with people in your state, LGA, ward and polling unit. Joining records which communities "
        "you're in, and what you post, comment and like there. If your PVC is verified, we place you in the communities for your polling "
        "unit. Nobody can see who is in a community; posts show your first name and last initial. You can leave any community or "
        "withdraw at any time from Your data.",
        "You leave every community. Your posts and comments stay, shown as 'Former member', unless you choose to delete them too.",
        notice_anchor="communities"),
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


