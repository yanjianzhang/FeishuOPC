"""Config-loader tests for ``fallback_model_source`` wiring.

Covers the translation from ``.larkagent/secrets/ai_key/model_sources.json``
to the ``llm_secondary_*`` settings consumed by
``_build_provider_pool``. We only exercise the loader in isolation
here — the runtime wiring is covered by
``test_harness_integration.py::test_provider_pool_builds_bedrock_secondary_when_transport_set``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_agent.config import _load_agent_llm_settings

# Env vars the loader reads out of ``.env``. ``_load_secret_file`` is
# intentionally defensive: it won't let ``.env`` override a real
# ``os.environ`` entry, which means a developer with any of these
# exported in their shell would see the loader return empty-handed
# and fail every test below. Clearing them in a fixture keeps the
# tests deterministic without leaking back into the shell.
_RELEVANT_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "VOLCENGINE_ARK_API_KEY",
    "ANTHROPIC_MODEL",
    "AWS_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "OPENCLAW_DEFAULT_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
    for key in _RELEVANT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _write_ai_key_dir(root: Path, *, env: dict[str, str], config: dict) -> None:
    ai_dir = root / ".larkagent" / "secrets" / "ai_key"
    ai_dir.mkdir(parents=True)
    (ai_dir / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in env.items()) + "\n",
        encoding="utf-8",
    )
    (ai_dir / "model_sources.json").write_text(
        json.dumps(config), encoding="utf-8"
    )


def test_fallback_aws_bedrock_populates_secondary_settings(tmp_path: Path):
    _write_ai_key_dir(
        tmp_path,
        env={
            "OPENAI_COMPATIBLE_API_KEY": "primary-key",
            "AWS_REGION": "ap-northeast-1",
            "AWS_ACCESS_KEY_ID": "AKIA_TEST",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "ANTHROPIC_MODEL": "arn:aws:bedrock:ap-northeast-1:0:application-inference-profile/xyz",
        },
        config={
            "model_source": "openai_compatible",
            "fallback_model_source": "aws_bedrock",
            "openai_compatible": {
                "url": "https://primary.example.com/v1",
                "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
                "model": "primary-model",
            },
            "aws_bedrock": {
                "region_env": "AWS_REGION",
                "access_key_id_env": "AWS_ACCESS_KEY_ID",
                "secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
                "model_id_env": "ANTHROPIC_MODEL",
                "model_id": "",
            },
        },
    )
    mapped = _load_agent_llm_settings(tmp_path)

    assert mapped["llm_secondary_transport"] == "anthropic_bedrock"
    assert mapped["llm_secondary_model"].startswith("arn:aws:bedrock:")
    assert mapped["llm_secondary_aws_region"] == "ap-northeast-1"
    assert mapped["llm_secondary_aws_access_key_id"] == "AKIA_TEST"
    assert mapped["llm_secondary_aws_secret_access_key"] == "secret"
    # Primary is still resolved normally
    assert mapped["techbot_llm_base_url"] == "https://primary.example.com/v1"
    assert mapped["techbot_llm_api_key"] == "primary-key"


def test_fallback_aws_bedrock_skipped_when_aws_env_missing(tmp_path: Path):
    """If any AWS env var is missing, the loader must refuse to populate
    the secondary settings — a half-wired Bedrock fallback would pass
    ``_build_provider_pool``'s presence check only to 400 on first call."""
    _write_ai_key_dir(
        tmp_path,
        env={
            "OPENAI_COMPATIBLE_API_KEY": "primary-key",
            # AWS_REGION deliberately absent
            "AWS_ACCESS_KEY_ID": "AKIA_TEST",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "ANTHROPIC_MODEL": "arn:aws:bedrock:x",
        },
        config={
            "model_source": "openai_compatible",
            "fallback_model_source": "aws_bedrock",
            "openai_compatible": {
                "url": "https://p.example.com/v1",
                "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
                "model": "primary-model",
            },
            "aws_bedrock": {
                "region_env": "AWS_REGION",
                "access_key_id_env": "AWS_ACCESS_KEY_ID",
                "secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
                "model_id_env": "ANTHROPIC_MODEL",
                "model_id": "",
            },
        },
    )
    mapped = _load_agent_llm_settings(tmp_path)
    assert "llm_secondary_transport" not in mapped
    assert "llm_secondary_aws_region" not in mapped


def test_fallback_volcengine_ark_populates_openai_http_secondary(tmp_path: Path):
    """``volcengine_ark`` is OpenAI-compatible; the loader should map it
    onto the HTTP secondary fields, leaving ``llm_secondary_transport``
    at ``openai_http``."""
    _write_ai_key_dir(
        tmp_path,
        env={
            "OPENAI_COMPATIBLE_API_KEY": "primary-key",
            "VOLCENGINE_ARK_API_KEY": "ark-key",
        },
        config={
            "model_source": "openai_compatible",
            "fallback_model_source": "volcengine_ark",
            "openai_compatible": {
                "url": "https://p.example.com/v1",
                "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
                "model": "primary-model",
            },
            "volcengine_ark": {
                "base_url": "https://ark.example.com/v3",
                "api_key_env": "VOLCENGINE_ARK_API_KEY",
                "model": "ark-model",
            },
        },
    )
    mapped = _load_agent_llm_settings(tmp_path)
    assert mapped["llm_secondary_transport"] == "openai_http"
    assert mapped["llm_secondary_base_url"] == "https://ark.example.com/v3"
    assert mapped["llm_secondary_api_key"] == "ark-key"
    assert mapped["llm_secondary_model"] == "ark-model"


def test_no_fallback_declared_leaves_secondary_unset(tmp_path: Path):
    _write_ai_key_dir(
        tmp_path,
        env={"OPENAI_COMPATIBLE_API_KEY": "primary-key"},
        config={
            "model_source": "openai_compatible",
            "openai_compatible": {
                "url": "https://p.example.com/v1",
                "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
                "model": "primary-model",
            },
        },
    )
    mapped = _load_agent_llm_settings(tmp_path)
    for key in (
        "llm_secondary_transport",
        "llm_secondary_base_url",
        "llm_secondary_api_key",
        "llm_secondary_model",
        "llm_secondary_aws_region",
    ):
        assert key not in mapped
