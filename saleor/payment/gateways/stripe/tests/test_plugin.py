import warnings
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from stripe.error import AuthenticationError, StripeError
from stripe.stripe_object import StripeObject

from .....plugins.models import PluginConfiguration
from .... import TransactionKind
from ....interface import GatewayResponse
from ....utils import (
    create_payment_information,
    create_transaction,
    price_to_minor_unit,
)
from ..consts import (
    ACTION_REQUIRED_STATUSES,
    AUTHORIZED_STATUS,
    AUTOMATIC_CAPTURE_METHOD,
    MANUAL_CAPTURE_METHOD,
    PROCESSING_STATUS,
    SUCCESS_STATUS,
)


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.WebhookEndpoint.list")
def test_validate_plugin_configuration_correct_configuration(
    mocked_stripe, stripe_plugin
):
    plugin = stripe_plugin(
        public_api_key="public",
        secret_api_key="ABC",
        active=True,
    )
    configuration = PluginConfiguration.objects.get()
    plugin.validate_plugin_configuration(configuration)

    assert mocked_stripe.called


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.WebhookEndpoint.list")
def test_validate_plugin_configuration_incorrect_configuration(
    mocked_stripe, stripe_plugin
):
    mocked_stripe.side_effect = AuthenticationError()
    plugin = stripe_plugin(
        public_api_key="public",
        secret_api_key="wrong",
        active=True,
    )
    configuration = PluginConfiguration.objects.get()
    with pytest.raises(ValidationError):
        plugin.validate_plugin_configuration(configuration)

    assert mocked_stripe.called


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.WebhookEndpoint.list")
def test_validate_plugin_configuration_missing_required_fields(
    mocked_stripe, stripe_plugin
):
    plugin = stripe_plugin(
        secret_api_key="wrong",
        active=True,
    )
    configuration = PluginConfiguration.objects.get()

    for config_field in configuration.configuration:
        if config_field["name"] == "public_api_key":
            config_field["value"] = None
            break
    with pytest.raises(ValidationError):
        plugin.validate_plugin_configuration(configuration)

    assert not mocked_stripe.called


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.WebhookEndpoint.list")
def test_validate_plugin_configuration_validate_only_when_active(
    mocked_stripe, stripe_plugin
):
    plugin = stripe_plugin(
        secret_api_key="wrong",
        active=False,
    )
    configuration = PluginConfiguration.objects.get()

    for config_field in configuration.configuration:
        if config_field["name"] == "public_api_key":
            config_field["value"] = None
            break

    plugin.validate_plugin_configuration(configuration)

    assert not mocked_stripe.called


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.WebhookEndpoint.delete")
def test_pre_save_plugin_configuration_removes_webhook_when_disabled(
    mocked_stripe, stripe_plugin
):
    plugin = stripe_plugin(
        active=False, webhook_secret_key="secret", webhook_endpoint_id="endpoint"
    )
    configuration = PluginConfiguration.objects.get()
    plugin.pre_save_plugin_configuration(configuration)

    assert all(
        [
            c_field["name"] != "webhook_endpoint_id"
            for c_field in configuration.configuration
        ]
    )
    assert all(
        [
            c_field["name"] != "webhook_secret_key"
            for c_field in configuration.configuration
        ]
    )
    assert mocked_stripe.called


def get_field_from_plugin_configuration(
    plugin_configuration: PluginConfiguration, field_name: str
):
    configuration = plugin_configuration.configuration
    for config_field in configuration:
        if config_field["name"] == field_name:
            return config_field
    return None


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.WebhookEndpoint.create")
def test_pre_save_plugin_configuration(mocked_stripe, stripe_plugin):
    webhook_object = StripeObject(id="stripe_webhook_id", last_response={})
    webhook_object.secret = "stripe_webhook_secret"
    mocked_stripe.return_value = webhook_object

    plugin = stripe_plugin(
        active=True, webhook_endpoint_id=None, webhook_secret_key=None
    )
    configuration = PluginConfiguration.objects.get()
    plugin.pre_save_plugin_configuration(configuration)

    webhook_id = get_field_from_plugin_configuration(
        configuration, "webhook_endpoint_id"
    )
    webhook_secret = get_field_from_plugin_configuration(
        configuration, "webhook_secret_key"
    )

    assert webhook_id["value"] == "stripe_webhook_id"
    assert webhook_secret["value"] == "stripe_webhook_secret"

    assert mocked_stripe.called


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.create")
def test_process_payment(
    mocked_payment_intent, stripe_plugin, payment_stripe_for_checkout
):
    payment_intent = Mock()
    mocked_payment_intent.return_value = payment_intent
    client_secret = "client-secret"
    dummy_response = {
        "id": "evt_1Ip9ANH1Vac4G4dbE9ch7zGS",
    }
    payment_intent_id = "payment-intent-id"
    payment_intent.id = payment_intent_id
    payment_intent.client_secret = client_secret
    payment_intent.last_response.data = dummy_response

    plugin = stripe_plugin(auto_capture=True)

    payment_info = create_payment_information(
        payment_stripe_for_checkout,
    )

    response = plugin.process_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is True
    assert response.kind == TransactionKind.ACTION_TO_CONFIRM
    assert response.amount == payment_info.amount
    assert response.currency == payment_info.currency
    assert response.transaction_id == payment_intent_id
    assert response.error is None
    assert response.raw_response == dummy_response
    assert response.action_required_data == {"client_secret": client_secret}

    api_key = plugin.config.connection_params["secret_api_key"]
    mocked_payment_intent.assert_called_once_with(
        api_key=api_key,
        amount=price_to_minor_unit(payment_info.amount, payment_info.currency),
        currency=payment_info.currency,
        capture_method=AUTOMATIC_CAPTURE_METHOD,
    )


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.create")
def test_process_payment_with_manual_capture(
    mocked_payment_intent, stripe_plugin, payment_stripe_for_checkout
):
    payment_intent = Mock()
    mocked_payment_intent.return_value = payment_intent
    client_secret = "client-secret"
    dummy_response = {
        "id": "evt_1Ip9ANH1Vac4G4dbE9ch7zGS",
    }
    payment_intent_id = "payment-intent-id"
    payment_intent.id = payment_intent_id
    payment_intent.client_secret = client_secret
    payment_intent.last_response.data = dummy_response

    plugin = stripe_plugin(auto_capture=False)

    payment_info = create_payment_information(
        payment_stripe_for_checkout,
    )

    plugin.process_payment(payment_info, None)

    api_key = plugin.config.connection_params["secret_api_key"]
    mocked_payment_intent.assert_called_once_with(
        api_key=api_key,
        amount=price_to_minor_unit(payment_info.amount, payment_info.currency),
        currency=payment_info.currency,
        capture_method=MANUAL_CAPTURE_METHOD,
    )


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.create")
def test_process_payment_with_error(
    mocked_payment_intent, stripe_plugin, payment_stripe_for_checkout
):
    mocked_payment_intent.side_effect = StripeError(message="stripe-error")

    plugin = stripe_plugin()

    payment_info = create_payment_information(
        payment_stripe_for_checkout,
    )

    response = plugin.process_payment(payment_info, None)

    assert response.is_success is False
    assert response.action_required is True
    assert response.kind == TransactionKind.ACTION_TO_CONFIRM
    assert response.amount == payment_info.amount
    assert response.currency == payment_info.currency
    assert response.transaction_id == ""
    assert response.error == "stripe-error"
    assert response.raw_response is None
    assert response.action_required_data == {"client_secret": None}

    api_key = plugin.config.connection_params["secret_api_key"]
    mocked_payment_intent.assert_called_once_with(
        api_key=api_key,
        amount=price_to_minor_unit(payment_info.amount, payment_info.currency),
        currency=payment_info.currency,
        capture_method=AUTOMATIC_CAPTURE_METHOD,
    )


@pytest.mark.parametrize("kind", [TransactionKind.AUTH, TransactionKind.CAPTURE])
def test_confirm_payment_for_webhook(kind, stripe_plugin, payment_stripe_for_checkout):
    payment_intent_id = "payment-intent-id"
    gateway_response = {
        "id": "evt_1Ip9ANH1Vac4G4dbE9ch7zGS",
    }
    payment = payment_stripe_for_checkout
    payment.transactions.create(
        is_success=True,
        action_required=False,
        kind=kind,
        token=payment_intent_id,
        gateway_response=gateway_response,
        amount=payment.total,
        currency=payment.currency,
    )

    payment_info = create_payment_information(
        payment_stripe_for_checkout,
    )

    plugin = stripe_plugin()
    response = plugin.confirm_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is False
    assert response.kind == kind
    assert response.amount == payment_info.amount
    assert response.currency == payment_info.currency
    assert response.transaction_id == payment_intent_id
    assert response.error is None
    assert response.raw_response == gateway_response
    assert response.action_required_data is None
    assert response.transaction_already_processed is True


@pytest.mark.parametrize(
    "kind, status",
    [
        (TransactionKind.AUTH, AUTHORIZED_STATUS),
        (TransactionKind.CAPTURE, SUCCESS_STATUS),
    ],
)
@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.retrieve")
def test_confirm_payment(
    mocked_intent_retrieve, kind, status, stripe_plugin, payment_stripe_for_checkout
):
    gateway_response = {
        "id": "evt_1Ip9ANH1Vac4G4dbE9ch7zGS",
    }

    payment_intent_id = "payment-intent-id"

    payment = payment_stripe_for_checkout
    payment.transactions.create(
        is_success=True,
        action_required=False,
        kind=TransactionKind.ACTION_TO_CONFIRM,
        token=payment_intent_id,
        gateway_response=gateway_response,
        amount=payment.total,
        currency=payment.currency,
    )

    payment_intent = StripeObject(id=payment_intent_id)
    payment_intent["amount"] = price_to_minor_unit(payment.total, payment.currency)
    payment_intent["status"] = status
    payment_intent["currency"] = payment.currency
    mocked_intent_retrieve.return_value = payment_intent

    payment_info = create_payment_information(
        payment_stripe_for_checkout, payment_token=payment_intent_id
    )

    plugin = stripe_plugin()
    response = plugin.confirm_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is False
    assert response.kind == kind
    assert response.amount == payment.total
    assert response.currency == payment.currency
    assert response.transaction_id == payment_intent_id
    assert response.error is None


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.retrieve")
def test_confirm_payment_incorrect_payment_intent(
    mocked_intent_retrieve, stripe_plugin, payment_stripe_for_checkout
):

    gateway_response = {
        "id": "evt_1Ip9ANH1Vac4G4dbE9ch7zGS",
    }

    payment_intent_id = "payment-intent-id"

    payment = payment_stripe_for_checkout
    payment.transactions.create(
        is_success=True,
        action_required=False,
        kind=TransactionKind.ACTION_TO_CONFIRM,
        token=payment_intent_id,
        gateway_response=gateway_response,
        amount=payment.total,
        currency=payment.currency,
    )

    mocked_intent_retrieve.side_effect = StripeError(message="stripe-error")

    payment_info = create_payment_information(
        payment_stripe_for_checkout, payment_token=payment_intent_id
    )

    plugin = stripe_plugin()
    with warnings.catch_warnings(record=True):
        response = plugin.confirm_payment(payment_info, None)

    assert response.is_success is False
    assert response.action_required is False
    assert response.kind == TransactionKind.AUTH
    assert response.amount == payment.total
    assert response.currency == payment.currency
    assert response.transaction_id == ""
    assert response.error == "stripe-error"


@pytest.mark.parametrize("status", ACTION_REQUIRED_STATUSES)
@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.retrieve")
def test_confirm_payment_action_required_status(
    mocked_intent_retrieve, status, stripe_plugin, payment_stripe_for_checkout
):
    gateway_response = {
        "id": "evt_1Ip9ANH1Vac4G4dbE9ch7zGS",
    }

    payment_intent_id = "payment-intent-id"

    payment = payment_stripe_for_checkout
    payment.transactions.create(
        is_success=True,
        action_required=False,
        kind=TransactionKind.ACTION_TO_CONFIRM,
        token=payment_intent_id,
        gateway_response=gateway_response,
        amount=payment.total,
        currency=payment.currency,
    )

    payment_intent = StripeObject(id=payment_intent_id)
    payment_intent["capture_method"] = "automatic"
    payment_intent["amount"] = price_to_minor_unit(payment.total, payment.currency)
    payment_intent["status"] = status
    payment_intent["currency"] = payment.currency
    mocked_intent_retrieve.return_value = payment_intent

    payment_info = create_payment_information(
        payment_stripe_for_checkout, payment_token=payment_intent_id
    )

    plugin = stripe_plugin()
    response = plugin.confirm_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is True
    assert response.kind == TransactionKind.ACTION_TO_CONFIRM
    assert response.amount == payment.total
    assert response.currency == payment.currency
    assert response.transaction_id == payment_intent_id
    assert response.error is None


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.retrieve")
def test_confirm_payment_processing_status(
    mocked_intent_retrieve, stripe_plugin, payment_stripe_for_checkout
):
    gateway_response = {
        "id": "evt_1Ip9ANH1Vac4G4dbE9ch7zGS",
    }

    payment_intent_id = "payment-intent-id"

    payment = payment_stripe_for_checkout
    payment.transactions.create(
        is_success=True,
        action_required=False,
        kind=TransactionKind.ACTION_TO_CONFIRM,
        token=payment_intent_id,
        gateway_response=gateway_response,
        amount=payment.total,
        currency=payment.currency,
    )

    payment_intent = StripeObject(id=payment_intent_id)
    payment_intent["capture_method"] = "automatic"
    payment_intent["amount"] = price_to_minor_unit(payment.total, payment.currency)
    payment_intent["status"] = PROCESSING_STATUS
    payment_intent["currency"] = payment.currency
    mocked_intent_retrieve.return_value = payment_intent

    payment_info = create_payment_information(
        payment_stripe_for_checkout, payment_token=payment_intent_id
    )

    plugin = stripe_plugin()
    response = plugin.confirm_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is False
    assert response.kind == TransactionKind.PENDING
    assert response.amount == payment.total
    assert response.currency == payment.currency
    assert response.transaction_id == payment_intent_id
    assert response.error is None


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.capture")
def test_capture_payment(
    mocked_capture, payment_stripe_for_order, order_with_lines, stripe_plugin
):
    payment = payment_stripe_for_order

    payment_intent_id = "ABC"
    payment_intent = StripeObject(id=payment_intent_id)
    payment_intent["amount"] = price_to_minor_unit(payment.total, payment.currency)
    payment_intent["status"] = SUCCESS_STATUS
    payment_intent["currency"] = payment.currency
    payment_intent["last_response"] = StripeObject()
    payment_intent["last_response"]["data"] = {"response": "json"}

    mocked_capture.return_value = payment_intent

    payment_info = create_payment_information(
        payment,
        payment_token=payment_intent_id,
    )
    gateway_response = GatewayResponse(
        kind=TransactionKind.AUTH,
        action_required=False,
        transaction_id=payment_intent_id,
        is_success=True,
        amount=payment_info.amount,
        currency=payment_info.currency,
        error="",
        raw_response={},
    )

    create_transaction(
        payment=payment,
        payment_information=payment_info,
        kind=TransactionKind.AUTH,
        gateway_response=gateway_response,
    )

    plugin = stripe_plugin()

    response = plugin.capture_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is False
    assert response.kind == TransactionKind.CAPTURE
    assert response.amount == payment.total
    assert response.currency == order_with_lines.currency
    assert response.transaction_id == payment_intent_id


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.Refund.create")
def test_refund_payment(
    mocked_refund, payment_stripe_for_order, order_with_lines, stripe_plugin
):
    payment = payment_stripe_for_order

    payment_intent_id = "ABC"
    refund_object = StripeObject(id=payment_intent_id)
    refund_object["amount"] = price_to_minor_unit(payment.total, payment.currency)
    refund_object["status"] = SUCCESS_STATUS
    refund_object["currency"] = payment.currency
    refund_object["last_response"] = StripeObject()
    refund_object["last_response"]["data"] = {"response": "json"}

    mocked_refund.return_value = refund_object

    payment_info = create_payment_information(
        payment,
        payment_token=payment_intent_id,
    )
    gateway_response = GatewayResponse(
        kind=TransactionKind.CAPTURE,
        action_required=False,
        transaction_id=payment_intent_id,
        is_success=True,
        amount=payment_info.amount,
        currency=payment_info.currency,
        error="",
        raw_response={},
    )

    create_transaction(
        payment=payment,
        payment_information=payment_info,
        kind=TransactionKind.CAPTURE,
        gateway_response=gateway_response,
    )

    plugin = stripe_plugin()

    response = plugin.refund_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is False
    assert response.kind == TransactionKind.REFUND
    assert response.amount == payment.total
    assert response.currency == order_with_lines.currency
    assert response.transaction_id == payment_intent_id


@patch("saleor.payment.gateways.stripe.stripe_api.stripe.PaymentIntent.cancel")
def test_void_payment(
    mocked_cancel, payment_stripe_for_order, order_with_lines, stripe_plugin
):
    payment = payment_stripe_for_order

    payment_intent_id = "ABC"
    payment_intent = StripeObject(id=payment_intent_id)
    payment_intent["amount"] = price_to_minor_unit(payment.total, payment.currency)
    payment_intent["status"] = SUCCESS_STATUS
    payment_intent["currency"] = payment.currency
    payment_intent["last_response"] = StripeObject()
    payment_intent["last_response"]["data"] = {"response": "json"}

    mocked_cancel.return_value = payment_intent

    payment_info = create_payment_information(
        payment,
        payment_token=payment_intent_id,
    )
    gateway_response = GatewayResponse(
        kind=TransactionKind.AUTH,
        action_required=False,
        transaction_id=payment_intent_id,
        is_success=True,
        amount=payment_info.amount,
        currency=payment_info.currency,
        error="",
        raw_response={},
    )

    create_transaction(
        payment=payment,
        payment_information=payment_info,
        kind=TransactionKind.AUTH,
        gateway_response=gateway_response,
    )

    plugin = stripe_plugin()

    response = plugin.void_payment(payment_info, None)

    assert response.is_success is True
    assert response.action_required is False
    assert response.kind == TransactionKind.VOID
    assert response.amount == payment.total
    assert response.currency == order_with_lines.currency
    assert response.transaction_id == payment_intent_id