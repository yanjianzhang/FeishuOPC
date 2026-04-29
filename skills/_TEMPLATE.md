---
# This is the canonical structure every skill file in this repo should follow.
# Copy this file, rename it, fill in the sections, and delete the sections you
# don't need.
#
# Runtime behavior:
# - Files under `skills/roles/*.md` are parsed by RoleRegistryService; the YAML
#   frontmatter block (tags, tool_allow_list, model) is extracted, the rest of
#   the file body becomes the role's system_prompt verbatim.
# - Files directly under `skills/` (tech_lead.md, product_manager.md) are loaded
#   by FeishuRuntimeService._load_system_prompt() as-is — no frontmatter is
#   parsed, the whole file content becomes the bot's system prompt. Those two
#   files DO NOT have frontmatter today; don't add one unless you also change
#   the runtime.
# - This _TEMPLATE.md file itself is never auto-loaded: bot loader hardcodes the
#   two filenames above, and role registry only globs skills/roles/*.md.
#
# This file uses a YAML frontmatter block with only comment lines so a human can
# read this guidance but the file is still a valid role-shaped skeleton.
tags: []
tool_allow_list: []
---

<!--
  Skill file conventions (Claude / Anthropic system-prompt best practice, v1).

  Keep the ordering below. Claude weighs top-of-prompt and bottom-of-prompt
  segments more heavily than mid-prompt ones, so:
    - <role> sits at the top (identity is the strongest anchor)
    - <forbidden_behaviors> and <anti_patterns> sit at the bottom (last-place
      bias reinforces hard no-nos)
    - <workflows> and <examples> sit in the middle where context-heavy content
      lives

  Every section below is optional EXCEPT <role>. Delete what you don't need.
  Do not invent new top-level tags without updating this template.
-->

<role>
  One paragraph. Identity + tone + output language.
  Example: "You are the Reviewer role. You find hidden blockers in a developer's
  implementation. Reply in English."
</role>

<context>
  <!--
    For bot files: describe the three system-injected blocks appended by the
    runtime (repo baseline, last_run_context, AGENT_NOTES) so the model knows
    they're trustworthy ground-truth.
    For role files: one sentence is enough ("You are dispatched by the tech
    lead with a task + acceptance criteria in the dispatch packet.").
    Omit entirely if neither applies.
  -->
  <system_reminder>
    <!--
      BOILERPLATE — keep verbatim unless the role has a very specific reason.
      The runtime stores every Feishu thread as a persistent Task and injects
      a `<system_reminder>…</system_reminder>` user message before each LLM
      turn, summarizing what needs attention (stale todos / plan-mode
      violations / recovered tools / pending confirmations / compression).
      Treat that block as authoritative for the current turn and ephemeral:
      act on it (e.g. update_todo / mark_todo_done) rather than echo it.
    -->
    The runtime may inject a `<system_reminder>` user message before each
    turn. It is authoritative for this turn and NOT persisted to the next;
    respond by acting on it with self-state tools, not by quoting it back.
  </system_reminder>

  <self_state>
    <!--
      BOILERPLATE — keep verbatim unless the role literally never runs in
      plan/act mode (rare). These tools only write to the task event log;
      they never touch files, git, or Feishu.
    -->
    Self-state tools available to every role: `set_mode`, `set_plan`,
    `update_plan_step`, `add_todo`, `update_todo`, `mark_todo_done`, `note`.
    Use them to make your reasoning explicit so the reminder bus can nudge
    you (stale_todos, empty_plan, plan_mode, compression, pending_action).
  </self_state>
</context>

<capabilities>
  <!-- 3-6 bullets describing what this agent is good at. Keep it declarative. -->
  - ...
</capabilities>

<when_to_use>
  <!--
    For bot files: the intent categories the bot handles (implement / plan /
    status / review).
    For role files: the dispatch conditions the manager uses to pick this role.
  -->
  - ...
</when_to_use>

<tools>
  <available>
    <!--
      Every tool the agent can call. Use format "tool_name — one-line purpose".
      When the user asks "what tools do you have", the agent is required (by
      <enumeration_rule> below) to reproduce this list verbatim.
    -->
    - tool_name — purpose
  </available>

  <disabled>
    <!--
      Only include this block if the agent has NEARBY tools it must not call
      (e.g. tech lead has write_project_code disabled because developer owns
      that). Keep it short; one line per disabled tool. Do NOT repeat these in
      other sections — that's what caused the tech_lead hallucination.
    -->
    - tool_name — why disabled + who should be called instead
  </disabled>

  <enumeration_rule>
    When the user asks "what tools do you have" / "list your tools" / "what can
    you do", reproduce the <available> list verbatim. Entries under <disabled>
    affect call-site routing only, never the self-reported tool list.
  </enumeration_rule>
</tools>

<workflows>
  <!--
    Bot files typically have multiple workflows keyed by intent, e.g.
      <workflow id="implement"> ... </workflow>
      <workflow id="plan"> ... </workflow>
    Role files usually have one <mandatory_workflow> with ordered steps.
    Use Markdown ordered lists inside; avoid nesting XML deeper than 3 levels.
  -->
</workflows>

<allowed_behaviors>
  <!-- Positive authorization list. Covers 90% of compliant moves. -->
  - ...
</allowed_behaviors>

<forbidden_behaviors>
  <!--
    2-5 hard no-nos. Each on one line. Do NOT repeat the same rule across
    multiple sections. Prefer these over scattered "don't do X" scolding.
  -->
  - ...
</forbidden_behaviors>

<examples>
  <!--
    Few-shot. Claude learns more from 1-2 good examples than from 5 paragraphs
    of prose rules. Recommended counts:
      - tech_lead (big bot): 3 examples
      - product_manager (small bot): 2 examples
      - developer / reviewer / bug_fixer (complex roles): 1 example each
      - simple roles (<2KB content): 0 examples is fine
  -->
  <example id="name-the-example">
    <user>The user's request, verbatim or paraphrased.</user>
    <correct>What the agent should do. Include tool calls if relevant.</correct>
    <incorrect>A common wrong move + why it's wrong (one sentence).</incorrect>
  </example>
</examples>

<output_format>
  <!--
    For bot files: what the final Feishu reply should contain (summary,
    verdict, URLs, risks).
    For role files: the artifact's path + section structure (the old
    "Deliverable" section).
  -->
</output_format>

<anti_patterns>
  <!--
    Thinking-level traps (not hard rules — those live in <forbidden_behaviors>).
    E.g. "do not nitpick style while missing critical blockers" is a mindset
    trap, not a hard rule.
  -->
  - ...
</anti_patterns>
