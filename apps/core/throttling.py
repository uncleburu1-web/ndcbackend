"""
The rate-limit catalogue (spec 15 §11.4): every throttle scope, who it counts (IP or user) and why. Rates live in
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]; a test fails when a scope in use is missing here. Published in the schema
(`x-rate-limit`) and docs/api.md.

Throttles **fail open**: if the shared cache is unreachable the request continues and the failure is logged,
because authentication and permissions never depend on the cache. `install()` (CoreConfig.ready) applies that to
every DRF throttle, so existing throttle classes need no change.
"""
import logging

logger = logging.getLogger("apps.core.throttling")

CATALOGUE = {
    "profile_write": ("user", "Profile edits (02)"),
    "clerk_webhook": ("ip", "Clerk webhooks (02)"),
    "candidates_public": ("ip", "Public candidate pages, against scraping bursts (03)"),
    "follows": ("user", "Follow, unfollow and bell changes (04)"),
    "donations_start": ("ip and user", "Starting a donation (05)"),
    "donations_receipt": ("ip", "Receipt lookups by reference (05)"),
    "finance_public": ("ip", "Public finance totals (05)"),
    "payment_webhook": ("ip", "Paystack webhooks (05)"),
    "pvc_submit": ("user", "PVC submissions (06)"),
    "membership_pay": ("user", "Membership fee payments (06)"),
    "pii_reveal": ("user", "Officer reveals of VIN, NIN and phone numbers (06)"),
    "news_public": ("ip", "Public news pages (07)"),
    "news_submit": ("user", "Community news submissions (07)"),
    "news_write": ("user", "News edits, images, withdrawals (07)"),
    "news_report": ("user or ip", "Reports of news and posts (07, 09)"),
    "posts_write": ("user", "Candidate post writes (09)"),
    "community_post_hour": ("user", "Community posts: 5 an hour (17)"),
    "community_post_day": ("user", "Community posts: 20 a day (17)"),
    "community_comment": ("user", "Community comments: 30 an hour (17)"),
    "community_reaction": ("user", "Community likes: 120 an hour (17)"),
    "community_write": ("user", "Community joins, leaves, edits and deletes (17)"),
    "admin_write": ("user", "Admin changes: grants, revokes, deactivations, retries (11)"),
    "admin_read": ("user", "Admin lists and searches (11)"),
    "search_public": ("ip", "Search (12)"),
    "moderation_public": ("user or ip", "Copyright notices, appeals, counter-notices (13)"),
    "data_export": ("user", "Your data downloads (14)"),
    "privacy_public": ("user", "Privacy requests, consent changes, deletion (14)"),
    "media_sign": ("user", "Image upload signatures (15)"),
    "ready": ("ip", "Readiness checks (15)"),
}


def install():
    from rest_framework.throttling import SimpleRateThrottle

    if getattr(SimpleRateThrottle, "_ndc_fail_open", False):
        return
    from rest_framework.exceptions import APIException

    from .exceptions import ServiceError

    original = SimpleRateThrottle.allow_request

    def allow_request(self, request, view):
        try:
            return original(self, request, view)
        except (APIException, ServiceError):
            raise  # a throttle's own "too many" answer, not a cache failure
        except Exception:
            logger.warning("Rate-limit cache unavailable for %s; allowing the request", getattr(self, "scope", "?"))
            return True

    SimpleRateThrottle.allow_request = allow_request
    SimpleRateThrottle._ndc_fail_open = True
