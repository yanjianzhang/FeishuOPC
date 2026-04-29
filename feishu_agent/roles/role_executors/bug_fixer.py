"""Bug-fixer role executor.

Purpose
-------
``bug_fixer`` is what the tech lead dispatches when the reviewer
returns ``verdict: blocked`` on a freshly-implemented story.
Mechanically it is ``DeveloperExecutor`` under a different name —
same tool surface (read/write project code, sync, commit, role
artifact), same trust filter (no push, no PR, no inspection). The
distinction lives in the *skill doc* and the *role registry entry*:

- ``developer`` greenfields the feature per the story.
- ``bug_fixer`` only touches what the review artifact says is broken;
  anything outside that scope is a discipline violation (spelled out
  in ``skills/roles/bug_fixer.md``).

Keeping it as a subclass (not just re-registering DeveloperExecutor
under two names) gives us:

- A distinct module path so ``test_tool_isolation_contract`` can
  enumerate "which modules may hold code-write" without surprise.
- A grep-able anchor — when someone asks "who can fix a review
  finding?", the answer is ``bug_fixer.py``.
- Room for future divergence (tighter size cap, forced "must cite
  review_artifact path in reason", etc.) without touching the
  greenfield developer path.
"""

from __future__ import annotations

from typing import Any

from feishu_agent.roles.role_executors.developer import DeveloperExecutor


class BugFixerExecutor(DeveloperExecutor):
    """Identical to ``DeveloperExecutor`` today, different role label.

    See module docstring for why it's a subclass instead of an alias.
    """

    def __init__(
        self,
        *,
        role_name: str = "bug_fixer",
        **kwargs: Any,
    ) -> None:
        super().__init__(role_name=role_name, **kwargs)
