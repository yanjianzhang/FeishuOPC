# `feishu_agent/` — 核心 Python 包

本目录为 **feishu_opc** 的主体代码：飞书事件入口、多角色智能体循环、工具注册表、飞书 OpenAPI 客户端封装等。

## 主要子模块（速览）

| 路径 | 作用 |
|------|------|
| `agent_main.py` | FastAPI 应用入口（HTTP 回调模式） |
| `feishu_ws_main.py` | WebSocket 长连接入口（可选） |
| `config.py` | 配置加载：环境变量 + `.larkagent/secrets/` 下密钥文件合并 |
| `routers/` | HTTP 路由（含飞书事件、健康检查等） |
| `runtime/` | 运行时服务：飞书客户端、OAuth 回调、代发 token 等 |
| `roles/` | 各角色执行器（如技术组长工具循环） |
| `tools/` | 工具实现与工具包（Bitable、Git、文件系统等） |
| `presentation/` | 卡片与消息体构造 |
| `team/` | 任务图、工件存储、Sprint 状态、**记忆拼装**（`memory_assembler`、上次运行、会话摘要等） |
| `core/` | 含 **上下文压缩**、会话血缘等与 Harness 相关的核心逻辑 |
| `tests/` | 单元测试；含 `test_harness_integration.py`（Tier-1 装配形状） |

**Harness（记忆与上文）** 的用户向说明见仓库根目录 [README.md → Harness 专节](../README.md#harness-memory)。

## 与轻度 / 重度使用的关系

- **轻度**：通常只需启动 `agent_main` 或 `feishu_ws_main`，配置好根目录 `.env` 中的飞书机器人与 **OpenAI 兼容** 大模型变量（`TECHBOT_LLM_*`，见根目录 README）。  
- **重度**：会用到更多模块（Bitable、委派、多 bot、OAuth）；配置分散在 `.env` 与 `.larkagent/secrets/` 多个文件中，详见仓库根目录 `README.md` 与 `.larkagent/README.md`。

## 开发提示

```bash
# 在项目根目录 feishu_opc/ 下
pip install -r requirements.txt
pytest feishu_agent/tests -q
```
