# project_secrets/

This directory holds **downstream-project business secrets** — credentials
that belong to a project FeishuOPC orchestrates on behalf of, not to
FeishuOPC itself.

## Layout convention

```
.larkagent/secrets/project_secrets/<project_id>/
    deploy/          # project's own server / hosting secrets
    tools/           # project's own ETL / CLI / build-time secrets
    ...              # whatever shape that project needs
```

`<project_id>` must match the project's id in
`.larkagent/secrets/projects/projects.jsonl` (the FeishuOPC project
registry).

## Why a separate directory?

The other siblings under `.larkagent/secrets/` — `ai_key/`, `deploy/`,
`feishu_app/`, `feishu_bot/`, `github_key/`, `ssh_key/`, `code_write/`,
`projects/` — are **FeishuOPC's own** secret categories. A per-project
bucket mixed in with them confuses the two axes:

- **FeishuOPC system secrets**: things the agent framework needs to run
  (LLM keys, Feishu app/bot tokens, the agent's own GitHub deploy key,
  code-write policies, project registry, …).
- **Per-project business secrets**: things specific to a downstream
  project (e.g. ExampleApp's production DB credentials, its ETL API
  keys, its deploy targets).

Keeping them split means:

1. Onboarding a new project can never accidentally mutate FeishuOPC
   system config — it's just `mkdir project_secrets/<new-id>/`.
2. `.gitignore` rules stay simple: the default-deny pattern
   `.larkagent/secrets/**` already covers every file here; only
   explicit whitelist files (`*.example.*`, `README*`) get tracked.
3. Removing a project is a clean directory delete; no cross-cutting
   edits.

## Access pattern

`.larkagent/agent_deploy.sh` reads
`project_secrets/$PROJECT_NAME_DEFAULT/` when syncing downstream-project
secrets to the shared-repo on the server. The FeishuOPC runtime does
**not** read these files directly — they're strictly for the deploy
pipeline to hand off to the downstream project's runtime.
