from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from apps.candidates.reference import State
from apps.core.i18n import CONTENT_CHOICES

from . import images
from .models import Article, Category


def excerpt(body: str, limit: int = 200) -> str:
    text = " ".join((body or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def byline(a: Article):
    """The public byline: the author's name only if they chose it (spec 07 §3.6)."""
    if a.source != "community" or not a.show_author_name:
        return None
    from apps.accounts.naming import format_name, is_gone, name_style

    u = a.author
    if is_gone(u) or not u.full_name:  # a paused account keeps its byline (spec 20 §12)
        return None
    return format_name(u.full_name, name_style(u))  # spec 20 §5.2: the person's chosen name style


def image_payload(a: Article, request, full=False):
    if not a.image:
        return None
    from apps.media.services import srcset

    out = {"url_thumb": images.url(a.image_thumb, request), "width": a.image_width, "height": a.image_height,
           "srcset": srcset(a.image, "content", request, small=a.image_thumb, width=a.image_width)}
    if full:
        out["url"] = images.url(a.image, request)
    return out


class PublicArticleSerializer(serializers.ModelSerializer):
    category_label = serializers.SerializerMethodField()
    state_label = serializers.SerializerMethodField()
    excerpt = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    byline = serializers.SerializerMethodField()
    byline_verified = serializers.SerializerMethodField()
    byline_profile = serializers.SerializerMethodField()
    pinned = serializers.SerializerMethodField()
    audience = serializers.SerializerMethodField()

    class Meta:
        model = Article
        fields = ["id", "source", "category", "category_label", "title", "excerpt", "state", "state_label", "image", "published_at",
                  "edited_at", "byline", "byline_verified", "byline_profile", "pinned", "audience", "language"]

    @extend_schema_field(inline_serializer("ArticleAudience", {"id": serializers.UUIDField(), "name": serializers.CharField(),
                                                                "path": serializers.CharField()}, allow_null=True))
    def get_audience(self, a):
        """Spec 17 §9: "For: Ikeja LGA community"."""
        c = a.audience_community if a.audience_community_id else None
        return {"id": c.id, "name": c.name, "path": c.path} if c else None

    def get_category_label(self, a):
        return a.get_category_display() if a.category else ""

    def get_state_label(self, a):
        return a.get_state_display() if a.state else ""

    def get_excerpt(self, a):
        return excerpt(a.body)

    def get_image(self, a):
        return image_payload(a, self.context.get("request"))

    def get_byline(self, a):
        return byline(a)

    def get_byline_verified(self, a):
        """Only beside a name the author chose to show, and only if they opted in (spec 08 §3.7)."""
        if byline(a) is None:
            return False
        from apps.pvc.services import public_badge

        return public_badge(a.author)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_byline_profile(self, a):
        """Spec 20 §5.1: the author's public profile handle, only beside a name they chose to show."""
        if byline(a) is None:
            return None
        from apps.accounts.naming import public_handle

        return public_handle(a.author)

    def get_pinned(self, a):
        return bool(a.pinned_at)


class PublicArticleDetailSerializer(PublicArticleSerializer):
    class Meta(PublicArticleSerializer.Meta):
        fields = PublicArticleSerializer.Meta.fields + ["body", "source_name", "source_url"]

    def get_image(self, a):
        return image_payload(a, self.context.get("request"), full=True)


class MyArticleSerializer(PublicArticleDetailSerializer):
    status_label = serializers.CharField(source="get_status_display")

    class Meta(PublicArticleDetailSerializer.Meta):
        fields = PublicArticleDetailSerializer.Meta.fields + ["status", "status_label", "show_author_name", "decision_reason",
                                                             "removed_reason", "removed_rule", "submitted_at", "created_at"]


class AdminArticleSerializer(MyArticleSerializer):
    author_name = serializers.SerializerMethodField()
    author_email = serializers.SerializerMethodField()
    is_own = serializers.SerializerMethodField()
    open_reports = serializers.IntegerField(read_only=True, default=0)
    important = serializers.BooleanField(read_only=True)
    removed_at = serializers.DateTimeField(read_only=True)
    decided_at = serializers.DateTimeField(read_only=True)

    class Meta(MyArticleSerializer.Meta):
        fields = MyArticleSerializer.Meta.fields + ["author_name", "author_email", "is_own", "open_reports", "important", "removed_at",
                                                   "decided_at"]

    def get_author_name(self, a):
        return a.author.full_name if a.author else ""

    def get_author_email(self, a):
        return a.author.email if a.author else ""

    def get_is_own(self, a):
        request = self.context.get("request")
        return bool(request and a.author_id and a.author_id == request.user.pk)


class AdminArticleDetailSerializer(AdminArticleSerializer):
    events = serializers.SerializerMethodField()
    case = serializers.SerializerMethodField()

    class Meta(AdminArticleSerializer.Meta):
        fields = AdminArticleSerializer.Meta.fields + ["events", "case", "declarations"]

    def get_events(self, a):
        return [{"event": e.event, "note": e.note, "created_at": e.created_at, "actor_email": e.actor.email if e.actor else None}
                for e in a.events.select_related("actor")]

    def get_case(self, a):  # spec 13 §8.2: reports live in the moderation case
        from apps.moderation.services import case_summary

        return case_summary("article", a.pk)


class SubmitSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=150, allow_blank=True)
    body = serializers.CharField(allow_blank=True, trim_whitespace=False, max_length=20_000)
    state = serializers.ChoiceField(choices=State.choices, required=False, allow_blank=True)
    source_url = serializers.CharField(max_length=500, required=False, allow_blank=True)
    show_author_name = serializers.BooleanField(required=False, default=False)
    declarations = serializers.DictField(child=serializers.BooleanField(), required=False)
    language = serializers.ChoiceField(choices=CONTENT_CHOICES, required=False, help_text="Spec 21 §9: the language it is written in")

    def validate_source_url(self, v):
        if v and not v.lower().startswith("https://"):
            raise serializers.ValidationError("Use a link that starts with https://")
        if v:
            serializers.URLField().run_validation(v)
        return v


class EditSerializer(SubmitSerializer):
    title = serializers.CharField(max_length=150, required=False, allow_blank=True)
    body = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, max_length=20_000)
    show_author_name = serializers.BooleanField(required=False)


class AnnouncementSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=Category.choices)
    title = serializers.CharField(max_length=150)
    body = serializers.CharField(trim_whitespace=False, max_length=25_000)
    state = serializers.ChoiceField(choices=State.choices, required=False, allow_blank=True)
    source_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    source_url = serializers.CharField(max_length=500, required=False, allow_blank=True)
    notify = serializers.BooleanField(required=False, default=False)
    language = serializers.ChoiceField(choices=CONTENT_CHOICES, required=False, help_text="Spec 21 §10: the original's language")
    audience_community = serializers.UUIDField(required=False, allow_null=True,
                                               help_text="Spec 17 §9: show pinned in this community and below; required for Community Announcements")

    validate_source_url = SubmitSerializer.validate_source_url


class ReportSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=16)
    details = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
