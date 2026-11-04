from rest_framework import serializers

from apps.candidates.photos import public_url
from apps.news import images
from apps.media.services import srcset
from apps.news.serializers import excerpt

from .models import CandidatePost


def candidate_summary(c, request):
    return {"id": c.id, "slug": c.slug, "full_name": c.full_name, "position": c.position, "position_label": c.get_position_display(),
            "state": c.state, "photo": {"url_200": public_url(request, c.photo_200), "url_800": public_url(request, c.photo_800),
                                        "srcset": srcset(c.photo_800, "square", request, small=c.photo_200)}
            if c.photo_200 else None, "verified": c.verified_at is not None}


def image_payload(p, request, full=False):
    if not p.image:
        return None
    out = {"url_thumb": images.url(p.image_thumb, request), "width": p.image_width, "height": p.image_height,
           "srcset": srcset(p.image, "content", request, small=p.image_thumb, width=p.image_width)}
    if full:
        out["url"] = images.url(p.image, request)
    return out


class PublicPostSerializer(serializers.ModelSerializer):
    """Public fields only: never status, authors, editors, removal or report data (spec 09 §7.1)."""

    kind_label = serializers.CharField(source="get_kind_display")
    excerpt = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()

    class Meta:
        model = CandidatePost
        fields = ["id", "kind", "kind_label", "title", "excerpt", "image", "published_at", "edited_at"]

    def get_excerpt(self, p):
        return excerpt(p.body)

    def get_image(self, p):
        return image_payload(p, self.context.get("request"))


class PublicPostDetailSerializer(PublicPostSerializer):
    body = serializers.CharField()
    candidate = serializers.SerializerMethodField()

    class Meta(PublicPostSerializer.Meta):
        fields = PublicPostSerializer.Meta.fields + ["body", "candidate"]

    def get_image(self, p):
        return image_payload(p, self.context.get("request"), full=True)

    def get_candidate(self, p):
        return candidate_summary(p.candidate, self.context.get("request"))


class FeedPostSerializer(PublicPostSerializer):
    """A candidate post as a /news feed item (spec 09 §6.4)."""

    source = serializers.SerializerMethodField()
    candidate = serializers.SerializerMethodField()
    state = serializers.CharField(source="candidate.state")
    state_label = serializers.SerializerMethodField()

    class Meta(PublicPostSerializer.Meta):
        fields = PublicPostSerializer.Meta.fields + ["source", "candidate", "state", "state_label"]

    def get_source(self, p):
        return "candidate"

    def get_candidate(self, p):
        return candidate_summary(p.candidate, self.context.get("request"))

    def get_state_label(self, p):
        return p.candidate.get_state_display() if p.candidate.state else ""


class CampaignPostSerializer(PublicPostDetailSerializer):
    """The campaign's own view: status and removal reason so it knows why one disappeared."""

    status_label = serializers.CharField(source="get_status_display")

    class Meta(PublicPostDetailSerializer.Meta):
        fields = PublicPostDetailSerializer.Meta.fields + ["status", "status_label", "removed_reason", "removed_rule", "removed_at", "created_at"]


class AdminPostSerializer(CampaignPostSerializer):
    author_email = serializers.SerializerMethodField()
    editor_email = serializers.SerializerMethodField()
    open_reports = serializers.IntegerField(read_only=True, default=0)

    class Meta(CampaignPostSerializer.Meta):
        fields = CampaignPostSerializer.Meta.fields + ["author_email", "editor_email", "open_reports"]

    def get_author_email(self, p):
        return p.created_by.email if p.created_by else ""

    def get_editor_email(self, p):
        return p.updated_by.email if p.updated_by else ""


class AdminPostDetailSerializer(AdminPostSerializer):
    events = serializers.SerializerMethodField()
    case = serializers.SerializerMethodField()

    class Meta(AdminPostSerializer.Meta):
        fields = AdminPostSerializer.Meta.fields + ["events", "case"]

    def get_events(self, p):
        return [{"event": e.event, "note": e.note, "created_at": e.created_at, "actor_email": e.actor.email if e.actor else None}
                for e in p.events.select_related("actor")]

    def get_case(self, p):  # spec 13 §8.2
        from apps.moderation.services import case_summary

        return case_summary("post", p.pk)


class PostWriteSerializer(serializers.Serializer):
    kind = serializers.CharField(max_length=12, required=False)
    title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    body = serializers.CharField(max_length=12_000, required=False, allow_blank=True, trim_whitespace=False)
