"""The Railway worker's schedule (run_schedule): every job is known, tracked under its own name, and run only when due."""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command, get_commands, load_command_class
from django.test import TestCase
from django.utils import timezone

from apps.core.jobs import KNOWN_JOBS, TrackedCommand
from apps.core.management.commands import run_schedule as RS
from apps.core.models import JobRun


class ScheduleTests(TestCase):
    def test_every_scheduled_command_exists_is_tracked_under_its_name_and_is_known(self):
        commands = get_commands()
        for cmd, _args, every in RS.SCHEDULE:
            self.assertIn(cmd, commands, cmd)
            c = load_command_class(commands[cmd], cmd)
            self.assertIsInstance(c, TrackedCommand, cmd)
            self.assertEqual(c.job, cmd)
            self.assertIn(cmd, KNOWN_JOBS)
            overdue = KNOWN_JOBS[cmd][1]
            if overdue:  # the dashboard must not call a job overdue before the worker is due to run it
                self.assertGreaterEqual(overdue, every, cmd)

    def test_every_job_with_an_overdue_limit_is_scheduled_or_the_outbox(self):
        scheduled = {c for c, _, _ in RS.SCHEDULE} | {"process_outbox", "sweep_donations"}
        missing = [j for j, (_, overdue) in KNOWN_JOBS.items() if overdue and j not in scheduled]
        self.assertEqual(missing, [])

    def test_due_uses_the_last_finished_run(self):
        now = timezone.now()
        self.assertIn(("events_tick", []), RS.due(now))  # never ran
        JobRun.objects.create(job="events_tick", started_at=now - timedelta(minutes=5), finished_at=now - timedelta(minutes=5), ok=True)
        JobRun.objects.create(job="prune_events", started_at=now - timedelta(days=2), finished_at=now - timedelta(days=2), ok=False)
        JobRun.objects.create(job="verify_audit", started_at=now - timedelta(minutes=1))  # running now
        names = [c for c, _ in RS.due(now)]
        self.assertNotIn("events_tick", names)
        self.assertIn("prune_events", names)  # a failed run still counts; it's due again a day later
        self.assertNotIn("verify_audit", names)
        self.assertIn(("sync_communities", ["--placements"]), RS.due(now))
        self.assertIn("events_tick", [c for c, _ in RS.due(now + timedelta(minutes=11))])

    def test_once_drains_the_outbox_runs_due_jobs_and_survives_a_failure(self):
        ran = []

        def fake(cmd, *args, **kw):
            ran.append(cmd)
            if cmd == "prune_news":
                raise RuntimeError("boom")

        with mock.patch.object(RS, "call_command", side_effect=fake), mock.patch.object(RS.signal, "signal"):
            call_command("run_schedule", "--once", stdout=StringIO())
        self.assertEqual(ran[0], "process_outbox")
        self.assertEqual(set(ran[1:]), {c for c, _, _ in RS.SCHEDULE})
