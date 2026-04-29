"""Leaf formatter modules.

Each submodule (landing incrementally across M1-B/F/G and M2) defines
one `LeafFormatter` implementation and registers it into the module
singleton ``LEAF_REGISTRY`` on import.

M1-A only provides the base protocol + registry; concrete leaves ship
in M1-B (generic_text / error_card / final_answer_card), M1-F
(tool_use_group), and M1-G (pending_action_card).
"""

from feishu_agent.presentation.messages._base import (
    LEAF_REGISTRY,
    LeafFormatter,
    LeafRegistry,
)

__all__ = ["LEAF_REGISTRY", "LeafFormatter", "LeafRegistry"]
