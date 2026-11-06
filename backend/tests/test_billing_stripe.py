"""
Unit tests for Stripe Billing endpoints and session verification.
"""
from unittest.mock import MagicMock, patch
import pytest
from flask import Flask

from api.billing import billing_bp
from config import Config


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(billing_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestBillingEndpoints:
    @patch("api.billing.stripe.checkout.Session.create")
    def test_create_checkout_session(self, mock_create, client):
        mock_create.return_value = MagicMock(
            id="cs_test_12345",
            url="https://checkout.stripe.com/pay/cs_test_12345",
        )

        res = client.post(
            "/api/billing/create-checkout-session",
            headers={
                "X-User-Email": "test@example.com",
                "Origin": "https://plexisai.vercel.app",
            },
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["session_id"] == "cs_test_12345"
        assert "checkout_url" in data
        assert mock_create.called

    @patch("api.billing.stripe.checkout.Session.retrieve")
    def test_verify_session_success(self, mock_retrieve, client):
        mock_retrieve.return_value = MagicMock(
            id="cs_test_success_123",
            payment_status="paid",
            status="complete",
            customer="cus_test_999",
            subscription="sub_test_888",
            customer_email="test@example.com",
            metadata={"user_id": "11111111-1111-1111-1111-111111111111", "email": "test@example.com"},
            client_reference_id="11111111-1111-1111-1111-111111111111",
        )

        res = client.get(
            "/api/billing/verify-session/cs_test_success_123",
            headers={"X-User-Email": "test@example.com"},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["verified"] is True
        assert data["plan"] == "plus"

    @patch("api.billing.stripe.checkout.Session.retrieve")
    def test_verify_session_unpaid(self, mock_retrieve, client):
        mock_retrieve.return_value = MagicMock(
            id="cs_test_unpaid",
            payment_status="unpaid",
            status="open",
        )

        res = client.get(
            "/api/billing/verify-session/cs_test_unpaid",
            headers={"X-User-Email": "test@example.com"},
        )
        assert res.status_code == 400
        data = res.get_json()
        assert data["success"] is False
        assert data["verified"] is False
