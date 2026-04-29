#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_ENV="$SCRIPT_DIR/secrets/deploy/server.env"
JUMP_ENV=""

resolve_path() {
  local raw_path="$1"
  if [[ -z "$raw_path" ]]; then
    return 1
  fi
  if [[ "$raw_path" == "~" ]]; then
    raw_path="$HOME"
  elif [[ "$raw_path" == ~/* ]]; then
    raw_path="$HOME/${raw_path#~/}"
  fi
  if [[ "$raw_path" = /* ]]; then
    printf '%s\n' "$raw_path"
    return 0
  fi
  if [[ -f "$raw_path" ]]; then
    printf '%s\n' "$raw_path"
    return 0
  fi
  printf '%s\n' "$REPO_ROOT/$raw_path"
}

usage() {
  cat <<'EOF'
Usage:
  .larkagent/agent_deploy.sh [--server-env <path>] [--jump-env <path>] --role <role_name>
  .larkagent/agent_deploy.sh [--server-env <path>] [--jump-env <path>] --all

Behavior:
  - Reads server-level settings from the selected server env file
  - Reads role definitions from roles.jsonl
  - Supports role transport values: ws, webhook
  - Optional jump env is used as SSH/rsync bastion
  - Server env may set LARK_JUMP_ENV to define a default bastion
  - Requires --role or --all when multiple roles exist

Host bootstrap:
  - If .larkagent/secrets/deploy_projects/<pid>.json declares
    "host_bootstrap_script", that script is executed once on the
    agent host during setup_shared_resources (after shared-repo
    sync, before systemd restart). A non-zero exit fails the whole
    deploy so the host never ends up half-provisioned.
  - Use --skip-bootstrap to skip host bootstrap in fast iteration
    loops (e.g. when only the agent code changed and the host
    toolchain is already installed from a previous deploy).
EOF
}

PARSED_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-env)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --server-env."
        usage
        exit 1
      fi
      SERVER_ENV="$(resolve_path "$2")"
      shift 2
      ;;
    --jump-env)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --jump-env."
        usage
        exit 1
      fi
      JUMP_ENV="$(resolve_path "$2")"
      shift 2
      ;;
    *)
      PARSED_ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${PARSED_ARGS[@]}"

if [[ ! -f "$SERVER_ENV" ]]; then
  echo "Missing agent server env at $SERVER_ENV."
  exit 1
fi

# shellcheck source=/dev/null
source "$SERVER_ENV"

DEFAULT_JUMP_ENV_RAW="${LARK_JUMP_ENV:-}"
if [[ -z "$JUMP_ENV" && -n "$DEFAULT_JUMP_ENV_RAW" ]]; then
  JUMP_ENV="$(resolve_path "$DEFAULT_JUMP_ENV_RAW")"
fi

HOST="${LARK_SERVER_HOST:-}"
PORT="${LARK_SERVER_PORT:-22}"
USER="${LARK_SERVER_USER:-}"
KEY="${LARK_SERVER_KEY:-}"
PASS="${LARK_SERVER_PASSWORD:-}"
PUBLIC_URL="${LARK_SERVER_URL:-}"
PROJECT_NAME_DEFAULT="${LARK_PROJECT_NAME:-exampleapp}"
PROJECTS_ROOT_DEFAULT="${LARK_PROJECTS_ROOT:-/home/$USER/projects}"
ROLES_FILE="${LARK_ROLES_FILE:-$SCRIPT_DIR/secrets/deploy/roles.jsonl}"

if [[ -n "$KEY" ]]; then
  KEY="$(resolve_path "$KEY")"
fi

TARGET_ROLE=""
DEPLOY_ALL=false
SKIP_HOST_BOOTSTRAP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --role."
        usage
        exit 1
      fi
      TARGET_ROLE="$2"
      shift 2
      ;;
    --all)
      DEPLOY_ALL=true
      shift
      ;;
    --skip-bootstrap)
      # Escape hatch for operators iterating on agent code when the
      # agent host's build toolchain is already installed. A normal
      # --all run always runs bootstrap; this only skips it.
      SKIP_HOST_BOOTSTRAP=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$HOST" || -z "$USER" ]]; then
  echo "Missing LARK_SERVER_HOST or LARK_SERVER_USER in .larkagent/secrets/deploy/server.env."
  exit 1
fi

if [[ ! -f "$ROLES_FILE" ]]; then
  echo "Missing roles config at $ROLES_FILE."
  exit 1
fi

LARKAGENT_SECRETS_DIR="$SCRIPT_DIR/secrets"
GITHUB_KEY_DIR="$LARKAGENT_SECRETS_DIR/github_key"

ROLE_TMP_FILE="$(mktemp)"
SSH_CONFIG_FILE="$(mktemp)"
trap 'rm -f "$ROLE_TMP_FILE" "$SSH_CONFIG_FILE"' EXIT

if [[ -n "$JUMP_ENV" ]]; then
  if [[ ! -f "$JUMP_ENV" ]]; then
    echo "Missing jump env at $JUMP_ENV."
    exit 1
  fi
  JUMP_HOST="$(
    # shellcheck disable=SC1090
    ( source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_HOST:-}" )
  )"
  JUMP_PORT="$(
    # shellcheck disable=SC1090
    ( source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_PORT:-22}" )
  )"
  JUMP_USER="$(
    # shellcheck disable=SC1090
    ( source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_USER:-}" )
  )"
  JUMP_KEY="$(
    # shellcheck disable=SC1090
    ( source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_KEY:-}" )
  )"

  if [[ -n "$JUMP_KEY" ]]; then
    JUMP_KEY="$(resolve_path "$JUMP_KEY")"
  fi

  if [[ -z "$KEY" || -z "$JUMP_KEY" ]]; then
    echo "Jump mode requires SSH key auth for both target and jump hosts."
    exit 1
  fi
  if [[ -z "$JUMP_HOST" || -z "$JUMP_USER" ]]; then
    echo "Missing LARK_SERVER_HOST or LARK_SERVER_USER in jump env $JUMP_ENV."
    exit 1
  fi

  cat >"$SSH_CONFIG_FILE" <<EOF
Host lark-jump
  HostName $JUMP_HOST
  User $JUMP_USER
  Port $JUMP_PORT
  IdentityFile $JUMP_KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new

Host lark-target
  HostName $HOST
  User $USER
  Port $PORT
  IdentityFile $KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  ProxyJump lark-jump
EOF

  SSH_CMD=(ssh -F "$SSH_CONFIG_FILE" lark-target)
  RSYNC_CMD=(rsync -avz -e "ssh -F $SSH_CONFIG_FILE")
  RSYNC_REMOTE_HOST="lark-target"
else
  SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$PORT")

  if [[ -n "$KEY" ]]; then
    SSH_OPTS+=(-i "$KEY")
    SSH_CMD=(ssh "${SSH_OPTS[@]}" "$USER@$HOST")
    RSYNC_SSH_E="ssh ${SSH_OPTS[*]}"
  elif [[ -n "$PASS" ]] && command -v sshpass &>/dev/null; then
    export SSHPASS="$PASS"
    SSH_CMD=(sshpass -e ssh "${SSH_OPTS[@]}" "$USER@$HOST")
    RSYNC_SSH_E="sshpass -e ssh ${SSH_OPTS[*]}"
  else
    SSH_CMD=(ssh "${SSH_OPTS[@]}" "$USER@$HOST")
    RSYNC_SSH_E="ssh ${SSH_OPTS[*]}"
  fi

  RSYNC_CMD=(rsync -avz -e "$RSYNC_SSH_E")
  RSYNC_REMOTE_HOST="$USER@$HOST"
fi

ROLE_ENTRIES=()
python3 - "$ROLES_FILE" "$TARGET_ROLE" "$DEPLOY_ALL" "$PROJECT_NAME_DEFAULT" "$PROJECTS_ROOT_DEFAULT" >"$ROLE_TMP_FILE" <<'PY'
import json
import pathlib
import sys

roles_path = pathlib.Path(sys.argv[1])
target_role = sys.argv[2]
deploy_all = sys.argv[3].lower() == "true"
default_project = sys.argv[4]
default_root = sys.argv[5]

entries = []
for line_no, raw_line in enumerate(roles_path.read_text().splitlines(), start=1):
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON on line {line_no} of {roles_path}: {exc}")

    if payload.get("enabled", True) is False:
        continue

    role_name = payload.get("role_name") or payload.get("role")
    if not role_name:
        raise SystemExit(f"Missing role_name on line {line_no} of {roles_path}.")

    project_name = payload.get("project_name", default_project)
    projects_root = payload.get("projects_root", default_root)
    transport = str(payload.get("transport", "ws")).strip().lower() or "ws"
    agent_port = payload.get("agent_port")
    if transport == "webhook" and agent_port is None:
        raise SystemExit(f"Missing agent_port for webhook role '{role_name}' on line {line_no}.")
    service_name = payload.get("service_name") or f"{project_name}-{role_name}"
    service_name = service_name.replace("_", "-")

    if target_role and target_role not in {role_name, service_name}:
        continue

    entries.append((role_name, project_name, projects_root, "__NONE__" if agent_port is None else str(agent_port), service_name, transport))

if target_role and not entries:
    raise SystemExit(f"Role '{target_role}' not found in {roles_path}.")

if not deploy_all and not target_role:
    if len(entries) == 1:
        pass
    elif len(entries) == 0:
        raise SystemExit(f"No enabled roles found in {roles_path}.")
    else:
        raise SystemExit(
            f"Multiple roles found in {roles_path}. Use --role <role_name> or --all."
        )

for entry in entries:
    print("\t".join(entry))
PY
while IFS= read -r role_entry; do
  ROLE_ENTRIES+=("$role_entry")
done < "$ROLE_TMP_FILE"

if [[ ${#ROLE_ENTRIES[@]} -eq 0 ]]; then
  echo "No roles selected for deployment."
  exit 1
fi

PROJECT_REPO="${LARK_PROJECT_REPO:-git@github.com:your-org/your-project.git}"
PROJECT_BRANCH="${LARK_PROJECT_BRANCH:-main}"

setup_shared_resources() {
  local project_root="$1"
  local shared_res_dir="$project_root/shared-resources"
  local shared_repo_dir="$project_root/shared-repo"
  local github_key_dir="$shared_res_dir/.github-key"
  local persistent_key="$github_key_dir/id_ed25519_agent"

  echo "===> Setting up shared resources at $shared_res_dir ..."

  # NOTE on user_tokens: directory is created but intentionally NOT
  # rsynced from local. The agent process refreshes the access_token on
  # the server in-place (~7200s cadence), so blindly pushing the local
  # file would overwrite a newer server-side token. Use
  # scripts/setup_impersonation_cron.sh push-token only when you've
  # just re-run the OAuth authorize step locally.
  "${SSH_CMD[@]}" "mkdir -p \
    '$github_key_dir' \
    '$shared_res_dir/.larkagent/secrets/feishu_app' \
    '$shared_res_dir/.larkagent/secrets/feishu_bot' \
    '$shared_res_dir/.larkagent/secrets/ai_key' \
    '$shared_res_dir/.larkagent/secrets/projects' \
    '$shared_res_dir/.larkagent/secrets/code_write' \
    '$shared_res_dir/.larkagent/secrets/deploy_projects' \
    '$shared_res_dir/.larkagent/secrets/github_key' \
    '$shared_res_dir/.larkagent/secrets/user_tokens' \
    '$shared_res_dir/project-adapters' \
    '$shared_res_dir/skills/roles' \
    '$shared_res_dir/docs' \
    '$shared_res_dir/data'"

  echo "===> Syncing GitHub SSH key (persistent)..."
  "${RSYNC_CMD[@]}" \
    "$GITHUB_KEY_DIR/" \
    "$RSYNC_REMOTE_HOST:$github_key_dir/"
  "${SSH_CMD[@]}" "chmod 700 '$github_key_dir' && chmod 600 '$persistent_key' && chmod 644 '${persistent_key}.pub' 2>/dev/null || true"

  echo "===> Syncing shared project-adapters..."
  "${RSYNC_CMD[@]}" \
    "$REPO_ROOT/project-adapters/" \
    "$RSYNC_REMOTE_HOST:$shared_res_dir/project-adapters/"

  echo "===> Syncing shared skills..."
  "${RSYNC_CMD[@]}" \
    "$REPO_ROOT/skills/" \
    "$RSYNC_REMOTE_HOST:$shared_res_dir/skills/"

  echo "===> Syncing shared docs..."
  "${RSYNC_CMD[@]}" \
    "$REPO_ROOT/docs/" \
    "$RSYNC_REMOTE_HOST:$shared_res_dir/docs/"

  echo "===> Syncing shared Feishu app secrets..."
  "${RSYNC_CMD[@]}" \
    "$LARKAGENT_SECRETS_DIR/feishu_app/" \
    "$RSYNC_REMOTE_HOST:$shared_res_dir/.larkagent/secrets/feishu_app/"
  "${RSYNC_CMD[@]}" \
    "$LARKAGENT_SECRETS_DIR/feishu_bot/" \
    "$RSYNC_REMOTE_HOST:$shared_res_dir/.larkagent/secrets/feishu_bot/"

  echo "===> Syncing shared AI secrets..."
  "${RSYNC_CMD[@]}" \
    "$LARKAGENT_SECRETS_DIR/ai_key/" \
    "$RSYNC_REMOTE_HOST:$shared_res_dir/.larkagent/secrets/ai_key/"

  # Project registry (consumed by feishu_agent.tools.project_registry);
  # without it the CodeWriteService / PrePushInspector / GitOpsService are
  # all fail-closed because no project_id can be resolved on the server.
  if [[ -d "$LARKAGENT_SECRETS_DIR/projects" ]]; then
    echo "===> Syncing project registry (projects/)..."
    "${RSYNC_CMD[@]}" \
      "$LARKAGENT_SECRETS_DIR/projects/" \
      "$RSYNC_REMOTE_HOST:$shared_res_dir/.larkagent/secrets/projects/"
  else
    echo "(skip) No project registry at $LARKAGENT_SECRETS_DIR/projects"
  fi

  # Code-write policies (consumed by _resolve_code_write_policies);
  # same fail-closed behavior as above — no policies → no code writes.
  if [[ -d "$LARKAGENT_SECRETS_DIR/code_write" ]]; then
    echo "===> Syncing code-write policies (code_write/)..."
    "${RSYNC_CMD[@]}" \
      "$LARKAGENT_SECRETS_DIR/code_write/" \
      "$RSYNC_REMOTE_HOST:$shared_res_dir/.larkagent/secrets/code_write/"
  else
    echo "(skip) No code-write policies at $LARKAGENT_SECRETS_DIR/code_write"
  fi

  # Per-project deploy metadata (consumed by DeployService). Without
  # this directory on the remote host, ``deploy_project`` and
  # ``describe_deploy_project`` are fail-closed (tool hidden from TL,
  # TL cannot invoke project deploys). The directory is gitignored
  # except for ``*.example`` templates; we always rsync the whole
  # thing so new projects onboarded locally become deployable after
  # the next ``agent_deploy.sh --all``.
  if [[ -d "$LARKAGENT_SECRETS_DIR/deploy_projects" ]]; then
    echo "===> Syncing deploy-project metadata (deploy_projects/)..."
    "${RSYNC_CMD[@]}" \
      "$LARKAGENT_SECRETS_DIR/deploy_projects/" \
      "$RSYNC_REMOTE_HOST:$shared_res_dir/.larkagent/secrets/deploy_projects/"
  else
    echo "(skip) No deploy-project metadata at $LARKAGENT_SECRETS_DIR/deploy_projects"
  fi

  # Seed host-specific projects.jsonl / policies.jsonl if missing.
  #
  # Locally we only ship ``*.example.jsonl`` (committed templates with dev
  # paths). The real files carry host-specific absolute paths (e.g.
  # ``/home/ubuntu/projects/<name>/shared-repo``) and are gitignored. On
  # first deploy to a fresh host these real files don't exist — without
  # them ProjectRegistry is empty, so WorkflowService / CodeWriteService /
  # SprintStateService all run in project-less mode and everything degrades
  # to "project doesn't exist". That's the gap this block closes.
  #
  # We seed only when the real file is absent: any hand-edit by ops on
  # the remote survives subsequent deploys. To force a re-seed, ops can
  # delete the remote file and rerun this script.
  echo "===> Seeding host-specific project registry / code-write policies (if absent)..."
  "${SSH_CMD[@]}" "bash -s" <<SEEDHOSTCONF
set -euo pipefail

projects_target='$shared_res_dir/.larkagent/secrets/projects/projects.jsonl'
if [ -f "\$projects_target" ]; then
  echo "   (skip) projects.jsonl already exists; not overwriting"
else
  {
    echo '# Auto-seeded by agent_deploy.sh on first deploy to this host.'
    echo '# Edit freely — subsequent deploys will not overwrite this file.'
    echo '# Delete and redeploy to force a re-seed.'
    echo '{"project_id":"$project_name","display_name":"$project_name","project_repo_root":"$shared_repo_dir","is_default":true}'
  } > "\$projects_target"
  echo "   seeded projects.jsonl (project_repo_root=$shared_repo_dir)"
fi

policies_target='$shared_res_dir/.larkagent/secrets/code_write/policies.jsonl'
policies_example='$shared_res_dir/.larkagent/secrets/code_write/policies.example.jsonl'
if [ -f "\$policies_target" ]; then
  echo "   (skip) policies.jsonl already exists; not overwriting"
elif [ -f "\$policies_example" ]; then
  # Derive from the committed example: keep the example's allowed_write/read
  # roots and denied segments intact, only rewrite the dev project_repo_root
  # to this host's shared-repo. Keeps ops editing ONE source of truth
  # (the example) for the shape of the policy.
  grep -vE '^[[:space:]]*(#|$)' "\$policies_example" \
    | sed 's|~/Documents/Github/$project_name|$shared_repo_dir|g; s|~/Documents/Github/ExampleApp|$shared_repo_dir|g' \
    > "\$policies_target"
  echo "   seeded policies.jsonl from policies.example.jsonl (project_repo_root=$shared_repo_dir)"
else
  echo "   (skip) policies.jsonl missing and no example to derive from"
fi
SEEDHOSTCONF

  # gh CLI token for PullRequestService. The SSH key already lives
  # under $github_key_dir (used for git pull), but gh_token.env is
  # separate and consumed by PullRequestService via the
  # .larkagent/secrets/github_key path.
  if [[ -d "$LARKAGENT_SECRETS_DIR/github_key" ]]; then
    echo "===> Syncing GitHub PR token (.larkagent/secrets/github_key/)..."
    "${RSYNC_CMD[@]}" \
      "$LARKAGENT_SECRETS_DIR/github_key/" \
      "$RSYNC_REMOTE_HOST:$shared_res_dir/.larkagent/secrets/github_key/"
    "${SSH_CMD[@]}" "chmod 700 '$shared_res_dir/.larkagent/secrets/github_key' && find '$shared_res_dir/.larkagent/secrets/github_key' -type f -exec chmod 600 {} \\;"
  else
    echo "(skip) No GitHub PR token at $LARKAGENT_SECRETS_DIR/github_key"
  fi

  echo "===> Updating ExampleApp shared repo..."
  "${SSH_CMD[@]}" "bash -s" <<GITEOF
    set -euo pipefail
    export GIT_SSH_COMMAND="ssh -i '$persistent_key' -o StrictHostKeyChecking=accept-new"
    if [ -d '$shared_repo_dir/.git' ]; then
      cd '$shared_repo_dir'
      git fetch origin '$PROJECT_BRANCH' 2>&1 || true
      git checkout '$PROJECT_BRANCH' 2>/dev/null || true
      git pull --ff-only 2>&1 || echo 'Pull skipped (non-fast-forward or dirty)'
    else
      git clone -b '$PROJECT_BRANCH' '$PROJECT_REPO' '$shared_repo_dir'
      cd '$shared_repo_dir'
    fi
    git config core.sshCommand "ssh -i '$persistent_key' -o StrictHostKeyChecking=accept-new"
    git config user.name 'FeishuOPC Agent'
    git config user.email 'feishu-opc-agent@users.noreply.github.com'

    # Seed .git/info/exclude so agent-runtime artifacts (run history +
    # its lock file) never show up as dirty in \`git status\`. This
    # file is per-clone and intentionally NOT the same as the project's
    # .gitignore — using .gitignore would create a phantom change the
    # user has to merge to main each deploy. Idempotent.
    info_exclude='$shared_repo_dir/.git/info/exclude'
    mkdir -p "\$(dirname "\$info_exclude")"
    [ -f "\$info_exclude" ] || : > "\$info_exclude"
    grep -qxF '/.feishu_run_history.jsonl' "\$info_exclude" \
      || printf '\n# feishu-agent run history (auto-managed, local-only)\n/.feishu_run_history.jsonl\n/.feishu_run_history.jsonl.lock\n' >> "\$info_exclude"
    echo "ExampleApp on branch: \$(git branch --show-current) — \$(git log --oneline -1)"
GITEOF

  # Per-project business secrets live under
  # `.larkagent/secrets/project_secrets/<project_id>/`. The layout under
  # each project is determined by that project, not by FeishuOPC —
  # we just rsync the `deploy/` and `tools/etl/` subtrees (the two
  # shapes we've needed so far) if they exist.
  local project_secrets_local="$LARKAGENT_SECRETS_DIR/project_secrets/$PROJECT_NAME_DEFAULT"
  if [[ -d "$project_secrets_local/deploy" ]]; then
    echo "===> Syncing $PROJECT_NAME_DEFAULT deploy secrets into shared-repo/deploy/secrets/..."
    "${SSH_CMD[@]}" "mkdir -p '$shared_repo_dir/deploy/secrets'"
    "${RSYNC_CMD[@]}" \
      "$project_secrets_local/deploy/" \
      "$RSYNC_REMOTE_HOST:$shared_repo_dir/deploy/secrets/"
  else
    echo "(skip) No $PROJECT_NAME_DEFAULT deploy secrets at $project_secrets_local/deploy"
  fi

  if [[ -d "$project_secrets_local/tools/etl" ]]; then
    echo "===> Syncing $PROJECT_NAME_DEFAULT ETL secrets into shared-repo/tools/etl/secrets/..."
    "${SSH_CMD[@]}" "mkdir -p '$shared_repo_dir/tools/etl/secrets'"
    "${RSYNC_CMD[@]}" \
      "$project_secrets_local/tools/etl/" \
      "$RSYNC_REMOTE_HOST:$shared_repo_dir/tools/etl/secrets/"
  else
    echo "(skip) No $PROJECT_NAME_DEFAULT ETL secrets at $project_secrets_local/tools/etl"
  fi

  local ssh_key_local="$LARKAGENT_SECRETS_DIR/ssh_key"
  if [[ -d "$ssh_key_local" ]]; then
    local ssh_key_remote="$shared_res_dir/.ssh-keys"
    echo "===> Syncing SSH keys into shared-resources/.ssh-keys/..."
    "${SSH_CMD[@]}" "mkdir -p '$ssh_key_remote'"
    "${RSYNC_CMD[@]}" \
      "$ssh_key_local/" \
      "$RSYNC_REMOTE_HOST:$ssh_key_remote/"
    "${SSH_CMD[@]}" "chmod 700 '$ssh_key_remote' && find '$ssh_key_remote' -type f -exec chmod 600 {} \\;"
  else
    echo "(skip) No SSH keys at $ssh_key_local"
  fi

  run_host_bootstrap "$shared_repo_dir"

  echo "===> Shared resources setup complete."
}

# Execute the project-side host bootstrap script on the agent host.
#
# Why this exists:
#   ``deploy_project`` (the TL tool) only runs the *deploy* script on
#   the agent host. That script assumes its build toolchain
#   (``flutter``, ``docker``, language SDKs, …) is already installed.
#   Without this stage the operator would have to SSH into the agent
#   host by hand to install things, which defeats the whole point of
#   having an "agent host" — and crucially fails silently at first
#   ``deploy_project`` call ("command not found", exit 127).
#
# Contract:
#   - Runs on the agent host (same host where ``deploy_project`` will
#     later run the project's deploy script).
#   - Idempotent: must be safe to re-run after every
#     ``agent_deploy.sh --all``. Use ``command -v`` guards + version
#     pins inside the project-side script.
#   - Non-zero exit fails the whole deploy (we'd rather stop early
#     than leave the agent host half-provisioned).
#   - Only runs when the project's
#     ``deploy_projects/<pid>.json`` declares
#     ``host_bootstrap_script``. No declaration → skip.
#   - Skipped entirely when operator passes ``--skip-bootstrap``.
run_host_bootstrap() {
  local shared_repo_dir="$1"
  local cfg_local="$LARKAGENT_SECRETS_DIR/deploy_projects/$PROJECT_NAME_DEFAULT.json"

  if [[ "$SKIP_HOST_BOOTSTRAP" == "true" ]]; then
    echo "(skip) --skip-bootstrap set; not running host bootstrap for $PROJECT_NAME_DEFAULT"
    return 0
  fi

  if [[ ! -f "$cfg_local" ]]; then
    echo "(skip) No deploy-project metadata at $cfg_local (create it from the .example template to enable host bootstrap)"
    return 0
  fi

  # Extract host_bootstrap_script from JSON. Use python3 (stdlib-only)
  # for portability — jq is not guaranteed on operator laptops.
  local bootstrap_rel
  bootstrap_rel="$(python3 - "$cfg_local" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:  # noqa: BLE001 — surface parse errors to operator
    sys.stderr.write(f"Failed to parse {path}: {exc}\n")
    sys.exit(2)

value = data.get("host_bootstrap_script")
if value is None:
    sys.exit(0)
if not isinstance(value, str) or not value.strip():
    sys.stderr.write("host_bootstrap_script must be a non-empty string when set.\n")
    sys.exit(2)
print(value.strip())
PY
)"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "Host bootstrap config for $PROJECT_NAME_DEFAULT is malformed; aborting."
    exit $rc
  fi

  if [[ -z "$bootstrap_rel" ]]; then
    echo "(skip) $PROJECT_NAME_DEFAULT: no host_bootstrap_script declared in $cfg_local"
    return 0
  fi

  # Don't let a config mistake escape the project repo on the remote
  # host. DeployService rejects these at parse time, but this script
  # is the operator-facing path so we double-check. Keep the allowlist
  # in lockstep with ``_SAFE_RELATIVE_PATH`` in deploy_service.py —
  # drift here = drift everywhere.
  case "$bootstrap_rel" in
    /*|*..*)
      echo "host_bootstrap_script must be a relative path inside the project repo, got $bootstrap_rel"
      exit 1
      ;;
  esac
  if ! printf '%s' "$bootstrap_rel" | LC_ALL=C grep -Eq '^[A-Za-z0-9._/-]+$'; then
    echo "host_bootstrap_script must match [A-Za-z0-9._/-]+ (letters, digits, dot, underscore, slash, hyphen). Got: $bootstrap_rel"
    exit 1
  fi

  local bootstrap_remote="$shared_repo_dir/$bootstrap_rel"
  echo "===> Running $PROJECT_NAME_DEFAULT host bootstrap: $bootstrap_remote"
  if ! "${SSH_CMD[@]}" "test -x '$bootstrap_remote' || { echo 'host_bootstrap_script not found or not executable: $bootstrap_remote'; exit 1; }"; then
    echo "Host bootstrap script missing on agent host; did the project commit it and push? Aborting."
    exit 1
  fi
  if ! "${SSH_CMD[@]}" "cd '$shared_repo_dir' && bash '$bootstrap_remote'"; then
    echo "Host bootstrap FAILED for $PROJECT_NAME_DEFAULT; aborting deploy."
    exit 1
  fi
  echo "===> Host bootstrap for $PROJECT_NAME_DEFAULT OK."
}

FIRST_PROJECT_ROOT=""

deploy_role() {
  local role_name="$1"
  local project_name="$2"
  local projects_root="$3"
  local agent_port="$4"
  local service_name="$5"
  local transport="$6"

  local project_root="$projects_root/$project_name"
  local remote_dir="$project_root/roles/$role_name"
  local shared_env_dir="$project_root/shared-env"
  local shared_res_dir="$project_root/shared-resources"
  local shared_repo_dir="$project_root/shared-repo"
  local run_dir="$remote_dir/run"
  local log_dir="$remote_dir/logs"
  local venv_dir="$shared_env_dir/.venv"
  local persistent_key="$shared_res_dir/.github-key/id_ed25519_agent"
  local start_script_name="start-${role_name}.sh"
  local unit_file_name="${service_name}.service"
  local tmp_start_script
  local tmp_service_file

  tmp_start_script="$(mktemp)"
  tmp_service_file="$(mktemp)"

  if [[ -z "$FIRST_PROJECT_ROOT" || "$FIRST_PROJECT_ROOT" != "$project_root" ]]; then
    FIRST_PROJECT_ROOT="$project_root"
    setup_shared_resources "$project_root"
  fi

  echo "==> [$role_name] Preparing role directories..."
  "${SSH_CMD[@]}" "mkdir -p '$project_root/roles' '$shared_env_dir' '$remote_dir' '$remote_dir/data' '$run_dir' '$log_dir'"

  echo "==> [$role_name] Syncing agent server code..."
  "${RSYNC_CMD[@]}" --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' \
    --exclude '.larkagent' --exclude '.env' --exclude 'data/' \
    "$REPO_ROOT/" "$RSYNC_REMOTE_HOST:$remote_dir/"

  # Per-instance identity: which project this FeishuOPC instance defaults to.
  #
  # APP_REPO_ROOT == $shared_res_dir is correct — that's the agent's home
  # (hosts .larkagent/secrets/, project-adapters/, skills/). The *project*
  # source code lives separately at $shared_repo_dir and is registered via
  # projects.jsonl (seeded above in setup_shared_resources). Runtime services
  # (WorkflowService / CodeWriteService / SprintStateService) look up
  # project_repo_root through ProjectRegistry, so they all resolve
  # project-relative paths against $shared_repo_dir, not $shared_res_dir.
  local default_project_id="${DEFAULT_PROJECT_ID:-${project_name}}"
  if [[ "$transport" == "ws" ]]; then
    cat >"$tmp_start_script" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$remote_dir"
export PYTHONPATH="$remote_dir"
export APP_REPO_ROOT="$shared_res_dir"
export DEFAULT_PROJECT_ID="$default_project_id"
# Project source lives at $shared_repo_dir and is registered in
# \$APP_REPO_ROOT/.larkagent/secrets/projects/projects.jsonl — NOT via this env var.
export SECRET_KEY="agent-only"
export DEBUG="false"
export LARK_ROLE_NAME="$role_name"
exec "$venv_dir/bin/python" -m feishu_agent.feishu_ws_main
EOF
  else
    cat >"$tmp_start_script" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$remote_dir"
export PYTHONPATH="$remote_dir"
export APP_REPO_ROOT="$shared_res_dir"
export DEFAULT_PROJECT_ID="$default_project_id"
# Project source lives at $shared_repo_dir and is registered in
# \$APP_REPO_ROOT/.larkagent/secrets/projects/projects.jsonl — NOT via this env var.
export SECRET_KEY="agent-only"
export DEBUG="false"
exec "$venv_dir/bin/python" -m uvicorn feishu_agent.agent_main:app --host 127.0.0.1 --port ${agent_port}
EOF
  fi

  cat >"$tmp_service_file" <<EOF
[Unit]
Description=${project_name} ${role_name} agent (${transport})
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$remote_dir
ExecStart=$run_dir/$start_script_name
Restart=always
RestartSec=5
StandardOutput=append:$log_dir/feishu-agent.log
StandardError=append:$log_dir/feishu-agent.log

[Install]
WantedBy=multi-user.target
EOF

  echo "==> [$role_name] Syncing agent start script..."
  "${RSYNC_CMD[@]}" \
    "$tmp_start_script" \
    "$RSYNC_REMOTE_HOST:$run_dir/$start_script_name"
  "${SSH_CMD[@]}" "chmod +x '$run_dir/$start_script_name'"

  echo "==> [$role_name] Syncing agent systemd unit..."
  "${RSYNC_CMD[@]}" \
    "$tmp_service_file" \
    "$RSYNC_REMOTE_HOST:$run_dir/$unit_file_name"

  echo "==> [$role_name] Setting up shared project virtualenv..."
  "${SSH_CMD[@]}" "cd '$remote_dir' && \
    command -v python3 >/dev/null && \
    if ! python3 -m venv '$venv_dir' >/dev/null 2>&1; then \
      sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv git; \
      rm -rf '$venv_dir'; \
      python3 -m venv '$venv_dir'; \
    fi && \
    GIT_SSH_COMMAND='ssh -i \"$persistent_key\" -o StrictHostKeyChecking=accept-new' \
    '$venv_dir/bin/pip' install --upgrade pip && \
    GIT_SSH_COMMAND='ssh -i \"$persistent_key\" -o StrictHostKeyChecking=accept-new' \
    '$venv_dir/bin/pip' install --no-cache-dir -r requirements.txt"

  echo "==> [$role_name] Restarting Feishu agent process..."
  "${SSH_CMD[@]}" "for legacy_service in exampleapp-feishu-agent ${project_name}-feishu-agent; do \
      if [[ \"\$legacy_service\" != '${service_name}' ]]; then \
        sudo systemctl stop \"\$legacy_service\" 2>/dev/null || true; \
        sudo systemctl disable \"\$legacy_service\" 2>/dev/null || true; \
      fi; \
    done && \
    sudo cp '$run_dir/$unit_file_name' /etc/systemd/system/${service_name}.service && \
    sudo systemctl daemon-reload && \
    sudo systemctl enable ${service_name} >/dev/null 2>&1 || true && \
    sudo systemctl restart ${service_name} && \
    sudo systemctl is-active ${service_name}"

  echo "==> [$role_name] Checking Feishu agent runtime..."
  sleep 5
  if [[ "$transport" == "ws" ]]; then
    "${SSH_CMD[@]}" "sudo systemctl is-active ${service_name}"
  else
    "${SSH_CMD[@]}" "curl -s http://localhost:${agent_port}/health"
  fi

  echo ""
  echo "==> [$role_name] Agent runtime deploy complete!"
  echo "Project root: $project_root"
  echo "Role workspace: $remote_dir"
  echo "Shared env: $venv_dir"
  echo "Shared resources: $shared_res_dir"
  echo "ExampleApp repo: $shared_repo_dir"
  if [[ "$transport" == "ws" ]]; then
    echo "Feishu transport: ws"
  else
    echo "Feishu agent running at http://localhost:${agent_port}"
  fi
  echo "Feishu agent log: $log_dir/feishu-agent.log"
  echo "systemd service: ${service_name}"
  if [[ "$transport" == "webhook" && -n "$PUBLIC_URL" ]]; then
    echo "Feishu webhook endpoint: https://${PUBLIC_URL}/api/v1/feishu/events"
  fi

  rm -f "$tmp_start_script" "$tmp_service_file"
}

for role_entry in "${ROLE_ENTRIES[@]}"; do
  IFS=$'\t' read -r role_name project_name projects_root agent_port service_name transport <<< "$role_entry"
  if [[ "$agent_port" == "__NONE__" ]]; then
    agent_port=""
  fi
  deploy_role "$role_name" "$project_name" "$projects_root" "$agent_port" "$service_name" "$transport"
done
