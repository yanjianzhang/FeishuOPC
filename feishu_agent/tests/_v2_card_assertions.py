"""Shared Feishu Card JSON 2.0 assertions for tests.

T503c in ``specs/005-feishu-message-composer/tasks.md`` asks for a
single source of truth so every leaf test, composer integration test,
and smoke-script run can call the same check. Keeping it here (instead
of in ``feishu_agent.presentation.cards``) preserves the "helpers are
pure, no validation" rule from the helper module's docstring while
still giving tests something strong to assert against.

Import from tests only:

    from feishu_agent.tests._v2_card_assertions import assert_valid_v2_card
"""

from __future__ import annotations

import re
from typing import Any

ACTION_ID_RE = re.compile(r"^[a-z_]+:[a-zA-Z0-9_-]+:[a-z_]+$")

_ALLOWED_TOP_LEVEL = {"schema", "config", "header", "body"}
_ALLOWED_HEADER_KEYS = {"title", "subtitle", "template", "icon", "padding"}


def assert_valid_v2_card(card: dict[str, Any]) -> None:
    """Assert ``card`` is a well-formed Feishu Card JSON 2.0 dict.

    Checks:
      * ``schema == "2.0"``
      * ``config.update_multi is True``
      * ``body.elements`` is a non-empty list
      * if ``streaming_mode`` → must also carry ``summary`` content
      * any ``button`` element's callback ``action_id`` matches
        ``{kind}:{trace_id}:{name}``
      * top-level keys are whitelisted (catches typos like ``bodys``)

    Raises ``AssertionError`` with a pointer path on first mismatch.
    """
    assert isinstance(card, dict), f"card must be dict, got {type(card).__name__}"

    extra = set(card) - _ALLOWED_TOP_LEVEL
    assert not extra, f"unknown top-level keys: {sorted(extra)}"

    assert card.get("schema") == "2.0", (
        f"schema must be '2.0', got {card.get('schema')!r}"
    )

    config = card.get("config")
    assert isinstance(config, dict), "card.config must be a dict"
    assert config.get("update_multi") is True, (
        "JSON 2.0 requires config.update_multi=True"
    )

    if config.get("streaming_mode"):
        summary = config.get("summary")
        assert isinstance(summary, dict) and summary.get("content"), (
            "streaming_mode requires config.summary.content to be set"
        )

    header = card.get("header")
    if header is not None:
        assert isinstance(header, dict), "header must be a dict"
        bad = set(header) - _ALLOWED_HEADER_KEYS
        assert not bad, f"unknown header keys: {sorted(bad)}"
        title = header.get("title")
        assert (
            isinstance(title, dict)
            and title.get("tag") == "plain_text"
            and title.get("content")
        ), "header.title must be plain_text with non-empty content"

    body = card.get("body")
    assert isinstance(body, dict), "body must be a dict"
    elements = body.get("elements")
    assert isinstance(elements, list) and elements, (
        "body.elements must be a non-empty list"
    )

    for idx, element in enumerate(elements):
        _assert_element(element, path=f"body.elements[{idx}]")


def _assert_element(element: Any, *, path: str) -> None:
    assert isinstance(element, dict), f"{path} must be a dict"
    tag = element.get("tag")
    assert tag, f"{path} missing tag"

    if tag == "button":
        behaviors = element.get("behaviors") or []
        assert behaviors, f"{path} button must declare behaviors"
        callback = next(
            (b for b in behaviors if b.get("type") == "callback"), None
        )
        assert callback, f"{path} button must have a callback behavior"
        action_id = (callback.get("value") or {}).get("action_id")
        assert action_id, f"{path} button callback missing action_id"
        assert ACTION_ID_RE.match(action_id), (
            f"{path} action_id {action_id!r} does not match "
            "'{kind}:{trace_id}:{name}'"
        )

    elif tag == "column_set":
        for ci, col in enumerate(element.get("columns") or []):
            for ei, el in enumerate(col.get("elements") or []):
                _assert_element(el, path=f"{path}.columns[{ci}].elements[{ei}]")

    elif tag == "collapsible_panel":
        for ei, el in enumerate(element.get("elements") or []):
            _assert_element(el, path=f"{path}.elements[{ei}]")
