from dataclasses import dataclass


@dataclass(slots=True)
class FeishuAuthConfig:
    app_id: str
    app_secret: str


@dataclass(slots=True)
class FeishuWebhookConfig:
    verification_token: str | None = None
    encrypt_key: str | None = None


@dataclass(slots=True)
class BitableTarget:
    app_token: str
    table_id: str
    external_key_field: str = "ExternalKey"
