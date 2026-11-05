"""
Communities — spec 17 §12. Every rule about who may read, post, join or leave lives here; views call these functions.

Placement (§5.2) uses only `pvc.services.verified_location` (08's boundary) and the `community_membership` consent (14).
Moderation actions on posts and comments are called only by `moderation.services` (13's owning-app rule).
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import is_system_admin
from apps.core.exceptions import ServiceError

from . import text as T
from .models import (LOWER_LEVELS, Community, CommunityComment, CommunityEvent, CommunityMembership, CommunityPost, CommunityReaction,
                     CommunitySettings, EndReason, How, ItemStatus, Level, MembershipStatus, ReadScope)

PURPOSE = "community_membership"
EDIT_MINUTES = 30
NEW_ACCOUNT_HOURS = 24
STATE_CHANGE_DAYS = 30
DUPLICATE_HOURS = 24
HIDE_DECIDE_HOURS = 48
POST_MIN, POST_MAX, POST_LINKS = 10, 2000, 5
COMMENT_MIN, COMMENT_MAX, COMMENT_LINKS = 2, 1000, 2
PAUSE_DAYS = (1, 7, 30)
E = CommunityEvent.Kind
ACTIVE = MembershipStatus.ACTIVE


def _err(msg, code, status=400, **extra):
    e = ServiceError(msg, code=code, status_code=status)
    if extra:
        e.extra = extra
    return e


def _signed_in(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.is_active)


# --------------------------------------------------------------------------- structure
def national():
    return Community.objects.get(level=Level.NATIONAL)


def chain(community) -> list:
    """The community and its parents, nearest first (at most 5 levels)."""
    out, c = [], community
    while c is not None:
        out.append(c)
        c = c.parent
    return out


def chain_ids(community) -> list:
    return [c.pk for c in chain(community)]


def load(pk):
    return Community.objects.select_related("parent__parent__parent__parent").filter(pk=pk).first()


def band(n: int) -> str:
    """§4.4: what people see instead of a member count."""
    if n < 10:
        return "Fewer than 10"
    if n < 100:
        return "10–99"
    if n < 1000:
        return "100–999"
    return "1,000+"


def admin_count(community):
    """Admins: exact for national, state and LGA communities; banded for wards and polling units (§4.4)."""
    return community.member_count if community.level in (Level.NATIONAL, Level.STATE, Level.LGA) else band(community.member_count)


# --------------------------------------------------------------------------- moderators
def moderator_scopes(user):
    """None = every community (system admin or global moderator); otherwise the set of community IDs the person moderates."""
    if not _signed_in(user):
        return set()
    if is_system_admin(user):
        return None
    qs = RoleAssignment.objects.filter(user=user, role=Role.MODERATOR, is_active=True)
    if qs.filter(scope_type=ScopeType.GLOBAL).exists():
        return None
    return set(qs.filter(scope_type=ScopeType.COMMUNITY).values_list("scope_id", flat=True))


def moderates(user, community) -> bool:
    scopes = moderator_scopes(user)
    if scopes is None:
        return True
    return bool(scopes & set(chain_ids(community)))


def scope_paths(user):
    """Paths of the communities a scoped moderator covers (with everything below them); None = everything."""
    scopes = moderator_scopes(user)
    if scopes is None:
        return None
    return list(Community.objects.filter(pk__in=scopes).values_list("path", flat=True))


def under_paths(paths, field="path"):
    q = Q(pk__in=[])
    for p in paths:
        q |= Q(**{field: p}) | Q(**{f"{field}__startswith": p + "/"})
    return q


# --------------------------------------------------------------------------- consent and eligibility
def has_consent(user) -> bool:
    from apps.privacy.consent import has_consent as hc

    return _signed_in(user) and hc(user, PURPOSE)


def membership(user, community):
    if not _signed_in(user):
        return None
    return CommunityMembership.objects.filter(user=user, community=community, status=ACTIVE).first()


def memberships(user):
    return CommunityMembership.objects.filter(user=user, status=ACTIVE).select_related("community__parent")


def can_read(user, community) -> bool:
    """§5.1 and §6: guests never read posts; members-only levels are for members (and the moderators who cover them)."""
    if not _signed_in(user):
        return False
    if community.read_scope == ReadScope.SIGNED_IN:
        return True
    return membership(user, community) is not None or moderates(user, community)


def pause_until(user, community):
    """An active community_pause covering this community (or every community)."""
    from apps.moderation.models import Restriction

    now = timezone.now()
    r = (Restriction.objects.filter(user=user, kind=Restriction.Kind.COMMUNITY_PAUSE, lifted_at__isnull=True, starts_at__lte=now,
                                    ends_at__gt=now).filter(Q(community__isnull=True) | Q(community_id__in=chain_ids(community)))
         .order_by("-ends_at").first())
    return r.ends_at if r else None


def _is_ndc_member(user) -> bool:
    from apps.membership.models import Membership

    return Membership.objects.filter(user=user, status="active").exists()


def linked_candidates(user):
    from apps.posts.services import linked_candidates as lc

    return list(lc(user))


def post_block(user, community):
    """Why this person can't post, comment or react here: (code, message, extra) or None. One place for every view (§12.1)."""
    if not _signed_in(user):
        return "not_authenticated", "Sign in to take part.", {}
    if not user.profile_complete:
        return "profile_incomplete", "Finish your profile first.", {}
    if not has_consent(user):
        from apps.privacy.register import CONSENT_PURPOSES

        p = CONSENT_PURPOSES[PURPOSE]
        return "consent_required", "Agree to how NDCConnect uses this information first: communities.", {
            "purpose": p.key, "version": p.version, "text": p.text, "label": p.label}
    if not community.active:
        return "community_closed", "This community has closed.", {}
    if not community.posting_open:
        return "posting_paused", "Posting is paused in this community.", {}
    if community.level != Level.NATIONAL and membership(user, community) is None:
        if community.level in LOWER_LEVELS:
            from apps.pvc.services import is_verified

            if not is_verified(user):
                return "verification_required", "Verify your PVC to join your LGA, ward and polling unit communities.", {}
        return "not_a_member", "Only members of this community can post here.", {}
    if community.ndc_members_only and not _is_ndc_member(user):
        return "ndc_members_only", "Only NDC members can take part in this community.", {}
    until = pause_until(user, community)
    if until:
        return "posting_paused", f"You can't post in communities until {timezone.localtime(until):%d %b %Y}.", {"until": until.isoformat()}
    ready = user.profile_completed_at + timedelta(hours=NEW_ACCOUNT_HOURS)
    if timezone.now() < ready:
        return "too_soon", f"You can post from {timezone.localtime(ready):%H:%M on %d %b}.", {"until": ready.isoformat()}
    return None


def require_can_post(user, community):
    b = post_block(user, community)
    if b:
        code, msg, extra = b
        status = {"not_authenticated": 401, "consent_required": 403, "profile_incomplete": 403, "verification_required": 403,
                  "not_a_member": 403, "ndc_members_only": 403, "posting_paused": 409, "community_closed": 409, "too_soon": 403}[code]
        raise _err(msg, code, status, **extra)


def _campaign(user, campaign_candidate_id):
    """Rule 7: linked campaign accounts always post with a label, and not in a blackout window for that office."""
    linked = linked_candidates(user)
    if not linked:
        if campaign_candidate_id:
            raise _err("You're not linked to that candidate.", "permission_denied", 403)
        return None
    c = next((x for x in linked if str(x.pk) == str(campaign_candidate_id or "")), None)
    if c is None:
        if len(linked) == 1 and not campaign_candidate_id:
            c = linked[0]
        else:
            e = _err("Choose which campaign you're posting for.", "validation_error")
            e.detail = {"campaign_candidate": ["Choose which campaign you're posting for."]}
            raise e
    from apps.posts.services import paused_until

    until = paused_until(c)
    if until:
        raise _err(f"Campaign posting is paused for the election until {timezone.localtime(until):%d %b %Y, %H:%M}.", "publishing_paused",
                   409, until=until.isoformat())  # §18: paused or blackout → 409 with the end time
    return c


# --------------------------------------------------------------------------- placement (§5.2)
def _settings(user, lock=False):
    qs = CommunitySettings.objects.select_for_update() if lock else CommunitySettings.objects
    obj = qs.filter(user=user).first()
    return obj or CommunitySettings.objects.create(user=user)


def _end(m, reason):
    m.status, m.end_reason, m.left_at = MembershipStatus.ENDED, reason, timezone.now()
    m.notify_new_posts = False
    m.save(update_fields=["status", "end_reason", "left_at", "notify_new_posts"])
    Community.objects.filter(pk=m.community_id, member_count__gt=0).update(member_count=F("member_count") - 1)


def _join(user, community, how):
    m = CommunityMembership.objects.create(user=user, community=community, level=community.level, how=how)
    Community.objects.filter(pk=community.pk).update(member_count=F("member_count") + 1)
    return m


def _targets(loc):
    state, lga_id, ward_id, pu_id = loc
    out = {Level.STATE: Community.objects.filter(level=Level.STATE, state=state, active=True).first()}
    if lga_id:
        out[Level.LGA] = Community.objects.filter(geo_lga_id=lga_id, active=True).first()
    if ward_id:
        out[Level.WARD] = Community.objects.filter(geo_ward_id=ward_id, active=True).first()
    if pu_id:
        out[Level.POLLING_UNIT] = Community.objects.filter(geo_polling_unit_id=pu_id, active=True).first()
    return {k: v for k, v in out.items() if v is not None}


def place(user, *, notify=True) -> list:
    """Create, keep, move or end `verified_registration` memberships from the verified PVC. All-or-nothing. Returns joined communities."""
    from apps.pvc.services import verified_location

    if not _signed_in(user):
        return []
    with transaction.atomic():
        st = _settings(user, lock=True)
        current = {m.level: m for m in CommunityMembership.objects.select_for_update().filter(user=user, status=ACTIVE)
                   .select_related("community")}
        for m in current.values():  # members of a community that closed (§18)
            if not m.community.active:
                _end(m, EndReason.COMMUNITY_CLOSED)
        current = {lv: m for lv, m in current.items() if m.community.active}
        loc = verified_location(user) if has_consent(user) else None
        if loc is None:
            for m in current.values():
                if m.how == How.VERIFIED:
                    _end(m, EndReason.PVC_ENDED)
            return []
        targets = _targets(loc)
        left = {Level.STATE: st.left_state, Level.LGA: st.left_lga, Level.WARD: st.left_ward, Level.POLLING_UNIT: st.left_polling_unit}
        joined, moved_state = [], None
        for level in (Level.POLLING_UNIT, Level.WARD, Level.LGA, Level.STATE):  # end before create (one active per level)
            m, target = current.get(level), targets.get(level)
            if left[level]:
                target = None
            if m and (target is None or m.community_id != target.pk):
                if m.how == How.VERIFIED or target is not None:
                    if level == Level.STATE and m.how == How.CHOSEN and target is not None:
                        moved_state = target
                    _end(m, EndReason.MOVED)
                    current.pop(level)
        for level in (Level.STATE, Level.LGA, Level.WARD, Level.POLLING_UNIT):
            m, target = current.get(level), targets.get(level)
            if left[level] or target is None:
                continue
            if m is None:
                joined.append(_join(user, target, How.VERIFIED).community)
            elif m.how == How.CHOSEN:  # the chosen state is the verified one: mark it verified (§5.3)
                m.how = How.VERIFIED
                m.save(update_fields=["how"])
        if notify and joined:
            from apps.notifications.services import notify as send

            send(user, "community_placed", decision="moved" if moved_state else "placed")
    return joined


def end_verified(user, reason=EndReason.PVC_ENDED) -> int:
    """PVC record erased or no longer successful: verified memberships end at once (§5.2). Called in the caller's transaction."""
    n = 0
    for m in CommunityMembership.objects.select_for_update().filter(user=user, status=ACTIVE, how=How.VERIFIED):
        _end(m, reason)
        n += 1
    return n


def choose_state(user, state):
    """§5.3: one chosen state for people not yet verified; changeable once every 30 days."""
    from apps.candidates.reference import State

    if state not in State.values:
        raise _err("Choose a state.", "validation_error")
    if not _signed_in(user) or not user.profile_complete:
        raise _err("Finish your profile first.", "profile_incomplete", 403)
    from apps.privacy.consent import require_consent

    require_consent(user, PURPOSE)
    target = Community.objects.filter(level=Level.STATE, state=state, active=True).first()
    if target is None:
        raise _err("That community isn't open.", "community_closed", 409)
    with transaction.atomic():
        st = _settings(user, lock=True)
        cur = CommunityMembership.objects.select_for_update().filter(user=user, status=ACTIVE, level=Level.STATE).first()
        if cur and cur.how == How.VERIFIED:
            raise _err(f"You're in the {cur.community.name} community from your verified PVC.", "invalid_transition", 409)
        if cur and cur.community_id == target.pk:
            return cur
        now = timezone.now()
        if cur and st.state_changed_at and now - st.state_changed_at < timedelta(days=STATE_CHANGE_DAYS):
            until = st.state_changed_at + timedelta(days=STATE_CHANGE_DAYS)
            raise _err(f"You can change your state community again on {timezone.localtime(until):%d %b %Y}.", "state_change_too_soon", 409,
                       until=until.isoformat())
        if cur:
            _end(cur, EndReason.LEFT)
        m = _join(user, target, How.CHOSEN)
        st.state_changed_at, st.left_state = now, False
        st.save(update_fields=["state_changed_at", "left_state", "updated_at"])
    return m


def leave(user, community):
    """§5.4. Leaving a level stops placement re-adding it; leaving the state also ends the levels below."""
    with transaction.atomic():
        st = _settings(user, lock=True)
        m = CommunityMembership.objects.select_for_update().filter(user=user, community=community, status=ACTIVE).first()
        if m is None:
            raise _err("You're not in this community.", "not_a_member", 404)
        levels = [m.level] + (list(LOWER_LEVELS) if m.level == Level.STATE else [])
        for other in CommunityMembership.objects.select_for_update().filter(user=user, status=ACTIVE, level__in=levels):
            _end(other, EndReason.LEFT)
        for lv in levels:
            if lv != Level.STATE or m.how == How.VERIFIED:
                setattr(st, f"left_{lv}", True)
        st.save()
    return levels


def rejoin(user, level):
    if level not in (Level.STATE, *LOWER_LEVELS):
        raise _err("Choose a level.", "validation_error")
    from apps.privacy.consent import require_consent

    require_consent(user, PURPOSE)
    with transaction.atomic():
        st = _settings(user, lock=True)
        setattr(st, f"left_{level}", False)
        if level == Level.STATE:
            for lv in LOWER_LEVELS:
                setattr(st, f"left_{lv}", False)
        st.save()
    return place(user)


def set_bell(user, community, on: bool):
    m = membership(user, community)
    if m is None:
        raise _err("You're not in this community.", "not_a_member", 404)
    if on and m.level not in LOWER_LEVELS:
        raise _err("New-post notifications are for LGA, ward and polling-unit communities.", "validation_error")
    m.notify_new_posts = bool(on)
    m.save(update_fields=["notify_new_posts"])
    return m


def withdraw(user, delete_content=False) -> str:
    """Consent withdrawn (14): every membership ends at once; likes deleted; posts stay as 'Former member' unless asked (§5.4)."""
    n = 0
    for m in CommunityMembership.objects.select_for_update().filter(user=user, status=ACTIVE):
        _end(m, EndReason.CONSENT_WITHDRAWN)
        n += 1
    likes = _delete_reactions(user)
    deleted = _delete_content(user) if delete_content else 0
    words = f"left {n} communit{'y' if n == 1 else 'ies'}, {likes} like{'s' if likes != 1 else ''} removed"
    return words + (f", {deleted} posts and comments deleted" if delete_content else "")


def _delete_reactions(user) -> int:
    n = 0
    for r in CommunityReaction.objects.select_for_update().filter(user=user).select_related("post"):
        CommunityPost.objects.filter(pk=r.post_id, reaction_count__gt=0).update(reaction_count=F("reaction_count") - 1)
        r.delete()
        n += 1
    return n


def _delete_content(user) -> int:
    """The person's published or hidden posts and comments become 'deleted' with their text erased now, except removed items and
    items under a legal hold or an open case (kept for the preservation duty until their prune date)."""
    from apps.moderation.models import Case, CaseStatus

    now, n = timezone.now(), 0
    held_posts = set(Case.objects.filter(target_kind="community_post").filter(Q(legal_hold=True) | ~Q(status=CaseStatus.CLOSED))
                     .values_list("target_id", flat=True))
    held_comments = set(Case.objects.filter(target_kind="community_comment").filter(Q(legal_hold=True) | ~Q(status=CaseStatus.CLOSED))
                        .values_list("target_id", flat=True))
    for p in CommunityPost.objects.select_for_update().filter(author=user, status__in=[ItemStatus.PUBLISHED, ItemStatus.HIDDEN]):
        _set_deleted(p, user, erase=p.pk not in held_posts, now=now)
        n += 1
    for c in CommunityComment.objects.select_for_update().filter(author=user, status__in=[ItemStatus.PUBLISHED, ItemStatus.HIDDEN]):
        _set_deleted(c, user, erase=c.pk not in held_comments, now=now)
        n += 1
    return n


# --------------------------------------------------------------------------- posts and comments (§7)
def _check_body(body, lo, hi, max_links):
    body = T.clean(body)
    if len(body) < lo:
        e = _err("Write a little more.", "validation_error")
        e.detail = {"body": [f"Write at least {lo} characters."]}
        raise e
    if len(body) > hi:
        e = _err("That's too long.", "too_long")
        e.detail = {"body": [f"Keep it to {hi:,} characters."]}
        raise e
    if T.has_bad_link(body):
        e = _err("Only web links (http or https) are allowed.", "validation_error")
        e.detail = {"body": ["Only web links (http or https) are allowed."]}
        raise e
    if T.link_count(body) > max_links:
        e = _err(f"Use at most {max_links} link{'s' if max_links != 1 else ''}.", "validation_error")
        e.detail = {"body": [f"Use at most {max_links} link{'s' if max_links != 1 else ''}."]}
        raise e
    return body


def _event(post, kind, actor=None, comment=None, previous_body="", note=""):
    CommunityEvent.objects.create(post=post, comment=comment, kind=kind, actor=actor, previous_body=previous_body, note=note[:300])


def _fanout_post(post):
    if post.community.level in LOWER_LEVELS:
        from apps.core import outbox

        outbox.enqueue("community_post_fanout", {"post": str(post.pk)}, dedupe_key=f"community_post_fanout:{post.pk}")


def create_post(user, community, body, campaign_candidate_id=None):
    require_can_post(user, community)
    body = _check_body(body, POST_MIN, POST_MAX, POST_LINKS)
    candidate = _campaign(user, campaign_candidate_id)
    h = T.body_hash(body)
    since = timezone.now() - timedelta(hours=DUPLICATE_HOURS)
    if CommunityPost.objects.filter(author=user, body_hash=h, created_at__gte=since).exclude(status=ItemStatus.DELETED).exists():
        raise _err("You've already posted this.", "duplicate_post", 409)
    with transaction.atomic():
        p = CommunityPost.objects.create(community=community, author=user, campaign_candidate=candidate, body=body, body_hash=h)
        _event(p, E.CREATED, user)
        Community.objects.filter(pk=community.pk).update(post_count=F("post_count") + 1)
        _fanout_post(p)
    return p


def _open_case(kind, obj) -> bool:
    from apps.moderation.models import Case, CaseStatus

    return Case.objects.filter(target_kind=kind, target_id=obj.pk).exclude(status=CaseStatus.CLOSED).exists()


def _require_author(user, obj):
    if not _signed_in(user) or obj.author_id != user.pk:
        raise _err("Only the author can do this.", "permission_denied", 403)


def _require_editable(obj, kind):
    if obj.status != ItemStatus.PUBLISHED:
        raise _err("This can't be edited now.", "invalid_transition", 409)
    if timezone.now() - obj.created_at > timedelta(minutes=EDIT_MINUTES) or _open_case(kind, obj):
        raise _err(f"You can edit for {EDIT_MINUTES} minutes after posting, unless it has been reported.", "edit_window_closed", 409)


def edit_post(user, post, body):
    _require_author(user, post)
    _require_editable(post, "community_post")
    body = _check_body(body, POST_MIN, POST_MAX, POST_LINKS)
    if body == post.body:
        return post
    with transaction.atomic():
        p = CommunityPost.objects.select_for_update().get(pk=post.pk)
        _event(p, E.EDITED, user, previous_body=p.body)
        p.body, p.body_hash, p.edited_at = body, T.body_hash(body), timezone.now()
        p.save(update_fields=["body", "body_hash", "edited_at", "updated_at"])
    return p


def _set_deleted(obj, actor, *, erase=False, now=None):
    now = now or timezone.now()
    was_published = obj.status == ItemStatus.PUBLISHED
    post = obj if isinstance(obj, CommunityPost) else obj.post
    comment = None if isinstance(obj, CommunityPost) else obj
    obj.status, obj.deleted_at = ItemStatus.DELETED, now
    fields = ["status", "deleted_at", "updated_at"]
    if erase:
        obj.body, obj.body_erased_at = "", now
        fields += ["body", "body_erased_at"]
    obj.save(update_fields=fields)
    _event(post, E.DELETED, actor, comment=comment)
    if erase:
        _event(post, E.BODY_ERASED, None, comment=comment)
    if was_published:
        if comment is None:
            Community.objects.filter(pk=post.community_id, post_count__gt=0).update(post_count=F("post_count") - 1)
        else:
            CommunityPost.objects.filter(pk=post.pk, comment_count__gt=0).update(comment_count=F("comment_count") - 1)
    from apps.moderation.services import author_removed

    author_removed("community_post" if comment is None else "community_comment", obj)


def delete_post(user, post):
    _require_author(user, post)
    if post.status not in (ItemStatus.PUBLISHED, ItemStatus.HIDDEN):
        raise _err("This post can't be deleted now.", "invalid_transition", 409)
    with transaction.atomic():
        p = CommunityPost.objects.select_for_update().get(pk=post.pk)
        _set_deleted(p, user)
    return p


def create_comment(user, post, body, campaign_candidate_id=None):
    require_can_post(user, post.community)
    if post.status != ItemStatus.PUBLISHED:
        raise _err("Comments are closed on this post.", "invalid_transition", 409)
    body = _check_body(body, COMMENT_MIN, COMMENT_MAX, COMMENT_LINKS)
    candidate = _campaign(user, campaign_candidate_id)
    with transaction.atomic():
        c = CommunityComment.objects.create(post=post, author=user, campaign_candidate=candidate, body=body)
        _event(post, E.CREATED, user, comment=c)
        CommunityPost.objects.filter(pk=post.pk).update(comment_count=F("comment_count") + 1)
        if post.author_id and post.author_id != user.pk:
            _notify_reply(post)
    return c


def _notify_reply(post):
    """§10: one community_reply per post per hour, folded ("3 new comments on your post")."""
    from apps.notifications.models import Notification, NotificationPreference
    from apps.notifications.services import notify

    author = post.author
    pref = NotificationPreference.objects.filter(user=author).first()
    if pref is not None and not pref.community_replies:
        return
    recent = (Notification.objects.select_for_update().filter(recipient=author, kind="community_reply", community_post=post,
                                                               read_at__isnull=True, created_at__gte=timezone.now() - timedelta(hours=1)).first())
    if recent:
        Notification.objects.filter(pk=recent.pk).update(count=F("count") + 1)
    else:
        notify(author, "community_reply", target=post, community=post.community)


def edit_comment(user, comment, body):
    _require_author(user, comment)
    _require_editable(comment, "community_comment")
    body = _check_body(body, COMMENT_MIN, COMMENT_MAX, COMMENT_LINKS)
    if body == comment.body:
        return comment
    with transaction.atomic():
        c = CommunityComment.objects.select_for_update().get(pk=comment.pk)
        _event(c.post, E.EDITED, user, comment=c, previous_body=c.body)
        c.body, c.edited_at = body, timezone.now()
        c.save(update_fields=["body", "edited_at", "updated_at"])
    return c


def delete_comment(user, comment):
    _require_author(user, comment)
    if comment.status not in (ItemStatus.PUBLISHED, ItemStatus.HIDDEN):
        raise _err("This comment can't be deleted now.", "invalid_transition", 409)
    with transaction.atomic():
        c = CommunityComment.objects.select_for_update().select_related("post").get(pk=comment.pk)
        _set_deleted(c, user)
    return c


def react(user, post, on: bool):
    require_can_post(user, post.community)
    if post.status != ItemStatus.PUBLISHED:
        raise _err("This post can't be liked now.", "invalid_transition", 409)
    with transaction.atomic():
        p = CommunityPost.objects.select_for_update().get(pk=post.pk)
        mine = CommunityReaction.objects.filter(post=p, user=user).first()
        if on and mine is None:
            CommunityReaction.objects.create(post=p, user=user)
            p.reaction_count += 1
        elif not on and mine is not None:
            mine.delete()
            p.reaction_count = max(0, p.reaction_count - 1)
        p.save(update_fields=["reaction_count"])
    return {"liked": bool(on), "reaction_count": p.reaction_count}


def liked_ids(user, posts) -> set:
    if not _signed_in(user):
        return set()
    return set(CommunityReaction.objects.filter(user=user, post__in=posts).values_list("post_id", flat=True))


# --------------------------------------------------------------------------- moderation (called by moderation.services only)
def _post_of(obj):
    return obj if isinstance(obj, CommunityPost) else obj.post


def _adjust_counts(obj, delta):
    if isinstance(obj, CommunityPost):
        q = Community.objects.filter(pk=obj.community_id)
        q = q.filter(post_count__gt=0) if delta < 0 else q
        q.update(post_count=F("post_count") + delta)
    else:
        q = CommunityPost.objects.filter(pk=obj.post_id)
        q = q.filter(comment_count__gt=0) if delta < 0 else q
        q.update(comment_count=F("comment_count") + delta)


def hide(obj, moderator, note=""):
    if obj.status != ItemStatus.PUBLISHED:
        raise _err("Only visible items can be hidden.", "invalid_transition", 409)
    obj.status, obj.hidden_at, obj.hidden_by = ItemStatus.HIDDEN, timezone.now(), moderator
    obj.save(update_fields=["status", "hidden_at", "hidden_by", "updated_at"])
    _adjust_counts(obj, -1)
    _event(_post_of(obj), E.HIDDEN, moderator, comment=None if isinstance(obj, CommunityPost) else obj, note=note)


def unhide(obj, moderator, note=""):
    if obj.status != ItemStatus.HIDDEN:
        raise _err("This item isn't hidden.", "invalid_transition", 409)
    obj.status = ItemStatus.PUBLISHED
    obj.save(update_fields=["status", "updated_at"])
    _adjust_counts(obj, 1)
    _event(_post_of(obj), E.UNHIDDEN, moderator, comment=None if isinstance(obj, CommunityPost) else obj, note=note)


def remove(obj, moderator, *, rule, note_to_author="", internal_note="", source=None):
    """A moderator removes a post or comment (13 §6.2): it's kept, hidden, with the rule; the case closes as Removed."""
    from apps.moderation.services import content_removed

    if obj.status not in (ItemStatus.PUBLISHED, ItemStatus.HIDDEN):
        raise _err("Only visible or hidden items can be removed.", "invalid_transition", 409)
    was_published = obj.status == ItemStatus.PUBLISHED
    obj.status, obj.removed_at, obj.removed_by, obj.removed_rule = ItemStatus.REMOVED, timezone.now(), moderator, rule
    obj.save(update_fields=["status", "removed_at", "removed_by", "removed_rule", "updated_at"])
    if was_published:
        _adjust_counts(obj, -1)
    kind = "community_post" if isinstance(obj, CommunityPost) else "community_comment"
    _event(_post_of(obj), E.REMOVED, moderator, comment=None if kind == "community_post" else obj, note=rule)
    return content_removed(kind, obj, moderator, rule_id=rule, note_to_author=note_to_author, internal_note=internal_note, source=source)


def restore(obj, moderator, note=""):
    from apps.moderation.services import content_restored

    if obj.status != ItemStatus.REMOVED:
        raise _err("Only removed items can be restored.", "invalid_transition", 409)
    obj.status, obj.removed_at, obj.removed_rule = ItemStatus.PUBLISHED, None, ""
    obj.save(update_fields=["status", "removed_at", "removed_rule", "updated_at"])
    _adjust_counts(obj, 1)
    kind = "community_post" if isinstance(obj, CommunityPost) else "community_comment"
    _event(_post_of(obj), E.RESTORED, moderator, comment=None if kind == "community_post" else obj, note=note)
    return content_restored(kind, obj, moderator, note)


def pause(user, by, days, rule, reason="", community=None, case=None):
    """§8.4: a community posting pause, for one community (and below) or, with community=None, every community."""
    from apps.moderation.models import Restriction

    if days not in PAUSE_DAYS:
        raise _err("Choose 1, 7 or 30 days.", "validation_error")
    now = timezone.now()
    return Restriction.objects.create(user=user, kind=Restriction.Kind.COMMUNITY_PAUSE, rule=rule, reason=(reason or "")[:500], case=case,
                                      community=community, starts_at=now, ends_at=now + timedelta(days=days), created_by=by)


# --------------------------------------------------------------------------- feeds (§6)
def pinned_announcements(community, now=None):
    """Official announcements aimed at this community or one of its parents, published in the last 30 days (§9)."""
    from apps.news.models import Article

    now = now or timezone.now()
    return (Article.objects.filter(source="official", status="successful", audience_community_id__in=chain_ids(community),
                                   published_at__gte=now - timedelta(days=30)).order_by("-published_at")[:5])


def feed(community):
    return (CommunityPost.objects.filter(community=community, status__in=[ItemStatus.PUBLISHED, ItemStatus.HIDDEN, ItemStatus.REMOVED])
            .select_related("author", "campaign_candidate").order_by("-created_at", "-id"))


def visible_comments(post):
    return (post.comments.filter(status__in=[ItemStatus.PUBLISHED, ItemStatus.HIDDEN, ItemStatus.REMOVED])
            .select_related("author", "campaign_candidate").order_by("created_at", "id"))


# --------------------------------------------------------------------------- erasure (§17.4)
def erase_for_deleted_user(user) -> dict:
    """Inside accounts.mark_deleted's transaction: memberships, settings and likes go; published items stay as 'Former member';
    deleted and hidden items are erased; removed items stay detached until their prune date."""
    likes = _delete_reactions(user)
    for m in CommunityMembership.objects.select_for_update().filter(user=user, status=ACTIVE):
        _end(m, EndReason.CONSENT_WITHDRAWN)
    memberships_n, _ = CommunityMembership.objects.filter(user=user).delete()
    CommunitySettings.objects.filter(user=user).delete()
    now = timezone.now()
    erased = 0
    for model in (CommunityPost, CommunityComment):
        for obj in model.objects.select_for_update().filter(author=user):
            if obj.status == ItemStatus.HIDDEN:
                _set_deleted(obj, None, erase=True, now=now)
                erased += 1
            elif obj.status == ItemStatus.DELETED and not obj.body_erased_at:
                obj.body, obj.body_erased_at = "", now
                obj.save(update_fields=["body", "body_erased_at", "updated_at"])
                _event(_post_of(obj), E.BODY_ERASED, None, comment=None if model is CommunityPost else obj)
                erased += 1
            _event(_post_of(obj), E.AUTHOR_DETACHED, None, comment=None if model is CommunityPost else obj)
        model.objects.filter(author=user).update(author=None)
    return {"memberships": memberships_n, "likes": likes, "erased": erased}
