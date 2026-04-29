"""Unit tests for ``feishu_agent.presentation.cards.v2`` (T503b).

Every helper is exercised by at least one test and — more importantly
— every card we can build by composing helpers must pass
``assert_valid_v2_card`` from :mod:`feishu_agent.tests._v2_card_assertions`.

If a helper parameter name changes, these tests break on purpose:
the helper surface is our whitelist, so renames must be deliberate.
"""

from __future__ import annotations

import pytest

from feishu_agent.presentation.cards import v2
from feishu_agent.tests._v2_card_assertions import (
    ACTION_ID_RE,
    assert_valid_v2_card,
)


def test_card_schema_defaults():
    c = v2.card(body=v2.body(elements=[v2.div_markdown("hi")]))
    assert c["schema"] == "2.0"
    assert c["config"] == {"update_multi": True}
    assert "header" not in c
    assert_valid_v2_card(c)


def test_card_rejects_update_multi_false():
    with pytest.raises(ValueError, match="update_multi=True"):
        v2.card(
            body=v2.body(elements=[v2.div_markdown("hi")]),
            update_multi=False,
        )


def test_card_with_header_and_streaming_summary():
    c = v2.card(
        body=v2.body(elements=[v2.div_markdown("executing…")]),
        header=v2.header(title="Title", subtitle="sub", template="blue"),
        streaming_mode=True,
        summary="Executing tools…",
    )
    assert c["config"]["streaming_mode"] is True
    assert c["config"]["summary"]["content"] == "Executing tools…"
    assert c["header"]["subtitle"]["content"] == "sub"
    assert_valid_v2_card(c)


def test_card_non_streaming_summary_preserved():
    c = v2.card(
        body=v2.body(elements=[v2.div_markdown("done")]),
        summary="Executing tools…",
    )
    assert c["config"]["summary"]["content"] == "Executing tools…"
    assert "streaming_mode" not in c["config"]


def test_body_spacing_overrides():
    b = v2.body(
        elements=[v2.div_markdown("x")],
        padding="16px",
        vertical_spacing="12px",
        horizontal_spacing="4px",
    )
    assert b["padding"] == "16px"
    assert b["vertical_spacing"] == "12px"
    assert b["horizontal_spacing"] == "4px"


def test_header_title_subtitle_icon():
    h = v2.header(title="T", subtitle="S", icon={"tag": "standard_icon", "token": "info"})
    assert h["title"]["tag"] == "plain_text"
    assert h["subtitle"]["content"] == "S"
    assert h["icon"]["token"] == "info"
    assert h["template"] == "blue"


def test_div_markdown_and_plain_text_element_ids():
    md = v2.div_markdown("**hi**", element_id="md-1")
    pt = v2.div_plain_text("hi", element_id="pt-1", text_color="grey")
    assert md["element_id"] == "md-1"
    assert md["tag"] == "markdown"
    assert pt["element_id"] == "pt-1"
    assert pt["text"]["text_color"] == "grey"


def test_button_action_id_format():
    btn = v2.button(
        text="Confirm",
        action_id="pending_action:abc-123:confirm",
        style="primary",
        value={"extra": "ctx"},
    )
    callback = btn["behaviors"][0]
    assert callback["type"] == "callback"
    assert callback["value"]["action_id"] == "pending_action:abc-123:confirm"
    assert callback["value"]["extra"] == "ctx"
    assert ACTION_ID_RE.match(callback["value"]["action_id"])


def test_action_row_wraps_buttons_in_columns():
    row = v2.action_row(actions=[
        v2.button(text="Yes", action_id="pending_action:t1:confirm", style="primary"),
        v2.button(text="No", action_id="pending_action:t1:cancel"),
    ])
    assert row["tag"] == "column_set"
    assert len(row["columns"]) == 2
    assert row["columns"][0]["elements"][0]["tag"] == "button"


def test_collapsible_wraps_elements():
    panel = v2.collapsible(
        title="Details",
        elements=[v2.div_plain_text("line 1"), v2.div_plain_text("line 2")],
        default_expanded=False,
    )
    assert panel["tag"] == "collapsible_panel"
    assert panel["expanded"] is False
    assert len(panel["elements"]) == 2


def test_hr_stable():
    assert v2.hr() == {"tag": "hr"}


def test_composed_card_with_header_action_row_collapsible_passes_validator():
    card = v2.card(
        header=v2.header(title="执行过程 (2/3)", template="blue"),
        body=v2.body(elements=[
            v2.div_markdown("**执行中…**"),
            v2.collapsible(
                title="查看明细 (3 步)",
                elements=[
                    v2.div_plain_text("• ls — finished (120ms)"),
                    v2.div_plain_text("• cat — finished (45ms)"),
                    v2.div_plain_text("• git diff — running…"),
                ],
            ),
            v2.hr(),
            v2.action_row(actions=[
                v2.button(
                    text="确认",
                    action_id="pending_action:trace-xyz:confirm",
                    style="primary",
                ),
                v2.button(
                    text="取消",
                    action_id="pending_action:trace-xyz:cancel",
                ),
            ]),
        ]),
        streaming_mode=True,
        summary="Executing…",
    )
    assert_valid_v2_card(card)
