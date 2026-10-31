"""
Local user record linked to a Clerk user — spec §5.1.
NDCConnect never stores passwords: every user gets an unusable password.
"""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models


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
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clerk_user_id = models.CharField(max_length=64, unique=True, db_index=True)
    # Mirror of Clerk's primary email; read-only in NDCConnect (changed through Clerk).
    email = models.EmailField(max_length=254, blank=True, db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=14, blank=True, help_text="E.164, e.g. +2348031234567 (unverified)")
    profile_completed_at = models.DateTimeField(null=True, blank=True)
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

    def __str__(self):
        return self.email or self.clerk_user_id

    @property
    def profile_complete(self) -> bool:
        return self.profile_completed_at is not None


class ProcessedWebhook(models.Model):
    """Svix message IDs already handled — makes the webhook idempotent (spec §6.2)."""

    svix_id = models.CharField(max_length=128, primary_key=True)
    event_type = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)
