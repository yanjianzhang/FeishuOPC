"""Module-scan entry point for M3 decorator-registered tools.

Every submodule under ``feishu_agent.tools.legacy_tools`` that applies
:func:`feishu_agent.tools.tool_registry.tool` decorators is
auto-discovered by the runtime via
``autodiscover(["feishu_agent.tools.legacy_tools"])``. Dropping a new
file in this directory is enough to expose a new tool to the LLM;
no central registry edit required.

Organize tools by topic, one module per topic. The ``self_state``
module owns the agent's self-mutation suite (set_mode / set_plan /
todos / note). Future ``git``, ``workflow``, ``feishu`` submodules
will replace the corresponding mixins in the same way.
"""

from __future__ import annotations

# Re-exports are intentionally empty — discovery happens via
# autodiscover(), not ``from feishu_agent.tools.legacy_tools import *``.

__all__: list[str] = []
