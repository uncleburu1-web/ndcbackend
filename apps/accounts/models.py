"""
Local user record linked to a Clerk user — spec §5.1.
NDCConnect never stores passwords: every user gets an unusable password.
"""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models

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
    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
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
        constraints = [state_check("home_state", "accounts_home_state_valid")]  # spec 16 F2

    def __str__(self):
        return self.email or self.clerk_user_id

    @property
    def profile_complete(self) -> bool:
        return self.profile_completed_at is not None and self.adult_confirmed_at is not None
