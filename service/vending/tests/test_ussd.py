import time
from unittest.mock import patch

from django.test import TestCase, override_settings

from vending.models import Transaction
from vending.operator_client import OperatorSuccess


def ussd_post(client, session_id, text, phone="2348099990000"):
    return client.post(
        "/ussd",
        {"sessionId": session_id, "serviceCode": "*123#", "phoneNumber": phone, "text": text},
    )


class MenuNavigationTests(TestCase):
    def test_main_menu(self):
        resp = ussd_post(self.client, "s1", "")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.decode().startswith("CON"))
        self.assertIn("Buy airtime", resp.content.decode())

    def test_ask_for_phone_then_amount_then_confirm(self):
        c = self.client
        r1 = ussd_post(c, "s2", "1")
        self.assertTrue(r1.content.decode().startswith("CON"))

        r2 = ussd_post(c, "s2", "1*2348011112222")
        self.assertTrue(r2.content.decode().startswith("CON"))
        self.assertIn("amount", r2.content.decode().lower())

        r3 = ussd_post(c, "s2", "1*2348011112222*500")
        self.assertTrue(r3.content.decode().startswith("CON"))
        self.assertIn("Confirm", r3.content.decode())

    def test_invalid_phone_reprompts_instead_of_crashing(self):
        resp = ussd_post(self.client, "s3", "1*abc")
        self.assertTrue(resp.content.decode().startswith("CON"))
        self.assertIn("Invalid phone", resp.content.decode())

    def test_invalid_amount_reprompts(self):
        resp = ussd_post(self.client, "s4", "1*2348011112222*notanumber")
        self.assertTrue(resp.content.decode().startswith("CON"))
        self.assertIn("Invalid amount", resp.content.decode())

    def test_amount_over_cap_is_rejected(self):
        resp = ussd_post(self.client, "s5", "1*2348011112222*999999")
        self.assertTrue(resp.content.decode().startswith("CON"))
        self.assertIn("Invalid amount", resp.content.decode())

    def test_cancel_does_not_create_a_transaction(self):
        resp = ussd_post(self.client, "s6", "1*2348011112222*500*2")
        self.assertEqual(resp.content.decode(), "END Top-up cancelled. No funds were charged.")
        self.assertEqual(Transaction.objects.count(), 0)


class ConfirmVendTests(TestCase):
    @patch("vending.ussd.OperatorClient")
    def test_confirm_vends_and_terminates_session(self, mock_operator_cls):
        mock_operator_cls.return_value.vend.return_value = OperatorSuccess(operator_ref="OP-1")

        resp = ussd_post(self.client, "s7", "1*2348011112222*500*1")

        text = resp.content.decode()
        self.assertTrue(text.startswith("END"))
        self.assertIn("successful", text)
        self.assertEqual(Transaction.objects.count(), 1)

    @patch("vending.ussd.OperatorClient")
    def test_gateway_retry_does_not_double_vend(self, mock_operator_cls):
        mock_operator_cls.return_value.vend.return_value = OperatorSuccess(operator_ref="OP-1")

        first = ussd_post(self.client, "s8", "1*2348011112222*500*1")
        second = ussd_post(self.client, "s8", "1*2348011112222*500*1")  # gateway resends the same callback

        self.assertEqual(first.content, second.content)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(mock_operator_cls.return_value.vend.call_count, 1)

    @patch("vending.ussd.OperatorClient")
    def test_double_tap_confirm_does_not_double_vend(self, mock_operator_cls):
        mock_operator_cls.return_value.vend.return_value = OperatorSuccess(operator_ref="OP-1")

        tap1 = ussd_post(self.client, "s9", "1*2348011112222*500*1")
        tap2 = ussd_post(self.client, "s9", "1*2348011112222*500*1")

        self.assertEqual(tap1.content, tap2.content)
        self.assertEqual(Transaction.objects.count(), 1)


@override_settings(USSD_SESSION_TTL_SECONDS=0.5)
class SessionExpiryTests(TestCase):
    def test_stale_session_is_rejected_instead_of_resumed(self):
        c = self.client
        ussd_post(c, "s10", "1")
        ussd_post(c, "s10", "1*2348011112222")

        time.sleep(0.7)

        resumed = ussd_post(c, "s10", "1*2348011112222*500")
        text = resumed.content.decode()
        self.assertTrue(text.startswith("END"))
        self.assertIn("expired", text.lower())

    def test_fresh_dial_reusing_an_old_session_id_is_not_treated_as_expired(self):
        c = self.client
        ussd_post(c, "s11", "1")
        time.sleep(0.7)

        # A bare re-dial (text=="") on an old sessionId should just show the
        # main menu again, not an expiry error -- some gateways do reuse
        # session ids across genuinely separate dial-ins.
        resp = ussd_post(c, "s11", "")
        self.assertTrue(resp.content.decode().startswith("CON Welcome"))
