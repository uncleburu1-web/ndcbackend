"""seed_demo_donations: exact totals within limits, and it never runs outside local development."""
import io
import random

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.donations.management.commands.seed_demo_donations import (
    MAX_NAIRA,
    MIN_NAIRA,
    amounts,
)
from apps.donations.models import Donation


class DemoSeedTests(TestCase):
    def test_amounts_sum_exactly_within_limits(self):
        out = amounts(1500, 557_000_000, random.Random(1))
        self.assertEqual((len(out), sum(out)), (1500, 557_000_000))
        self.assertTrue(all(MIN_NAIRA <= a <= MAX_NAIRA for a in out))

    def test_impossible_total_refused(self):
        with self.assertRaises(CommandError):
            amounts(10, 100_000_000, random.Random(1))  # would need ₦10m each

    def test_refuses_without_debug(self):  # production runs with DEBUG off
        with self.assertRaises(CommandError):
            call_command("seed_demo_donations", "--count", "5", "--total", "50000", stdout=io.StringIO())
        self.assertFalse(Donation.objects.exists())

    @override_settings(DEBUG=True)
    def test_refuses_a_remote_database(self):
        from django.db import connection

        old = connection.settings_dict["HOST"]
        connection.settings_dict["HOST"] = "hayabusa.proxy.rlwy.net"
        try:
            with self.assertRaises(CommandError):
                call_command("seed_demo_donations", "--count", "5", "--total", "50000", stdout=io.StringIO())
        finally:
            connection.settings_dict["HOST"] = old
        self.assertFalse(Donation.objects.exists())

    @override_settings(DEBUG=True)
    def test_adds_and_removes_only_demo_rows(self):
        from django.db import connection

        old = connection.settings_dict["HOST"]
        connection.settings_dict["HOST"] = "localhost"
        try:
            call_command("seed_demo_donations", "--count", "20", "--total", "200000", "--seed", "3", stdout=io.StringIO())
            qs = Donation.objects.filter(reference__startswith="NDC-DEMO")
            self.assertEqual(qs.count(), 20)
            self.assertEqual(sum(qs.values_list("amount_kobo", flat=True)), 200_000 * 100)
            self.assertEqual(set(qs.values_list("public_label", flat=True)), {"Anonymous Donor"})
            call_command("seed_demo_donations", "--remove", stdout=io.StringIO())
            self.assertFalse(Donation.objects.exists())
        finally:
            connection.settings_dict["HOST"] = old
