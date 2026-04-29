"""Feishu Card JSON 2.0 dict helpers.

These are small pure functions that return dict fragments matching the
Feishu Card JSON 2.0 schema. The goal is not to build a DSL — it is to
act as a typed whitelist so that leaf authors (see
``feishu_agent/presentation/messages/*``) cannot silently misspell
field names, which JSON 2.0 rejects at the server with a 500-class
error (JSON 1.0 silently ignored unknown fields; 2.0 does not).

Design rules (enforced by the test at
``feishu_agent/tests/test_cards_v2_helpers.py``):

* Every parameter name mirrors the exact Feishu 2.0 field name.
* No helper contains business logic; they only assemble dicts.
* No helper performs validation — that is the job of
  ``feishu_agent/tests/_v2_card_assertions.py`` (for tests) and the
  Feishu server (at runtime).
* No helper class, no state, no builder chain; keep this file under
  ~150 lines so the whole schema surface is readable at a glance.

Spec references:
  specs/005-feishu-message-composer/spec.md §4.4, §5bis.1, §5bis.6.
"""

from __future__ import annotations

from typing import Any, Literal

CardSchema = Literal["2.0"]
ButtonStyle = Literal["default", "primary", "danger", "text", "laser"]
HeaderTemplate = Literal[
    "blue", "wathet", "turquoise", "green", "yellow",
    "orange", "red", "carmine", "violet", "purple",
    "indigo", "grey", "default",
]


def card(
    *,
    body: dict[str, Any],
    header: dict[str, Any] | None = None,
    streaming_mode: bool = False,
    summary: str | None = None,
    update_multi: bool = True,
) -> dict[str, Any]:
    """Root card dict conforming to JSON 2.0.

    ``update_multi`` is pinned to ``True`` by Feishu in 2.0; we accept
    the kwarg for explicitness but raise if False is requested.
    """
    if update_multi is not True:
        raise ValueError(
            "JSON 2.0 only supports update_multi=True (shared cards). "
            "Pass update_multi=True explicitly or omit the kwarg."
        )

    config: dict[str, Any] = {"update_multi": True}
    if streaming_mode:
        config["streaming_mode"] = True
        if summary:
            config["summary"] = {"content": summary}
    elif summary:
        # summary without streaming_mode is ignored by Feishu but we
        # accept it so callers can construct the final frame by toggling
        # streaming_mode off without losing the summary text.
        config["summary"] = {"content": summary}

    root: dict[str, Any] = {"schema": "2.0", "config": config, "body": body}
    if header is not None:
        root["header"] = header
    return root


def body(
    *,
    elements: list[dict[str, Any]],
    padding: str = "12px",
    vertical_spacing: str = "8px",
    horizontal_spacing: str = "8px",
) -> dict[str, Any]:
    return {
        "elements": elements,
        "padding": padding,
        "vertical_spacing": vertical_spacing,
        "horizontal_spacing": horizontal_spacing,
    }


def header(
    *,
    title: str,
    subtitle: str | None = None,
    template: HeaderTemplate = "blue",
    icon: dict[str, Any] | None = None,
    padding: str = "12px",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
        "padding": padding,
    }
    if subtitle:
        out["subtitle"] = {"tag": "plain_text", "content": subtitle}
    if icon is not None:
        out["icon"] = icon
    return out


def div_markdown(
    content: str,
    *,
    element_id: str | None = None,
    text_align: Literal["left", "center", "right"] = "left",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tag": "markdown",
        "content": content,
        "text_align": text_align,
    }
    if element_id:
        out["element_id"] = element_id
    return out


def div_plain_text(
    content: str,
    *,
    element_id: str | None = None,
    text_size: str = "normal",
    text_color: str | None = None,
) -> dict[str, Any]:
    text: dict[str, Any] = {"tag": "plain_text", "content": content}
    if text_color:
        text["text_color"] = text_color
    out: dict[str, Any] = {"tag": "div", "text": text, "text_size": text_size}
    if element_id:
        out["element_id"] = element_id
    return out


def button(
    *,
    text: str,
    action_id: str,
    style: ButtonStyle = "default",
    value: dict[str, Any] | None = None,
    element_id: str | None = None,
) -> dict[str, Any]:
    """A callback button. ``action_id`` follows the 005 contract
    ``{kind}:{trace_id}:{name}`` enforced by
    ``feishu_agent/tests/_v2_card_assertions.py``.
    """
    callback_value = dict(value) if value else {}
    callback_value["action_id"] = action_id
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": style,
        "behaviors": [{"type": "callback", "value": callback_value}],
        **({"element_id": element_id} if element_id else {}),
    }


def action_row(
    *,
    actions: list[dict[str, Any]],
    horizontal_spacing: str = "8px",
) -> dict[str, Any]:
    """Horizontal row of buttons / interactive elements.

    Replaces the deprecated JSON 1.0 ``tag: "action"`` module
    (see spec §5bis.6). Uses a column_set with inline direction so all
    buttons sit in one row.
    """
    return {
        "tag": "column_set",
        "horizontal_spacing": horizontal_spacing,
        "columns": [
            {"tag": "column", "width": "auto", "elements": [btn]}
            for btn in actions
        ],
    }


def collapsible(
    *,
    title: str,
    elements: list[dict[str, Any]],
    default_expanded: bool = False,
    element_id: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tag": "collapsible_panel",
        "expanded": default_expanded,
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "padding": "4px 8px",
        },
        "elements": elements,
    }
    if element_id:
        out["element_id"] = element_id
    return out


def hr() -> dict[str, Any]:
    return {"tag": "hr"}
