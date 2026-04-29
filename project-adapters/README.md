# `project-adapters/` — 下游项目适配器

每个 **JSON 文件** 描述一个下游代码仓库：Sprint 状态文件路径、飞书多维表格字段映射、可选 CI/部署元数据等。主服务在运行时会读取这些配置，将进度同步到 Bitable 或触发其他自动化。

## 使用方式

1. 复制现有 JSON，修改 `project_id`、`display_name`、`project_repo_root` 等字段。  
2. 将真实 token、表 ID 等放在 **`.larkagent/secrets/`** 或根目录 `.env` 中，**不要** 写进提交到 git 的 JSON（除非仅为占位示例）。

## 轻度 vs 重度

- **轻度**：若只做对话、不做多项目进度同步，可 **不配置** 本目录，或只保留最小示例。  
- **重度**：多项目、多环境时在此维护多份适配器，并与 `DEFAULT_PROJECT_ID`、密钥目录中的 `projects.jsonl` 等配合使用。

具体字段含义以各 JSON 内注释及代码中 `project_registry` / 适配器读取逻辑为准。
