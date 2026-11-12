"""
Local user record linked to a Clerk user — spec §5.1.
NDCConnect never stores passwords: every user gets an unusable password.
"""
import uuid

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models import Q

from apps.core.fields import StateField, state_check


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, clerk_user_id: str, **fields):
        if not clerk_user_id:
            raise ValueError("clerk_user_id is required.")
        user = self.model(clerk_user_id=clerk_user_id, **fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, *args, **kwargs):
        raise NotImplementedError(
            "NDCConnect has no password superusers. Grant system_admin with: python manage.py grant_role <email> system_admin"
        )


class DeactivationKind(models.TextChoices):
    """Spec 20 §12: who made an account inactive. Empty exactly when the account is active."""

    NONE = "", "Active"
    ADMIN = "admin", "Deactivated by an admin"
    SELF = "self", "Paused by the person"
    DELETED = "deleted", "Deleted"


class User(AbstractBaseUser):
    """A person's NDCConnect account, linked to Clerk by `clerk_user_id`. Never hard-deleted: anonymised when the person deletes their account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clerk_user_id = models.CharField(max_length=64, unique=True, db_index=True)
    # Mirror of Clerk's primary email; read-only in NDCConnect (changed through Clerk).
    email = models.EmailField(max_length=254, blank=True, db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=14, blank=True, help_text="E.164, e.g. +2348031234567 (unverified)")
    profile_completed_at = models.DateTimeField(null=True, blank=True)
    # Spec 14 §4.3: NDCConnect is for voters; a complete profile has confirmed "I'm 18 or older".
    adult_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Spec 14 §4.6: last activity, for the admin access review only (updated at most hourly; cleared on deletion).
    last_seen_at = models.DateTimeField(null=True, blank=True)
    mfa_enabled = models.BooleanField(null=True, blank=True)  # from the last session token's `mfa` claim (access review only)
    # Spec 12: optional, user-set, private. Only pre-fills state filters; never ranks or targets anything.
    home_state = StateField(default="")
    welcome_shown_at = models.DateTimeField(null=True, blank=True)  # set by the welcome-animation spec
    # Spec 20 §4.1: the profile photo the person uploaded in Clerk (empty when they haven't). Shown publicly only on an
    # opted-in public profile with "show photo" on.
    image_url = models.URLField(max_length=500, blank=True)
    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_kind = models.CharField(max_length=8, choices=DeactivationKind.choices, default="", blank=True)  # spec 20 §12
    # Set when Clerk reports the user deleted. Guards against an out-of-order
    # user.created/user.updated webhook bringing an anonymised record back.
    clerk_deleted_at = models.DateTimeField(null=True, blank=True)
    clerk_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "clerk_user_id"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            state_check("home_state", "accounts_home_state_valid"),  # spec 16 F2
            models.CheckConstraint(  # spec 20 §16.1
                condition=(Q(is_active=True) & Q(deactivation_kind="")) | (Q(is_active=False) & ~Q(deactivation_kind="")),
                name="accounts_deactivation_kind_matches"),
        ]

    def __str__(self):
        return self.email or self.clerk_user_id

    def save(self, *args, **kwargs):
        # Keep the deactivation kind consistent with is_active for code that only flips is_active (admin tools, tests).
        if self.is_active:
            self.deactivation_kind = ""
        elif not self.deactivation_kind:
            self.deactivation_kind = DeactivationKind.DELETED if self.clerk_deleted_at else DeactivationKind.ADMIN
        uf = kwargs.get("update_fields")
        if uf is not None and "is_active" in uf and "deactivation_kind" not in uf:
            kwargs["update_fields"] = [*uf, "deactivation_kind"]
        super().save(*args, **kwargs)

    @property
    def is_paused(self) -> bool:
        return not self.is_active and self.deactivation_kind == DeactivationKind.SELF

    @property
    def profile_complete(self) -> bool:
        return self.profile_completed_at is not None and self.adult_confirmed_at is not None


# ---------------------------------------------------------------------------------------------- spec 20: settings
class Language(models.TextChoices):
    EN = "en", "English"
    HA = "ha", "Hausa"
    IG = "ig", "Igbo"
    YO = "yo", "Yoruba"
    PCM = "pcm", "Nigerian Pidgin"


# How each language names itself (shown in the switcher beside the English name).
NATIVE_NAMES = {"en": "English", "ha": "Hausa", "ig": "Igbo", "yo": "Yorùbá", "pcm": "Naijá"}


class Theme(models.TextChoices):
    SYSTEM = "system", "System"
    LIGHT = "light", "Light"
    DARK = "dark", "Dark"


class TextSize(models.TextChoices):
    STANDARD = "standard", "Standard"
    LARGE = "large", "Large"
    LARGER = "larger", "Larger"


class Contrast(models.TextChoices):
    SYSTEM = "system", "System"
    STANDARD = "standard", "Standard"
    MORE = "more", "More"


class Motion(models.TextChoices):
    SYSTEM = "system", "System"
    REDUCE = "reduce", "Reduce motion"


class NameStyle(models.TextChoices):
    FIRST_INITIAL = "first_initial", "First name and last initial"
    FULL = "full", "Full name"


class DonationName(models.TextChoices):
    ANONYMOUS = "anonymous", "Anonymous"
    NAME = "name", "My public name"


class ProfileLocation(models.TextChoices):
    NONE = "none", "Nothing"
    STATE = "state", "My state"
    LGA = "lga", "My state and LGA"


class UserPreferences(models.Model):
    """A person's settings (spec 20 §16.1). One row, created on first save; defaults apply when it's missing.
    Only the person reads or changes it (rule 6)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preferences", primary_key=True)
    language = models.CharField(max_length=3, choices=Language.choices, default=Language.EN)
    theme = models.CharField(max_length=6, choices=Theme.choices, default=Theme.SYSTEM)
    text_size = models.CharField(max_length=8, choices=TextSize.choices, default=TextSize.STANDARD)
    contrast = models.CharField(max_length=8, choices=Contrast.choices, default=Contrast.SYSTEM)
    motion = models.CharField(max_length=6, choices=Motion.choices, default=Motion.SYSTEM)
    name_style = models.CharField(max_length=13, choices=NameStyle.choices, default=NameStyle.FIRST_INITIAL)
    donation_name_default = models.CharField(max_length=9, choices=DonationName.choices, default=DonationName.ANONYMOUS)
    public_profile = models.BooleanField(default=False)
    profile_handle = models.CharField(max_length=6, unique=True, null=True, blank=True)
    bio = models.CharField(max_length=300, blank=True)
    profile_location = models.CharField(max_length=5, choices=ProfileLocation.choices, default=ProfileLocation.NONE)
    profile_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    profile_photo = models.BooleanField(default=False)
    # §5.1: a moderator can remove the bio or photo (13), never edit them. Hidden from the public page until restored;
    # a new bio (or a new photo in Clerk) is new content and clears the mark.
    bio_removed_at = models.DateTimeField(null=True, blank=True)
    photo_removed_at = models.DateTimeField(null=True, blank=True)
    saved_at = models.DateTimeField(null=True, blank=True)  # first explicit save (§13: the one-time device copy)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(public_profile=False) | Q(profile_handle__isnull=False), name="accounts_prefs_profile_has_handle"),
            models.CheckConstraint(condition=Q(profile_lga__isnull=True) | Q(profile_location="lga"), name="accounts_prefs_lga_only_for_lga"),
        ]

    def __str__(self):
        return f"Settings for {self.user_id}"

    @property
    def status(self) -> str:
        """For moderation (13's case payload): whether the profile page is live."""
        return "published" if self.public_profile else "off"


class RetiredHandle(models.Model):
    """Public-profile handles that belonged to a deleted account: never handed out again (spec 20 §16.2)."""

    handle = models.CharField(max_length=6, primary_key=True)
    retired_at = models.DateTimeField(auto_now_add=True)


class SettingsDefaults(models.Model):
    """One row (spec 20 §14): what new accounts start with, and which languages can be chosen. System admins only; audited."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled_languages = models.JSONField(default=list)  # English is always enabled, whatever is stored
    notifications = models.JSONField(default=dict)  # switchable category → bool, for new accounts
    default_theme = models.CharField(max_length=6, choices=Theme.choices, default=Theme.SYSTEM)
    default_text_size = models.CharField(max_length=8, choices=TextSize.choices, default=TextSize.STANDARD)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="accounts_settings_defaults_single_row")]
