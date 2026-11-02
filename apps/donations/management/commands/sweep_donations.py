"""Alias kept from unit 05: runs the shared sweep (donations + membership payments), recorded as its own job (spec 11 §6.3)."""
from apps.payments.management.commands.sweep_payments import Command as SweepPayments


class Command(SweepPayments):
    job = "sweep_donations"
