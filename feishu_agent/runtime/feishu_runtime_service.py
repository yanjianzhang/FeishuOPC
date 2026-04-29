from __future__ import annotations

import base64
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from feishu_fastapi_sdk import FeishuAuthConfig

from feishu_agent.config import get_settings
from feishu_agent.core.agent_types import AgentToolExecutor
from feishu_agent.core.cancel_token import GLOBAL_REGISTRY
from feishu_agent.core.combined_executor import CombinedExecutor
from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.generic_role_executor import GenericRoleExecutor
from feishu_agent.roles.pm_executor import PMToolExecutor
from feishu_agent.roles.role_executors import register_role_executors
from feishu_agent.roles.role_registry_service import RoleDefinition, RoleRegistryService
from feishu_agent.roles.tech_lead_executor import TechLeadToolExecutor
from feishu_agent.runtime.impersonation_token_service import ImpersonationTokenService
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient
from feishu_agent.team.artifact_publish_service import ArtifactPublishService
from feishu_agent.team.artifact_store import ArtifactStore
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.memory_assembler import (
    MemoryAssembler,
    MemoryQueryContext,
)
from feishu_agent.team.memory_writer import MemoryWriterService
from feishu_agent.team.pending_action_service import PendingAction, PendingActionService
from feishu_agent.team.session_summary_service import SessionSummaryService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_graph import TaskGraph
from feishu_agent.team.worktree_manager import WorktreeManager
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_event_projector import TaskEventProjector
from feishu_agent.team.task_service import (
    TaskHandle,
    TaskService,
    get_default_task_service,
)
from feishu_agent.team.task_state_executor import TaskStateExecutor
from feishu_agent.team.tier2_wiring import (
    Tier2RuntimeContext,
    allocate_runtime_context,
    attach_lineage_audit,
    build_mcp_adapters,
    cancel_key_for,
    close_mcp_adapters,
    is_live_cancel_command,
    load_mcp_server_specs,
    release_runtime_context,
)
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import BundleRegistry
from feishu_agent.tools.bundles import register_builtin_bundles
from feishu_agent.tools.code_write_service import (
    CodeWritePolicy,
    CodeWriteService,
    PolicyFileError,
    load_policy_file,
)
from feishu_agent.tools.deploy_service import (
    DeployService,
    load_deploy_project_configs,
)
from feishu_agent.tools.git_sync_preflight import (
    PreflightSnapshot,
    render_baseline_for_prompt,
    run_preflight_sync,
)
from feishu_agent.tools.progress_sync_service import ProgressSyncService
from feishu_agent.tools.project_registry import (
    ProjectRegistry,
    ProjectRegistryError,
    build_project_registry,
)
from feishu_agent.tools.speckit_script_service import SpeckitScriptService
from feishu_agent.tools.workflow_service import WORKFLOW_REGISTRY, WorkflowService

logger = logging.getLogger(__name__)

settings = get_settings()
INLINE_IMAGE_MAX_BYTES = int(os.environ.get("LARK_INLINE_IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))


# Bundle registry — populated once at import with all nine built-in
# tool bundles. It is stateless: ``BundleRegistry.build`` constructs
# a fresh composite executor per dispatch using the caller's
# :class:`BundleContext`, so concurrent sessions never share mutable
# handler state. Keeping this at module scope avoids rebuilding the
# factory table on every provider call.
_BUNDLE_REGISTRY = BundleRegistry()
register_builtin_bundles(_BUNDLE_REGISTRY)


@dataclass(frozen=True)
class FeishuThreadContext:
    chat_id: str
    message_id: str
    root_id: str | None = None
    thread_id: str | None = None
    chat_type: str = "p2p"

    @property
    def is_topic(self) -> bool:
        return self.thread_id is not None

    @property
    def reply_target_id(self) -> str:
        """The message_id to reply to for staying in the same topic."""
        return self.root_id or self.message_id


ThreadUpdateFn = Callable[[str], None]


@dataclass(frozen=True)
class FeishuBotContext:
    bot_name: str
    app_id: str
    app_secret: str
    verification_token: str | None
    encrypt_key: str | None
    bot_open_id: str | None = None


@dataclass
class FeishuRuntimeResult:
    ok: bool
    trace_id: str
    message: str
    route_action: str | None = None
    target_table_name: str | None = None
    warnings: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class FeishuImageInput:
    file_key: str
    mime_type: str
    base64_data: str
    source: str = "feishu_message_resource"
    byte_size: int | None = None

    def to_llm_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "file_key": self.file_key,
            "mime_type": self.mime_type,
            "base64_data": self.base64_data,
            "source": self.source,
        }
        if self.byte_size is not None:
            payload["byte_size"] = self.byte_size
        return payload


@dataclass(frozen=True)
class FeishuInboundMessage:
    message_type: str
    command_text: str
    images: list[FeishuImageInput] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.command_text.strip() or self.images)


@dataclass(frozen=True)
class FeishuBitableTable:
    table_name: str
    bitable_app_token: str
    table_id: str
    view_id: str | None = None
    notes: str | None = None


def available_bot_contexts() -> list[FeishuBotContext]:
    # Read settings each call rather than via the module-level snapshot so that
    # tests which mutate env + get_settings.cache_clear() see the updated config.
    s = get_settings()
    contexts: list[FeishuBotContext] = []
    if s.tech_lead_feishu_bot_app_id and s.tech_lead_feishu_bot_app_secret:
        contexts.append(
            FeishuBotContext(
                bot_name="tech_lead",
                app_id=s.tech_lead_feishu_bot_app_id,
                app_secret=s.tech_lead_feishu_bot_app_secret,
                verification_token=s.tech_lead_feishu_verification_token or s.feishu_verification_token,
                encrypt_key=s.tech_lead_feishu_encrypt_key or s.feishu_encrypt_key,
            )
        )
    if s.product_manager_feishu_bot_app_id and s.product_manager_feishu_bot_app_secret:
        contexts.append(
            FeishuBotContext(
                bot_name="product_manager",
                app_id=s.product_manager_feishu_bot_app_id,
                app_secret=s.product_manager_feishu_bot_app_secret,
                verification_token=s.product_manager_feishu_verification_token,
                encrypt_key=s.product_manager_feishu_encrypt_key,
            )
        )
    if not contexts and s.feishu_bot_app_id and s.feishu_bot_app_secret:
        contexts.append(
            FeishuBotContext(
                bot_name="default",
                app_id=s.feishu_bot_app_id,
                app_secret=s.feishu_bot_app_secret,
                verification_token=s.feishu_verification_token,
                encrypt_key=s.feishu_encrypt_key,
            )
        )
    return contexts


def resolve_bot_context_for_role(role_name: str) -> FeishuBotContext:
    normalized = role_name.strip().lower().replace("-", "_")
    if normalized.startswith("tech_lead"):
        target = "tech_lead"
    elif normalized.startswith("product_manager"):
        target = "product_manager"
    else:
        target = normalized

    for context in available_bot_contexts():
        if context.bot_name == target:
            return context

    available = ", ".join(context.bot_name for context in available_bot_contexts()) or "none"
    raise RuntimeError(f"No Feishu bot context configured for role '{role_name}'. Available: {available}")


def _runtime_repo_root() -> Path | None:
    if settings.app_repo_root:
        return Path(settings.app_repo_root).expanduser()
    return None


def _build_task_service() -> TaskService | None:
    """Return the process-wide TaskService when the feature is enabled.

    Honors ``task_event_log_enabled``; returns ``None`` when disabled so
    callers can gracefully skip task wiring in legacy-only mode. The
    tasks root resolves relative to ``app_repo_root`` when configured,
    otherwise falls back to an absolute ``feishu_tasks_root`` string
    (useful for tests / harnesses that don't set a repo root).
    """
    if not getattr(settings, "task_event_log_enabled", True):
        return None
    tasks_root_str = getattr(settings, "feishu_tasks_root", "data/tasks") or "data/tasks"
    tasks_root = Path(tasks_root_str)
    if not tasks_root.is_absolute():
        repo_root = _runtime_repo_root()
        if repo_root is not None:
            tasks_root = repo_root / tasks_root
        else:
            tasks_root = (Path.cwd() / tasks_root).resolve()
    return get_default_task_service(tasks_root)


def _open_task_handle(
    *,
    bot_context: "FeishuBotContext",
    thread_context: "FeishuThreadContext | None",
    chat_id: str | None,
    role_name: str | None,
    project_id: str | None,
) -> TaskHandle | None:
    """Open or resume the task corresponding to this Feishu thread.

    Returns ``None`` when the task feature is disabled or when we lack
    enough identity to form a stable key. Errors are logged and
    downgrade to ``None`` so the runtime keeps working even if the
    event log has a permissions problem.
    """
    svc = _build_task_service()
    if svc is None:
        return None
    try:
        key = TaskKey.derive(
            bot_name=bot_context.bot_name,
            chat_id=chat_id,
            root_id=(thread_context.root_id if thread_context else None),
            message_id=(thread_context.message_id if thread_context else None),
        )
        return svc.open_or_resume(
            key,
            role_name=role_name,
            project_id=project_id,
        )
    except Exception:  # noqa: BLE001 — self-healing
        logger.warning(
            "task handle open failed (bot=%s chat=%s)",
            bot_context.bot_name,
            chat_id,
            exc_info=True,
        )
        return None


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _conversation_log_path(role_name: str) -> Path | None:
    repo_root = _runtime_repo_root()
    if repo_root is None:
        return None
    return repo_root / settings.techbot_run_log_dir / "conversations" / f"{role_name}.jsonl"


def load_recent_conversation(
    *,
    role_name: str,
    chat_id: str | None,
    limit: int = 6,
) -> list[dict[str, object]]:
    if not chat_id:
        return []
    path = _conversation_log_path(role_name)
    if path is None or not path.exists():
        return []

    rows = _load_jsonl(path)
    matched = [
        {
            "timestamp": row.get("timestamp"),
            "user_text": row.get("user_text"),
            "reply_text": row.get("reply_text"),
            "route_action": row.get("route_action"),
            "target_table_name": row.get("target_table_name"),
        }
        for row in rows
        if row.get("chat_id") == chat_id
    ]
    if limit <= 0:
        return matched
    return matched[-limit:]


def _extract_text_content(content: str | None) -> str:
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ""
    return str(payload.get("text") or "").strip()


# Feishu "post" (rich-text) elements we flatten to plain strings. The
# message type switches from ``text`` to ``post`` whenever the user
# inserts inline code, bold, a link, an @mention inside a formatted
# block, etc. Without this flattener those messages reach the agent as
# empty strings and surface as "解析消息失败，请发送文本或图片消息".
#
# Element reference:
# https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create_json#45e0953e
_POST_TEXT_TAGS: frozenset[str] = frozenset(
    {"text", "a", "md", "code_inline", "code_block"}
)


def _flatten_post_elements(elements: object) -> list[str]:
    """Recursively walk a post payload's ``content`` (list-of-lists of
    element dicts) and return ordered plain-text fragments. Unknown
    tags are ignored rather than raised so a future Feishu element
    (carousel / poll / whatever) doesn't brick the parser."""
    out: list[str] = []
    if not isinstance(elements, list):
        return out
    for item in elements:
        if isinstance(item, list):
            out.extend(_flatten_post_elements(item))
            continue
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "")
        if tag in _POST_TEXT_TAGS:
            text = str(item.get("text") or "")
            if text:
                out.append(text)
        elif tag == "at":
            # Represent @mentions as "@<user_name>" so the LLM still
            # sees who was addressed. The on-wire payload does NOT
            # include a leading "@".
            name = str(item.get("user_name") or "").strip()
            if name:
                out.append(f"@{name}")
        elif tag == "emotion":
            key = str(item.get("key") or "").strip()
            if key:
                out.append(f"[{key}]")
    return out


def _extract_post_content(content: str | None) -> str:
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ""
    paragraphs_out: list[str] = []
    raw_paragraphs = payload.get("content")
    if isinstance(raw_paragraphs, list):
        for para in raw_paragraphs:
            fragments = _flatten_post_elements(para)
            line = "".join(fragments).strip()
            if line:
                paragraphs_out.append(line)
    flattened = "\n".join(paragraphs_out).strip()
    # Some clients set a useful ``title`` on the post; prepend it so
    # the agent sees the same framing the user saw.
    title = str(payload.get("title") or "").strip()
    if title and title not in flattened:
        flattened = f"{title}\n{flattened}".strip() if flattened else title
    return flattened


def _extract_image_key(content: str | None) -> str | None:
    if not content:
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    image_key = str(payload.get("image_key") or "").strip()
    return image_key or None


async def _download_message_image(
    *,
    bot_context: FeishuBotContext,
    message_id: str,
    image_key: str,
) -> FeishuImageInput:
    client = ManagedFeishuClient(
        FeishuAuthConfig(app_id=bot_context.app_id, app_secret=bot_context.app_secret),
        default_internal_token_kind="tenant",
    )
    token = await client.get_tenant_access_token()
    encoded_message_id = quote(message_id, safe="")
    encoded_image_key = quote(image_key, safe="")
    async with httpx.AsyncClient(timeout=settings.techbot_llm_timeout_seconds) as http_client:
        response = await http_client.get(
            f"{client.base_url}/open-apis/im/v1/messages/{encoded_message_id}/resources/{encoded_image_key}",
            params={"type": "image"},
            headers={"Authorization": f"Bearer {token}"},
        )
    response.raise_for_status()
    if "application/json" in response.headers.get("Content-Type", ""):
        payload = response.json()
        raise RuntimeError(payload.get("msg") or "下载飞书图片失败。")
    byte_size = len(response.content)
    if byte_size > INLINE_IMAGE_MAX_BYTES:
        raise RuntimeError(
            f"图片过大，当前大小 {byte_size} bytes，超过内联上限 {INLINE_IMAGE_MAX_BYTES} bytes。"
        )
    mime_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip() or "image/png"
    return FeishuImageInput(
        file_key=image_key,
        mime_type=mime_type,
        base64_data=base64.b64encode(response.content).decode("ascii"),
        byte_size=byte_size,
    )


async def load_feishu_inbound_message(
    *,
    bot_context: FeishuBotContext,
    message_type: str | None,
    content: str | None,
    message_id: str | None,
) -> FeishuInboundMessage:
    normalized_type = str(message_type or "").strip() or "unknown"
    if normalized_type == "text":
        return FeishuInboundMessage(
            message_type=normalized_type,
            command_text=_extract_text_content(content),
        )
    if normalized_type == "post":
        # Rich-text / formatted messages — same code path as plain
        # text once flattened. Keep message_type="post" for logs so we
        # can tell the two apart in the field if debugging is needed.
        return FeishuInboundMessage(
            message_type=normalized_type,
            command_text=_extract_post_content(content),
        )
    if normalized_type == "image":
        image_key = _extract_image_key(content)
        if not image_key or not message_id:
            return FeishuInboundMessage(message_type=normalized_type, command_text="")
        image_input = await _download_message_image(
            bot_context=bot_context,
            message_id=message_id,
            image_key=image_key,
        )
        return FeishuInboundMessage(
            message_type=normalized_type,
            command_text="用户发送了一张图片，请直接查看图片内容并结合上下文处理。",
            images=[image_input],
        )
    return FeishuInboundMessage(message_type=normalized_type, command_text="")


def available_bitable_tables() -> dict[str, FeishuBitableTable]:
    repo_root = _runtime_repo_root()
    if repo_root is None:
        return {}
    path = repo_root / ".larkagent" / "secrets" / "feishu_app" / "bitable_tables.jsonl"
    tables: dict[str, FeishuBitableTable] = {}
    for row in _load_jsonl(path):
        table_name = str(row.get("table_name") or "").strip()
        app_token = str(row.get("bitable_app_token") or "").strip()
        table_id = str(row.get("table_id") or "").strip()
        if not table_name or not app_token or not table_id:
            continue
        tables[table_name] = FeishuBitableTable(
            table_name=table_name,
            bitable_app_token=app_token,
            table_id=table_id,
            view_id=str(row.get("view_id") or "").strip() or None,
            notes=str(row.get("notes") or "").strip() or None,
        )
    return tables


def build_progress_sync_service(
    bot_context: FeishuBotContext | None = None,
    *,
    bitable_table_name: str | None = None,
    bitable_target: FeishuBitableTable | None = None,
) -> ProgressSyncService:
    contexts = available_bot_contexts()
    context = bot_context or (contexts[0] if contexts else None)
    return ProgressSyncService(
        ManagedFeishuClient(
            FeishuAuthConfig(
                app_id=context.app_id if context else "",
                app_secret=context.app_secret if context else "",
            ),
            default_internal_token_kind="app",
        ),
        default_bitable_app_token=bitable_target.bitable_app_token if bitable_target else None,
        default_bitable_table_id=bitable_target.table_id if bitable_target else None,
        default_bitable_view_id=bitable_target.view_id if bitable_target else None,
        default_bitable_table_name=bitable_table_name,
    )


def _load_system_prompt(filename: str, fallback: str) -> str:
    roles_dir = _roles_dir()
    prompt_path = roles_dir.parent / filename if roles_dir else None
    if prompt_path and prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return fallback


def _load_tech_lead_system_prompt() -> str:
    return _load_system_prompt(
        "tech_lead.md",
        "你是飞书技术组长 Bot。使用工具读取 Sprint 状态，"
        "通过 dispatch_role_agent 委派子任务给专业角色，"
        "通过 delegate_to_application_agent 将飞书多维表格类请求交给应用助理： "
        "若配置了接入 URL 则走该通道，否则以纯文本发到群内（无 @）。回复使用简洁中文。",
    )


def _load_pm_system_prompt() -> str:
    return _load_system_prompt(
        "product_manager.md",
        "你是飞书产品经理 Bot。通过提问澄清需求，委派 prd_writer 生成 PRD，"
        "完成后通知技术组长。回复使用简洁中文。",
    )


def _enrich_system_prompt(
    base_prompt: str,
    *,
    load_tables: Callable[[], dict[str, Any]],
    role_registry: RoleRegistryService,
    agent_name: str | None = None,
    application_agent_display_name: str = "Application delegate",
) -> str:
    """Append runtime context (known tables, available roles, available
    workflows for ``agent_name``) to the system prompt."""
    sections: list[str] = []

    tables = load_tables()
    if tables:
        lines = [
            "## 已知飞书多维表格（通过 delegate_to_application_agent 委派读写）",
            f"你不直接操作这些表格，但需要了解它们以便向 **{application_agent_display_name}** 发出准确的委派指令。",
        ]
        for name, table in tables.items():
            note = table.notes
            line = f"- **{name}**"
            if note:
                line += f"：{note}"
            lines.append(line)
        sections.append("\n".join(lines))

    try:
        roles = role_registry.list_roles()
        if roles:
            lines = ["## 可委派的角色 Agent（通过 dispatch_role_agent）"]
            for r in roles:
                tools_str = "、".join(r.tool_allow_list) if r.tool_allow_list else "无"
                lines.append(f"- **{r.role_name}**：可用工具 [{tools_str}]")
            sections.append("\n".join(lines))
    except Exception:
        pass

    if agent_name:
        allowed = [
            w for w in WORKFLOW_REGISTRY.values() if agent_name in w.allowed_agents
        ]
        if allowed:
            lines = [
                "## 可调用的 Workflow 命令",
                "使用方式：",
                "1. `read_workflow_instruction(workflow_id)` 读取方法论指令",
                "2. 按指令产出内容；需要已有上下文时用 `read_repo_file` / `list_workflow_artifacts`",
                "3. `write_workflow_artifact(workflow_id, relative_path, content)` 落盘到对应 artifact 子目录",
                "",
                "| workflow_id | 用途 | 产物目录 |",
                "|-------------|------|----------|",
            ]
            for w in allowed:
                lines.append(
                    f"| `{w.workflow_id}` | {w.description} | `{w.artifact_subdir}/` |"
                )
            sections.append("\n".join(lines))

    if not sections:
        return base_prompt
    return base_prompt.rstrip() + "\n\n" + "\n\n".join(sections)


def _roles_dir() -> Path | None:
    repo_root = _runtime_repo_root()
    if repo_root is None:
        return None
    return repo_root / "skills" / "roles"


def _mtime_key(paths: list[Path]) -> tuple[tuple[str, float], ...]:
    """Compact cache key made of (path, mtime) pairs. Missing files map
    to mtime=-1.0 so a file appearing from nothing invalidates the
    cache. ``paths`` must be stable across callers (same order)."""
    out: list[tuple[str, float]] = []
    for p in paths:
        try:
            out.append((str(p), p.stat().st_mtime))
        except OSError:
            out.append((str(p), -1.0))
    return tuple(out)


# Module-level caches keyed on policy-file / projects-file mtimes. Every
# Feishu message used to re-read ``.larkagent/secrets/code_write/policies.jsonl``
# and ``projects/projects.jsonl`` up to 8 times (4 builders for tech-lead,
# extra for developer / reviewer spawns). The caches turn that into one
# disk read unless the files have actually changed on disk — which is
# the correct invalidation trigger for hot-edit workflows.
_REGISTRY_CACHE: dict[str, Any] = {"key": None, "value": None}
_POLICIES_CACHE: dict[str, Any] = {"key": None, "value": None}

# Process-wide cache of ImpersonationTokenService instances. Keeping
# them long-lived lets one in-memory ``_cached`` token serve many
# delegate calls (and lets the asyncio.Lock actually serialize concurrent
# refreshes across requests). Keyed by ``(app_id, token_path)``.
_IMPERSONATION_CACHE: dict[tuple[str, str], ImpersonationTokenService] = {}


def _build_impersonation_service(
    repo_root: Path | None,
) -> ImpersonationTokenService | None:
    """Return a shared ImpersonationTokenService for the configured app,
    or ``None`` when impersonation is disabled / misconfigured.

    We do not probe the disk here — the service handles a missing token
    file gracefully and simply returns ``None`` from ``get_access_token``
    so callers fall back to the bot-IM path.
    """
    if not getattr(settings, "impersonation_enabled", True):
        return None
    app_id = (settings.impersonation_app_id or "").strip()
    app_secret = (settings.impersonation_app_secret or "").strip()
    if not app_id or not app_secret:
        return None
    token_dir = Path(settings.impersonation_token_dir or ".larkagent/secrets/user_tokens")
    if not token_dir.is_absolute() and repo_root is not None:
        token_dir = repo_root / token_dir
    token_path = token_dir / f"{app_id}.json"
    key = (app_id, str(token_path))
    svc = _IMPERSONATION_CACHE.get(key)
    if svc is None:
        svc = ImpersonationTokenService(
            app_id=app_id, app_secret=app_secret, token_path=token_path
        )
        _IMPERSONATION_CACHE[key] = svc
    return svc


def _projects_file_candidate(app_repo_root: Path | None) -> Path | None:
    if app_repo_root is None:
        return None
    return app_repo_root / ".larkagent" / "secrets" / "projects" / "projects.jsonl"


def _policies_file_candidate(app_repo_root: Path | None) -> Path | None:
    if app_repo_root is None:
        return None
    return app_repo_root / ".larkagent" / "secrets" / "code_write" / "policies.jsonl"


def _get_project_registry(app_repo_root: Path | None) -> ProjectRegistry:
    """Load the ProjectRegistry for this FeishuOPC instance.

    Caches by (projects.jsonl path, mtime). Malformed file → empty
    registry, logged once per mtime so we don't spam.
    """
    projects_file = _projects_file_candidate(app_repo_root)
    cache_key = (
        str(app_repo_root),
        settings.default_project_id,
        _mtime_key([projects_file]) if projects_file else (),
    )
    if _REGISTRY_CACHE["key"] == cache_key:
        return _REGISTRY_CACHE["value"]

    try:
        registry = build_project_registry(
            app_repo_root=app_repo_root,
            default_project_id_override=settings.default_project_id,
        )
    except ProjectRegistryError:
        logger.exception(
            "project_registry malformed, treating as empty; "
            "fix .larkagent/secrets/projects/projects.jsonl."
        )
        registry = ProjectRegistry([])

    _REGISTRY_CACHE["key"] = cache_key
    _REGISTRY_CACHE["value"] = registry
    return registry


def _resolve_project_id(
    registry: ProjectRegistry,
    *,
    chat_id: str | None = None,  # reserved for future chat→project binding
) -> str | None:
    """Resolve which project this message is acting on.

    Today: pure default-per-instance (registry.default_project_id → settings).
    Tomorrow: chat→project binding reads go here as a first check.
    """
    del chat_id  # unused for now
    return registry.default_project_id() or settings.default_project_id


# Minimum viable default policy. Deliberately has **empty** write roots:
# every project MUST declare its own ``allowed_write_roots`` in
# ``policies.jsonl``. That's fail-closed — no project gets write access by
# accident. Read defaults are similarly tiny (doc-style roots).
_DEFAULT_CODE_WRITE_POLICY = CodeWritePolicy(
    allowed_write_roots=(),
    allowed_read_roots=("docs/", "README.md"),
)


def _build_workflow_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
) -> WorkflowService | None:
    """Build a WorkflowService with per-project artifact roots sourced
    from the project registry.

    For projects without a resolvable ``project_repo_root`` we fall back
    to a sandbox inside ``app_repo_root/project_knowledge/<project_id>/``
    so that speckit/bmad commands still produce an artifact, but that
    artifact lives in the FeishuOPC repo (not the real project).
    """
    if app_repo_root is None:
        return None

    project_roots: dict[str, Path] = {}
    for project in registry.list():
        if project.project_repo_root is not None:
            project_roots[project.project_id] = project.project_repo_root
        else:
            project_roots[project.project_id] = (
                app_repo_root / "project_knowledge" / project.project_id
            )

    return WorkflowService(
        app_repo_root=app_repo_root, project_roots=project_roots
    )


def _build_speckit_script_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
) -> SpeckitScriptService | None:
    """Build a SpeckitScriptService scoped to the same project_roots
    that ``_build_workflow_service`` uses.

    We deliberately reuse those roots (real ``project_repo_root`` if
    set, otherwise the per-project sandbox under
    ``app_repo_root/project_knowledge/<id>/``) so that running a
    speckit script lands the resulting branch + spec.md in the same
    place ``write_workflow_artifact`` would have written them. This
    keeps the speckit.specify flow internally consistent — the script
    creates ``specs/NNN-slug/`` and the next ``write_workflow_artifact``
    call overwrites ``specs/NNN-slug/spec.md`` with the LLM's filled-in
    content.

    Returns ``None`` when there is no app_repo_root, mirroring the
    workflow-service builder's contract.
    """
    if app_repo_root is None:
        return None

    project_roots: dict[str, Path] = {}
    for project in registry.list():
        if project.project_repo_root is not None:
            project_roots[project.project_id] = project.project_repo_root
        else:
            project_roots[project.project_id] = (
                app_repo_root / "project_knowledge" / project.project_id
            )

    return SpeckitScriptService(project_roots=project_roots)


def _build_deploy_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
) -> DeployService | None:
    """Build a DeployService scoped to real project repos only.

    Unlike ``_build_workflow_service`` / ``_build_speckit_script_service``
    we SKIP the sandbox-under-project_knowledge fallback. Deploy makes
    no sense without a synthetic sandbox to ship. Project eligibility
    is gated at TWO layers now: (1) the project must have a real
    ``project_repo_root`` in the registry, and (2) FeishuOPC must
    carry a ``.larkagent/secrets/deploy_projects/<pid>.json`` file
    for it — no JSON, no deploy (see ``docs/deploy-convention.md``).
    The TL executor additionally checks ``is_deployable`` (which also
    verifies the configured script exists on disk) so a project that
    had its script deleted can't be deployed even with stale metadata.

    Logs land under
    ``<app_repo_root>/.larkagent/logs/deploy/<project>-<ts>.log`` so
    operators can ``tail -f`` during a deploy. Returns ``None`` when
    ``app_repo_root`` is unknown (no place to put logs) or when no
    project has both a repo root and a config — callers pass the
    resulting ``None`` into the TL executor which then hides the tools.
    """
    if app_repo_root is None:
        return None

    project_roots: dict[str, Path] = {}
    for project in registry.list():
        if project.project_repo_root is not None:
            project_roots[project.project_id] = project.project_repo_root

    configs_dir = app_repo_root / ".larkagent" / "secrets" / "deploy_projects"
    configs = load_deploy_project_configs(configs_dir)

    # Only projects that pass BOTH gates matter; if that intersection
    # is empty, nobody can deploy — skip wiring the service at all so
    # the tool stays hidden across the board. Logging is informative
    # (operators who expected deploy to work will see why it didn't).
    deployable = set(project_roots) & set(configs)
    if not deployable:
        if project_roots:
            logger.info(
                "deploy: no project has both a project_repo_root and a "
                "deploy_projects/<pid>.json config; deploy tools disabled. "
                "roots=%s configs=%s configs_dir=%s",
                sorted(project_roots),
                sorted(configs),
                configs_dir,
            )
        return None

    log_dir = app_repo_root / ".larkagent" / "logs" / "deploy"
    logger.info(
        "deploy: wiring DeployService for projects=%s (configs_dir=%s)",
        sorted(deployable),
        configs_dir,
    )
    return DeployService(
        project_roots=project_roots,
        configs=configs,
        log_dir=log_dir,
    )


def _build_artifact_publish_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
) -> ArtifactPublishService | None:
    """Build an ArtifactPublishService scoped to real project repos only.

    Unlike ``_build_workflow_service`` we deliberately SKIP the
    sandbox-under-project_knowledge fallback: there is no git remote
    under ``app_repo_root/project_knowledge/<id>/``, so committing +
    pushing there would fail anyway. If a project has no
    ``project_repo_root`` the tool simply isn't surfaced for that
    project_id — callers get ``UNKNOWN_PROJECT`` instead of a
    confusing "no remote configured" push failure mid-flow.
    """
    if app_repo_root is None:
        return None

    project_roots: dict[str, Path] = {}
    for project in registry.list():
        if project.project_repo_root is not None:
            project_roots[project.project_id] = project.project_repo_root

    if not project_roots:
        return None
    return ArtifactPublishService(project_roots=project_roots)


def _resolve_code_write_policies(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
) -> tuple[dict[str, Path], dict[str, CodeWritePolicy]]:
    """Shared loader for CodeWriteService / PrePushInspector / GitOpsService.

    Returns ``(project_roots, policies)``. Empty when the policy file is
    absent or malformed so all dependent services degrade to "disabled"
    together — no partial wiring where e.g. inspection works but writes
    don't.

    Results are cached by the policy file's mtime AND the projects
    file's mtime (the registry's fallback roots come from there). If
    either file changes, the cache invalidates naturally on the next
    call. Returned dicts are **fresh copies** so callers can mutate
    without poisoning the cache.
    """
    policy_file = _policies_file_candidate(app_repo_root)
    projects_file = _projects_file_candidate(app_repo_root)
    cache_key = (
        str(app_repo_root),
        _mtime_key(
            [p for p in (policy_file, projects_file) if p is not None]
        ),
    )
    if _POLICIES_CACHE["key"] == cache_key:
        cached_roots, cached_policies = _POLICIES_CACHE["value"]
        return dict(cached_roots), dict(cached_policies)

    fallback_roots: dict[str, Path] = {
        pid: root
        for pid, root in registry.project_roots().items()
        if root.is_dir()
    }

    project_roots: dict[str, Path] = {}
    policies: dict[str, CodeWritePolicy] = {}

    if app_repo_root is None or policy_file is None:
        _POLICIES_CACHE["key"] = cache_key
        _POLICIES_CACHE["value"] = (project_roots, policies)
        return dict(project_roots), dict(policies)

    try:
        entries = load_policy_file(
            policy_file,
            default_policy=_DEFAULT_CODE_WRITE_POLICY,
            fallback_project_roots=fallback_roots,
        )
    except PolicyFileError:
        logger.exception(
            "code-write policy file malformed, refusing to enable code writes"
        )
        _POLICIES_CACHE["key"] = cache_key
        _POLICIES_CACHE["value"] = ({}, {})
        return {}, {}

    for pid, ent in entries.items():
        if ent.project_repo_root is None or not ent.project_repo_root.is_dir():
            logger.warning(
                "code-write policy: skipping project_id=%s, "
                "project_repo_root not resolvable.",
                pid,
            )
            continue
        project_roots[pid] = ent.project_repo_root
        policies[pid] = ent.policy

    _POLICIES_CACHE["key"] = cache_key
    _POLICIES_CACHE["value"] = (project_roots, policies)
    return dict(project_roots), dict(policies)


def _reset_runtime_caches() -> None:
    """Test helper: drop mtime-keyed caches so unit tests can mutate
    policy / registry files within a single process and observe the
    result. Production code never needs to call this — the mtime key
    invalidates on its own."""
    _REGISTRY_CACHE["key"] = None
    _REGISTRY_CACHE["value"] = None
    _POLICIES_CACHE["key"] = None
    _POLICIES_CACHE["value"] = None


def _build_code_write_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
    trace_id: str = "no-trace",
) -> CodeWriteService | None:
    """Build a CodeWriteService from ``.larkagent/secrets/code_write/policies.jsonl``.

    Fail-closed design:
    - Malformed policy file → disable code writes entirely.
    - A project with no resolvable ``project_repo_root`` (neither in the
      policy file nor in the project registry) → skipped.
    - A project with empty ``allowed_write_roots`` (the
      ``_DEFAULT_CODE_WRITE_POLICY``) is registered but can't actually
      write anywhere, which is intentional — it forces each deployment
      to opt into write permissions per project.
    """
    project_roots, policies = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    if not project_roots:
        return None

    audit_root: Path | None = None
    if app_repo_root is not None:
        audit_root = app_repo_root / "data" / "code-writes"

    return CodeWriteService(
        project_roots=project_roots,
        policies=policies,
        audit_root=audit_root,
        trace_id=trace_id,
    )


def _build_pre_push_inspector(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
) -> "PrePushInspector | None":
    from feishu_agent.tools.pre_push_inspector import PrePushInspector

    project_roots, policies = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    if not project_roots:
        return None
    # Only keep projects whose repo is a git checkout; otherwise the
    # inspector can't answer anything useful.
    gitty_roots = {
        pid: root for pid, root in project_roots.items() if (root / ".git").exists()
    }
    gitty_policies = {pid: policies[pid] for pid in gitty_roots}
    if not gitty_roots:
        return None

    # Persist inspection tokens so a user can run ``inspect`` in one
    # Feishu message and ``push`` in a later message without re-running
    # inspection. Tokens still TTL out after
    # ``PrePushInspector.TOKEN_TTL_SECONDS`` and are bound to the exact
    # HEAD sha, so an intervening code change still forces re-inspect.
    token_store: Path | None = None
    if app_repo_root is not None:
        token_store = (
            app_repo_root / "data" / "inspection-tokens" / "tokens.jsonl"
        )

    return PrePushInspector(
        project_roots=gitty_roots,
        policies=gitty_policies,
        token_store_path=token_store,
    )


def _build_pull_request_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
    trace_id: str = "no-trace",
) -> "PullRequestService | None":
    from feishu_agent.tools.code_write_service import CodeWriteAuditLog
    from feishu_agent.tools.pull_request_service import PullRequestService

    project_roots, policies = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    gitty_roots = {
        pid: root for pid, root in project_roots.items() if (root / ".git").exists()
    }
    gitty_policies = {pid: policies[pid] for pid in gitty_roots}
    if not gitty_roots:
        return None

    audit: CodeWriteAuditLog | None = None
    if app_repo_root is not None:
        audit = CodeWriteAuditLog(
            root=app_repo_root / "data" / "code-writes",
            trace_id=trace_id,
        )

    # Default token file path — operators put `GH_TOKEN=…` here to let
    # the Tech Lead open PRs without relying on the process env. If the
    # file is missing we silently fall back to the ambient env /
    # ``gh auth login`` credentials.
    gh_token_path: Path | None = None
    if app_repo_root is not None:
        candidate = (
            app_repo_root
            / ".larkagent"
            / "secrets"
            / "github_key"
            / "gh_token.env"
        )
        gh_token_path = candidate

    return PullRequestService(
        project_roots=gitty_roots,
        policies=gitty_policies,
        audit_log=audit,
        gh_token_path=gh_token_path,
    )


def _build_ci_watch_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
) -> "CIWatchService | None":
    """Wire ``CIWatchService`` for the tech-lead.

    Reuses the same git-checked project-root resolution as
    ``_build_pull_request_service`` (only repos with a ``.git/`` are
    candidates for CI watching) and the same operator-managed
    ``gh_token.env`` location so PR creation and CI watching share auth.
    Returns ``None`` when no git-managed projects are configured; the
    tech-lead executor then simply doesn't advertise ``watch_pr_checks``.
    """
    from feishu_agent.tools.ci_watch_service import CIWatchService

    project_roots, _ = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    gitty_roots = {
        pid: root for pid, root in project_roots.items() if (root / ".git").exists()
    }
    if not gitty_roots:
        return None

    gh_token_path: Path | None = None
    if app_repo_root is not None:
        gh_token_path = (
            app_repo_root
            / ".larkagent"
            / "secrets"
            / "github_key"
            / "gh_token.env"
        )

    return CIWatchService(
        project_roots=gitty_roots,
        gh_token_path=gh_token_path,
    )


def _build_git_ops_service(
    app_repo_root: Path | None,
    *,
    registry: ProjectRegistry,
    inspector: "PrePushInspector | None",
    trace_id: str = "no-trace",
) -> "GitOpsService | None":
    from feishu_agent.tools.code_write_service import CodeWriteAuditLog
    from feishu_agent.tools.git_ops_service import GitOpsService

    if inspector is None:
        return None
    project_roots, policies = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    gitty_roots = {
        pid: root for pid, root in project_roots.items() if (root / ".git").exists()
    }
    gitty_policies = {pid: policies[pid] for pid in gitty_roots}
    if not gitty_roots:
        return None

    audit: CodeWriteAuditLog | None = None
    if app_repo_root is not None:
        audit = CodeWriteAuditLog(
            root=app_repo_root / "data" / "code-writes",
            trace_id=trace_id,
        )

    return GitOpsService(
        project_roots=gitty_roots,
        policies=gitty_policies,
        inspector=inspector,
        audit_log=audit,
    )


# Per-role subdir convention under the downstream project's repo.
# All land under ``docs/`` so tech lead's PR docs-diff check picks
# them up naturally when they're included in a feature branch.
#
# This is hardcoded deliberately — each role has ONE place to put its
# output, and that place is predictable across projects. Adding a new
# destination for an existing role means changing this dict, not a
# config file (single source of truth during code review).
ROLE_ARTIFACT_SUBDIRS: dict[str, str] = {
    "reviewer": "docs/reviews",
    "qa_tester": "docs/qa",
    "repo_inspector": "docs/repo-analysis",
    "researcher": "docs/research",
    "spec_linker": "docs/spec-linkage",
    "ux_designer": "docs/ux",
    # The developer leaves a short impl-note for the tech lead to read
    # before pushing — that's how TL avoids having to diff by hand.
    "developer": "docs/implementation",
    # The bug_fixer lands fix-notes under a nested dir so they don't
    # collide with developer's impl-notes; tech lead's review loop
    # reads both. Keeping them under the same top-level ``docs/
    # implementation`` parent means a single PR-docs-diff check
    # covers the whole implementation story.
    "bug_fixer": "docs/implementation/fixes",
}


def _build_role_artifact_writer(
    *,
    role_name: str,
    project_id: str,
    app_repo_root: Path | None,
    registry: ProjectRegistry,
    trace_id: str = "no-trace",
) -> "RoleArtifactWriter | None":
    """Build a ``RoleArtifactWriter`` for ``role_name`` scoped to
    ``<project_repo>/<role_subdir>/``.

    Returns ``None`` (fail-closed) when:
    - the role isn't in the subdir convention (prd_writer, sprint_planner,
      progress_sync keep their existing write paths)
    - no project_id resolved for this invocation
    - the project has no repo root configured
    """
    from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
    from feishu_agent.tools.code_write_service import CodeWriteAuditLog

    subdir = ROLE_ARTIFACT_SUBDIRS.get(role_name)
    if subdir is None:
        return None
    if not project_id:
        return None

    project_roots, _ = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    project_root = project_roots.get(project_id)
    if project_root is None:
        logger.info(
            "role_artifact_writer skipped for role=%s project=%s: "
            "no project_root configured",
            role_name,
            project_id,
        )
        return None

    allowed = (project_root / subdir).resolve()

    audit: CodeWriteAuditLog | None = None
    if app_repo_root is not None:
        audit = CodeWriteAuditLog(
            root=app_repo_root / "data" / "code-writes",
            trace_id=trace_id,
        )

    return RoleArtifactWriter(
        role_name=role_name,
        project_id=project_id,
        allowed_write_root=allowed,
        audit_log=audit,
    )


def _build_role_executor_provider(
    *,
    role_registry: RoleRegistryService,
    progress_service: ProgressSyncService,
    sprint_state: SprintStateService,
    project_id: str,
    command_text: str,
    repo_root: Path | None,
    context: "FeishuBotContext",
    registry: ProjectRegistry | None = None,
    trace_id: str = "no-trace",
    chat_id: str = "",
) -> Callable[[str, RoleDefinition], AgentToolExecutor | None]:
    """Build a callback that instantiates the right role executor on demand.

    Returns None for roles without a registered factory so the caller can
    fall back to the old single-shot dispatch path.
    """

    def _target_progress_service(
        table_name: str | None, _bt: Any | None = None
    ) -> ProgressSyncService:
        return progress_service

    def _load_role_permissions(_role: str) -> list[Any]:
        return []

    prd_write_root = (repo_root / "specs") if repo_root else None

    # --- Per-role kwarg mutators --------------------------------------
    # Each role that needs special wiring owns a tiny helper that
    # mutates the shared kwargs dict in place. Registering helpers in
    # the table below replaces the old ``if role_name == …`` ladder:
    # the ladder grew one branch per role and was implicitly ordered
    # (reviewer vs developer/bug_fixer both touch code_write_service).
    # With the table, every mutation is self-contained, named, and
    # discoverable from one place.
    #
    # A mutator returns ``False`` to signal "this role cannot run in
    # this environment" (e.g. prd_writer without a repo_root). The
    # caller then skips the factory altogether — same semantics as
    # the old ``return None`` branch.

    WIRING_ROLES_NEED_WORKFLOW = frozenset(
        {
            "reviewer",
            "developer",
            "bug_fixer",
            "sprint_planner",
            "ux_designer",
            "researcher",
        }
    )

    def _wire_prd_writer(kwargs: dict[str, Any]) -> bool:
        if prd_write_root is None:
            logger.warning(
                "prd_writer skipped: no repo_root configured for allowed_write_root"
            )
            return False
        kwargs["allowed_write_root"] = prd_write_root
        return True

    def _wire_code_write(kwargs: dict[str, Any]) -> bool:
        # Developer + bug_fixer share the full CodeWriteService +
        # GitOpsService. The trust split happens at the TOOL SURFACE
        # (via DEVELOPER_CODE_WRITE_ALLOW on both executors), not at
        # the service layer. Identical wiring means a bug here can't
        # accidentally grant one of them more privilege than the
        # other.
        if registry is None:
            return True
        kwargs["code_write_service"] = _build_code_write_service(
            repo_root, registry=registry, trace_id=trace_id
        )
        inspector = _build_pre_push_inspector(repo_root, registry=registry)
        kwargs["git_ops_service"] = _build_git_ops_service(
            repo_root,
            registry=registry,
            inspector=inspector,
            trace_id=trace_id,
        )
        # Strip bitable-specific kwargs so trace logs stay clean when
        # these roles run; DeveloperExecutor.__init__ absorbs extras
        # via ``**_kwargs`` so presence wouldn't fail, just noise.
        for unused in (
            "progress_sync_service",
            "load_bitable_tables",
            "load_role_permissions",
            "build_progress_sync_service_for_target",
        ):
            kwargs.pop(unused, None)
        return True

    def _wire_reviewer(kwargs: dict[str, Any]) -> bool:
        # Reviewer gets a READ-ONLY slice of the CodeWriteService so
        # it can inspect source code during bmad:code-review. The
        # executor's ``REVIEWER_CODE_READ_ALLOW`` filter caps the
        # surface to describe_code_write_policy / read_project_code /
        # list_project_paths — any write / commit / push tool
        # surfaces nowhere (neither in tool_specs nor at dispatch).
        if registry is None:
            return True
        kwargs["code_write_service"] = _build_code_write_service(
            repo_root, registry=registry, trace_id=trace_id
        )
        return True

    def _wire_deploy_engineer(kwargs: dict[str, Any]) -> bool:
        # deploy_engineer owns the DeployService that used to be wired
        # on the tech lead. Service wiring is unchanged; only the
        # executor it attaches to moved. When ``_build_deploy_service``
        # returns None (no deployable project), we still register the
        # role with ``deploy_service=None`` — the executor surfaces
        # ``DEPLOY_SERVICE_DISABLED`` which is more diagnostic than
        # refusing to register the role at all.
        if registry is None:
            return True
        kwargs["deploy_service"] = _build_deploy_service(
            repo_root, registry=registry
        )
        # Progress-sync plumbing is not relevant for this role; drop
        # to keep trace logs tidy. The executor's ``**_kwargs`` absorbs
        # presence, so this is aesthetic rather than required.
        for unused in (
            "progress_sync_service",
            "load_bitable_tables",
            "load_role_permissions",
            "build_progress_sync_service_for_target",
        ):
            kwargs.pop(unused, None)
        return True

    # Role-specific mutator table. Roles not listed get the default
    # kwargs only. Order of execution is: role-specific entry, then
    # the shared "workflow / artifact writer" mutators below.
    ROLE_SPECIFIC_MUTATORS: dict[
        str, Callable[[dict[str, Any]], bool]
    ] = {
        "prd_writer": _wire_prd_writer,
        "developer": _wire_code_write,
        "bug_fixer": _wire_code_write,
        "reviewer": _wire_reviewer,
        "deploy_engineer": _wire_deploy_engineer,
    }

    def _wire_shared(role_name: str, kwargs: dict[str, Any]) -> None:
        # Inject a read-only WorkflowService into the sub-agent roles
        # that have a ``WorkflowToolsMixin`` with
        # ``_workflow_readonly=True``. They get
        # read_workflow_instruction / list_workflow_artifacts /
        # read_repo_file; write tools stay stripped by the mixin's
        # readonly flag, so artifact creation remains with
        # tech_lead / prd_writer only.
        if registry is not None and role_name in WIRING_ROLES_NEED_WORKFLOW:
            kwargs["workflow_service"] = _build_workflow_service(
                repo_root, registry=registry
            )

        # Role-scoped artifact writer for specialist roles that have
        # an assigned docs/ subdir. Purely additive — prd_writer /
        # sprint_planner / progress_sync keep their existing
        # capabilities.
        if registry is not None and role_name in ROLE_ARTIFACT_SUBDIRS:
            writer = _build_role_artifact_writer(
                role_name=role_name,
                project_id=project_id,
                app_repo_root=repo_root,
                registry=registry,
                trace_id=trace_id,
            )
            if writer is not None:
                kwargs["role_artifact_writer"] = writer

    def _provider(
        role_name: str,
        _role: RoleDefinition,
        *,
        working_dir: Path | None = None,
    ) -> AgentToolExecutor | None:
        # Story 004.5 — ``working_dir`` override lets the TL hand us
        # a per-dispatch git worktree path for B-3 isolation. When
        # ``None`` (every call path pre-004.5 + every role that
        # doesn't declare ``needs_worktree``) we fall back to
        # ``bundle_repo_root`` below, preserving old behaviour.
        #
        # Build the shared kwargs dict up-front — both the bundle path
        # and the legacy factory path consume exactly the same wiring,
        # so assembling it once keeps the two branches impossible to
        # accidentally desynchronize.
        kwargs: dict[str, Any] = {
            "sprint_state_service": sprint_state,
            "progress_sync_service": progress_service,
            "project_id": project_id,
            "command_text": command_text,
            "role_name": role_name,
            "load_bitable_tables": available_bitable_tables,
            "load_role_permissions": _load_role_permissions,
            "build_progress_sync_service_for_target": _target_progress_service,
        }

        mutator = ROLE_SPECIFIC_MUTATORS.get(role_name)
        if mutator is not None and not mutator(kwargs):
            return None

        _wire_shared(role_name, kwargs)

        # --- A-2 Wave 3: bundle-first dispatch ------------------------
        # When a role declares ``tool_bundles`` in its frontmatter, the
        # tool surface is composed from the BundleRegistry rather than
        # through a hand-written per-role executor class. The 6
        # migrated roles (repo_inspector / researcher / spec_linker /
        # ux_designer / qa_tester / sprint_planner) have no
        # ``LEGACY_ROLE_FACTORIES`` entry after Wave 3 — the bundle
        # path is their ONLY registration, so this branch must run
        # before the factory lookup below (which would otherwise short
        # -circuit to ``None``).
        #
        # Previously this branch additionally required ``repo_root is
        # not None`` "to keep fs/git bundles safe". That guard was a
        # code-review regression (A-2 Wave 3 H1): in environments with
        # ``settings.app_repo_root`` unset (so ``_runtime_repo_root()``
        # returns ``None``), the 6 migrated roles silently dropped
        # their entire tool surface because the guard short-circuited
        # to the now-empty legacy factory. The correct behaviour is:
        # still build the bundle executor; bundles that need an
        # absolute repo path (fs_read / fs_write / git_local /
        # git_remote) already rely on their ``ctx.*_service`` refs
        # being ``None`` and degrade to an empty spec list. Falling
        # back to ``Path(".").resolve()`` gives them a syntactically
        # valid root without inviting them to actually write anything
        # — services gate that.
        if _role.tool_bundles:
            bundle_repo_root = repo_root or Path(".").resolve()
            # Story 004.5 — honour the ``working_dir`` override when
            # set. Keep ``repo_root`` pointing at the main working
            # copy: ``git_remote`` bundle tools still need to acquire
            # ``repo_filelock`` against the real .git dir, not the
            # worktree's private one (a worktree's .git is a file
            # pointer, not a repo).
            ctx_working_dir = working_dir or bundle_repo_root
            ctx = BundleContext(
                working_dir=ctx_working_dir,
                repo_root=bundle_repo_root,
                chat_id=chat_id,
                trace_id=trace_id,
                role_name=role_name,
                project_id=project_id,
                command_text=command_text,
                sprint_service=kwargs.get("sprint_state_service"),
                progress_sync_service=kwargs.get("progress_sync_service"),
                code_write_service=kwargs.get("code_write_service"),
                git_ops_service=kwargs.get("git_ops_service"),
                workflow_service=kwargs.get("workflow_service"),
                role_artifact_writer=kwargs.get("role_artifact_writer"),
                load_bitable_tables=kwargs.get("load_bitable_tables")
                or (lambda: {}),
                load_role_permissions=kwargs.get("load_role_permissions")
                or (lambda _role_name: []),
                build_progress_sync_service_for_target=kwargs.get(
                    "build_progress_sync_service_for_target"
                ),
            )
            try:
                return GenericRoleExecutor(_role, _BUNDLE_REGISTRY, ctx)
            except Exception:
                logger.exception(
                    "GenericRoleExecutor failed for %s; falling back to "
                    "legacy factory",
                    role_name,
                )
                # Fall through to legacy path rather than 503-ing the
                # dispatch — a legacy factory may still be registered.

        factory = role_registry.get_executor_factory(role_name)
        if factory is None:
            return None
        try:
            return factory(**kwargs)
        except TypeError:
            logger.exception(
                "role_executor factory signature mismatch for %s", role_name
            )
            return None

    return _provider


def _run_bot_preflight(
    *,
    git_ops_service: "GitOpsService | None",
    project_id: str,
    repo_root: Path | None,
    registry: ProjectRegistry,
    bot_name: str,
    chat_id: str | None,
    thread_context: "FeishuThreadContext | None",
    thread_update_fn: ThreadUpdateFn | None,
    base_branch: str | None = None,
    pending_action_service: PendingActionService | None = None,
) -> PreflightSnapshot | None:
    """Resolve the project repo root and call the preflight helper.

    Shared by both the tech-lead and product-manager entry points —
    PM reads spec/roadmap files and is equally vulnerable to a stale
    shared-repo clone.

    We look up ``project_root`` from the code-write policy map — same
    source the git-ops service was built from, so the two are always
    in sync. For "project-less" sessions (no policy entry) we still
    return ``None`` and the caller skips baseline injection.

    The thread id used for caching is ``root_id`` (when we're inside
    a topic thread) or the plain ``chat_id`` otherwise. Per-thread
    caching prevents re-fetching on every reply while still catching
    staleness across distinct threads / different conversations.

    ``base_branch`` is passed through to ``run_preflight_sync``. The
    TL path leaves it ``None`` because TL legitimately works on
    feature branches; PM passes the project's semantic baseline
    (typically ``main``) so the shared-repo isn't inherited from
    whatever branch TL last checked out.
    """
    if repo_root is None or not project_id:
        return None
    project_roots, _ = _resolve_code_write_policies(
        repo_root, registry=registry
    )
    project_root = project_roots.get(project_id)
    if project_root is None:
        return None

    thread_id = thread_context.root_id if thread_context else None

    try:
        return run_preflight_sync(
            git_ops_service=git_ops_service,
            project_id=project_id,
            project_root=project_root,
            bot_name=bot_name,
            chat_id=chat_id,
            thread_id=thread_id,
            thread_update_fn=thread_update_fn,
            base_branch=base_branch,
            pending_action_service=pending_action_service,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning("preflight sync crashed; continuing", exc_info=True)
        return None


def _pm_baseline_branch(
    registry: ProjectRegistry, project_id: str
) -> str:
    """Pick the branch the PM bot should reset onto before every thread.

    PM is always "reading the merged world" — fresh briefs, approved
    specs, the shipping PRD — so its baseline is the product trunk,
    not whatever feature branch someone else left checked out. We
    let the registry override the default per-project via
    ``extra.default_branch`` (e.g. older projects still on
    ``master``) and fall back to ``main`` otherwise.
    """
    project = registry.get(project_id)
    if project is None or not project.extra:
        return "main"
    candidate = project.extra.get("default_branch")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return "main"


def _build_sprint_state_service(
    progress_service: ProgressSyncService,
    project_id: str,
    *,
    registry: ProjectRegistry | None = None,
) -> SprintStateService:
    adapter = progress_service.load_adapter(project_id)
    status_relative_path = (adapter.get("source_roots") or {}).get("status_file")
    if not status_relative_path:
        raise RuntimeError("Project adapter does not define a status_file source.")
    # ``status_file`` in the adapter is a path relative to the **project
    # repo** (where the actual source and specs live), not relative to
    # the FeishuOPC agent home (which hosts .larkagent/secrets/ and
    # project-adapters/). Prefer the per-project ``project_repo_root``
    # from the ProjectRegistry; fall back to ``progress_service.repo_root``
    # only when the registry is empty (single-root dev / test setups).
    base_root: Path = progress_service.repo_root
    if registry is not None:
        project = registry.get(project_id)
        if project is not None and project.project_repo_root is not None:
            base_root = project.project_repo_root
    return SprintStateService(base_root, status_relative_path)


# ---------------------------------------------------------------------------
# Harness builders — context compression, tool verification, provider pool,
# per-project notes. All degrade to ``None`` / no-op when their upstream
# inputs are missing so ``LlmAgentAdapter`` can be instantiated in any
# environment (CI, local dev, production) without branching.
# ---------------------------------------------------------------------------


def _build_context_compressor() -> "ContextCompressor":
    """Build the active compressor from settings.

    We do NOT wire an LLM-backed summarizer at construction time: the
    adapter that would run the summarizer is the same adapter we're
    configuring, which would introduce a circular dependency (and a
    summarizer call in the middle of a tool loop could itself trip the
    compressor). Start with the deterministic-summary fallback; operators
    opt into LLM summarization later via an explicit callback.
    """
    from feishu_agent.core.context_compression import (
        NoOpContextCompressor,
        TailWindowCompressor,
    )

    max_tokens = int(getattr(settings, "llm_max_context_tokens", 0) or 0)
    if max_tokens <= 0:
        # Feature is opt-in: without an explicit cap we refuse to guess
        # the model's context window and let the adapter pay the full
        # token bill.
        return NoOpContextCompressor()
    return TailWindowCompressor(
        max_context_tokens=max_tokens,
        trigger_ratio=float(getattr(settings, "llm_compression_trigger", 0.7)),
        keep_tail_turns=int(getattr(settings, "llm_compression_keep_tail", 6)),
    )


def _build_tool_verifier(
    *, app_repo_root: Path | None, registry: "ProjectRegistry"
) -> "ToolVerifier | None":
    """Build a verifier with the project-aware default validators.

    Returns ``None`` when no project roots can be resolved — without
    them we can't locate the files we'd need to stat, so verification
    would degrade to always-pass and add latency for no value.
    """
    from feishu_agent.tools.tool_verification import (
        ToolVerifier,
        build_default_validators,
    )

    project_roots, _policies = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    if not project_roots:
        return None

    def _resolver(args: dict[str, Any]) -> Path | None:
        # The write_project_code / git_commit tools carry an explicit
        # ``project_id`` arg. Fall back to ``default_project_id`` when
        # the arg is missing (single-project deployments).
        pid = args.get("project_id") or getattr(
            settings, "default_project_id", None
        )
        if not pid:
            return None
        return project_roots.get(pid)

    return ToolVerifier(build_default_validators(project_root_resolver=_resolver))


def _build_provider_pool() -> "LlmProviderPool | None":
    """Build a provider pool from settings.

    The pool has two independent value axes — **retry on transient
    errors** and **failover across providers**. We only need a primary
    provider to earn the first one; secondary is strictly additive.

    - Primary missing → ``None`` (adapter would have no endpoint anyway).
    - Primary only   → single-provider pool (still gets retry + backoff
      on 429/5xx/timeout, which was the #1 transient-failure fix this
      harness was supposed to deliver).
    - Primary + secondary → two-provider pool with full failover.
    """
    from feishu_agent.providers.llm_provider_pool import (
        LlmProviderConfig,
        LlmProviderPool,
    )

    primary_base = getattr(settings, "techbot_llm_base_url", "") or ""
    primary_key = getattr(settings, "techbot_llm_api_key", "") or ""
    if not (primary_base and primary_key):
        return None
    primary_model = (
        getattr(settings, "techbot_llm_model", None)
        or getattr(settings, "role_agent_default_model", None)
        or "doubao-seed-2-0-pro-260215"
    )
    timeout_seconds = float(
        getattr(settings, "role_agent_timeout_seconds", 120)
    )

    providers: list[LlmProviderConfig] = [
        LlmProviderConfig(
            name="primary",
            base_url=primary_base,
            api_key=primary_key,
            model=primary_model,
            timeout_seconds=timeout_seconds,
        )
    ]

    secondary_transport = (
        getattr(settings, "llm_secondary_transport", "openai_http") or "openai_http"
    )
    if secondary_transport == "anthropic_bedrock":
        # Bedrock fallback: no base_url / api_key — signing is SigV4 and
        # the ``anthropic[bedrock]`` SDK owns the transport. We still
        # require ``llm_secondary_model`` (the Bedrock inference profile
        # ARN / model id) + region + both AWS keys to be present; if any
        # are missing the loader should have refused to populate
        # transport in the first place, but we double-check here so a
        # half-configured secondary never silently joins the pool.
        secondary_model = getattr(settings, "llm_secondary_model", "") or ""
        aws_region = getattr(settings, "llm_secondary_aws_region", "") or ""
        aws_access = (
            getattr(settings, "llm_secondary_aws_access_key_id", "") or ""
        )
        aws_secret = (
            getattr(settings, "llm_secondary_aws_secret_access_key", "") or ""
        )
        if secondary_model and aws_region and aws_access and aws_secret:
            providers.append(
                LlmProviderConfig(
                    name="secondary",
                    base_url="",
                    api_key="",
                    model=secondary_model,
                    timeout_seconds=timeout_seconds,
                    transport="anthropic_bedrock",
                    aws_region=aws_region,
                    aws_access_key_id=aws_access,
                    aws_secret_access_key=aws_secret,
                )
            )
    else:
        secondary_base = getattr(settings, "llm_secondary_base_url", "") or ""
        secondary_key = getattr(settings, "llm_secondary_api_key", "") or ""
        if secondary_base and secondary_key:
            providers.append(
                LlmProviderConfig(
                    name="secondary",
                    base_url=secondary_base,
                    api_key=secondary_key,
                    model=(
                        getattr(settings, "llm_secondary_model", None)
                        or primary_model
                    ),
                    timeout_seconds=timeout_seconds,
                )
            )

    return LlmProviderPool(
        providers=providers,
        max_retries_per_provider=int(
            getattr(settings, "llm_retries_per_provider", 2)
        ),
    )


def _build_last_run_memory_service(
    *,
    app_repo_root: Path | None,
    registry: "ProjectRegistry",
    project_id: str,
) -> "LastRunMemoryService | None":
    """Build the per-project ``.feishu_run_history.jsonl`` store.

    Mirrors :func:`_build_agent_notes_service` — we only activate it
    when a concrete project root is resolvable, so "project-less"
    sessions (no registry mapping) don't leak run history into the
    FeishuOPC repo.
    """
    from feishu_agent.team.last_run_memory_service import (
        LastRunMemoryService,
    )

    if not project_id:
        return None
    project_roots, _ = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    project_root = project_roots.get(project_id)
    if project_root is None:
        return None
    return LastRunMemoryService(
        project_id=project_id,
        project_root=project_root,
        enabled=bool(getattr(settings, "last_run_memory_enabled", True)),
    )


def _build_agent_notes_service(
    *, app_repo_root: Path | None, registry: "ProjectRegistry", project_id: str
) -> "AgentNotesService | None":
    """Build the per-project notes service for the TechLead.

    Returns ``None`` when there's no resolvable project root — we
    intentionally do NOT fall back to writing into the FeishuOPC repo
    (that would leak agent memory of customer project A into the repo
    of customer project B).
    """
    from feishu_agent.team.agent_notes_service import AgentNotesService

    if not project_id:
        return None
    project_roots, _ = _resolve_code_write_policies(
        app_repo_root, registry=registry
    )
    project_root = project_roots.get(project_id)
    if project_root is None:
        return None
    return AgentNotesService(
        project_id=project_id,
        project_root=project_root,
        max_notes_per_session=int(
            getattr(settings, "agent_notes_max_per_session", 5)
        ),
    )


async def _build_mcp_adapters_for_session() -> list[object]:
    """Load MCP server specs and connect an adapter per enabled entry.

    Returns an empty list when the setting is not configured — which
    is the default deployment mode. Keeping the dynamic import inside
    this function means ``feishu_runtime_service`` doesn't pay the
    ``mcp_tool_adapter`` import cost (subprocess helpers etc.) unless
    MCP is actually turned on.

    Failures are logged and skipped so a single broken MCP server
    can't keep the rest of the bot from answering. The caller is
    responsible for calling ``close_mcp_adapters`` in a ``finally``.
    """
    config_path = getattr(settings, "mcp_servers_config_path", "") or ""
    if not config_path:
        return []
    specs = load_mcp_server_specs(config_path)
    if not specs:
        return []
    from feishu_agent.tools.mcp_tool_adapter import (
        McpToolAdapter,
        StdioMcpTransport,
    )

    async def _factory(spec, timeout):
        transport = StdioMcpTransport(
            command=list(spec.command),
            env=spec.env,
            cwd=spec.cwd,
            name=spec.name,
        )
        adapter = McpToolAdapter(
            server_name=spec.name,
            transport=transport,
            call_timeout_seconds=timeout,
        )
        await adapter.connect()
        return adapter

    return await build_mcp_adapters(
        specs,
        factory=_factory,
        call_timeout_seconds=float(
            getattr(settings, "mcp_call_timeout_seconds", 30.0)
        ),
    )


async def process_role_message(
    *,
    role_name: str,
    command_text: str,
    image_inputs: list[FeishuImageInput] | None = None,
    message_type: str = "text",
    trace_id: str | None,
    chat_id: str | None,
    bot_context: FeishuBotContext | None = None,
    llm_agent_adapter: LlmAgentAdapter | None = None,
    thread_context: FeishuThreadContext | None = None,
    thread_update_fn: ThreadUpdateFn | None = None,
) -> FeishuRuntimeResult:
    context = bot_context or resolve_bot_context_for_role(role_name)
    trace = trace_id or str(uuid4())

    # M1: open or resume the per-thread task. We defer project_id
    # resolution (which is TL-specific) and stamp it later via
    # ``meta`` updates; the handle carries what we know right now so
    # ``message.inbound`` can go into the log immediately. A ``None``
    # handle means the feature is disabled OR we failed to open the
    # log — either way the rest of the runtime uses the same code
    # paths as before.
    task_handle = _open_task_handle(
        bot_context=context,
        thread_context=thread_context,
        chat_id=chat_id,
        role_name=role_name,
        project_id=None,
    )
    if task_handle is not None:
        try:
            task_handle.append(
                kind="message.inbound",
                trace_id=trace,
                payload={
                    "role_name": role_name,
                    "message_type": message_type,
                    "command_text": (command_text or "")[:4000],
                    "message_id": (
                        thread_context.message_id if thread_context else None
                    ),
                    "thread_id": (
                        thread_context.thread_id if thread_context else None
                    ),
                    "image_count": len(image_inputs or []),
                },
            )
        except Exception:  # noqa: BLE001 — never break the runtime
            logger.warning(
                "append message.inbound failed task_id=%s trace=%s",
                task_handle.task_id,
                trace,
                exc_info=True,
            )

    # Tier-2 cancel fast-path.
    #
    # If the message is a live-cancel command (``取消`` / ``stop`` /
    # ``打断``) AND a session is currently registered for this same
    # (bot, chat, thread) key, we cancel it and return immediately
    # instead of spinning up a new session. The token is cooperative,
    # so the live session stops at its next checkpoint — usually
    # within a second — and its ``on_session_end`` hook reports the
    # stop reason back through Feishu.
    #
    # The pending-action handler below ALSO checks cancel keywords,
    # but that's a different state (user responding to a prompt).
    # The two paths are compatible because we only fire the live
    # cancel when the registry has a matching token, and a token is
    # only registered while a tool loop is running.
    if is_live_cancel_command(command_text):
        cancel_bot_name = context.bot_name if context else role_name
        cancel_key = cancel_key_for(
            bot_name=cancel_bot_name,
            chat_id=chat_id,
            thread_id=thread_context.root_id if thread_context else None,
        )
        if GLOBAL_REGISTRY.cancel(cancel_key, reason="user_cancel"):
            logger.info(
                "live cancel accepted key=%s trace=%s",
                cancel_key.describe(),
                trace,
            )
            return FeishuRuntimeResult(
                ok=True,
                trace_id=trace,
                message="已请求停止当前会话，稍后即可看到停止结果。",
                route_action="session_cancelled",
            )
        # No live session — fall through so the message can still
        # route to the pending-action handler below (which may treat
        # "取消" as "cancel the pending prompt").

    roles_dir = _roles_dir()
    role_registry = RoleRegistryService(roles_dir) if roles_dir else RoleRegistryService(Path("/dev/null"))
    register_role_executors(role_registry)

    adapter = llm_agent_adapter
    adapter_owned = False
    if adapter is None:
        llm_base = settings.techbot_llm_base_url
        llm_key = settings.techbot_llm_api_key
        if not llm_base or not llm_key:
            return FeishuRuntimeResult(
                ok=False,
                trace_id=trace,
                message="LLM 未配置。请设置 techbot_llm_base_url 和 techbot_llm_api_key。",
                route_action="error",
            )
        # Harness plugins — all opt-in, all fall back to no-op when
        # settings don't configure them. ``context_compressor`` is
        # always installed (NoOp by default) so every turn passes
        # through the same code path; verifier / provider_pool are
        # ``None`` when there's nothing to do, keeping the fast path
        # a pointer comparison.
        _repo_root_for_adapter = _runtime_repo_root()
        _registry_for_adapter = _get_project_registry(_repo_root_for_adapter)
        adapter = LlmAgentAdapter(
            llm_base_url=llm_base,
            llm_api_key=llm_key,
            default_model=settings.techbot_llm_model
            or settings.role_agent_default_model
            or "doubao-seed-2-0-pro-260215",
            timeout=settings.role_agent_timeout_seconds,
            max_tool_turns=settings.role_agent_max_tool_turns,
            max_output_tokens=int(
                getattr(settings, "llm_max_output_tokens", 0) or 0
            ),
            context_compressor=_build_context_compressor(),
            tool_verifier=_build_tool_verifier(
                app_repo_root=_repo_root_for_adapter,
                registry=_registry_for_adapter,
            ),
            provider_pool=_build_provider_pool(),
            # B-2 effect-aware fan-out. Off by default (``None``) so
            # an operator can stage the rollout by flipping one env
            # var (``FEISHU_MAX_PARALLEL_TOOL_CALLS`` via the Settings
            # binding). A value of 1 is behaviourally identical to off
            # — useful as a staged kill-switch.
            max_parallel_tool_calls=(
                settings.max_parallel_tool_calls
                if settings.max_parallel_tool_calls
                and settings.max_parallel_tool_calls > 1
                else None
            ),
        )
        adapter_owned = True

    # Entire tech-lead flow is wrapped so a locally-created adapter is
    # released exactly once, no matter which branch returns first. This
    # fixes a socket leak where each Feishu message opened a fresh
    # httpx.AsyncClient that was never released.
    try:
        if context.bot_name == "product_manager":
            return await _process_pm_message(
                context=context,
                command_text=command_text,
                trace=trace,
                adapter=adapter,
                role_registry=role_registry,
                thread_context=thread_context,
                thread_update_fn=thread_update_fn,
                task_handle=task_handle,
            )

        pending_service = _build_pending_action_service()
        if chat_id and pending_service:
            pending_result = await _handle_pending_action(
                pending_service=pending_service,
                chat_id=chat_id,
                command_text=command_text,
                trace=trace,
                context=context,
            )
            if pending_result is not None:
                return pending_result

        recent_conversation = load_recent_conversation(role_name=role_name, chat_id=chat_id)
        progress_service = build_progress_sync_service(context)

        repo_root = _runtime_repo_root()
        registry = _get_project_registry(repo_root)
        project_id = _resolve_project_id(registry, chat_id=chat_id)
        if not project_id:
            logger.warning(
                "no project_id resolvable (empty registry / no default_project_id); "
                "tech-lead running in project-less mode."
            )
            project_id = ""

        try:
            sprint_state = (
                _build_sprint_state_service(
                    progress_service, project_id, registry=registry
                )
                if project_id
                else None
            )
        except RuntimeError:
            sprint_state = None
        if sprint_state is None:
            sprint_state = SprintStateService(progress_service.repo_root, "sprint-status.yaml")

        audit_dir = (repo_root / settings.techbot_run_log_dir) if repo_root else Path("/tmp/feishu-audit")

        tl_feishu_client = ManagedFeishuClient(
            FeishuAuthConfig(app_id=context.app_id, app_secret=context.app_secret),
            default_internal_token_kind="tenant",
        )

        tl_role_executor_provider = _build_role_executor_provider(
            role_registry=role_registry,
            progress_service=progress_service,
            sprint_state=sprint_state,
            project_id=project_id,
            command_text=command_text,
            repo_root=repo_root,
            context=context,
            registry=registry,
            trace_id=trace,
            chat_id=chat_id or "",
        )
        tl_workflow_service = _build_workflow_service(repo_root, registry=registry)
        tl_speckit_script_service = _build_speckit_script_service(
            repo_root, registry=registry
        )
        tl_code_write_service = _build_code_write_service(
            repo_root, registry=registry, trace_id=trace
        )
        tl_pre_push_inspector = _build_pre_push_inspector(
            repo_root, registry=registry
        )
        tl_git_ops_service = _build_git_ops_service(
            repo_root,
            registry=registry,
            inspector=tl_pre_push_inspector,
            trace_id=trace,
        )
        tl_pull_request_service = _build_pull_request_service(
            repo_root, registry=registry, trace_id=trace
        )
        tl_ci_watch_service = _build_ci_watch_service(
            repo_root, registry=registry
        )
        tl_agent_notes_service = _build_agent_notes_service(
            app_repo_root=repo_root,
            registry=registry,
            project_id=project_id,
        )
        tl_last_run_memory = _build_last_run_memory_service(
            app_repo_root=repo_root,
            registry=registry,
            project_id=project_id,
        )

        # Pre-flight git sync: make sure the ``shared-repo`` clone on
        # this server is caught up with the authoritative remote BEFORE
        # the LLM reads any project file. Cached per (bot, chat,
        # thread) so follow-up messages in the same thread skip the
        # fetch. See ``git_sync_preflight.run_preflight_sync`` for the
        # invariants (FF-only, typed-error skip, baseline always captured).
        preflight_snapshot = _run_bot_preflight(
            git_ops_service=tl_git_ops_service,
            project_id=project_id,
            repo_root=repo_root,
            registry=registry,
            bot_name=context.bot_name,
            chat_id=chat_id,
            thread_context=thread_context,
            thread_update_fn=thread_update_fn,
            pending_action_service=pending_service,
        )

        # Tier-2 per-message services: HookBus, CancelToken, lineage
        # tracker. Constructed before the TL executor so they can be
        # injected at construction time (not late-bound), which in
        # turn guarantees every dispatch the TL makes goes through
        # the same bus / token as the parent session.
        tier2_ctx: Tier2RuntimeContext = allocate_runtime_context(
            bot_name=context.bot_name,
            chat_id=chat_id,
            thread_id=thread_context.root_id if thread_context else None,
            trace_id=trace,
            root_role="tech_lead",
        )
        # Lineage persistence is opt-in; off by default so we don't
        # double the audit dir in production. When enabled, the
        # subscriber fires exactly once (on the ROOT session's end)
        # and writes a ``{trace}-lineage.json`` sibling.
        if bool(getattr(settings, "lineage_audit_enabled", False)):
            attach_lineage_audit(
                bus=tier2_ctx.hook_bus,
                tracker=tier2_ctx.lineage,
                audit_service=AuditService(audit_dir),
                root_trace_id=trace,
            )

        # Mirror HookBus session-meta events into the append-only task
        # event log so the auditable stream is complete even for the
        # cross-cutting concerns that run off the adapter's happy path.
        if task_handle is not None:
            TaskEventProjector(task_handle).attach(tier2_ctx.hook_bus)

        # Story 004.5 — one-per-session instances of WorktreeManager
        # (B-3) and TaskGraph (B-1). Both are optional: ``WorktreeManager``
        # no-ops into fallback handles when ``repo_root`` is unresolvable
        # or the operator flipped ``ENABLE_WORKTREE_ISOLATION=false``;
        # ``TaskGraph`` is harmless when no dispatch carries a
        # ``task_id``. Emitting through the TL's own ``AuditService``
        # (reused below) keeps ``claim.*`` and ``worktree.*`` events in
        # the same jsonl stream that the runbook tells operators to
        # grep.
        tl_audit_service = AuditService(audit_dir)
        tl_worktree_manager: WorktreeManager | None = None
        if repo_root is not None:
            try:
                tl_worktree_manager = WorktreeManager(
                    repo_root,
                    enabled=bool(
                        getattr(settings, "enable_worktree_isolation", True)
                    ),
                )
            except Exception:
                logger.exception(
                    "failed to construct WorktreeManager; dispatches "
                    "with needs_worktree=True will fall back to the "
                    "main working copy"
                )
                tl_worktree_manager = None
        tl_task_graph: TaskGraph | None = None
        if sprint_state is not None:
            try:
                tl_task_graph = TaskGraph(
                    sprint_state, audit=tl_audit_service
                )
            except Exception:
                logger.exception(
                    "failed to construct TaskGraph; dispatches carrying "
                    "a task_id will skip the claim lease"
                )
                tl_task_graph = None

        executor = TechLeadToolExecutor(
            progress_sync_service=progress_service,
            sprint_state_service=sprint_state,
            audit_service=tl_audit_service,
            llm_agent_adapter=adapter,
            role_registry=role_registry,
            pending_action_service=pending_service,
            feishu_client=tl_feishu_client,
            application_agent_open_id=settings.application_agent_open_id,
            application_agent_group_chat_id=settings.application_agent_group_chat_id,
            application_agent_delegate_url=settings.application_agent_delegate_url,
            application_agent_display_name=settings.application_agent_display_name,
            tech_lead_bot_open_id=settings.tech_lead_bot_open_id,
            impersonation_token_service=_build_impersonation_service(repo_root),
            project_id=project_id,
            command_text=command_text,
            trace_id=trace,
            chat_id=chat_id,
            recent_conversation=recent_conversation,
            timeout_seconds=settings.role_agent_timeout_seconds,
            thread_update_fn=thread_update_fn,
            role_executor_provider=tl_role_executor_provider,
            workflow_service=tl_workflow_service,
            speckit_script_service=tl_speckit_script_service,
            code_write_service=tl_code_write_service,
            pre_push_inspector=tl_pre_push_inspector,
            git_ops_service=tl_git_ops_service,
            pull_request_service=tl_pull_request_service,
            ci_watch_service=tl_ci_watch_service,
            agent_notes_service=tl_agent_notes_service,
            hook_bus=tier2_ctx.hook_bus,
            cancel_token=tier2_ctx.cancel_token,
            task_handle=task_handle,
            artifact_store=_build_artifact_store(),
            # TL is the top-of-tree session for this Feishu thread,
            # so parent == root. A future nested-TL topology would
            # thread the outer session's trace in here instead.
            root_trace_id=trace,
            worktree_manager=tl_worktree_manager,
            task_graph=tl_task_graph,
        )

        system_prompt = _load_tech_lead_system_prompt()
        system_prompt = _enrich_system_prompt(
            system_prompt,
            load_tables=available_bitable_tables,
            role_registry=role_registry,
            agent_name="tech_lead",
            application_agent_display_name=settings.application_agent_display_name,
        )
        memory_assembler = MemoryAssembler(
            session_summary_service=SessionSummaryService()
        )
        memory_assembly = memory_assembler.build(
            MemoryQueryContext(
                role_name="tech_lead",
                project_id=project_id,
                user_query=command_text,
                task_handle=task_handle,
                notes_service=tl_agent_notes_service,
                last_run_service=tl_last_run_memory,
                baseline_fragment=(
                    render_baseline_for_prompt(preflight_snapshot)
                    if preflight_snapshot is not None
                    else ""
                ),
                current_trace_id=trace,
                notes_limit=int(getattr(settings, "agent_notes_prompt_limit", 20)),
            )
        )
        memory_suffix = memory_assembly.system_prompt_suffix()
        if memory_suffix:
            system_prompt = (
                system_prompt.rstrip()
                + "\n\n"
                + memory_suffix
            )

        # Tier-2 MCP wiring. Load + connect any configured MCP servers
        # on demand; if none configured (the default), this returns
        # an empty list and we use the TL executor directly. The
        # ``CompositeToolExecutor`` wrapper is only added when at
        # least one MCP adapter connected — keeps the hot path free
        # of an extra indirection for users who don't use MCP.
        mcp_adapters: list[object] = []
        try:
            mcp_adapters = await _build_mcp_adapters_for_session()
        except Exception:
            logger.warning(
                "MCP adapter build failed (trace=%s); continuing without MCP",
                trace,
                exc_info=True,
            )
            mcp_adapters = []

        session_executor: AgentToolExecutor = executor
        if mcp_adapters:
            from feishu_agent.tools.mcp_tool_adapter import (
                CompositeToolExecutor,
            )

            session_executor = CompositeToolExecutor(
                native=executor,
                mcp_adapters=mcp_adapters,  # type: ignore[arg-type]
            )

        # M3 self-state overlay: expose set_mode / set_plan / todos /
        # note on top of whatever world executor (native or MCP
        # composite) we just built. ``CombinedExecutor`` filters name
        # collisions "self wins", so the TL's own tools are never
        # shadowed by world tools with the same name. This is
        # deliberately narrow — we only overlay self-state when a
        # task_handle exists; older tests that run without a task
        # service see the unchanged executor.
        if task_handle is not None:
            session_executor = CombinedExecutor(
                self_executor=TaskStateExecutor(task_handle),
                world_executor=session_executor,
            )

        # Hook-bus-driven run digest. Persists on ``on_session_end``;
        # we also keep the handle so we can write a failure digest if
        # the adapter itself raises before the event fires.
        run_digest_collector = None
        if tl_last_run_memory is not None and tl_last_run_memory.enabled:
            from feishu_agent.team.last_run_memory_service import (
                RunDigestCollector,
            )

            run_digest_collector = RunDigestCollector(
                service=tl_last_run_memory,
                trace_id=trace,
                user_command=command_text,
                role="tech_lead",
            )
            run_digest_collector.attach(tier2_ctx.hook_bus)

        if task_handle is not None:
            MemoryWriterService(
                task_handle=task_handle,
                notes_service=tl_agent_notes_service,
                last_run_service=tl_last_run_memory,
                session_summary_service=SessionSummaryService(),
            ).attach(tier2_ctx.hook_bus)

        try:
            if not adapter.is_connected:
                await adapter.connect()

            agent = await adapter.create_agent(
                agent_id=f"tech-lead-{trace}",
                system_prompt=system_prompt,
            )
            session_result = await adapter.execute_with_tools(
                agent,
                command_text,
                session_executor,
                hook_bus=tier2_ctx.hook_bus,
                cancel_token=tier2_ctx.cancel_token,
                trace_id=trace,
                task_handle=task_handle,
            )
        except Exception as exc:
            logger.exception("LLM session failed (trace=%s)", trace)
            if run_digest_collector is not None:
                run_digest_collector.flush_on_exception(exc)
            await close_mcp_adapters(mcp_adapters)
            release_runtime_context(tier2_ctx)
            return FeishuRuntimeResult(
                ok=False,
                trace_id=trace,
                message=f"LLM 会话失败：{exc}",
                route_action="error",
            )
        else:
            await close_mcp_adapters(mcp_adapters)
            release_runtime_context(tier2_ctx)

        # Translate a cooperative cancel into a user-visible notice.
        # The tool loop stops at the next safe checkpoint and comes
        # back with ``stop_reason="cancelled"`` + empty content. We
        # override the reply here so the user sees "已停止" rather
        # than a confusing "LLM 会话未返回内容".
        if session_result.stop_reason == "cancelled":
            return FeishuRuntimeResult(
                ok=True,
                trace_id=trace,
                message="会话已按请求停止。",
                route_action="session_cancelled",
                target_table_name=executor.last_table_name,
            )

        if session_result.success:
            reply = session_result.content or "（无回复内容）"
        else:
            reply = session_result.error_message or session_result.content or "LLM 会话未返回内容"

        return FeishuRuntimeResult(
            ok=session_result.success,
            trace_id=trace,
            message=reply,
            route_action="role_llm_session",
            target_table_name=executor.last_table_name,
        )
    finally:
        if adapter_owned:
            try:
                await adapter.close()
            except Exception:  # pragma: no cover - defensive
                logger.warning("adapter close failed (trace=%s)", trace, exc_info=True)


_CONFIRM_KEYWORDS = {"ok", "确认", "确定", "yes", "执行", "好的", "go", "approve", "approved"}
_CANCEL_KEYWORDS = {"取消", "cancel", "no", "算了", "不行", "abort", "stop"}

# Prefixes that users commonly type as shorthand intent signals.
# We match these as LEFT-anchored substrings so "确认下" or "确定吧"
# still counts as confirmation. Deliberately NOT using plain
# "substring anywhere in the message" to avoid matching partials like
# "不确定" → confirm (bad) or "不是吧" → cancel (bad).
_CONFIRM_PREFIXES = ("确认", "确定", "好的", "ok", "yes", "approve")
_CANCEL_PREFIXES = ("取消", "cancel", "abort", "算了")

# Feishu renders every @-mention in the message body as a placeholder token of
# the form ``@_user_N`` (where N is 1-based index into the message's
# ``mentions`` array). We must strip ONE OR MORE of these leading tokens from
# a pending-action reply like ``@_user_1 确认`` before trying keyword matching,
# otherwise the confirm/cancel prefix match fails and the user gets stuck in
# the reminder loop. Intentionally only strips the *leading run* of mentions;
# a mention in the middle of the text is preserved so substring checks stay
# honest. ``@_user_N`` is also the literal format in Feishu IM event payloads.
_MENTION_PREFIX_RE = re.compile(r"^(?:@_user_\d+\s*)+")


def _build_pending_action_service() -> PendingActionService | None:
    repo_root = _runtime_repo_root()
    if repo_root is None:
        return None
    run_log_dir = repo_root / settings.techbot_run_log_dir
    # A-3 migration: the flat ``{run_log_dir}/pending/`` dir stays as
    # the read-compat fallback; writes with a ``root_trace_id`` now
    # land under ``{run_log_dir}/teams/{root}/pending/`` alongside the
    # artifact envelope. Both layouts are scanned on read for one
    # sprint cycle so in-flight confirmations survive the rollout.
    return PendingActionService(
        run_log_dir / "pending",
        teams_pending_root=run_log_dir / "teams",
    )


def _build_artifact_store() -> ArtifactStore | None:
    """Return an :class:`ArtifactStore` anchored at the configured
    run-log dir, or ``None`` when the feature is disabled or the
    runtime couldn't locate a repo root.

    Returning ``None`` is deliberate: ``TechLeadToolExecutor``
    treats a missing store as "operator opted out" and skips the
    whole A-3 envelope pipeline. Keeps tests (which don't build
    a runtime) symmetric with production — both paths work.
    """
    if not settings.artifact_store_enabled:
        return None
    repo_root = _runtime_repo_root()
    if repo_root is None:
        return None
    return ArtifactStore(repo_root / settings.techbot_run_log_dir)


async def _handle_pending_action(
    *,
    pending_service: PendingActionService,
    chat_id: str,
    command_text: str,
    trace: str,
    context: FeishuBotContext,
) -> FeishuRuntimeResult | None:
    pending = pending_service.load_by_chat_id(chat_id)
    if pending is None:
        return None

    # ``advance_sprint_state`` used to be gated through request_confirmation
    # but isn't anymore — a YAML status flip is reversible and audit-logged,
    # so the confirm round-trip only added friction. Any ``advance_sprint_state``
    # pending file on disk is therefore stale (written before the gate was
    # removed). Delete it silently and let the user's new message proceed
    # to the LLM as if no pending existed, so a fresh "推进下一个 sprint"
    # never gets hijacked by a week-old reminder.
    if pending.action_type == "advance_sprint_state":
        logger.info(
            "discarding stale advance_sprint_state pending trace=%s chat=%s",
            pending.trace_id,
            chat_id,
        )
        pending_service.delete(pending.trace_id)
        return None

    # Strip any leading ``@_user_N`` mention tokens (Feishu group messages
    # always include the mention in the body) so ``@_user_1 确认`` still
    # matches the confirm prefix. Without this the user's confirm reply
    # silently falls through to the "pending_reminder" canned response
    # forever, which is the symptom behind the confirm-loop incident.
    normalized = _MENTION_PREFIX_RE.sub("", command_text).strip().lower()

    def _matches(value: str, exact: set[str], prefixes: tuple[str, ...]) -> bool:
        if value in exact:
            return True
        return any(value.startswith(p) for p in prefixes)

    if _matches(normalized, _CONFIRM_KEYWORDS, _CONFIRM_PREFIXES):
        try:
            result = await _execute_pending_action(
                pending=pending,
                context=context,
                trace=trace,
            )
        except Exception as exc:
            logger.exception("Pending action execution failed (trace=%s)", pending.trace_id)
            result = FeishuRuntimeResult(
                ok=False,
                trace_id=trace,
                message=f"执行待确认操作失败：{exc}",
                route_action="error",
            )
        pending_service.delete(pending.trace_id)
        return result

    if _matches(normalized, _CANCEL_KEYWORDS, _CANCEL_PREFIXES):
        pending_service.delete(pending.trace_id)
        return FeishuRuntimeResult(
            ok=True,
            trace_id=trace,
            message=f"已取消操作：{pending.action_type}",
            route_action="pending_cancelled",
        )

    return FeishuRuntimeResult(
        ok=True,
        trace_id=trace,
        message=(
            f"当前有待确认的操作：{pending.action_type}\n"
            "回复「确认」执行，或「取消」放弃。"
        ),
        route_action="pending_reminder",
    )


async def _execute_pending_action(
    *,
    pending: PendingAction,
    context: FeishuBotContext,
    trace: str,
) -> FeishuRuntimeResult:
    progress_service = build_progress_sync_service(context)

    repo_root = _runtime_repo_root()
    registry = _get_project_registry(repo_root)
    project_id = _resolve_project_id(registry, chat_id=pending.chat_id)
    if not project_id:
        return FeishuRuntimeResult(
            ok=False,
            trace_id=trace,
            message="无法执行：当前实例未配置 default_project_id，且该会话未绑定项目。",
            route_action="error",
        )

    if pending.action_type == "write_progress_sync":
        from feishu_agent.schemas.progress_sync import ProgressSyncRequest

        module = pending.action_args.get("module")
        command_text = "同步 vineyard module" if module == "vineyard_module" else "同步当前进度"
        req = ProgressSyncRequest(
            project_id=project_id,
            command_text=command_text,
            mode="write",
            trace_id=trace,
            chat_id=pending.chat_id,
        )
        sync_result = await progress_service.execute(req)
        return FeishuRuntimeResult(
            ok=sync_result.ok,
            trace_id=trace,
            message=sync_result.message or "进度同步完成。",
            route_action="pending_executed",
        )

    if pending.action_type == "advance_sprint_state":
        try:
            sprint_state = _build_sprint_state_service(
                progress_service, project_id, registry=registry
            )
        except RuntimeError:
            sprint_state = SprintStateService(progress_service.repo_root, "sprint-status.yaml")

        # Don't call ``progress_service.read_records`` here: that reads
        # the status file relative to the *agent* repo root, while the
        # file actually lives under the *project* repo (see
        # ``_build_sprint_state_service`` for the correct rooting).
        # ``SprintStateService.advance`` falls back to picking the next
        # story from its own loaded status data when ``records`` is
        # empty, which is the correct source of truth anyway.
        args = pending.action_args
        changes = sprint_state.advance(
            [],
            story_key=args.get("story_key"),
            to_status=args.get("to_status"),
            reason=f"Confirmed pending action: {pending.trace_id}",
            dry_run=False,
        )
        summary = f"Sprint 状态已更新：{len(changes)} 条变更。"
        if changes:
            summary += f"\n{changes[0].story_key}: {changes[0].from_status} → {changes[0].to_status}"
        return FeishuRuntimeResult(
            ok=True,
            trace_id=trace,
            message=summary,
            route_action="pending_executed",
        )

    if pending.action_type == "force_sync_to_remote":
        if repo_root is None:
            return FeishuRuntimeResult(
                ok=False,
                trace_id=trace,
                message="无法执行硬重置：未配置 app_repo_root。",
                route_action="error",
            )
        return await _execute_force_sync_pending(
            pending=pending,
            repo_root=repo_root,
            registry=registry,
            project_id=project_id,
            trace=trace,
        )

    return FeishuRuntimeResult(
        ok=False,
        trace_id=trace,
        message=f"未知的待确认操作类型：{pending.action_type}",
        route_action="error",
    )


async def _execute_force_sync_pending(
    *,
    pending: PendingAction,
    repo_root: Path,
    registry: ProjectRegistry,
    project_id: str,
    trace: str,
) -> FeishuRuntimeResult:
    """Execute a confirmed ``force_sync_to_remote`` pending action.

    Runs the destructive git pipeline on the resolved project repo,
    invalidates the preflight snapshot cache (so the next message in
    any chat starts a fresh fetch), and renders a compact Chinese
    summary for the user. Errors from git are surfaced verbatim in
    the reply so the human can react (e.g. "remote doesn't have
    main" → they reconfigure the remote).
    """
    from feishu_agent.tools.git_ops_service import (
        ForceSyncResult,
        GitOpsError,
    )
    from feishu_agent.tools.git_sync_preflight import (
        _invalidate_cache_for_root,
    )

    args = pending.action_args or {}
    remote = str(args.get("remote") or "origin")
    target_branch = str(args.get("target_branch") or "main")

    pending_project_id = str(args.get("project_id") or "") or project_id
    if not pending_project_id:
        return FeishuRuntimeResult(
            ok=False,
            trace_id=trace,
            message="无法执行硬重置：当前会话未绑定 project_id。",
            route_action="error",
        )

    # Resolve policies ONCE (M-2) and share the project_roots map
    # with _build_git_ops_service indirectly — the helper re-reads
    # through the same mtime-keyed cache, so this is O(1) by the
    # time we ask for project_root below.
    project_roots, _policies = _resolve_code_write_policies(
        repo_root, registry=registry
    )
    project_root = project_roots.get(pending_project_id)

    inspector = _build_pre_push_inspector(repo_root, registry=registry)
    git_ops = _build_git_ops_service(
        repo_root, registry=registry, inspector=inspector, trace_id=trace
    )
    if git_ops is None:
        return FeishuRuntimeResult(
            ok=False,
            trace_id=trace,
            message="无法执行硬重置：项目未配置 git-ops policy。",
            route_action="error",
        )

    try:
        result: ForceSyncResult = git_ops.force_sync_to_remote(
            project_id=pending_project_id,
            remote=remote,
            target_branch=target_branch,
        )
    except GitOpsError as exc:
        return FeishuRuntimeResult(
            ok=False,
            trace_id=trace,
            message=f"硬重置失败（{exc.code}）：{exc.message}",
            route_action="error",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "force_sync_to_remote crashed (trace=%s)", pending.trace_id
        )
        return FeishuRuntimeResult(
            ok=False,
            trace_id=trace,
            message=f"硬重置失败（未预期异常）：{exc}",
            route_action="error",
        )

    # H-2: ALWAYS invalidate the preflight cache after a successful
    # force sync — HEAD moved on disk and a cache hit now would serve
    # a phantom "branch diverged" for the *old* state. The callee is
    # coarse (it clears every entry regardless of the path argument),
    # so we pass ``repo_root`` when the project_root lookup fails
    # rather than silently skipping. Misconfigured policies warrant
    # a warning because the bot reached force_sync despite missing
    # config — likely a bad deploy.
    if project_root is None:
        logger.warning(
            "force_sync succeeded but no project_root in code-write "
            "policy for project_id=%s (cache still flushed via repo_root fallback); "
            "check .larkagent/secrets/code_write/policies.jsonl",
            pending_project_id,
        )
    _invalidate_cache_for_root(project_root or repo_root)

    old_sha = (result.previous_head_sha or "")[:8] or "（未知）"
    new_sha = (result.new_head_sha or "")[:8] or "（未知）"
    prev_branch = result.previous_branch or "（未知）"
    lines = [
        (
            f"✅ 已硬重置 `{result.branch}` 至 `{result.remote}/{result.branch}`"
            f"（{old_sha} → {new_sha}）"
        ),
        f"- 原分支：`{prev_branch}`（若需找回旧提交，使用 `git reflog`）",
    ]
    if result.cleaned_paths_count:
        preview = "、".join(result.cleaned_paths_preview[:5])
        extra = (
            f"（示例：{preview}）" if preview else ""
        )
        lines.append(
            f"- 清理了 {result.cleaned_paths_count} 个未跟踪文件/目录{extra}"
        )
    return FeishuRuntimeResult(
        ok=True,
        trace_id=trace,
        message="\n".join(lines),
        route_action="pending_executed",
    )


async def _process_pm_message(
    *,
    context: FeishuBotContext,
    command_text: str,
    trace: str,
    adapter: LlmAgentAdapter,
    role_registry: RoleRegistryService,
    thread_context: FeishuThreadContext | None = None,
    thread_update_fn: ThreadUpdateFn | None = None,
    task_handle: TaskHandle | None = None,
) -> FeishuRuntimeResult:
    pm_client = ManagedFeishuClient(
        FeishuAuthConfig(app_id=context.app_id, app_secret=context.app_secret),
        default_internal_token_kind="tenant",
    )
    notify_chat_id = settings.pm_notify_tech_lead_chat_id

    repo_root = _runtime_repo_root()
    registry = _get_project_registry(repo_root)
    project_id = _resolve_project_id(registry) or ""

    # Pre-flight git sync for PM. PM doesn't write code, but it reads
    # specs / roadmap files from the project repo, so a stale clone
    # still yields wrong answers. Same cache semantics as tech-lead,
    # with one extra: PM's semantic baseline is the product trunk
    # (typically ``main``), not whatever feature branch the
    # shared-repo happens to be on when TL last worked. Without the
    # ``base_branch`` argument below, PM would inherit TL's branch
    # and write ``research-brief`` / ``product-brief`` artifacts to
    # the wrong place — the exact bug we saw with
    # ``feat/define-sync-interfaces``.
    pm_chat_id = thread_context.chat_id if thread_context else None
    pm_inspector = _build_pre_push_inspector(repo_root, registry=registry)
    pm_git_ops = _build_git_ops_service(
        repo_root, registry=registry, inspector=pm_inspector, trace_id=trace
    )
    pm_base_branch = _pm_baseline_branch(registry, project_id) if project_id else None
    # Pending action service is also wired into PM's preflight so a
    # diverged shared-repo can prompt "确认 / 取消" via the same path
    # the TL path uses. ``_handle_pending_action`` in the PM entry
    # point picks up the reply on the next user message.
    pm_pending_service = _build_pending_action_service()
    pm_preflight_snapshot = _run_bot_preflight(
        git_ops_service=pm_git_ops,
        project_id=project_id,
        repo_root=repo_root,
        registry=registry,
        bot_name=context.bot_name,
        chat_id=pm_chat_id,
        thread_context=thread_context,
        thread_update_fn=thread_update_fn,
        base_branch=pm_base_branch,
        pending_action_service=pm_pending_service,
    )

    pm_provider: Callable[[str, RoleDefinition], AgentToolExecutor | None] | None
    try:
        pm_progress = build_progress_sync_service(context)
        try:
            pm_sprint_state = (
                _build_sprint_state_service(
                    pm_progress, project_id, registry=registry
                )
                if project_id
                else None
            )
        except RuntimeError:
            pm_sprint_state = None
        if pm_sprint_state is None:
            pm_sprint_state = SprintStateService(
                pm_progress.repo_root, "sprint-status.yaml"
            )
        pm_provider = _build_role_executor_provider(
            role_registry=role_registry,
            progress_service=pm_progress,
            sprint_state=pm_sprint_state,
            project_id=project_id,
            command_text=command_text,
            repo_root=repo_root,
            context=context,
            registry=registry,
            trace_id=trace,
            chat_id=pm_chat_id or "",
        )
    except Exception:
        logger.exception("failed to build PM role_executor_provider; falling back to single-shot")
        pm_provider = None

    pm_workflow_service = _build_workflow_service(repo_root, registry=registry)
    pm_speckit_script_service = _build_speckit_script_service(
        repo_root, registry=registry
    )
    pm_artifact_publish_service = _build_artifact_publish_service(
        repo_root, registry=registry
    )

    executor = PMToolExecutor(
        llm_agent_adapter=adapter,
        role_registry=role_registry,
        feishu_client=pm_client,
        notify_chat_id=notify_chat_id,
        timeout_seconds=settings.role_agent_timeout_seconds,
        role_executor_provider=pm_provider,
        workflow_service=pm_workflow_service,
        speckit_script_service=pm_speckit_script_service,
        artifact_publish_service=pm_artifact_publish_service,
        project_id=project_id,
    )

    system_prompt = _load_pm_system_prompt()
    system_prompt = _enrich_system_prompt(
        system_prompt,
        load_tables=available_bitable_tables,
        role_registry=role_registry,
        agent_name="product_manager",
        application_agent_display_name=settings.application_agent_display_name,
    )
    if pm_preflight_snapshot is not None:
        system_prompt = (
            system_prompt.rstrip()
            + "\n\n"
            + render_baseline_for_prompt(pm_preflight_snapshot)
        )

    try:
        if not adapter.is_connected:
            await adapter.connect()

        agent = await adapter.create_agent(
            agent_id=f"product-manager-{trace}",
            system_prompt=system_prompt,
        )
        # M3 self-state overlay — same pattern as the TL path. See
        # the comment at the TL ``execute_with_tools`` call site for
        # rationale.
        pm_session_executor: AgentToolExecutor = executor
        if task_handle is not None:
            pm_session_executor = CombinedExecutor(
                self_executor=TaskStateExecutor(task_handle),
                world_executor=executor,
            )
        session_result = await adapter.execute_with_tools(
            agent,
            command_text,
            pm_session_executor,
            trace_id=trace,
            task_handle=task_handle,
        )
    except Exception as exc:
        logger.exception("PM LLM session failed (trace=%s)", trace)
        return FeishuRuntimeResult(
            ok=False,
            trace_id=trace,
            message=f"产品经理会话失败：{exc}",
            route_action="error",
        )

    if session_result.success:
        pm_reply = session_result.content or "（无回复内容）"
    else:
        pm_reply = session_result.error_message or session_result.content or "产品经理会话未返回内容"

    return FeishuRuntimeResult(
        ok=session_result.success,
        trace_id=trace,
        message=pm_reply,
        route_action="role_llm_session",
    )
