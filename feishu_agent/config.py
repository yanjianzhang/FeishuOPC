import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "FeishuOPC Agent"
    debug: bool = False
    app_repo_root: str | None = None
    default_project_id: str | None = None

    feishu_bot_app_id: str | None = None
    feishu_bot_app_secret: str | None = None
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    tech_lead_feishu_bot_app_id: str | None = None
    tech_lead_feishu_bot_app_secret: str | None = None
    tech_lead_feishu_verification_token: str | None = None
    tech_lead_feishu_encrypt_key: str | None = None
    tech_lead_bot_open_id: str = ""
    product_manager_feishu_bot_app_id: str | None = None
    product_manager_feishu_bot_app_secret: str | None = None
    product_manager_feishu_verification_token: str | None = None
    product_manager_feishu_encrypt_key: str | None = None
    feishu_bitable_base_url: str | None = None
    feishu_default_bitable_app_token: str | None = None
    feishu_default_bitable_table_id: str | None = None
    feishu_default_user_access_token: str | None = None

    techbot_llm_api_key: str | None = None
    techbot_llm_base_url: str | None = None
    techbot_llm_model: str | None = None
    techbot_llm_source: str | None = None
    techbot_llm_config_path: str | None = None
    techbot_llm_env_path: str | None = None
    techbot_llm_timeout_seconds: float = 30.0
    techbot_run_log_dir: str = "data/techbot-runs"
    techbot_allow_repo_state_writes: bool = True
    # Root directory for append-only task event logs (one subdir per
    # Feishu-thread-scoped ``task_id``). Every bot session emits events
    # under this root; see ``feishu_agent.team.task_service``.
    feishu_tasks_root: str = "data/tasks"
    # Master opt-in for writing the event log. Defaults ON. When disabled,
    # ``TaskService`` still constructs but the runtime skips
    # ``open_or_resume`` and falls back to the legacy per-role logs only.
    task_event_log_enabled: bool = True

    # A-3 Role Artifact Envelope (spec 004). When ON (default), each
    # ``dispatch_role_agent`` writes a JSON summary under
    # ``{techbot_run_log_dir}/teams/{root_trace_id}/artifacts/``. The
    # flag exists so the side-effect can be disabled for cold-start
    # benchmarks or for environments where disk is constrained; the
    # TL swallows write failures to a warning even when ON, so this
    # is strictly an *intent* switch, not a safety switch.
    artifact_store_enabled: bool = True

    # Whole-role session envelope. Bumped 420→900s to leave headroom
    # for the provider pool's longer retry/backoff schedule (up to ~130s
    # of pure sleep across 3 retries) and for Claude-class LLM calls
    # that take 60-180s when the context is heavy. 900s = 15min per
    # role session is the new hard wall; sub-dispatches (developer etc.)
    # inherit the remainder via ``_remaining_seconds``.
    role_agent_timeout_seconds: int = 900
    role_agent_default_model: str | None = None
    role_agent_max_tool_turns: int = 64

    # B-2 effect-aware fan-out. Caps concurrent tool executions WITHIN a
    # single LLM turn. The partitioner groups tool calls by effect
    # (self/read run in parallel; world calls are serialized) and this
    # semaphore bounds the gather width inside each concurrent group.
    # ``1`` fully disables fan-out (pure sequential, identical to
    # pre-B-2 behaviour) — useful as a kill-switch. Upper bound of ``8``
    # keeps the runtime within provider-pool concurrency budgets.
    max_parallel_tool_calls: int = 3

    # B-3 git-worktree isolation. When enabled (default), a child
    # dispatch whose role declares ``needs_worktree: true`` runs in
    # its own ``.worktrees/{trace_id}`` checkout on an ``agent/*``
    # branch so two concurrent code-writing dispatches don't contend
    # on the main repo's ``repo_filelock``. Disable
    # (``FEISHU_ENABLE_WORKTREE_ISOLATION=false``) to force the
    # pre-B-3 serial behaviour — useful as a staged kill-switch or
    # on constrained CI runners where worktree disk pressure hurts.
    enable_worktree_isolation: bool = True
    pm_notify_tech_lead_chat_id: str | None = None
    application_agent_open_id: str = ""
    application_agent_group_chat_id: str = ""
    application_agent_display_name: str = "Application delegate"
    application_agent_delegate_url: str | None = None

    # Impersonation ("send as user") lets tech-lead delegate messages
    # reach downstream delegate bots that only wake on human @mentions
    # by attributing the message to an authorized human. Without this the bot-to-bot
    # @mention is not delivered by Feishu and OpenClaw never triggers.
    # Defaults are ON — if a token file is present under
    # ``impersonation_token_dir/<app_id>.json`` it is used automatically;
    # otherwise the runtime silently falls back to the bot-IM path.
    impersonation_enabled: bool = True
    impersonation_app_id: str = ""
    impersonation_app_secret: str = ""
    impersonation_token_dir: str = ".larkagent/secrets/user_tokens"

    # Tier-1 harness knobs. Compression is ON by default (see below);
    # the remaining features (secondary provider, post-dispatch
    # verification, per-project notes) are still opt-in and fall back
    # to no-op when unset. The runtime builders in
    # ``feishu_runtime_service`` only activate a feature when these
    # fields resolve to meaningful values, so declaring them here is
    # the single place to flip things on via env / .env.
    # Compression is now ON by default. ``cla4.6-opus`` (and most
    # Claude-class models we run against) ship a 200k context window;
    # we cap usable input at 180k so TailWindowCompressor has a safety
    # margin for the system prompt + tool schemas + the model's own
    # tool-call reply budget. Operators can still disable by exporting
    # ``LLM_MAX_CONTEXT_TOKENS=0`` in the role env.
    llm_max_context_tokens: int = 180_000
    llm_compression_trigger: float = 0.7
    llm_compression_keep_tail: int = 6
    # Cap on output tokens per /chat/completions response. If 0, we don't
    # send ``max_tokens`` at all and let the upstream relay pick (which
    # for Anthropic-compat relays has been as low as 4096 and truncated
    # large tool-call JSON). 16384 is big enough for a 30-file
    # ``write_project_code_batch`` envelope while still leaving the full
    # 180k context for input — set higher if a role starts hitting it
    # again.
    llm_max_output_tokens: int = 16384
    llm_secondary_base_url: str | None = None
    llm_secondary_api_key: str | None = None
    llm_secondary_model: str | None = None
    # Transport for the secondary (fallback) provider. Defaults to the
    # OpenAI-compatible HTTP path that the primary uses — same pool
    # client, same ``Authorization: Bearer`` header, same
    # ``/chat/completions`` endpoint. Set to ``"anthropic_bedrock"`` to
    # route the fallback through the ``anthropic[bedrock]`` SDK (SigV4
    # signing, Anthropic Messages API, Bedrock inference profile ARN);
    # the adapter and the provider pool then branch on this value.
    llm_secondary_transport: str = "openai_http"
    llm_secondary_aws_region: str | None = None
    llm_secondary_aws_access_key_id: str | None = None
    llm_secondary_aws_secret_access_key: str | None = None
    llm_retries_per_provider: int = 2
    agent_notes_max_per_session: int = 5
    agent_notes_prompt_limit: int = 20
    # Per-project "last run memory": when enabled, every tech-lead
    # session writes a compact digest to
    # ``<project_root>/.feishu_run_history.jsonl`` via the HookBus, and
    # the most recent *non-success* digest is prepended to the next
    # session's system prompt as a ``## Last run context`` block.
    # Set to ``false`` to opt out project-wide (history file stays
    # untouched; existing records are ignored on read).
    last_run_memory_enabled: bool = True

    # Tier-2 harness knobs. ``mcp_servers_config_path`` points at a
    # JSONL file (one MCP server per line, schema in
    # ``mcp_tool_adapter.py``). When unset or missing, MCP support is
    # off — runtime never imports ``mcp_tool_adapter`` so we don't
    # spawn subprocesses at boot.
    #
    # ``lineage_audit_enabled`` controls whether the session lineage
    # tree is persisted to the audit dir as a sibling JSON. Off by
    # default because lineage is most useful during active debugging;
    # leaving it opt-in keeps the audit dir small in production.
    mcp_servers_config_path: str = ""
    mcp_call_timeout_seconds: float = 30.0
    lineage_audit_enabled: bool = False

    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"

    # Pydantic v2: replace the deprecated class-based ``Config`` with an
    # explicit :class:`SettingsConfigDict`. Behavior is identical; the
    # migration just removes the ``PydanticDeprecatedSince20`` warning
    # that class-based config emits in ``pydantic>=2``.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


def _load_secret_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue

        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            values[key] = value
    return values


def _load_feishu_bot_secret_file(
    path: Path, prefix: str, *, include_legacy: bool = False
) -> dict[str, str]:
    values = _load_secret_file(path)
    mapped: dict[str, str] = {}
    app_id = values.get("FEISHU_BOT_APP_ID") or values.get("AppID")
    app_secret = values.get("FEISHU_BOT_APP_SECRET") or values.get("AppSecret")
    verification_token = values.get("FEISHU_VERIFICATION_TOKEN") or values.get(
        "VerificationToken"
    )
    encrypt_key = values.get("FEISHU_ENCRYPT_KEY") or values.get("EncryptKey")
    if app_id:
        mapped[f"{prefix}_feishu_bot_app_id"] = app_id
        if include_legacy:
            mapped["feishu_bot_app_id"] = app_id
    if app_secret:
        mapped[f"{prefix}_feishu_bot_app_secret"] = app_secret
        if include_legacy:
            mapped["feishu_bot_app_secret"] = app_secret
    if verification_token:
        mapped[f"{prefix}_feishu_verification_token"] = verification_token
        if include_legacy:
            mapped["feishu_verification_token"] = verification_token
    if encrypt_key:
        mapped[f"{prefix}_feishu_encrypt_key"] = encrypt_key
        if include_legacy:
            mapped["feishu_encrypt_key"] = encrypt_key
    if prefix == "tech_lead":
        bot_open = (
            values.get("TECH_LEAD_BOT_OPEN_ID") or values.get("BotOpenID") or ""
        ).strip()
        if bot_open:
            mapped["tech_lead_bot_open_id"] = bot_open
    return mapped


def _load_agent_llm_settings(repo_root: Path) -> dict[str, str]:
    env_path = repo_root / ".larkagent" / "secrets" / "ai_key" / ".env"
    config_path = repo_root / ".larkagent" / "secrets" / "ai_key" / "model_sources.json"
    values = _load_secret_file(env_path)
    mapped: dict[str, str] = {}
    mapped["techbot_llm_env_path"] = str(env_path)
    mapped["techbot_llm_config_path"] = str(config_path)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            config = {}
        source = (config.get("model_source") or "").strip()
        if source:
            mapped["techbot_llm_source"] = source
        openai_compatible = config.get("openai_compatible") or {}
        if source == "openai_compatible":
            if openai_compatible.get("url"):
                mapped.setdefault(
                    "techbot_llm_base_url", str(openai_compatible["url"])
                )
            if openai_compatible.get("model"):
                mapped.setdefault(
                    "techbot_llm_model", str(openai_compatible["model"])
                )
            oc_key_env = openai_compatible.get("api_key_env", "OPENAI_COMPATIBLE_API_KEY")
            if values.get(oc_key_env):
                mapped.setdefault("techbot_llm_api_key", values[oc_key_env])
        elif source == "volcengine_ark":
            ark_config = config.get("volcengine_ark") or {}
            if ark_config.get("base_url"):
                mapped.setdefault(
                    "techbot_llm_base_url", str(ark_config["base_url"])
                )
            if ark_config.get("model"):
                mapped.setdefault(
                    "techbot_llm_model", str(ark_config["model"])
                )
            ark_key_env = ark_config.get("api_key_env", "VOLCENGINE_ARK_API_KEY")
            if values.get(ark_key_env):
                mapped.setdefault("techbot_llm_api_key", values[ark_key_env])
        elif source == "aws_bedrock":
            aws_bedrock = config.get("aws_bedrock") or {}
            model_id = aws_bedrock.get("model_id") or values.get(
                aws_bedrock.get("model_id_env", "ANTHROPIC_MODEL")
            )
            if model_id:
                mapped.setdefault("techbot_llm_model", str(model_id))

    if values.get("OPENAI_API_KEY"):
        mapped.setdefault("techbot_llm_api_key", values["OPENAI_API_KEY"])
    if values.get("OPENAI_COMPATIBLE_API_KEY"):
        mapped.setdefault("techbot_llm_api_key", values["OPENAI_COMPATIBLE_API_KEY"])
    if values.get("ANTHROPIC_MODEL"):
        mapped.setdefault("techbot_llm_model", values["ANTHROPIC_MODEL"])

    if values.get("OPENCLAW_DEFAULT_MODEL"):
        mapped.setdefault(
            "role_agent_default_model", values["OPENCLAW_DEFAULT_MODEL"]
        )

    # ------------------------------------------------------------------
    # Fallback (secondary) provider wiring.
    #
    # ``model_sources.json`` carries an optional ``fallback_model_source``
    # key naming another section in the same file (``aws_bedrock`` /
    # ``volcengine_ark`` / ``openai_compatible`` / ``openai``). When
    # present, we map the named section into ``llm_secondary_*`` settings
    # so ``_build_provider_pool()`` downstream can construct a second
    # provider with real failover behavior.
    #
    # Why the mapping lives here and not in the pool builder:
    # ``_build_provider_pool`` reads ``settings`` attributes (which are
    # Pydantic-typed and env-overridable); keeping the JSON-driven
    # translation in this loader means ops can still override any
    # individual field by environment variable in ``server.env`` without
    # shipping a new ``model_sources.json``.
    if config_path.exists():
        fallback_source = (
            (config.get("fallback_model_source") or "").strip()
            if isinstance(config, dict)
            else ""
        )
        if fallback_source:
            section = config.get(fallback_source) or {}
            if fallback_source == "aws_bedrock":
                model_id = section.get("model_id") or values.get(
                    section.get("model_id_env", "ANTHROPIC_MODEL")
                )
                region = values.get(section.get("region_env", "AWS_REGION"))
                access = values.get(
                    section.get("access_key_id_env", "AWS_ACCESS_KEY_ID")
                )
                secret = values.get(
                    section.get("secret_access_key_env", "AWS_SECRET_ACCESS_KEY")
                )
                # Bedrock fallback requires model + region + both keys
                # to be usable. If any are missing we silently skip
                # (rather than half-wiring and failing at first call) —
                # operators then see a single-provider pool in logs and
                # know the secret env is incomplete.
                if model_id and region and access and secret:
                    mapped.setdefault("llm_secondary_transport", "anthropic_bedrock")
                    mapped.setdefault("llm_secondary_model", str(model_id))
                    mapped.setdefault("llm_secondary_aws_region", region)
                    mapped.setdefault("llm_secondary_aws_access_key_id", access)
                    mapped.setdefault("llm_secondary_aws_secret_access_key", secret)
            elif fallback_source == "openai_compatible":
                url = section.get("url")
                key_env = section.get("api_key_env", "OPENAI_COMPATIBLE_API_KEY")
                key = values.get(key_env)
                if url and key:
                    mapped.setdefault("llm_secondary_transport", "openai_http")
                    mapped.setdefault("llm_secondary_base_url", str(url))
                    mapped.setdefault("llm_secondary_api_key", key)
                    if section.get("model"):
                        mapped.setdefault("llm_secondary_model", str(section["model"]))
            elif fallback_source == "volcengine_ark":
                url = section.get("base_url")
                key_env = section.get("api_key_env", "VOLCENGINE_ARK_API_KEY")
                key = values.get(key_env)
                if url and key:
                    mapped.setdefault("llm_secondary_transport", "openai_http")
                    mapped.setdefault("llm_secondary_base_url", str(url))
                    mapped.setdefault("llm_secondary_api_key", key)
                    if section.get("model"):
                        mapped.setdefault("llm_secondary_model", str(section["model"]))
            elif fallback_source == "openai":
                key_env = section.get("api_key_env", "OPENAI_API_KEY")
                key = values.get(key_env)
                if key:
                    mapped.setdefault("llm_secondary_transport", "openai_http")
                    mapped.setdefault(
                        "llm_secondary_base_url",
                        str(section.get("base_url") or "https://api.openai.com/v1"),
                    )
                    mapped.setdefault("llm_secondary_api_key", key)
                    if section.get("model"):
                        mapped.setdefault("llm_secondary_model", str(section["model"]))

    return mapped


_SETTINGS_KEY_MAP = {
    "DEBUG": "debug",
    "APP_REPO_ROOT": "app_repo_root",
    "DEFAULT_PROJECT_ID": "default_project_id",
    "SECRET_KEY": "secret_key",
    "FEISHU_BOT_APP_ID": "feishu_bot_app_id",
    "FEISHU_BOT_APP_SECRET": "feishu_bot_app_secret",
    "FEISHU_VERIFICATION_TOKEN": "feishu_verification_token",
    "FEISHU_ENCRYPT_KEY": "feishu_encrypt_key",
    "TECH_LEAD_FEISHU_BOT_APP_ID": "tech_lead_feishu_bot_app_id",
    "TECH_LEAD_FEISHU_BOT_APP_SECRET": "tech_lead_feishu_bot_app_secret",
    "TECH_LEAD_FEISHU_VERIFICATION_TOKEN": "tech_lead_feishu_verification_token",
    "TECH_LEAD_FEISHU_ENCRYPT_KEY": "tech_lead_feishu_encrypt_key",
    "TECH_LEAD_BOT_OPEN_ID": "tech_lead_bot_open_id",
    "PRODUCT_MANAGER_FEISHU_BOT_APP_ID": "product_manager_feishu_bot_app_id",
    "PRODUCT_MANAGER_FEISHU_BOT_APP_SECRET": "product_manager_feishu_bot_app_secret",
    "PRODUCT_MANAGER_FEISHU_VERIFICATION_TOKEN": "product_manager_feishu_verification_token",
    "PRODUCT_MANAGER_FEISHU_ENCRYPT_KEY": "product_manager_feishu_encrypt_key",
    "BITABLE_BASE_URL": "feishu_bitable_base_url",
    "FEISHU_DEFAULT_BITABLE_APP_TOKEN": "feishu_default_bitable_app_token",
    "FEISHU_DEFAULT_BITABLE_TABLE_ID": "feishu_default_bitable_table_id",
    "FEISHU_DEFAULT_USER_ACCESS_TOKEN": "feishu_default_user_access_token",
    "TECHBOT_LLM_API_KEY": "techbot_llm_api_key",
    "TECHBOT_LLM_BASE_URL": "techbot_llm_base_url",
    "TECHBOT_LLM_MODEL": "techbot_llm_model",
    "TECHBOT_LLM_SOURCE": "techbot_llm_source",
    "TECHBOT_LLM_CONFIG_PATH": "techbot_llm_config_path",
    "TECHBOT_LLM_ENV_PATH": "techbot_llm_env_path",
    "TECHBOT_LLM_TIMEOUT_SECONDS": "techbot_llm_timeout_seconds",
    "TECHBOT_RUN_LOG_DIR": "techbot_run_log_dir",
    "TECHBOT_ALLOW_REPO_STATE_WRITES": "techbot_allow_repo_state_writes",
    "FEISHU_TASKS_ROOT": "feishu_tasks_root",
    "TASK_EVENT_LOG_ENABLED": "task_event_log_enabled",
    "ARTIFACT_STORE_ENABLED": "artifact_store_enabled",
    "OPENCLAW_DEFAULT_MODEL": "role_agent_default_model",
    "ROLE_AGENT_TIMEOUT_SECONDS": "role_agent_timeout_seconds",
    "ROLE_AGENT_DEFAULT_MODEL": "role_agent_default_model",
    "PM_NOTIFY_TECH_LEAD_CHAT_ID": "pm_notify_tech_lead_chat_id",
    "APPLICATION_AGENT_OPEN_ID": "application_agent_open_id",
    "APPLICATION_AGENT_GROUP_CHAT_ID": "application_agent_group_chat_id",
    "APPLICATION_AGENT_DISPLAY_NAME": "application_agent_display_name",
    "APPLICATION_AGENT_DELEGATE_URL": "application_agent_delegate_url",
    "IMPERSONATION_ENABLED": "impersonation_enabled",
    "IMPERSONATION_APP_ID": "impersonation_app_id",
    "IMPERSONATION_APP_SECRET": "impersonation_app_secret",
    "IMPERSONATION_TOKEN_DIR": "impersonation_token_dir",
    "LLM_MAX_CONTEXT_TOKENS": "llm_max_context_tokens",
    "LLM_COMPRESSION_TRIGGER": "llm_compression_trigger",
    "LLM_COMPRESSION_KEEP_TAIL": "llm_compression_keep_tail",
    "LLM_MAX_OUTPUT_TOKENS": "llm_max_output_tokens",
    "LLM_SECONDARY_BASE_URL": "llm_secondary_base_url",
    "LLM_SECONDARY_API_KEY": "llm_secondary_api_key",
    "LLM_SECONDARY_MODEL": "llm_secondary_model",
    "LLM_SECONDARY_TRANSPORT": "llm_secondary_transport",
    "LLM_SECONDARY_AWS_REGION": "llm_secondary_aws_region",
    "LLM_SECONDARY_AWS_ACCESS_KEY_ID": "llm_secondary_aws_access_key_id",
    "LLM_SECONDARY_AWS_SECRET_ACCESS_KEY": "llm_secondary_aws_secret_access_key",
    "LLM_RETRIES_PER_PROVIDER": "llm_retries_per_provider",
    "AGENT_NOTES_MAX_PER_SESSION": "agent_notes_max_per_session",
    "AGENT_NOTES_PROMPT_LIMIT": "agent_notes_prompt_limit",
    "LAST_RUN_MEMORY_ENABLED": "last_run_memory_enabled",
    "MCP_SERVERS_CONFIG_PATH": "mcp_servers_config_path",
    "MCP_CALL_TIMEOUT_SECONDS": "mcp_call_timeout_seconds",
    "LINEAGE_AUDIT_ENABLED": "lineage_audit_enabled",
}


def _normalize_settings_keys(values: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized[_SETTINGS_KEY_MAP.get(key, key)] = value
    return normalized


def _looks_like_repo_root(path: Path) -> bool:
    if (path / ".larkagent").exists():
        return True
    if (path / "project-adapters").exists() and (path / "feishu_agent").exists():
        return True
    return False


def _discover_repo_root(config_path: Path) -> Path | None:
    explicit_root = os.environ.get("APP_REPO_ROOT")
    candidates: list[Path] = []
    if explicit_root:
        candidates.append(Path(explicit_root).expanduser())
    candidates.append(Path.cwd())
    candidates.extend(config_path.parents)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _looks_like_repo_root(resolved):
            return resolved
    return None


@lru_cache
def get_settings() -> Settings:
    config_path = Path(__file__).resolve()
    secret_overrides: dict[str, str] = {}
    repo_root = _discover_repo_root(config_path)
    if repo_root:
        agent_bitable_path = (
            repo_root / ".larkagent" / "secrets" / "feishu_app" / "bitable.env"
        )
        if agent_bitable_path.exists():
            secret_overrides.update(
                _normalize_settings_keys(_load_secret_file(agent_bitable_path))
            )

        tech_lead_secret_path = (
            repo_root / ".larkagent" / "secrets" / "feishu_bot" / "tech-lead-planner.env"
        )
        if tech_lead_secret_path.exists():
            secret_overrides.update(
                _load_feishu_bot_secret_file(
                    tech_lead_secret_path,
                    "tech_lead",
                    include_legacy=True,
                )
            )

        product_manager_secret_path = (
            repo_root
            / ".larkagent"
            / "secrets"
            / "feishu_bot"
            / "product-manager-prd.env"
        )
        if product_manager_secret_path.exists():
            secret_overrides.update(
                _load_feishu_bot_secret_file(
                    product_manager_secret_path,
                    "product_manager",
                )
            )

        app_agent_path = (
            repo_root / ".larkagent" / "secrets" / "feishu_bot" / "application_agent.env"
        )
        if app_agent_path.exists():
            app_agent_values = _load_secret_file(app_agent_path)
            if app_agent_values.get("APPLICATION_AGENT_OPEN_ID"):
                secret_overrides["application_agent_open_id"] = app_agent_values["APPLICATION_AGENT_OPEN_ID"]
            if app_agent_values.get("APPLICATION_AGENT_GROUP_CHAT_ID"):
                secret_overrides["application_agent_group_chat_id"] = app_agent_values["APPLICATION_AGENT_GROUP_CHAT_ID"]
            app_label = (app_agent_values.get("AppName") or "").strip()
            if app_label:
                secret_overrides["application_agent_display_name"] = app_label
            delegate_url = (app_agent_values.get("APPLICATION_AGENT_DELEGATE_URL") or "").strip()
            if delegate_url:
                secret_overrides["application_agent_delegate_url"] = delegate_url
            tl_open = (app_agent_values.get("TECH_LEAD_BOT_OPEN_ID") or "").strip()
            if tl_open:
                secret_overrides["tech_lead_bot_open_id"] = tl_open
            # Default the impersonation app to the delegate Feishu app whose
            # credentials live in this file, unless explicitly overridden by env.
            # Bot-to-bot @mentions are unreliable for some delegate bots, so
            # we send as a real human using this app's OAuth token when configured.
            imp_app_id = (
                app_agent_values.get("IMPERSONATION_APP_ID")
                or app_agent_values.get("AppID")
                or ""
            ).strip()
            if imp_app_id:
                secret_overrides["impersonation_app_id"] = imp_app_id
            imp_app_secret = (
                app_agent_values.get("IMPERSONATION_APP_SECRET")
                or app_agent_values.get("AppSecret")
                or ""
            ).strip()
            if imp_app_secret:
                secret_overrides["impersonation_app_secret"] = imp_app_secret
            imp_flag = (app_agent_values.get("IMPERSONATION_ENABLED") or "").strip()
            if imp_flag:
                secret_overrides["impersonation_enabled"] = imp_flag.lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            imp_dir = (app_agent_values.get("IMPERSONATION_TOKEN_DIR") or "").strip()
            if imp_dir:
                secret_overrides["impersonation_token_dir"] = imp_dir

        secret_overrides.update(_load_agent_llm_settings(repo_root))
        secret_overrides["app_repo_root"] = str(repo_root)

    default_pid = os.environ.get("DEFAULT_PROJECT_ID")
    if default_pid:
        secret_overrides["default_project_id"] = default_pid

    return Settings(**secret_overrides)
