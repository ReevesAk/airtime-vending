"""Unit tests for how OperatorClient interprets each operator response
shape. HTTP is mocked here deliberately -- this is testing our own
interpretation logic, not the operator itself. test_timeout_integration.py
covers the one shape (timeout-after-commit) against the real mock operator,
since that's the case the brief singles out as the one not to resolve by
guessing.
"""
from unittest.mock import patch
from uuid import uuid4

import requests
from django.test import SimpleTestCase

from vending.operator_client import OperatorClient, OperatorFailure, OperatorSuccess, OperatorUnknown


class OperatorClientResponseShapeTests(SimpleTestCase):
    def setUp(self):
        self.client = OperatorClient(base_url="http://operator.test", timeout_seconds=1.0)
        self.reference = uuid4()

    @patch("vending.operator_client.requests.post")
    def test_success(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"status": "SUCCESS", "operatorRef": "OP-1"}

        result = self.client.vend(reference=self.reference, phone_number="2348000000000", amount_minor=50000)

        self.assertEqual(result, OperatorSuccess(operator_ref="OP-1"))

    @patch("vending.operator_client.requests.post")
    def test_clean_failure(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"status": "FAILED", "reason": "INVALID_NUMBER"}

        result = self.client.vend(reference=self.reference, phone_number="2348000000002", amount_minor=50000)

        self.assertEqual(result, OperatorFailure(reason="INVALID_NUMBER"))

    @patch("vending.operator_client.requests.post")
    def test_transient_error_is_a_clean_failure_not_unknown(self, mock_post):
        mock_post.return_value.status_code = 503
        mock_post.return_value.content = b'{"reason": "TRANSIENT"}'
        mock_post.return_value.json.return_value = {"reason": "TRANSIENT"}

        result = self.client.vend(reference=self.reference, phone_number="2348000000003", amount_minor=50000)

        # Critically NOT OperatorUnknown: the operator affirmatively said
        # nothing happened, so funds are safe to release.
        self.assertEqual(result, OperatorFailure(reason="TRANSIENT"))

    @patch("vending.operator_client.requests.post")
    def test_rate_limited_is_a_clean_failure(self, mock_post):
        mock_post.return_value.status_code = 429
        mock_post.return_value.content = b'{"reason": "RATE_LIMITED"}'
        mock_post.return_value.json.return_value = {"reason": "RATE_LIMITED"}

        result = self.client.vend(reference=self.reference, phone_number="2348000000009", amount_minor=50000)

        self.assertEqual(result, OperatorFailure(reason="RATE_LIMITED"))

    @patch("vending.operator_client.requests.post", side_effect=requests.Timeout)
    def test_client_timeout_is_unknown_not_failure(self, mock_post):
        result = self.client.vend(reference=self.reference, phone_number="2348000000004", amount_minor=50000)

        # This is the load-bearing assertion: a timeout must never be
        # reported as OperatorFailure, because that would tell the caller
        # it's safe to release funds the operator may have already spent.
        self.assertIsInstance(result, OperatorUnknown)

    @patch("vending.operator_client.requests.post", side_effect=requests.ConnectionError)
    def test_connection_error_is_unknown_not_failure(self, mock_post):
        result = self.client.vend(reference=self.reference, phone_number="2348000000005", amount_minor=50000)

        self.assertIsInstance(result, OperatorUnknown)
