"""Spec 06 §6.3 — one webhook URL, routed by reference prefix; records never mix."""
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.payments import dispatch
from apps.payments.providers.fake import FakeProvider, sign


class DispatchTests(TestCase):
    def post(self, event, ref):
        body = FakeProvider.webhook_body(event, ref)
        return self.client.post(reverse("payment-webhook", kwargs={"provider": "fake"}), data=body, content_type="application/json",
                                HTTP_X_FAKE_SIGNATURE=sign(body))

    def test_routes_by_prefix(self):
        with mock.patch("apps.donations.services.on_provider_event") as don, mock.patch("apps.membership.services.on_provider_event") as mem:
            self.post("charge.success", "NDC-AAAA")
            self.post("charge.success", "MEM-BBBB")
            self.post("charge.success", "XYZ-CCCC")  # unknown prefix: acknowledged, ignored
        self.assertEqual(don.call_args.args[1], "NDC-AAAA")
        self.assertEqual(mem.call_args.args[1], "MEM-BBBB")
        self.assertEqual((don.call_count, mem.call_count), (1, 1))

    def test_unhandled_event_ignored(self):
        with mock.patch("apps.donations.services.on_provider_event") as don:
            self.assertEqual(self.post("transfer.success", "NDC-AAAA").status_code, 200)
        don.assert_not_called()

    def test_handler_lookup(self):
        self.assertIsNone(dispatch.handler_for("ABC-1"))
        self.assertEqual(dispatch.handler_for("MEM-1").__name__, "apps.membership.services")
