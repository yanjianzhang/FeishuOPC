"""CLI-arg parsing tests for ``scripts/dev_send_test_card.py`` (T519).

No network — we only hit ``_parse_args`` and the card-builder helpers.
The manual flow (real API / real chat) lives behind T520.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from feishu_agent.tests._v2_card_assertions import assert_valid_v2_card


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dev_send_test_card.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("dev_send_test_card", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def test_parse_args_requires_bot_and_chat_id(script):
    with pytest.raises(SystemExit):
        script._parse_args([])
    with pytest.raises(SystemExit):
        script._parse_args(["--bot", "tech_lead"])
    with pytest.raises(SystemExit):
        script._parse_args(["--chat-id", "oc_x"])


def test_parse_args_defaults_to_baseline(script):
    ns = script._parse_args(["--bot", "tech_lead", "--chat-id", "oc_x"])
    assert ns.bot == "tech_lead"
    assert ns.chat_id == "oc_x"
    assert ns.update is False
    assert ns.streaming is False
    assert ns.reply is False


def test_mode_flags_mutually_exclusive(script):
    for pair in [("--update", "--reply"), ("--update", "--streaming"), ("--reply", "--streaming")]:
        with pytest.raises(SystemExit):
            script._parse_args(["--bot", "tl", "--chat-id", "c", *pair])


@pytest.mark.parametrize(
    "flag,attr",
    [("--update", "update"), ("--streaming", "streaming"), ("--reply", "reply")],
)
def test_each_mode_flag(script, flag, attr):
    ns = script._parse_args(["--bot", "tl", "--chat-id", "c", flag])
    assert getattr(ns, attr) is True


def test_baseline_card_passes_validator(script):
    card = script._build_baseline_card()
    assert_valid_v2_card(card)
    assert card["config"].get("streaming_mode") in (None, False)


def test_streaming_card_has_summary_and_streaming_mode(script):
    card = script._build_baseline_card(streaming=True)
    assert_valid_v2_card(card)
    assert card["config"]["streaming_mode"] is True
    assert card["config"]["summary"]["content"] == "生成中…"


def test_updated_card_passes_validator(script):
    assert_valid_v2_card(script._build_updated_card())


def test_streaming_final_card_passes_validator(script):
    card = script._build_streaming_final_card()
    assert_valid_v2_card(card)
    assert "streaming_mode" not in card["config"]


def test_reply_card_passes_validator(script):
    assert_valid_v2_card(script._build_reply_card())


def test_smoke_action_ids_match_format(script):
    """The smoke card's two buttons must themselves be valid action_ids
    so that the M0-C logged action_value doesn't look malformed."""
    card = script._build_baseline_card()
    row = card["body"]["elements"][-1]  # action_row is last
    buttons = [col["elements"][0] for col in row["columns"]]
    action_ids = [b["behaviors"][0]["value"]["action_id"] for b in buttons]
    assert action_ids == ["smoke:dev:confirm", "smoke:dev:cancel"]
