#!/usr/bin/env bash
# Set up the impersonation token freshness cron on the SV server.
#
# The token file is refreshed in-place by the agent process on the
# server side, so the source of truth for cron checks is on the
# server — never locally. This orchestrator wires that up:
#
#   push-token      scp the locally authorized token file to the
#                   server (use after running
#                   spikes/probe_as_user.py auth-server --app=application_agent)
#   install-cron    drop the wrapper script + a crontab line on the
#                   server (idempotent; replaces any previous entry
#                   that carries the FeishuOPC marker)
#   run-once        run the check remotely (via cron wrapper path) so
#                   you can see the live state in stdout
#   uninstall-cron  remove the crontab line (does not touch tokens)
#   all             push-token + install-cron + run-once
#
# Server topology (as wired by .larkagent/agent_deploy.sh):
#   $shared_res_dir = $projects_root/<project>/shared-resources
#     └── .larkagent/secrets/user_tokens/<app_id>.json   (token lives here on server)
#   $role_dir       = $projects_root/<project>/roles/<role>
#     └── scripts/check_impersonation_token.py           (code, rsynced by deploy)
#   $venv_dir       = $projects_root/<project>/shared-env/.venv
#
# The cron wrapper lives under $shared_res_dir/scripts/ so it survives
# role redeploys.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEPLOY_ENV_DEFAULT="$REPO_ROOT/.larkagent/secrets/deploy/server_sv.env"
DEPLOY_ENV="${DEPLOY_ENV:-$DEPLOY_ENV_DEFAULT}"
JUMP_ENV_OVERRIDE=""
ROLE_NAME="${IMPERSONATION_CRON_ROLE:-tech-lead-planner}"
CRON_SCHEDULE_DEFAULT="0 9,21 * * *"
CRON_SCHEDULE="${IMPERSONATION_CRON_SCHEDULE:-$CRON_SCHEDULE_DEFAULT}"
CRON_MARKER="# feishuopc:impersonation-check"
SSH_CONFIG_FILE="$(mktemp)"
trap 'rm -f "$SSH_CONFIG_FILE"' EXIT

usage() {
  cat <<EOF
Usage: $0 <command> [--deploy-env PATH] [--jump-env PATH] [--role NAME]

Commands:
  push-token        Push .larkagent/secrets/user_tokens/<app_id>.json to server.
  install-cron      Install cron wrapper + crontab entry on server.
  uninstall-cron    Remove the FeishuOPC impersonation-check crontab entry.
  run-once          Run the check on the server (via cron wrapper).
  status            Print server-side token + cron state.
  all               push-token + install-cron + run-once (a full setup).

Environment overrides:
  DEPLOY_ENV                server env file   (default: $DEPLOY_ENV_DEFAULT)
  IMPERSONATION_CRON_ROLE   role name whose venv + scripts dir to use (default: tech-lead-planner)
  IMPERSONATION_CRON_SCHEDULE cron schedule string (default: "$CRON_SCHEDULE_DEFAULT")

Examples:
  $0 all
  IMPERSONATION_CRON_SCHEDULE="15 */6 * * *" $0 install-cron
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

CMD="$1"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy-env) DEPLOY_ENV="$2"; shift 2 ;;
    --jump-env) JUMP_ENV_OVERRIDE="$2"; shift 2 ;;
    --role) ROLE_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

resolve_path() {
  local p="$1"
  case "$p" in
    "~"|"~/"*) echo "${HOME}${p:1}" ;;
    *) echo "$p" ;;
  esac
}

if [[ ! -f "$DEPLOY_ENV" ]]; then
  echo "Missing deploy env: $DEPLOY_ENV" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$DEPLOY_ENV"

HOST="${LARK_SERVER_HOST:?LARK_SERVER_HOST missing in $DEPLOY_ENV}"
PORT="${LARK_SERVER_PORT:-22}"
SERVER_USER="${LARK_SERVER_USER:?LARK_SERVER_USER missing in $DEPLOY_ENV}"
KEY="$(resolve_path "${LARK_SERVER_KEY:-}")"
PROJECT_NAME="${LARK_PROJECT_NAME:-exampleapp}"
PROJECTS_ROOT="${LARK_PROJECTS_ROOT:-/home/$SERVER_USER/projects}"

if [[ -n "${JUMP_ENV_OVERRIDE}" ]]; then
  JUMP_ENV="$(resolve_path "$JUMP_ENV_OVERRIDE")"
elif [[ -n "${LARK_JUMP_ENV:-}" ]]; then
  JUMP_ENV="$(resolve_path "$LARK_JUMP_ENV")"
  case "$JUMP_ENV" in
    /*) ;;
    *) JUMP_ENV="$REPO_ROOT/$JUMP_ENV" ;;
  esac
else
  JUMP_ENV=""
fi

# ---------------------------------------------------------------------------
# SSH plumbing (mirrors .larkagent/agent_deploy.sh)
# ---------------------------------------------------------------------------

if [[ -n "$JUMP_ENV" ]]; then
  if [[ ! -f "$JUMP_ENV" ]]; then
    echo "Missing jump env at $JUMP_ENV." >&2
    exit 1
  fi
  JH_HOST="$( (source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_HOST:-}") )"
  JH_PORT="$( (source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_PORT:-22}") )"
  JH_USER="$( (source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_USER:-}") )"
  JH_KEY="$(resolve_path "$( (source "$JUMP_ENV"; printf '%s' "${LARK_SERVER_KEY:-}") )")"
  if [[ -z "$JH_HOST" || -z "$JH_USER" || -z "$JH_KEY" || -z "$KEY" ]]; then
    echo "Jump mode requires SSH key auth for both target and jump hosts." >&2
    exit 1
  fi
  cat >"$SSH_CONFIG_FILE" <<EOF
Host lark-jump
  HostName $JH_HOST
  User $JH_USER
  Port $JH_PORT
  IdentityFile $JH_KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new

Host lark-target
  HostName $HOST
  User $SERVER_USER
  Port $PORT
  IdentityFile $KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  ProxyJump lark-jump
EOF
  SSH_CMD=(ssh -F "$SSH_CONFIG_FILE" lark-target)
  SCP_CMD=(scp -F "$SSH_CONFIG_FILE")
  REMOTE="lark-target"
  RSYNC_CMD=(rsync -avz -e "ssh -F $SSH_CONFIG_FILE")
else
  SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$PORT")
  [[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")
  SSH_CMD=(ssh "${SSH_OPTS[@]}" "$SERVER_USER@$HOST")
  SCP_CMD=(scp "${SSH_OPTS[@]}")
  REMOTE="$SERVER_USER@$HOST"
  RSYNC_CMD=(rsync -avz -e "ssh ${SSH_OPTS[*]}")
fi

# ---------------------------------------------------------------------------
# Server-side paths (must match agent_deploy.sh layout)
# ---------------------------------------------------------------------------

PROJECT_ROOT_REMOTE="$PROJECTS_ROOT/$PROJECT_NAME"
SHARED_RES_REMOTE="$PROJECT_ROOT_REMOTE/shared-resources"
SHARED_ENV_REMOTE="$PROJECT_ROOT_REMOTE/shared-env"
ROLE_DIR_REMOTE="$PROJECT_ROOT_REMOTE/roles/$ROLE_NAME"
VENV_REMOTE="$SHARED_ENV_REMOTE/.venv"
USER_TOKENS_REMOTE="$SHARED_RES_REMOTE/.larkagent/secrets/user_tokens"
WRAPPER_REMOTE="$SHARED_RES_REMOTE/scripts/impersonation_check_wrapper.sh"
LOG_REMOTE="$SHARED_RES_REMOTE/logs/impersonation-check.log"

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

resolve_local_token_path() {
  local app_id_override="${IMPERSONATION_APP_ID:-}"
  local app_env="$REPO_ROOT/.larkagent/secrets/feishu_bot/application_agent.env"
  local app_id="$app_id_override"
  if [[ -z "$app_id" && -f "$app_env" ]]; then
    app_id="$(awk -F'[:=]' '/^[[:space:]]*AppID[[:space:]]*[:=]/{gsub(/^[[:space:]]+|[[:space:]]+$|["'"'"']/,"",$2); print $2; exit}' "$app_env")"
  fi
  if [[ -z "$app_id" ]]; then
    echo "Could not determine impersonation app_id (set IMPERSONATION_APP_ID or fill AppID in application_agent.env)" >&2
    exit 1
  fi
  LOCAL_APP_ID="$app_id"
  LOCAL_TOKEN_PATH="$REPO_ROOT/.larkagent/secrets/user_tokens/$app_id.json"
}

remote_wrapper_contents() {
  cat <<EOF
#!/usr/bin/env bash
# Auto-generated by scripts/setup_impersonation_cron.sh — do not edit by hand.
# Runs the impersonation token freshness check using the deployed role's
# venv and code. Any extra args are forwarded to the Python script, so
# cron can pass --only-on-change and humans can pass --dry-run etc.
set -euo pipefail
ROLE_DIR="$ROLE_DIR_REMOTE"
VENV="$VENV_REMOTE"
SHARED_RES="$SHARED_RES_REMOTE"
export APP_REPO_ROOT="\$SHARED_RES"
export PYTHONPATH="\$ROLE_DIR"
cd "\$ROLE_DIR"
exec "\$VENV/bin/python" "\$ROLE_DIR/scripts/check_impersonation_token.py" "\$@"
EOF
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_push_token() {
  resolve_local_token_path
  if [[ ! -f "$LOCAL_TOKEN_PATH" ]]; then
    echo "Local token file not found: $LOCAL_TOKEN_PATH" >&2
    echo "Run: .venv/bin/python spikes/probe_as_user.py auth-server --app=application_agent" >&2
    exit 1
  fi
  echo "==> Ensuring remote dir: $USER_TOKENS_REMOTE"
  "${SSH_CMD[@]}" "mkdir -p '$USER_TOKENS_REMOTE' && chmod 700 '$USER_TOKENS_REMOTE'"
  echo "==> Pushing token: $LOCAL_TOKEN_PATH -> $USER_TOKENS_REMOTE/$LOCAL_APP_ID.json"
  "${RSYNC_CMD[@]}" "$LOCAL_TOKEN_PATH" "$REMOTE:$USER_TOKENS_REMOTE/$LOCAL_APP_ID.json"
  "${SSH_CMD[@]}" "chmod 600 '$USER_TOKENS_REMOTE/$LOCAL_APP_ID.json'"
  echo "==> Done. Remote token file metadata:"
  "${SSH_CMD[@]}" "ls -l '$USER_TOKENS_REMOTE/$LOCAL_APP_ID.json'"
}

cmd_install_cron() {
  echo "==> Ensuring remote scripts + log dirs"
  "${SSH_CMD[@]}" "mkdir -p '$SHARED_RES_REMOTE/scripts' '$SHARED_RES_REMOTE/logs'"

  echo "==> Writing wrapper at $WRAPPER_REMOTE"
  local tmp_wrapper
  tmp_wrapper="$(mktemp)"
  remote_wrapper_contents >"$tmp_wrapper"
  "${SCP_CMD[@]}" "$tmp_wrapper" "$REMOTE:$WRAPPER_REMOTE" >/dev/null
  rm -f "$tmp_wrapper"
  "${SSH_CMD[@]}" "chmod +x '$WRAPPER_REMOTE'"

  echo "==> Verifying the Python check script is present on the server"
  "${SSH_CMD[@]}" "test -f '$ROLE_DIR_REMOTE/scripts/check_impersonation_token.py' || { echo 'check_impersonation_token.py missing on remote — redeploy the role first.' >&2; exit 1; }"

  echo "==> Installing crontab entry (schedule: $CRON_SCHEDULE)"
  local cron_line="$CRON_SCHEDULE $WRAPPER_REMOTE --only-on-change >>$LOG_REMOTE 2>&1 $CRON_MARKER"
  # Idempotent: strip any previous FeishuOPC marker line, append fresh.
  "${SSH_CMD[@]}" "crontab -l 2>/dev/null | grep -vF '$CRON_MARKER' > /tmp/cron.new || true; echo '$cron_line' >> /tmp/cron.new; crontab /tmp/cron.new && rm -f /tmp/cron.new"
  echo "==> Current crontab:"
  "${SSH_CMD[@]}" "crontab -l | grep --color=never -F '$CRON_MARKER' || echo '(entry not visible — check user crontab manually)'"
}

cmd_uninstall_cron() {
  echo "==> Removing FeishuOPC impersonation cron entry"
  "${SSH_CMD[@]}" "crontab -l 2>/dev/null | grep -vF '$CRON_MARKER' > /tmp/cron.new || true; crontab /tmp/cron.new && rm -f /tmp/cron.new"
  echo "==> Done."
}

cmd_run_once() {
  echo "==> Running remote check (dry run)"
  "${SSH_CMD[@]}" "test -x '$WRAPPER_REMOTE' || { echo 'wrapper missing; run install-cron first' >&2; exit 1; }"
  "${SSH_CMD[@]}" "$WRAPPER_REMOTE --dry-run" || echo "(remote exited non-zero — see summary above)"
}

cmd_status() {
  echo "==> Token file"
  "${SSH_CMD[@]}" "ls -l '$USER_TOKENS_REMOTE' 2>/dev/null || echo '(user_tokens dir missing — run push-token)'"
  echo
  echo "==> Cron entry"
  "${SSH_CMD[@]}" "crontab -l 2>/dev/null | grep --color=never -F '$CRON_MARKER' || echo '(no cron entry — run install-cron)'"
  echo
  echo "==> Last log tail"
  "${SSH_CMD[@]}" "tail -n 10 '$LOG_REMOTE' 2>/dev/null || echo '(no log yet)'"
}

cmd_all() {
  cmd_push_token
  cmd_install_cron
  cmd_run_once
}

case "$CMD" in
  push-token)      cmd_push_token ;;
  install-cron)    cmd_install_cron ;;
  uninstall-cron)  cmd_uninstall_cron ;;
  run-once)        cmd_run_once ;;
  status)          cmd_status ;;
  all)             cmd_all ;;
  -h|--help|help)  usage ;;
  *)
    echo "Unknown command: $CMD" >&2
    usage
    exit 1
    ;;
esac
