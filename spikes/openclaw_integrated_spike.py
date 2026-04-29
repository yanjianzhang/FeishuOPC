#!/usr/bin/env python3
"""Deprecated: this spike previously depended on ``openclaw-sdk`` (removed).

The product runtime uses direct OpenAI-compatible HTTP from
:class:`feishu_agent.core.llm_agent_adapter.LlmAgentAdapter`.  For the old
MockGateway-style harness, see ``feishu_agent.core.llm_gateway_shim`` and
``feishu_agent/tests/test_llm_agent_adapter.py``.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "openclaw_integrated_spike.py is retired: openclaw-sdk is no longer a "
        "dependency. Use pytest (feishu_agent/tests) and the HTTP LLM path "
        "documented in README.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
