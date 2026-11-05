"""Date wording shared by server-written text. Portable: the glibc no-padding flag (percent, dash, d) fails on Windows."""
from django.utils import timezone


def day_month(dt) -> str:
    """'5 Nov' in Lagos time."""
    d = timezone.localtime(dt)
    return f"{d.day} {d:%b}"
