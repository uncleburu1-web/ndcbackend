from django.core.management.base import CommandError

from apps.accounts.models import User

ACTOR = "system:cli"  # spec §6.5: CLI actions are audited with this actor label


def add_user_args(parser):
    parser.add_argument("email", nargs="?", help="Email of the NDCConnect user")
    parser.add_argument("--clerk-id", help="Clerk user ID (user_…) instead of email")


def find_user(options) -> User:
    if options.get("clerk_id"):
        user = User.objects.filter(clerk_user_id=options["clerk_id"]).first()
        if not user:
            raise CommandError(f"No user with Clerk ID {options['clerk_id']}.")
        return user
    email = (options.get("email") or "").strip().lower()
    if not email:
        raise CommandError("Give an email or --clerk-id.")
    matches = list(User.objects.filter(email__iexact=email)[:2])
    if not matches:
        raise CommandError(f"No user with email {email}. They must sign up first.")
    if len(matches) > 1:
        raise CommandError(f"More than one user has email {email}; use --clerk-id.")
    return matches[0]
