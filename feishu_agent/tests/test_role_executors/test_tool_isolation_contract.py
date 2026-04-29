"""Contract test: role executors must not leak tools outside their
trust tier.

Tiers
-----
- **developer** (role_executors/developer.py) and **bug_fixer**
  (role_executors/bug_fixer.py): the ONLY role executors that may
  expose ``write_project_code*``, ``git_sync_remote``, and
  ``git_commit``. developer greenfields the story; bug_fixer remediates
  review findings. bug_fixer is a subclass of DeveloperExecutor and
  intentionally shares the same tool surface — the trust split with
  reviewer lives at the skill-doc level, not the tool-surface level.
- **tech_lead** (roles/tech_lead_executor.py — NOT in role_executors/):
  sole holder of the gatekeeper tools (``run_pre_push_inspection``,
  ``git_push``, ``create_pull_request``). Also has read-only + sync +
  commit for last-mile fixups. Not tested here because this test only
  scans role_executors/ — but ``test_developer.py`` pins the exact
  developer surface so any future mixin creep fails loudly.
- Every other specialist role (reviewer, qa_tester, …) is READ-ONLY
  against source code and has NO git / workflow mutation tools.
  (reviewer additionally gets ``read_project_code`` /
  ``list_project_paths`` / ``describe_code_write_policy`` from
  CodeWriteToolsMixin at runtime, gated by its read-only allow-set;
  those read tools aren't in this test's forbidden lists, so this
  contract still holds.)

If anyone later adds a mixin that leaks a forbidden tool into a role's
``*_TOOL_SPECS``, or onto ``developer``'s surface that isn't in
``DEVELOPER_ALLOWED``, this test fires.
"""

from __future__ import annotations

import importlib
import pkgutil

import feishu_agent.roles.role_executors as role_pkg

# Gatekeeper tools — TL-only. No role executor (including developer)
# may surface these through its module-level *_TOOL_SPECS lists.
GATEKEEPER_TOOLS: set[str] = {
    "run_pre_push_inspection",
    "git_push",
    "create_pull_request",
    "watch_pr_checks",
    # Workflow artifact mutation stays on TL as well (PM has write_file
    # scoped elsewhere; roles have nothing).
    "write_workflow_artifact",
    "write_workflow_artifacts",
}

# Code-write tools — developer-only on the role_executors side.
CODE_WRITE_TOOLS: set[str] = {
    "write_project_code",
    "write_project_code_batch",
}

# Sync+commit tools — developer-only on the role_executors side (TL
# also has them, but TL isn't scanned here).
SYNC_COMMIT_TOOLS: set[str] = {
    "git_sync_remote",
    "git_commit",
}

# Modules allowed to surface the code-write / sync-commit tools. The
# static *_TOOL_SPECS lists in developer.py / bug_fixer.py don't include
# them (they come from CodeWriteToolsMixin at runtime), so this set
# exists as a declarative checkpoint — grep-able when someone wonders
# "who can write project code".
CODE_WRITE_ALLOWED_MODULES: set[str] = {"developer", "bug_fixer"}

# Special-case: `write_file` is allowed ONLY on prd_writer (scoped to
# FeishuOPC's own specs/ dir, not to project source code).
WRITE_FILE_ALLOWED_ON = {"prd_writer.py"}


def _iter_role_spec_lists():
    """Yield (module_name, list_name, tool_names)."""
    for info in pkgutil.iter_modules(role_pkg.__path__):
        module_name = info.name
        if module_name in {"tool_handlers", "__init__"}:
            continue
        mod = importlib.import_module(
            f"feishu_agent.roles.role_executors.{module_name}"
        )
        for attr_name in dir(mod):
            if not attr_name.endswith("_TOOL_SPECS"):
                continue
            specs = getattr(mod, attr_name)
            tool_names = [s.name for s in specs]
            yield module_name, attr_name, tool_names


def test_no_role_has_gatekeeper_tools():
    """Gatekeeper tools (push / PR / pre-push inspection / workflow
    artifact writes) must never appear in any role executor's static
    spec list. TL lives outside this package."""
    violations = []
    for module_name, list_name, tool_names in _iter_role_spec_lists():
        for name in tool_names:
            if name in GATEKEEPER_TOOLS:
                violations.append(
                    f"{module_name}::{list_name} exposes gatekeeper "
                    f"tool {name!r}"
                )
    assert not violations, "\n".join(violations)


def test_code_write_tools_only_on_developer_module():
    """``write_project_code*`` may appear in static spec lists only
    inside developer.py. (developer.py itself doesn't list them
    statically — they come via CodeWriteToolsMixin at runtime — but
    guarding the module name here catches any future refactor that
    moves them into a module-level list.)"""
    violations = []
    for module_name, list_name, tool_names in _iter_role_spec_lists():
        if module_name in CODE_WRITE_ALLOWED_MODULES:
            continue
        for name in tool_names:
            if name in CODE_WRITE_TOOLS:
                violations.append(
                    f"{module_name}::{list_name} exposes code-write "
                    f"tool {name!r} — only {CODE_WRITE_ALLOWED_MODULES} "
                    f"may do that"
                )
    assert not violations, "\n".join(violations)


def test_sync_commit_tools_only_on_developer_module():
    """``git_sync_remote`` / ``git_commit`` similarly restricted on
    the role_executors side to developer.py."""
    violations = []
    for module_name, list_name, tool_names in _iter_role_spec_lists():
        if module_name in CODE_WRITE_ALLOWED_MODULES:
            continue
        for name in tool_names:
            if name in SYNC_COMMIT_TOOLS:
                violations.append(
                    f"{module_name}::{list_name} exposes sync/commit "
                    f"tool {name!r} — only {CODE_WRITE_ALLOWED_MODULES} "
                    f"may do that"
                )
    assert not violations, "\n".join(violations)


def test_write_file_only_on_prd_writer():
    """`write_file` is PM-side scoped write (specs/ dir); it must not
    appear anywhere else."""
    violations = []
    for module_name, list_name, tool_names in _iter_role_spec_lists():
        if "write_file" in tool_names:
            source_file = f"{module_name}.py"
            if source_file not in WRITE_FILE_ALLOWED_ON:
                violations.append(
                    f"{module_name}::{list_name} exposes write_file "
                    f"but is not in {WRITE_FILE_ALLOWED_ON}"
                )
    assert not violations, "\n".join(violations)


def test_advance_sprint_state_only_on_sprint_planner():
    """Only sprint_planner may mutate sprint state."""
    allowed_module = "sprint_planner"
    violations = []
    for module_name, list_name, tool_names in _iter_role_spec_lists():
        if (
            "advance_sprint_state" in tool_names
            and module_name != allowed_module
        ):
            violations.append(
                f"{module_name}::{list_name} exposes advance_sprint_state"
            )
    assert not violations, "\n".join(violations)
