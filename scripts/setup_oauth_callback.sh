#!/usr/bin/env bash
# Install / (re)install the Feishu OAuth callback microservice on the
# deployment target. The service is the public HTTPS entrypoint behind
# a public HTTPS hostname (see OAUTH_CALLBACK_DOMAIN) that lets operators re-authorize the impersonation
# refresh_token by simply clicking a link in a Feishu alert, instead of
# having to run `spikes/probe_as_user.py auth-server` on their laptop.
#
# Prerequisites on the target host:
#   - .larkagent/agent_deploy.sh --all has run at least once (so the
#     shared-env/.venv + shared-resources exist and a role dir
#     contains the current feishu_agent code tree).
#   - sudo access for the deployment user (for systemctl + nginx +
#     certbot).
#   - Nginx and Certbot (`python3-certbot-nginx`) are installed; the
#     script will apt-install them on demand if missing.
#
# What this script does (idempotent):
#   1. Uploads the FastAPI unit template and the Nginx vhost, rendering
#      the placeholders against the chosen role's paths.
#   2. Installs /etc/systemd/system/feishu-oauth-callback.service and
#      /etc/nginx/sites-available/<your-oauth-domain>, enables both, and
#      reloads nginx.
#   3. Invokes `certbot --nginx` once to obtain the Let's Encrypt
#      certificate and convert the vhost to HTTPS. Re-running is safe:
#      Certbot is a no-op if a valid cert already exists.
#   4. Starts (or restarts) feishu-oauth-callback, then hits
#      https://<your-oauth-domain>/healthz to verify.
#
# Commands:
#   install   Full install / reinstall (default).
#   status    Show unit status + last log lines + cert expiry.
#   restart   Restart the unit without touching nginx/certbot.
#   uninstall Stop the unit and remove /etc/systemd + /etc/nginx
#             entries. Leaves the certificate and the Python code
#             behind.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEPLOY_ENV_DEFAULT="$REPO_ROOT/.larkagent/secrets/deploy/server_sv.env"
DEPLOY_ENV="${DEPLOY_ENV:-$DEPLOY_ENV_DEFAULT}"
JUMP_ENV_OVERRIDE=""
ROLE_NAME="${OAUTH_CALLBACK_ROLE:-tech-lead-planner}"
OAUTH_DOMAIN="${OAUTH_CALLBACK_DOMAIN:-oauth.example.com}"
OAUTH_PORT="${OAUTH_CALLBACK_PORT:-18766}"
CERTBOT_EMAIL="${OAUTH_CALLBACK_CERTBOT_EMAIL:-admin@example.com}"
SSH_CONFIG_FILE="$(mktemp)"
trap 'rm -f "$SSH_CONFIG_FILE"' EXIT

usage() {
  cat <<EOF
Usage: $0 [command] [--deploy-env PATH] [--jump-env PATH] [--role NAME] [--domain NAME] [--port N] [--email ADDR]

Commands:
  install     Install / reinstall the OAuth callback service (default).
  status      Show service + certificate status.
  restart     systemctl restart feishu-oauth-callback.
  uninstall   Stop + remove systemd and nginx entries (keeps cert + code).

Env overrides:
  DEPLOY_ENV                    deploy env file
  OAUTH_CALLBACK_ROLE           role dir to borrow venv + code from (default: tech-lead-planner)
  OAUTH_CALLBACK_DOMAIN         public domain (default: oauth.example.com — override for production)
  OAUTH_CALLBACK_PORT           local uvicorn port (default: 18766)
  OAUTH_CALLBACK_CERTBOT_EMAIL  Let's Encrypt contact email

Examples:
  $0 install
  $0 status
  OAUTH_CALLBACK_DOMAIN=oauth.example.com $0 install
EOF
}

CMD="install"
if [[ $# -gt 0 && "$1" != --* ]]; then
  CMD="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy-env) DEPLOY_ENV="$2"; shift 2 ;;
    --jump-env) JUMP_ENV_OVERRIDE="$2"; shift 2 ;;
    --role) ROLE_NAME="$2"; shift 2 ;;
    --domain) OAUTH_DOMAIN="$2"; shift 2 ;;
    --port) OAUTH_PORT="$2"; shift 2 ;;
    --email) CERTBOT_EMAIL="$2"; shift 2 ;;
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
else
  SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$PORT")
  [[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")
  SSH_CMD=(ssh "${SSH_OPTS[@]}" "$SERVER_USER@$HOST")
  SCP_CMD=(scp "${SSH_OPTS[@]}")
  REMOTE="$SERVER_USER@$HOST"
fi

PROJECT_ROOT_REMOTE="$PROJECTS_ROOT/$PROJECT_NAME"
SHARED_RES_REMOTE="$PROJECT_ROOT_REMOTE/shared-resources"
SHARED_ENV_REMOTE="$PROJECT_ROOT_REMOTE/shared-env"
ROLE_DIR_REMOTE="$PROJECT_ROOT_REMOTE/roles/$ROLE_NAME"
VENV_REMOTE="$SHARED_ENV_REMOTE/.venv"
LOG_DIR_REMOTE="$SHARED_RES_REMOTE/logs"
PUBLIC_REDIRECT="https://${OAUTH_DOMAIN}/feishu/callback"
UNIT_NAME="feishu-oauth-callback"
NGINX_SITE="${OAUTH_DOMAIN}"

render_unit() {
  sed \
    -e "s|__USER__|$SERVER_USER|g" \
    -e "s|__REMOTE_DIR__|$ROLE_DIR_REMOTE|g" \
    -e "s|__SHARED_RES_DIR__|$SHARED_RES_REMOTE|g" \
    -e "s|__PUBLIC_REDIRECT__|$PUBLIC_REDIRECT|g" \
    -e "s|__VENV_DIR__|$VENV_REMOTE|g" \
    -e "s|__PORT__|$OAUTH_PORT|g" \
    -e "s|__LOG_DIR__|$LOG_DIR_REMOTE|g" \
    "$REPO_ROOT/deploy/systemd/feishu-oauth-callback.service.tmpl"
}

render_nginx() {
  sed \
    -e "s|__PORT__|$OAUTH_PORT|g" \
    -e "s|oauth.example.com|$OAUTH_DOMAIN|g" \
    "$REPO_ROOT/deploy/nginx/oauth.example.com.conf"
}

cmd_install() {
  echo "==> Preflight: shared venv, role dir, shared resources"
  "${SSH_CMD[@]}" "test -x '$VENV_REMOTE/bin/python' || { echo 'Missing shared venv at $VENV_REMOTE — run .larkagent/agent_deploy.sh --all first.' >&2; exit 1; }"
  "${SSH_CMD[@]}" "test -d '$ROLE_DIR_REMOTE' || { echo 'Missing role dir $ROLE_DIR_REMOTE — role $ROLE_NAME not deployed.' >&2; exit 1; }"
  "${SSH_CMD[@]}" "test -f '$ROLE_DIR_REMOTE/feishu_agent/oauth_callback_main.py' || { echo 'oauth_callback_main.py missing on remote — redeploy the role first.' >&2; exit 1; }"
  "${SSH_CMD[@]}" "mkdir -p '$LOG_DIR_REMOTE'"

  echo "==> Ensuring nginx + certbot are installed"
  "${SSH_CMD[@]}" "command -v nginx >/dev/null && command -v certbot >/dev/null && dpkg -s python3-certbot-nginx >/dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq nginx certbot python3-certbot-nginx; }"

  echo "==> Uploading systemd unit"
  local tmp_unit
  tmp_unit="$(mktemp)"
  render_unit >"$tmp_unit"
  "${SCP_CMD[@]}" "$tmp_unit" "$REMOTE:/tmp/${UNIT_NAME}.service" >/dev/null
  rm -f "$tmp_unit"
  "${SSH_CMD[@]}" "sudo mv '/tmp/${UNIT_NAME}.service' '/etc/systemd/system/${UNIT_NAME}.service' && sudo chmod 644 '/etc/systemd/system/${UNIT_NAME}.service' && sudo systemctl daemon-reload"

  echo "==> Uploading nginx vhost (HTTP-only stage 1, skipped if Certbot already rewrote it)"
  # IMPORTANT: once Certbot has rewritten sites-available/<domain> to add the
  # :443 server block + redirect, re-uploading the HTTP-only template here
  # would silently drop HTTPS (Certbot would then no-op the re-issuance and
  # the site would come back as plain :80 behind a valid cert). Detect that
  # case by checking for Certbot's "managed-by-Certbot" marker on the remote
  # file and leaving it alone if found.
  local already_https
  already_https="$("${SSH_CMD[@]}" "sudo test -f '/etc/nginx/sites-available/${NGINX_SITE}' && sudo grep -q 'managed by Certbot' '/etc/nginx/sites-available/${NGINX_SITE}' && echo yes || echo no")"
  if [[ "$already_https" == "yes" ]]; then
    echo "   (existing vhost is Certbot-managed HTTPS; leaving it in place)"
    "${SSH_CMD[@]}" "sudo ln -sf '/etc/nginx/sites-available/${NGINX_SITE}' '/etc/nginx/sites-enabled/${NGINX_SITE}' && sudo nginx -t && sudo systemctl reload nginx"
  else
    local tmp_nginx
    tmp_nginx="$(mktemp)"
    render_nginx >"$tmp_nginx"
    "${SCP_CMD[@]}" "$tmp_nginx" "$REMOTE:/tmp/${NGINX_SITE}.conf" >/dev/null
    rm -f "$tmp_nginx"
    "${SSH_CMD[@]}" "sudo mv '/tmp/${NGINX_SITE}.conf' '/etc/nginx/sites-available/${NGINX_SITE}' && sudo chmod 644 '/etc/nginx/sites-available/${NGINX_SITE}' && sudo ln -sf '/etc/nginx/sites-available/${NGINX_SITE}' '/etc/nginx/sites-enabled/${NGINX_SITE}' && sudo nginx -t && sudo systemctl reload nginx"
  fi

  echo "==> Starting ${UNIT_NAME} before Certbot (Certbot needs :80 → proxy to service for ACME)"
  "${SSH_CMD[@]}" "sudo systemctl enable '${UNIT_NAME}' >/dev/null 2>&1 || true && sudo systemctl restart '${UNIT_NAME}' && sudo systemctl is-active '${UNIT_NAME}'"

  echo "==> Ensuring HTTPS for ${OAUTH_DOMAIN} (email=${CERTBOT_EMAIL})"
  # Three branches, in order of preference:
  #   a) vhost already Certbot-managed → nothing to do.
  #   b) cert exists but vhost is plain HTTP (e.g. after a vhost overwrite)
  #      → re-install via `certbot --nginx --reinstall` so Certbot rewrites
  #      the vhost without touching the cert itself.
  #   c) no cert yet → issue one normally.
  "${SSH_CMD[@]}" "bash -s" <<REMOTE_CERTBOT
set -euo pipefail
if sudo grep -q 'managed by Certbot' '/etc/nginx/sites-available/${NGINX_SITE}' 2>/dev/null; then
  echo '(vhost already Certbot-managed — skipping)'
elif sudo test -f /etc/letsencrypt/live/${OAUTH_DOMAIN}/fullchain.pem; then
  echo '(cert present but vhost is HTTP-only — reinstalling HTTPS config)'
  sudo certbot --nginx -d '${OAUTH_DOMAIN}' --non-interactive --agree-tos \
    -m '${CERTBOT_EMAIL}' --redirect --no-eff-email --reinstall --keep-until-expiring
else
  sudo certbot --nginx -d '${OAUTH_DOMAIN}' --non-interactive --agree-tos \
    -m '${CERTBOT_EMAIL}' --redirect --no-eff-email
fi
REMOTE_CERTBOT

  echo "==> Reloading nginx (cert may have been installed)"
  "${SSH_CMD[@]}" "sudo nginx -t && sudo systemctl reload nginx"

  echo "==> Health checks"
  "${SSH_CMD[@]}" "curl -sS -o /dev/null -w 'local:  http_code=%{http_code}\n' -m 5 http://127.0.0.1:${OAUTH_PORT}/healthz"
  "${SSH_CMD[@]}" "curl -sS -o /dev/null -w 'public: http_code=%{http_code}\n' -m 10 https://${OAUTH_DOMAIN}/healthz || echo '(public healthz failed — may be DNS/cert issue)'"

  echo "==> Done. Authorize URL:"
  echo "    https://${OAUTH_DOMAIN}/feishu/authorize?app=application_agent"
}

cmd_status() {
  echo "==> systemd unit"
  "${SSH_CMD[@]}" "sudo systemctl is-active '${UNIT_NAME}' || true; sudo systemctl status '${UNIT_NAME}' --no-pager -l | head -20"
  echo
  echo "==> nginx vhost"
  "${SSH_CMD[@]}" "sudo nginx -T 2>/dev/null | grep -A2 'server_name ${OAUTH_DOMAIN}' || echo '(no vhost)'"
  echo
  echo "==> certificate"
  "${SSH_CMD[@]}" "sudo test -f /etc/letsencrypt/live/${OAUTH_DOMAIN}/fullchain.pem && sudo openssl x509 -in /etc/letsencrypt/live/${OAUTH_DOMAIN}/fullchain.pem -noout -dates -subject || echo '(no cert yet)'"
  echo
  echo "==> last log tail"
  "${SSH_CMD[@]}" "tail -n 20 '${LOG_DIR_REMOTE}/oauth-callback.log' 2>/dev/null || echo '(no log yet)'"
}

cmd_restart() {
  "${SSH_CMD[@]}" "sudo systemctl restart '${UNIT_NAME}' && sudo systemctl is-active '${UNIT_NAME}'"
}

cmd_uninstall() {
  "${SSH_CMD[@]}" "sudo systemctl stop '${UNIT_NAME}' 2>/dev/null || true; sudo systemctl disable '${UNIT_NAME}' 2>/dev/null || true; sudo rm -f '/etc/systemd/system/${UNIT_NAME}.service' && sudo systemctl daemon-reload"
  "${SSH_CMD[@]}" "sudo rm -f '/etc/nginx/sites-enabled/${NGINX_SITE}' '/etc/nginx/sites-available/${NGINX_SITE}' && sudo nginx -t && sudo systemctl reload nginx"
  echo "==> Uninstalled. (Certificate at /etc/letsencrypt/live/${OAUTH_DOMAIN}/ left in place.)"
}

case "$CMD" in
  install) cmd_install ;;
  status) cmd_status ;;
  restart) cmd_restart ;;
  uninstall) cmd_uninstall ;;
  *) echo "Unknown command: $CMD" >&2; usage; exit 1 ;;
esac
