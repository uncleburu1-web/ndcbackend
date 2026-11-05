#!/usr/bin/env python
import os
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # `python manage.py test` always uses the test settings and ignores .env (except DATABASE_URL), so a developer's
        # local keys, gates and consent switches can't change what the tests see. `--settings` still wins.
        os.environ["NDC_TESTS"] = "1"
        if not any(a.startswith("--settings") for a in sys.argv[2:]):
            os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
