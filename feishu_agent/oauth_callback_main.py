"""Entrypoint for the Feishu OAuth callback microservice.

Run via:
    uvicorn feishu_agent.oauth_callback_main:app --host 127.0.0.1 --port 18766
"""

from feishu_agent.runtime.oauth_callback_server import app

__all__ = ["app"]
