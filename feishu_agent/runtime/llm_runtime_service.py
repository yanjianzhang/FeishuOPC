from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from feishu_agent.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class LLMRuntimeService:
    def __init__(self) -> None:
        self.api_key = settings.techbot_llm_api_key
        self.base_url = (settings.techbot_llm_base_url or "https://api.openai.com").rstrip("/")
        self.model = settings.techbot_llm_model
        self.source = (settings.techbot_llm_source or "").strip().lower() or "openai"
        self.config_path = self._resolve_optional_path(settings.techbot_llm_config_path)
        self.env_path = self._resolve_optional_path(settings.techbot_llm_env_path)
        self.timeout = settings.techbot_llm_timeout_seconds

    @property
    def _responses_url(self) -> str:
        if self.source == "volcengine_ark":
            return f"{self.base_url}/responses"
        return f"{self.base_url}/v1/responses"

    def is_configured(self) -> bool:
        if self.source == "aws_bedrock":
            env = self._load_env_file()
            return bool(
                self.model
                and env.get("AWS_REGION")
                and env.get("AWS_ACCESS_KEY_ID")
                and env.get("AWS_SECRET_ACCESS_KEY")
            )
        if self.source in ("openai_compatible", "volcengine_ark"):
            return bool(self.api_key and self.base_url and self.model)
        return bool(self.api_key and self.model)

    async def summarize_manager_response(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> str | None:
        if not self.is_configured():
            return None

        if self.source == "aws_bedrock":
            return await self._summarize_with_bedrock(system_prompt=system_prompt, user_payload=user_payload)
        if self.source == "openai_compatible":
            return await self._summarize_with_openai_compatible(
                system_prompt=system_prompt,
                user_payload=user_payload,
            )
        return await self._summarize_with_openai_responses(  # openai, volcengine_ark
            system_prompt=system_prompt,
            user_payload=user_payload,
        )

    async def choose_feishu_route(
        self,
        *,
        user_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        raw = await self.summarize_manager_response(
            system_prompt=(
                "你是技术组长消息编排器。你的任务不是回答用户，而是从 available_tools 中选择下一步最合适调用的内部工具。"
                "请只依据用户原话和工具目录中的能力定义进行判断，不要假设存在任何本地关键词规则、硬编码路由规则或默认兜底。"
                "每个工具都带有用途说明、执行效果和参数提示。你需要选择唯一一个最合适的工具。"
                "如果工具需要参数，请在 arguments 中填写；如果没有明确参数，就返回空对象。"
                "返回严格 JSON，不要使用 Markdown 代码块。"
                "字段必须是：tool_name(string), confidence(number 0-1), rationale(string), arguments(object)。"
                "arguments 中只允许出现工具目录里声明过的参数，例如 module, story_key, sprint。"
            ),
            user_payload=user_payload,
        )
        if not raw:
            return None
        return self._extract_json_object(raw)

    async def choose_bitable_table(
        self,
        *,
        user_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        raw = await self.summarize_manager_response(
            system_prompt=(
                "你是技术组长的表格选择器。"
                "请从候选多维表中选择这次最适合读写的目标表。"
                "优先遵守用户原话中的目标表意图，其次遵守表的 is_default、can_read、can_write 与 notes。"
                "如果用户没有明确指定表，但只有一个可写默认表，就选它。"
                "返回严格 JSON，不要使用 Markdown 代码块。"
                "字段必须是：table_name(string), confidence(number 0-1), rationale(string)。"
            ),
            user_payload=user_payload,
        )
        if not raw:
            return None
        return self._extract_json_object(raw)

    # ------------------------------------------------------------------
    # Summarize backends
    # ------------------------------------------------------------------

    async def _summarize_with_openai_responses(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> str | None:
        user_content = self._build_openai_responses_user_content(user_payload)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self._responses_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "input": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                    },
                )
                response.raise_for_status()
                data = response.json()
        except Exception:
            logger.exception("Responses API summarize failed (source=%s)", self.source)
            return None

        return self._extract_response_text(data)

    async def _summarize_with_openai_compatible(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": self._build_openai_chat_user_content(user_payload)},
                        ],
                        "temperature": 0.2,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except Exception:
            logger.exception("OpenAI-compatible summarize failed")
            return None

        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        text = message.get("content")
        return text.strip() if isinstance(text, str) and text.strip() else None

    async def _summarize_with_bedrock(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> str | None:
        try:
            from anthropic import AnthropicBedrock
        except ImportError:
            return None

        env = self._load_env_file()
        saved_proxy = self._clear_proxy_env()
        try:
            client = AnthropicBedrock(
                aws_access_key=env.get("AWS_ACCESS_KEY_ID"),
                aws_secret_key=env.get("AWS_SECRET_ACCESS_KEY"),
                aws_region=env.get("AWS_REGION"),
                timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 30.0)),
            )
            response = await asyncio.to_thread(
                lambda: client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    temperature=0.2,
                    system=system_prompt,
                    messages=[{"role": "user", "content": self._build_bedrock_user_content(user_payload)}],
                )
            )
        except Exception:
            logger.exception("Bedrock summarize failed")
            return None
        finally:
            self._restore_proxy_env(saved_proxy)

        if not response or not response.content:
            return None
        first = response.content[0]
        text = getattr(first, "text", None)
        return text.strip() if isinstance(text, str) and text.strip() else None

    # ------------------------------------------------------------------
    # Environment / config helpers
    # ------------------------------------------------------------------

    def _load_env_file(self) -> dict[str, str]:
        values = {}
        if self.env_path and self.env_path.exists():
            for raw_line in self.env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
        return values

    @staticmethod
    def _resolve_optional_path(raw_path: str | None) -> Path | None:
        if not raw_path:
            return None
        return Path(raw_path).expanduser()

    # ------------------------------------------------------------------
    # Content builders
    # ------------------------------------------------------------------

    @classmethod
    def _build_openai_responses_user_content(cls, user_payload: dict[str, Any]) -> str | list[dict[str, Any]]:
        images = cls._extract_inline_images(user_payload)
        prompt_text = cls._build_user_prompt_text(user_payload)
        if not images:
            return prompt_text
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt_text}]
        for image in images:
            content.append({"type": "input_image", "image_url": cls._build_image_data_url(image)})
        return content

    @classmethod
    def _build_openai_chat_user_content(cls, user_payload: dict[str, Any]) -> str | list[dict[str, Any]]:
        images = cls._extract_inline_images(user_payload)
        prompt_text = cls._build_user_prompt_text(user_payload)
        if not images:
            return prompt_text
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for image in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": cls._build_image_data_url(image)},
                }
            )
        return content

    @classmethod
    def _build_bedrock_user_content(cls, user_payload: dict[str, Any]) -> str | list[dict[str, Any]]:
        images = cls._extract_inline_images(user_payload)
        prompt_text = cls._build_user_prompt_text(user_payload)
        if not images:
            return prompt_text
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for image in images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(image.get("mime_type") or "image/png"),
                        "data": str(image.get("base64_data") or ""),
                    },
                }
            )
        return content

    @classmethod
    def _build_user_prompt_text(cls, user_payload: dict[str, Any]) -> str:
        return cls._dump_json(cls._payload_without_inline_images(user_payload))

    @staticmethod
    def _payload_without_inline_images(user_payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(user_payload)
        images = payload.pop("image_inputs", None)
        if not isinstance(images, list) or not images:
            return payload
        payload["image_inputs"] = [
            {
                "file_key": item.get("file_key"),
                "mime_type": item.get("mime_type"),
                "byte_size": item.get("byte_size"),
                "source": item.get("source"),
            }
            for item in images
            if isinstance(item, dict)
        ]
        payload["image_count"] = len(payload["image_inputs"])
        return payload

    @staticmethod
    def _extract_inline_images(user_payload: dict[str, Any]) -> list[dict[str, Any]]:
        images = user_payload.get("image_inputs")
        if not isinstance(images, list):
            return []
        return [
            item
            for item in images
            if isinstance(item, dict) and item.get("base64_data") and item.get("mime_type")
        ]

    @staticmethod
    def _build_image_data_url(image_payload: dict[str, Any]) -> str:
        mime_type = str(image_payload.get("mime_type") or "image/png")
        base64_data = str(image_payload.get("base64_data") or "")
        return f"data:{mime_type};base64,{base64_data}"

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_response_text(data: dict[str, Any]) -> str | None:
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        for item in data.get("output", []):
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return None

    @staticmethod
    def _extract_json_object(payload: str) -> dict[str, Any] | None:
        try:
            data = json.loads(payload)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", payload, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None

    @staticmethod
    def _dump_json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # Proxy management for Bedrock
    # ------------------------------------------------------------------

    @staticmethod
    def _clear_proxy_env() -> dict[str, str]:
        saved = {}
        for key in list(os.environ.keys()):
            if "proxy" in key.lower():
                saved[key] = os.environ.pop(key)
        return saved

    @staticmethod
    def _restore_proxy_env(saved: dict[str, str]) -> None:
        for key, value in saved.items():
            os.environ[key] = value
