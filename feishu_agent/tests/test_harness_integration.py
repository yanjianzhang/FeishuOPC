"""Integration tests for the tier-1 harness wiring.

These tests exist because of a class of bug caught during the tier-1
review: **settings were not declared on ``Settings``**, so every
``_build_*`` function fell back to its off-switch default. Unit tests
passed while production was running un-tier-1'd code.

The tests here don't stub the builders — they call them with real
``Settings`` instances and assert the *shape* of what gets produced.
That way, any future "declared a field but forgot to plumb it" bug
is caught here instead of in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.config import Settings
from feishu_agent.core.context_compression import (
    NoOpContextCompressor,
    TailWindowCompressor,
)
from feishu_agent.providers.llm_provider_pool import LlmProviderPool
from feishu_agent.team.agent_notes_service import AgentNotesService


@pytest.fixture
def _patch_settings(monkeypatch):
    """Replace the cached ``settings`` singleton in feishu_runtime_service.

    ``get_settings`` is ``lru_cache``'d so it's imported as a reference
    in the runtime module. Tests must patch the module attribute to
    swap in a test-specific Settings without clobbering other tests.
    """
    def _do_patch(**overrides):
        import feishu_agent.runtime.feishu_runtime_service as runtime

        s = Settings(**overrides)
        monkeypatch.setattr(runtime, "settings", s)
        return runtime, s

    return _do_patch


# ---------------------------------------------------------------------------
# Settings declares every tier-1 knob
# ---------------------------------------------------------------------------


def test_tier1_settings_fields_are_declared():
    """If any of these raises AttributeError we've re-introduced the
    silent-off bug from the tier-1 review. Names and defaults are the
    single source of truth for operators configuring the harness."""
    s = Settings()
    # Each assertion deliberately names the field so a failure message
    # directly implicates the missing / renamed attribute.
    # Compression is ON by default as of the P2 / Q-series fixes — the
    # default is sized for Claude 4.6's 200k context (180k leaves 20k
    # headroom for the current message + tools). See feishu_agent/config.py.
    assert s.llm_max_context_tokens == 180_000
    assert s.llm_compression_trigger == 0.7
    assert s.llm_compression_keep_tail == 6
    assert s.llm_secondary_base_url is None
    assert s.llm_secondary_api_key is None
    assert s.llm_secondary_model is None
    # The fallback transport defaults to the OpenAI-compatible HTTP
    # path; flipping it to "anthropic_bedrock" is what activates the
    # Bedrock code path in ``_build_provider_pool``.
    assert s.llm_secondary_transport == "openai_http"
    assert s.llm_secondary_aws_region is None
    assert s.llm_secondary_aws_access_key_id is None
    assert s.llm_secondary_aws_secret_access_key is None
    assert s.llm_retries_per_provider == 2
    assert s.agent_notes_max_per_session == 5
    assert s.agent_notes_prompt_limit == 20


# ---------------------------------------------------------------------------
# Context compressor: on by default; explicit 0 turns it off
# ---------------------------------------------------------------------------


def test_context_compressor_on_by_default(_patch_settings):
    """No override → use the default 180k limit → TailWindowCompressor
    wired. Keeps us from re-regressing the P2 compression default that
    landed after the developer-sub-session ReadTimeout class of bugs."""
    runtime, _ = _patch_settings()
    compressor = runtime._build_context_compressor()
    assert isinstance(compressor, TailWindowCompressor)
    assert compressor.max_context_tokens == 180_000


def test_context_compressor_off_when_max_tokens_zero(_patch_settings):
    """Operator can still disable compression by setting the env var to 0."""
    runtime, _ = _patch_settings(llm_max_context_tokens=0)
    compressor = runtime._build_context_compressor()
    assert isinstance(compressor, NoOpContextCompressor)


def test_context_compressor_on_when_max_tokens_set(_patch_settings):
    runtime, _ = _patch_settings(
        llm_max_context_tokens=32000,
        llm_compression_trigger=0.6,
        llm_compression_keep_tail=4,
    )
    compressor = runtime._build_context_compressor()
    assert isinstance(compressor, TailWindowCompressor)
    assert compressor.max_context_tokens == 32000
    assert compressor.trigger_ratio == pytest.approx(0.6)
    assert compressor.keep_tail_turns == 4


# ---------------------------------------------------------------------------
# Provider pool: retry-only mode (primary only) + failover mode (both)
# ---------------------------------------------------------------------------


def test_provider_pool_none_when_primary_missing(_patch_settings):
    """Without a primary LLM endpoint, the pool has no providers to
    wrap and returns None (adapter falls back to its direct client)."""
    runtime, _ = _patch_settings()
    assert runtime._build_provider_pool() is None


def test_provider_pool_single_provider_for_retry_only(_patch_settings):
    """Primary configured, no secondary → single-provider pool.

    This is the shape that fixes B-3 from the review: previously we
    required both primary AND secondary before building a pool at all,
    so the most common deployment (one endpoint) got zero retry
    coverage. Now it gets retry + backoff on its one provider.
    """
    runtime, _ = _patch_settings(
        techbot_llm_base_url="https://primary.example.com/v1",
        techbot_llm_api_key="primary-key",
        techbot_llm_model="test-model",
    )
    pool = runtime._build_provider_pool()
    assert isinstance(pool, LlmProviderPool)
    assert len(pool.providers) == 1
    assert pool.providers[0].name == "primary"
    assert pool.providers[0].base_url == "https://primary.example.com/v1"


def test_provider_pool_includes_secondary_when_configured(_patch_settings):
    runtime, _ = _patch_settings(
        techbot_llm_base_url="https://primary.example.com/v1",
        techbot_llm_api_key="primary-key",
        techbot_llm_model="primary-model",
        llm_secondary_base_url="https://secondary.example.com/v1",
        llm_secondary_api_key="secondary-key",
        llm_secondary_model="secondary-model",
    )
    pool = runtime._build_provider_pool()
    assert isinstance(pool, LlmProviderPool)
    assert [p.name for p in pool.providers] == ["primary", "secondary"]
    assert pool.providers[1].base_url == "https://secondary.example.com/v1"
    assert pool.providers[1].model == "secondary-model"


def test_provider_pool_builds_bedrock_secondary_when_transport_set(_patch_settings):
    """Setting ``llm_secondary_transport="anthropic_bedrock"`` together
    with model + region + both AWS keys produces a secondary provider
    whose config carries the AWS credentials instead of base_url/api_key.

    Guards the wiring step: if anyone renames a setting or drops an
    AWS field from the pool builder's check list, the secondary
    silently disappears and the PM bot loses its Bedrock fallback.
    """
    runtime, _ = _patch_settings(
        techbot_llm_base_url="https://primary.example.com/v1",
        techbot_llm_api_key="primary-key",
        techbot_llm_model="primary-model",
        llm_secondary_transport="anthropic_bedrock",
        llm_secondary_model="arn:aws:bedrock:us-west-2:0:application-inference-profile/x",
        llm_secondary_aws_region="us-west-2",
        llm_secondary_aws_access_key_id="AKIA_TEST",
        llm_secondary_aws_secret_access_key="secret",
    )
    pool = runtime._build_provider_pool()
    assert pool is not None
    assert [p.name for p in pool.providers] == ["primary", "secondary"]
    bedrock_cfg = pool.providers[1]
    assert bedrock_cfg.transport == "anthropic_bedrock"
    assert bedrock_cfg.aws_region == "us-west-2"
    assert bedrock_cfg.aws_access_key_id == "AKIA_TEST"
    assert bedrock_cfg.aws_secret_access_key == "secret"
    assert bedrock_cfg.model.startswith("arn:aws:bedrock")
    # Bedrock transport should not populate the HTTP fields — those are
    # never used and setting them would imply wrong auth mode to any
    # future reader.
    assert bedrock_cfg.base_url == ""
    assert bedrock_cfg.api_key == ""


def test_provider_pool_omits_bedrock_secondary_when_aws_creds_missing(
    _patch_settings,
):
    """A half-configured Bedrock fallback (transport set but region or
    keys missing) should NOT join the pool: producing an unusable
    secondary would cause every primary failure to cascade into a second
    failed call instead of an honest "no fallback available".
    """
    runtime, _ = _patch_settings(
        techbot_llm_base_url="https://primary.example.com/v1",
        techbot_llm_api_key="primary-key",
        techbot_llm_model="primary-model",
        llm_secondary_transport="anthropic_bedrock",
        llm_secondary_model="arn:aws:bedrock:x",
        # region + keys deliberately absent
    )
    pool = runtime._build_provider_pool()
    assert pool is not None
    assert [p.name for p in pool.providers] == ["primary"]


def test_provider_pool_secondary_inherits_primary_model(_patch_settings):
    """If ``llm_secondary_model`` is omitted, the secondary provider
    uses the primary's model. Operators frequently want "same model,
    different endpoint" for geo failover; don't force them to repeat
    the model name."""
    runtime, _ = _patch_settings(
        techbot_llm_base_url="https://primary.example.com/v1",
        techbot_llm_api_key="primary-key",
        techbot_llm_model="shared-model",
        llm_secondary_base_url="https://secondary.example.com/v1",
        llm_secondary_api_key="secondary-key",
        # llm_secondary_model deliberately absent
    )
    pool = runtime._build_provider_pool()
    assert pool is not None
    assert pool.providers[1].model == "shared-model"


# ---------------------------------------------------------------------------
# Agent notes: project root discovery + per-session quota plumbed through
# ---------------------------------------------------------------------------


def test_agent_notes_service_respects_session_quota(_patch_settings, tmp_path, monkeypatch):
    """The session quota is read at build time from settings. If the
    field ever drifts (settings rename, typo) this catches it even
    when nothing else breaks."""
    runtime, _ = _patch_settings(agent_notes_max_per_session=2)
    # Stub _resolve_code_write_policies to return a known project root —
    # we're testing the wiring, not policy discovery.
    monkeypatch.setattr(
        runtime,
        "_resolve_code_write_policies",
        lambda app_repo_root, registry: (
            {"demo": tmp_path / "demo"},
            {},
        ),
    )
    (tmp_path / "demo").mkdir()

    service = runtime._build_agent_notes_service(
        app_repo_root=tmp_path, registry=object(), project_id="demo"
    )
    assert isinstance(service, AgentNotesService)
    # Can verify the quota by appending twice and expecting the third
    # to raise AgentNoteLimitError — integration, not unit.
    service.append(role="tech_lead", note="first", trace_id="t1")
    service.append(role="tech_lead", note="second", trace_id="t2")
    from feishu_agent.team.agent_notes_service import AgentNoteLimitError

    with pytest.raises(AgentNoteLimitError):
        service.append(role="tech_lead", note="third", trace_id="t3")


def test_agent_notes_service_none_when_project_missing(_patch_settings, monkeypatch):
    """Caller passes a ``project_id`` that the registry can't resolve
    → service is None. Intentional: we do NOT fall back to writing
    into the FeishuOPC repo (that would mix customer A's memory into
    customer B's repo)."""
    runtime, _ = _patch_settings()
    monkeypatch.setattr(
        runtime,
        "_resolve_code_write_policies",
        lambda app_repo_root, registry: ({}, {}),
    )
    assert (
        runtime._build_agent_notes_service(
            app_repo_root=Path("/tmp"),
            registry=object(),
            project_id="unknown",
        )
        is None
    )
