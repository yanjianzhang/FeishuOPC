#!/usr/bin/env python3
"""One-shot import rewriter for the 004 Phase A-1 directory reorg.

Rewrites every `feishu_agent.services.X` module reference to its new
`feishu_agent.<core|runtime|team|tools|roles|providers>.X` location per
`specs/004-scalable-agent-foundation/A-1-directory-reorg.md`.

Safe properties:
  * Idempotent: running twice does not change files already rewritten.
  * Scope-limited: only rewrites dotted module paths that begin with
    `feishu_agent.services.`; plain English mentions elsewhere are
    untouched because we require the exact dotted prefix.
  * File types: `.py` and `.md` under each target root.
  * Patterns: both `from feishu_agent.services.X import ...` and
    `import feishu_agent.services.X [as ...]`.

Usage::

    python scripts/reorg-imports.py feishu_agent/ feishu_agent/tests/ scripts/
    python scripts/reorg-imports.py --dry-run feishu_agent/

Exit code: 0 on success. Prints one line per rewritten file plus a
summary line.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Source-of-truth: specs/004-scalable-agent-foundation/A-1-directory-reorg.md
# Ordered logically for auditability. Longest keys are applied first at
# runtime (see _sorted_items) to prevent a prefix like
# `feishu_agent.services.tools` from shadowing the more specific
# `feishu_agent.services.tools.file_write` during regex substitution.
MAP: dict[str, str] = {
    # --- core (runtime primitives) ---------------------------------------
    "feishu_agent.services.agent_types":               "feishu_agent.core.agent_types",
    "feishu_agent.services.llm_agent_adapter":         "feishu_agent.core.llm_agent_adapter",
    "feishu_agent.services.combined_executor":         "feishu_agent.core.combined_executor",
    "feishu_agent.services.cancel_token":              "feishu_agent.core.cancel_token",
    "feishu_agent.services.hook_bus":                  "feishu_agent.core.hook_bus",
    "feishu_agent.services.tool_policy":               "feishu_agent.core.tool_policy",
    "feishu_agent.services.context_compression":       "feishu_agent.core.context_compression",
    "feishu_agent.services.session_lineage":           "feishu_agent.core.session_lineage",
    "feishu_agent.services.request_context":           "feishu_agent.core.request_context",

    # --- runtime (wire protocols & process lifecycle) --------------------
    "feishu_agent.services.feishu_runtime_service":    "feishu_agent.runtime.feishu_runtime_service",
    "feishu_agent.services.managed_feishu_client":     "feishu_agent.runtime.managed_feishu_client",
    "feishu_agent.services.message_deduper":           "feishu_agent.runtime.message_deduper",
    "feishu_agent.services.llm_runtime_service":       "feishu_agent.runtime.llm_runtime_service",
    "feishu_agent.services.oauth_callback_server":     "feishu_agent.runtime.oauth_callback_server",
    "feishu_agent.services.impersonation_token_service": "feishu_agent.runtime.impersonation_token_service",

    # --- team (cross-agent coordination & persistence) -------------------
    "feishu_agent.services.pending_action_service":    "feishu_agent.team.pending_action_service",
    "feishu_agent.services.audit_service":             "feishu_agent.team.audit_service",
    "feishu_agent.services.task_event_log":            "feishu_agent.team.task_event_log",
    "feishu_agent.services.task_event_projector":      "feishu_agent.team.task_event_projector",
    "feishu_agent.services.task_replay":               "feishu_agent.team.task_replay",
    "feishu_agent.services.task_service":              "feishu_agent.team.task_service",
    "feishu_agent.services.task_state":                "feishu_agent.team.task_state",
    "feishu_agent.services.task_state_executor":       "feishu_agent.team.task_state_executor",
    "feishu_agent.services.sprint_state_service":      "feishu_agent.team.sprint_state_service",
    "feishu_agent.services.session_summary_service":   "feishu_agent.team.session_summary_service",
    "feishu_agent.services.memory_assembler":          "feishu_agent.team.memory_assembler",
    "feishu_agent.services.memory_writer":             "feishu_agent.team.memory_writer",
    "feishu_agent.services.last_run_memory_service":   "feishu_agent.team.last_run_memory_service",
    "feishu_agent.services.agent_notes_service":       "feishu_agent.team.agent_notes_service",
    "feishu_agent.services.reminder_bus":              "feishu_agent.team.reminder_bus",
    "feishu_agent.services.role_artifact_writer":      "feishu_agent.team.role_artifact_writer",
    "feishu_agent.services.artifact_publish_service":  "feishu_agent.team.artifact_publish_service",
    "feishu_agent.services.tier2_wiring":              "feishu_agent.team.tier2_wiring",

    # --- tools (tool implementations & registries) -----------------------
    "feishu_agent.services.tool_registry":             "feishu_agent.tools.tool_registry",
    "feishu_agent.services.tool_verification":         "feishu_agent.tools.tool_verification",
    "feishu_agent.services.mcp_tool_adapter":          "feishu_agent.tools.mcp_tool_adapter",
    "feishu_agent.services.feishu_agent_tools":        "feishu_agent.tools.feishu_agent_tools",
    "feishu_agent.services.workflow_tools":            "feishu_agent.tools.workflow_tools",
    "feishu_agent.services.workflow_service":          "feishu_agent.tools.workflow_service",
    "feishu_agent.services.code_write_tools":          "feishu_agent.tools.code_write_tools",
    "feishu_agent.services.code_write_service":        "feishu_agent.tools.code_write_service",
    "feishu_agent.services.secret_scanner":            "feishu_agent.tools.secret_scanner",
    "feishu_agent.services.speckit_script_service":    "feishu_agent.tools.speckit_script_service",
    "feishu_agent.services.git_ops_service":           "feishu_agent.tools.git_ops_service",
    "feishu_agent.services.git_sync_preflight":        "feishu_agent.tools.git_sync_preflight",
    "feishu_agent.services.pre_push_inspector":        "feishu_agent.tools.pre_push_inspector",
    "feishu_agent.services.pull_request_service":      "feishu_agent.tools.pull_request_service",
    "feishu_agent.services.deploy_service":            "feishu_agent.tools.deploy_service",
    "feishu_agent.services.ci_watch_service":          "feishu_agent.tools.ci_watch_service",
    "feishu_agent.services.progress_sync_service":     "feishu_agent.tools.progress_sync_service",
    "feishu_agent.services.project_registry":          "feishu_agent.tools.project_registry",
    # services/tools subdir -> tools/legacy_tools (renamed to avoid name clash
    # with the new bundle registry; will be dissolved during A-2).
    "feishu_agent.services.tools.file_write":          "feishu_agent.tools.legacy_tools.file_write",
    "feishu_agent.services.tools.self_state":          "feishu_agent.tools.legacy_tools.self_state",
    "feishu_agent.services.tools":                     "feishu_agent.tools.legacy_tools",

    # --- roles (role-specific executors) ---------------------------------
    "feishu_agent.services.role_registry_service":     "feishu_agent.roles.role_registry_service",
    "feishu_agent.services.tech_lead_executor":        "feishu_agent.roles.tech_lead_executor",
    "feishu_agent.services.pm_executor":               "feishu_agent.roles.pm_executor",
    "feishu_agent.services.role_executors.developer":      "feishu_agent.roles.role_executors.developer",
    "feishu_agent.services.role_executors.bug_fixer":      "feishu_agent.roles.role_executors.bug_fixer",
    "feishu_agent.services.role_executors.deploy_engineer": "feishu_agent.roles.role_executors.deploy_engineer",
    "feishu_agent.services.role_executors.progress_sync":  "feishu_agent.roles.role_executors.progress_sync",
    "feishu_agent.services.role_executors.reviewer":       "feishu_agent.roles.role_executors.reviewer",
    # Deleted in A-2 but still moved in A-1 for clean blame (see A-1 doc).
    "feishu_agent.services.role_executors.repo_inspector": "feishu_agent.roles.role_executors.repo_inspector",
    "feishu_agent.services.role_executors.researcher":     "feishu_agent.roles.role_executors.researcher",
    "feishu_agent.services.role_executors.spec_linker":    "feishu_agent.roles.role_executors.spec_linker",
    "feishu_agent.services.role_executors.ux_designer":    "feishu_agent.roles.role_executors.ux_designer",
    "feishu_agent.services.role_executors.qa_tester":      "feishu_agent.roles.role_executors.qa_tester",
    "feishu_agent.services.role_executors.prd_writer":     "feishu_agent.roles.role_executors.prd_writer",
    "feishu_agent.services.role_executors.sprint_planner": "feishu_agent.roles.role_executors.sprint_planner",
    "feishu_agent.services.role_executors.tool_handlers":  "feishu_agent.roles.role_executors.tool_handlers",
    "feishu_agent.services.role_executors":                "feishu_agent.roles.role_executors",

    # --- providers (external LLM/service transports) ---------------------
    "feishu_agent.services.bedrock_transport":         "feishu_agent.providers.bedrock_transport",
    "feishu_agent.services.llm_provider_pool":         "feishu_agent.providers.llm_provider_pool",
}

# File extensions we rewrite. .md is included because skill/role markdown
# frontmatter and spec documents often embed dotted module paths.
REWRITE_SUFFIXES = {".py", ".md"}

# Paths (relative to any scan root) we never rewrite.
SKIP_DIR_NAMES = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".worktrees",
}


def _sorted_items(mapping: dict[str, str]) -> list[tuple[str, str]]:
    """Return mapping items sorted by key length descending.

    Longest-prefix-first ordering ensures that a fully qualified path such
    as `feishu_agent.services.role_executors.developer` is rewritten
    before the shorter `feishu_agent.services.role_executors` prefix has
    a chance to partially match it (which would leave a broken mixed
    path like `feishu_agent.roles.role_executors.developer` re-rewritten
    on subsequent iterations).
    """
    return sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)


_COMPILED: list[tuple[re.Pattern[str], str]] = [
    # Match `\bPREFIX\b` but only before a word boundary so we do not
    # partially rewrite longer paths. Using `\b` here keeps the leading
    # context (such as the preceding `from ` or `import `) intact.
    (re.compile(rf"\b{re.escape(old)}\b"), new)
    for old, new in _sorted_items(MAP)
]


def _path_variants(mapping: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    """Compile path-form variants of the module mapping.

    Doc files and shell scripts routinely reference modules as
    forward-slash paths (e.g. `feishu_agent/services/agent_types.py`).
    We rewrite both the bare directory form and the `.py` form so the
    text mirrors the dotted rewrite.
    """
    out: list[tuple[re.Pattern[str], str]] = []
    for old, new in _sorted_items(mapping):
        old_path = old.replace(".", "/")
        new_path = new.replace(".", "/")
        out.append((re.compile(rf"(?<![\w]){re.escape(old_path)}(?![\w.])"), new_path))
        out.append((re.compile(rf"(?<![\w]){re.escape(old_path)}\.py\b"), f"{new_path}.py"))
    return out


_COMPILED_PATHS: list[tuple[re.Pattern[str], str]] = _path_variants(MAP)


def _build_submodule_package_map(mapping: dict[str, str]) -> dict[str, str]:
    """Derive `{submodule_name: new_package}` for the
    `from feishu_agent.services import <name>` form.

    Only considers top-level mappings whose key matches
    `feishu_agent.services.<single_segment>` — nested targets
    (e.g. role_executors/developer.py) are already handled by the
    dotted-path rewriter and must not collide with the submodule
    import form.
    """
    out: dict[str, str] = {}
    for old, new in mapping.items():
        old_parts = old.split(".")
        new_parts = new.split(".")
        if len(old_parts) != 3:
            continue
        if old_parts[:2] != ["feishu_agent", "services"]:
            continue
        name = old_parts[2]
        new_pkg = ".".join(new_parts[:-1])
        out[name] = new_pkg
    return out


_SUBMODULE_PKG = _build_submodule_package_map(MAP)


def _rewrite_submodule_imports(text: str) -> tuple[str, int]:
    """Rewrite `from feishu_agent.services import NAME[, ...]` lines.

    Handles comma-separated single-line imports and preserves any
    trailing `as ALIAS`. Emits one rewritten `from` line per imported
    name when the imports cross package boundaries (the common case
    post-reorg).
    """
    pattern = re.compile(
        r"^(?P<indent>[ \t]*)from[ \t]+feishu_agent\.services[ \t]+import[ \t]+"
        r"(?P<names>[A-Za-z0-9_., \t]+?)(?P<trailing>[ \t]*#.*)?$",
        re.MULTILINE,
    )
    subs = 0

    def _split_names(names: str) -> list[tuple[str, str | None]]:
        parsed: list[tuple[str, str | None]] = []
        for raw in names.split(","):
            token = raw.strip()
            if not token:
                continue
            parts = token.split()
            if len(parts) == 1:
                parsed.append((parts[0], None))
            elif len(parts) == 3 and parts[1] == "as":
                parsed.append((parts[0], parts[2]))
            else:
                parsed.append((token, None))
        return parsed

    def _replace(match: re.Match[str]) -> str:
        nonlocal subs
        indent = match.group("indent")
        trailing = match.group("trailing") or ""
        items = _split_names(match.group("names"))
        lines: list[str] = []
        for name, alias in items:
            pkg = _SUBMODULE_PKG.get(name)
            if pkg is None:
                target = "feishu_agent.services"
            else:
                target = pkg
            suffix = f" as {alias}" if alias else ""
            lines.append(f"{indent}from {target} import {name}{suffix}")
        if trailing:
            lines[-1] = f"{lines[-1]}{trailing}"
        subs += sum(1 for name, _ in items if _SUBMODULE_PKG.get(name) is not None)
        return "\n".join(lines)

    new_text = pattern.sub(_replace, text)
    return new_text, subs


def rewrite_text(text: str) -> tuple[str, int]:
    """Apply the rewrite table to ``text``.

    Returns the possibly-modified text and the number of substitutions
    made across all mappings.
    """
    new = text
    total = 0
    # Pass 1: `from feishu_agent.services import X` forms are replaced
    # per-submodule. Run before the dotted-path pass so that the
    # submodule rewrite sees the original text.
    new, count = _rewrite_submodule_imports(new)
    total += count
    # Pass 2: dotted-path rewrites for all other uses.
    for pattern, replacement in _COMPILED:
        new, count = pattern.subn(replacement, new)
        total += count
    # Pass 3: path-form rewrites (docs/shell/markdown prose references).
    for pattern, replacement in _COMPILED_PATHS:
        new, count = pattern.subn(replacement, new)
        total += count
    return new, total


_SELF_PATH = Path(__file__).resolve()


def _is_self(path: Path) -> bool:
    """The rewriter must not touch its own source — doing so turns the
    MAP dict's keys into their replacements and silently breaks the
    tool. We also protect the canonical spec files that carry the same
    mapping table for audit purposes.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved == _SELF_PATH:
        return True
    # Guard the entire 004 spec folder. Every file there either documents
    # the mapping table itself or cross-references it for audit / ADR
    # purposes, so the rewriter must leave the pre-reorg paths intact.
    parts = resolved.parts
    if "specs" in parts and "004-scalable-agent-foundation" in parts:
        return True
    return False


def iter_target_files(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            print(f"warn: {root} does not exist, skipping", file=sys.stderr)
            continue
        if root.is_file():
            if root.suffix in REWRITE_SUFFIXES and not _is_self(root):
                out.append(root)
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in REWRITE_SUFFIXES:
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if _is_self(path):
                continue
            out.append(path)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite feishu_agent.services.* imports to the 004 layout."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["feishu_agent"],
        help="Files or directories to scan (default: feishu_agent).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report would-be changes without writing files.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file output; only print the summary line.",
    )
    args = parser.parse_args(argv)

    roots = [Path(p) for p in args.paths]
    files = iter_target_files(roots)

    changed_files = 0
    total_subs = 0
    for path in files:
        try:
            original = path.read_text()
        except UnicodeDecodeError:
            if not args.quiet:
                print(f"skip (binary): {path}", file=sys.stderr)
            continue
        new_text, n = rewrite_text(original)
        if n == 0 or new_text == original:
            continue
        total_subs += n
        changed_files += 1
        if not args.dry_run:
            path.write_text(new_text)
        if not args.quiet:
            verb = "would rewrite" if args.dry_run else "rewrote"
            print(f"{verb} {path} ({n} subs)")

    print(
        f"{'(dry-run) ' if args.dry_run else ''}"
        f"files touched: {changed_files}, total substitutions: {total_subs}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
