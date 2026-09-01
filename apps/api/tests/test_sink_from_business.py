"""Per-tenant sink construction from BusinessProfile.integrations.

Verify:
1. Empty crm_sinks → NoopSink
2. crm_sinks=['ghl'] with token+location → GHLSink
3. crm_sinks=['ghl'] without token → RuntimeError (clear message)
4. crm_sinks=['ghl', 'hubspot'] with both creds → CompositeSink with 2
5. crm_sinks=['hubspot'] without token → RuntimeError
6. crm_sinks=['webhook'] with url+secret → WebhookSink
7. business without integrations attr → NoopSink (backwards compat)
"""
from __future__ import annotations

import pytest

from packages.integrations.sinks import (
    build_sink_from_business,
    CompositeSink,
    GHLSink,
    HubSpotSink,
    NoopSink,
    WebhookSink,
)
from packages.schemas.business import BusinessProfile, Integrations


def _biz(**integ_kwargs) -> BusinessProfile:
    return BusinessProfile(
        id="clinic-x", name="Test Clinic",
        integrations=Integrations(**integ_kwargs),
    )


def test_empty_crm_sinks_is_noop():
    sink = build_sink_from_business(_biz())
    assert isinstance(sink, NoopSink)


def test_business_without_integrations_is_noop():
    b = BusinessProfile(id="x", name="y")  # default Integrations
    sink = build_sink_from_business(b)
    assert isinstance(sink, NoopSink)


def test_ghl_only_returns_ghl_sink():
    sink = build_sink_from_business(_biz(
        crm_sinks=["ghl"],
        ghl_api_token="pit-abc",
        ghl_location_id="loc123",
    ))
    assert isinstance(sink, GHLSink)


def test_ghl_missing_token_raises():
    with pytest.raises(RuntimeError, match="ghl_api_token"):
        build_sink_from_business(_biz(
            crm_sinks=["ghl"],
            ghl_location_id="loc123",
        ))


def test_ghl_missing_location_raises():
    with pytest.raises(RuntimeError, match="ghl_location_id"):
        build_sink_from_business(_biz(
            crm_sinks=["ghl"],
            ghl_api_token="pit-abc",
        ))


def test_hubspot_only_returns_hubspot_sink():
    sink = build_sink_from_business(_biz(
        crm_sinks=["hubspot"],
        hubspot_access_token="pat-abc",
    ))
    assert isinstance(sink, HubSpotSink)


def test_hubspot_missing_token_raises():
    with pytest.raises(RuntimeError, match="hubspot_access_token"):
        build_sink_from_business(_biz(
            crm_sinks=["hubspot"],
        ))


def test_composite_two_sinks():
    sink = build_sink_from_business(_biz(
        crm_sinks=["ghl", "hubspot"],
        ghl_api_token="pit-abc",
        ghl_location_id="loc123",
        hubspot_access_token="pat-def",
    ))
    assert isinstance(sink, CompositeSink)
    assert len(sink.sinks) == 2


def test_webhook_missing_secret_raises():
    with pytest.raises(RuntimeError, match="webhook_hmac_secret"):
        build_sink_from_business(_biz(
            crm_sinks=["webhook"],
            webhook_url="https://n8n.example/hook/abc",
        ))


def test_webhook_configured():
    sink = build_sink_from_business(_biz(
        crm_sinks=["webhook"],
        webhook_url="https://n8n.example/hook/abc",
        webhook_hmac_secret="super-secret-x",
    ))
    assert isinstance(sink, WebhookSink)
