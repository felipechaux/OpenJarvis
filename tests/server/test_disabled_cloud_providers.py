"""``[engine] disabled_cloud_providers`` hides API providers from /v1/models."""

from __future__ import annotations

from types import SimpleNamespace

from openjarvis.server.cloud_router import get_provider
from openjarvis.server.routes import _disabled_cloud_providers


def _request(providers):
    cfg = SimpleNamespace(engine=SimpleNamespace(disabled_cloud_providers=providers))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=cfg)))


def test_reads_config_normalised() -> None:
    assert _disabled_cloud_providers(_request([" Anthropic ", ""])) == {"anthropic"}
    assert _disabled_cloud_providers(_request(None)) == set()


def test_subscription_models_are_not_anthropic_api() -> None:
    # The filter keys on get_provider: API models are hidden, CLI ones stay.
    assert get_provider("claude-sonnet-4-6") == "anthropic"
    assert get_provider("claude-cli/opus") is None
    assert get_provider("antigravity/claude-sonnet-4-6") is None
