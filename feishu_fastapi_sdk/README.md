# feishu_fastapi_sdk

FastAPI 友好的飞书（Lark）开放平台最小客户端。本目录是 FeishuOPC 主仓库内联（vendored）的源码副本，源自作者独立维护的同名项目，便于发布时自包含安装、避免私有 `git+ssh` 依赖。

## 功能

- `FeishuClient`：基于 `httpx` 的异步客户端，封装应用鉴权、消息发送、多维表格写入等常用接口。
- `fastapi_helpers`：事件回调的 `url_verification` 与 token 校验工具，便于在 FastAPI 路由中直接使用。
- `schemas`：Pydantic 数据模型（事件信封、消息、多维表格写入结果等）。
- `config`：`FeishuAuthConfig` / `FeishuWebhookConfig` / `BitableTarget` 等配置对象。

## 安装

作为主仓库的一部分随项目安装，无需单独 `pip install`。在 `requirements.txt` 中已包含所需的运行时依赖（`httpx`、`pydantic` 等）。

## 使用示例

```python
from feishu_fastapi_sdk import FeishuAuthConfig, FeishuClient

auth = FeishuAuthConfig(app_id="cli_xxx", app_secret="xxx")
async with FeishuClient(auth) as client:
    await client.send_text_message(receive_id="ou_xxx", text="hello")
```

## 许可证

MIT License，见 [LICENSE](./LICENSE)。
