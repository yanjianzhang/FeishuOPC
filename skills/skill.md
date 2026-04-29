---
name: company
description: "Coordinate a two-manager internal operating model for this repo: a product manager orchestrator and a tech lead orchestrator backed by specialist role files. Use when the user wants manager-style triage, role-based delegation, PRD handoff, sprint planning, or progress-sync workflows modeled after OPC."
---

# Company

This skill defines a lightweight `two external managers + internal specialists` operating model for this repository.

## Managers

- `product-manager`: handles product-facing `brainstorm` and `analysis`
- `tech-lead`: handles engineering-facing `analysis`, `execute`, and `review`

Users should interact with the managers, not directly with worker roles.

## Core Rules

1. Triage first. Always choose the primary mode before doing detailed work.
2. Dispatch only `2-5` relevant roles.
3. Keep specialist outputs independent when possible.
4. Manager must challenge consensus, dedupe findings, and produce one final summary.
5. Final response should be manager-grade, not raw worker notes.

## Manager Contract

Each manager should expose the same internal execution shape:

```yaml
manager_contract:
  request:
    channel: chat|feishu|api
    user_message: string
    requested_mode: auto|string
    project_id: string
    trace_id: string|null
  triage:
    mode: string
    goal: string
    selected_roles: []
    required_inputs: []
    can_proceed: true|false
    ask_user: []
  handoff:
    manager_summary: string
    state_changes: []
    sync_request: {}
    reply_ready: true|false
```

This contract is intentionally stable so a server-side orchestrator can reuse the same semantics outside Cursor.

## Mode Map

### Product manager
- `brainstorm`: new idea, direction, tradeoff, scope discovery
- `analysis`: value check, phase planning, PRD readiness, handoff readiness

### Tech lead
- `analysis`: repo/spec assessment, sprint proposal, technical feasibility
- `execute`: planning output, progress sync, execution coordination
- `review`: report-prep review, blocker surfacing, quality/risk challenge

## Tag Mapping

To stay compatible with OPC-style role selection, internal role tags use OPC stage tags instead of custom mode names:

- manager `brainstorm` -> role tags `brainstorm`
- manager `analysis` -> role tags `plan`
- manager `execute` -> role tags `execute`
- manager `review` -> role tags `review`

This keeps the manager vocabulary business-friendly while keeping role metadata close to OPC conventions.

## Development Workflow Routing

This project uses **speckit** and **BMM (BMAD Method)** workflows for spec-driven development. Commands map to managers as follows:

### Product Manager commands

| Command | Purpose | When |
|---------|---------|------|
| `/bmad-bmm-document-project` | Generate AI-friendly project docs | New project/area with no docs |
| `/speckit.constitution` | Create/update governance principles | New project or principle changes |
| `/speckit.specify` | Create feature spec from description | New feature definition |
| `/speckit.clarify` | Refine ambiguities in a spec | Before planning |

### Tech Lead commands

| Command | Purpose | When |
|---------|---------|------|
| `/speckit.plan` | Generate technical implementation plan | After spec is ready |
| `/speckit.tasks` | Generate ordered task list from plan | After plan is ready |
| `/bmad-bmm-create-story` | Create implementation-ready story file | Ready for implementation |
| `/bmad-bmm-dev-story` | Execute story implementation end-to-end | Story file ready |
| `/speckit.analyze` | Cross-check spec/plan/tasks consistency | Before or after implementation |
| `/bmad-bmm-code-review` | Adversarial code review against story ACs | After implementation |
| `/bmad-bmm-sprint-planning` | Generate sprint status from epics | Sprint kickoff |
| `/bmad-bmm-sprint-status` | Summarize sprint status and risks | Sprint check-in |

### Workflow chains

**New project bootstrap** (PM then TL):
```
/bmad-bmm-document-project → /speckit.constitution → /speckit.plan → ...
```

**Normal development** (TL):
```
/speckit.plan → /speckit.tasks → /bmad-bmm-create-story → /bmad-bmm-dev-story → /speckit.analyze → /bmad-bmm-code-review
```

### Governance

All workflows reference `.specify/memory/constitution.md` for architecture principles. The `plan-template.md` includes an explicit constitution check gate.

## Context Sources

Prefer grounding decisions in:

- `specs/` (feature specs and plans)
- `docs/` (BMM output: planning and implementation artifacts)
- `.specify/memory/constitution.md` (governance principles)
- `project-adapters/` (downstream project adapter configs)
- downstream repo specs and artifacts (via `APP_REPO_ROOT`)
- relevant code or docs in the repo

## Role Files

Worker role definitions live in `roles/*.md`.

Every role file (and the two bot files `tech_lead.md` / `product_manager.md`)
follows the canonical XML structure documented in [_TEMPLATE.md](_TEMPLATE.md).
That template encodes the Claude / Anthropic system-prompt best practices used
in this repo — XML-partitioned sections, allowed/forbidden split, tool
enumeration rule, and few-shot examples. When adding a new role or editing an
existing one, start from the template rather than free-form Markdown.

Each role file must include:

- YAML frontmatter with `tags` and `tool_allow_list`
- `<role>` — identity
- `<capabilities>` — expertise (formerly `## Expertise`)
- `<when_to_use>` — dispatch conditions (formerly `## When to Include`)
- `<tools>` — available tools + enumeration rule
- `<mandatory_workflow>` or `<workflows>` — ordered steps
- `<output_format>` — deliverable contract
- `<forbidden_behaviors>` and/or `<anti_patterns>` — hard rules vs mindset traps

Each role should be selectable from:

- stage tags in frontmatter
- `<when_to_use>`
- a manager-generated dispatch packet containing task summary, scope, context paths, and required output

## Self-State Tools and `<system_reminder>`

Since the repo's Feishu bot runtime treats each thread as a **persistent Task**
(append-only event log under `data/tasks/<task_id>/`), every role now has
access to a small set of "self-state" tools whose only effect is to write a
structured event to that log. The runtime then projects the log into a
`TaskState` (mode / plan / todos / tool health / pending actions) and, before
each LLM turn, may inject a `<system_reminder>` user message reminding the
role of anything worth noting.

### Self-state tools (always present)

| Tool | Purpose |
|------|---------|
| `set_mode(mode, reason?)` | Switch cognitive mode. Canonical modes: `plan`, `act`. Free-form modes allowed. |
| `set_plan(title, summary?, steps[])` | Commit a structured plan document before entering act mode. |
| `update_plan_step(index, status, note?)` | Advance a step status (`pending` / `in_progress` / `done` / `blocked`). |
| `add_todo(text, id?, note?)` | Record an ad-hoc todo; returns a stable id. |
| `update_todo(id, status?, text?, note?)` | Update an existing todo. |
| `mark_todo_done(id)` | Close a todo. |
| `note(text, tags?)` | Free-form audit-only note — no behavioral effect. |

These tools **never touch the outside world**. They are strictly for making
the agent's own reasoning explicit so the reminder system can refer to it.

### How to use them

- Before a non-trivial task, call `set_mode(mode="plan")`, follow with
  `set_plan(...)`, then call `set_mode(mode="act")` to begin executing.
- Mid-session discoveries that need follow-up should become `add_todo(...)`
  items, not buried in chat — the reminder bus uses them to nudge you if they
  go stale.
- `note(...)` is for decisions / trade-offs worth citing later; it appears
  in the task log but not in `AGENT_NOTES.md`.

### `<system_reminder>` user messages

Before every LLM turn, the runtime may inject a user message whose content is
a single `<system_reminder>...</system_reminder>` block. Rules currently
implemented:

- **plan_mode** — you are in plan mode; hold off on world-effecting tools.
- **empty_plan** — you are in plan mode but haven't called `set_plan` yet.
- **stale_todos** — a todo has been open for > 5 minutes without an update.
- **tool_offline** / **tool_recovered** — a tool is down or just came back.
- **compression** — older context has been summarized; don't quote it verbatim.
- **pending_action** — a Feishu confirmation prompt is still unresolved.

Treat the reminder block as authoritative for the current turn. It is
**ephemeral** — it is not persisted to the next turn's history — so respond by
acting on it (e.g. call `update_todo` to unstick a stale todo), not by
echoing it back.

## Output Contracts

### Product manager final summary
- one-line summary
- user value
- in-scope now
- non-goals
- risks
- whether to hand off to tech lead

### Tech lead final summary
- what to do now
- sprint goal
- main tasks
- risks/blockers
- whether progress has been synced

## Current Worker Pool

Product side:
- `researcher`
- `prd_writer`
- `ux_designer`
- `spec_linker`

Engineering side:
- `repo_inspector`
- `sprint_planner`
- `progress_sync`
- `reviewer`
- `qa_tester`

## BMAD Module Wiring (what's live vs where)

The BMAD installer (`_bmad/_config/manifest.csv`) lays down five modules on disk:
`core`, `bmm`, `cis`, `bmb`, `tea`. They are wired into two different runtimes
with intentionally different surface areas. Contributors: check this map
before assuming "module X is dead code" — it usually isn't, it's just only
live in one of the two runtimes.

### Feishu bot runtime (server-side, `feishu_agent/`)

Live, callable via `read_workflow_instruction(...)` (see
`feishu_agent/tools/workflow_service.py` — `WORKFLOW_REGISTRY`):

- `_bmad/core/tasks/workflow.xml` — shared execution-engine OS loaded by
  every workflow (create-story, dev-story, code-review, ...).
- `_bmad/bmm/workflows/1-analysis/**` — PM-side: `bmad:research`,
  `bmad:create-product-brief`.
- `_bmad/bmm/workflows/2-plan-workflows/**` — PM-side: `bmad:create-ux-design`.
- `_bmad/bmm/workflows/4-implementation/**` — TL-side: `bmad:create-story`,
  `bmad:dev-story`, `bmad:code-review`, `bmad:correct-course`,
  `bmad:sprint-planning`, `bmad:sprint-status`, `bmad:retrospective`.

Not exposed to the Feishu bot (by design — the bot's flow is intentionally
narrow: shape requirement → plan → implement → review):

- `_bmad/cis/` (Creative Intelligence Suite — brainstorming, design thinking,
  innovation strategy, storytelling)
- `_bmad/bmb/` (BMad Builder — skill / agent / workflow authoring)
- `_bmad/tea/` (Test Engineering Architect — ATDD, NFR, test automation)

### Cursor / Claude Code IDE runtime (client-side, `.claude/skills/` + `.cursor/skills/`)

Live as IDE skills matched by trigger phrase. `cis`, `bmb`, `tea` modules
are fully active here:

- `.claude/skills/bmad-cis-*` — brainstorming coach, creative problem solver,
  design thinking, innovation strategy, storytelling, presentation master.
- `.claude/skills/bmad-bmb-*` — module builder, agent builder, workflow
  builder, skill maker.
- `.claude/skills/bmad-tea`, `.claude/skills/bmad-testarch-*` — test
  architecture, ATDD, CI setup, NFR assessment, trace/coverage.
- `.claude/skills/bmad-brainstorming`, `bmad-distillator`,
  `bmad-editorial-review-*`, `bmad-review-*`, `bmad-shard-doc`, etc.

The BMM slash commands (`/bmad-bmm-create-story` etc. under
`.claude/commands/` and `.cursor/commands/`) are *also* live in the IDE and
share the same `_bmad/core/tasks/workflow.xml` execution engine as the
Feishu bot — so editing a workflow updates both surfaces at once.

### How to move a module from "IDE only" to "Feishu live"

1. Pick the workflow(s) you want exposed (usually a `workflow.md` +
   `instructions.md`/`instructions.xml` pair under `_bmad/<module>/workflows/...`).
2. Add a `WorkflowDescriptor` entry in
   `feishu_agent/tools/workflow_service.py` under `WORKFLOW_REGISTRY`.
3. Add the workflow id to the invoking role's `<tools>` section in
   `skills/product_manager.md` or `skills/tech_lead.md` so the LLM knows it
   can call it.
4. If the workflow needs a new `artifact_subdir`, make sure that directory
   is listed in `_ALLOWED_READ_ROOTS` (or the artifact write path resolver).
5. Add at least one few-shot example in the role file so the LLM picks it
   up reliably in practice, not just in theory.
