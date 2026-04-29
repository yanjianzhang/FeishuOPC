# `.larkagent/` — 部署脚本与本地密钥布局

## `agent_deploy.sh`

用于将 **角色目录、共享 venv、密钥目录** 等同步到远端主机（SSH/rsync）。适合 **重度** 多机部署；轻度用户可不用。

## `secrets/` 目录（不入库）

真实密钥仅放在本机 `.larkagent/secrets/` 下；仓库中 **只提交** 带 `example` 字样的模板与 `README`。

典型子目录：

| 子目录 | 用途 |
|--------|------|
| `feishu_bot/` | 各机器人 `AppID` / `AppSecret` / 事件加密字段（参考 `*.example.env`） |
| `feishu_app/` | 多维表格 app_token / table_id（参考 `bitable.example.env`） |
| `ai_key/` | （可选）仅用 **OpenAI 兼容** 形态：`.env` 里放 Key，`model_sources.json` 可由 `model_sources.example.json` 复制；公开文档不介绍其他 provider |
| `deploy/` | 部署机 SSH、主机名等（参考 `server.example.env`） |
| `deploy_projects/` | 各项目部署脚本元数据（参考 `*.json.example`） |
| `user_tokens/` | OAuth 换发的用户 token（**切勿** 提交） |

复制模板时去掉 `.example` 中间段或按各文件说明重命名，例如：

`cp feishu_bot/tech-lead-planner.example.env feishu_bot/tech-lead-planner.env`

## 轻度 vs 重度

- **轻度**：可能 **只需要** 根目录 `.env`，或仅 `feishu_bot` 下一个 `.env` 文件。  
- **重度**：会同时使用多个子目录；并与 `agent_deploy.sh`、服务器上 `shared-resources` 目录结构一致。
