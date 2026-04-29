# 飞书 OPC

> **English summary:** Feishu OPC runs a team of collaborative agents (Tech Lead, Product Manager, and a dozen sub-roles) directly inside Feishu/Lark groups. The lead agents own the conversation; sub-agents take over when code needs to be written, reviewed, shipped, or when a PRD or sprint sync is due — turning "we talked about it" into "it's already in the repo." Requires a Feishu self-built app + bot and any OpenAI-compatible LLM endpoint. Bitable, multi-bot entrypoints, and OAuth-on-behalf flows are optional. Continue reading in Chinese below, or jump straight to **特性 / 部署** sections for setup.

---

在飞书里跑一组会协作、能办事的智能体：技术组长、产品经理等主角色在群里承接对话；需要落地代码、审查、写 PRD、同步进度或部署时，再按需唤起对应的子角色，把「说清楚」和「真在仓库里改东西」串成一条链。适合已经用飞书做研发沟通、希望把迭代状态与日常 IM 拉齐的团队。

你只需：一个飞书企业自建应用与机器人，以及任一兼容常见 Chat Completions 形态的大模型 HTTPS 接口（下文统一按「OpenAI 兼容」口径配置）。多维表格、多机器人入口、OAuth 代发等能力均为可选，可按「轻度 / 重度」分档阅读，不必一次配齐。

**阅读提示**：前半偏产品与交互（角色分工、群内长什么样）；从「特性」起逐步进入部署与配置；需要改代码或对照实现时再看 `feishu_agent/` 与各目录子 README。

---

## 多角色全景

**本仓库特色：** 飞书 OPC 不是「只会聊天的单一机器人」，而是 **主会话角色 + 十余种子角色** 的分工体系：群里的主角色负责理解意图、推进流程；子角色各自承担一类专职工作（写代码、评审、PRD、进度同步等），并拥有 **相互隔离的能力边界**。业务上你可以只开少数飞书机器人入口，子角色仍由主角色在对话里按需唤醒。

角色说明写在 Markdown 里（`agents/` 与 `skills/roles/`），便于按团队话术调整；主角色通过内置委派机制拉起子角色（实现侧工具名为 `dispatch_role_agent`，与下表中的 `role_name` 对应）。

### 飞书入口级 · 主会话角色（`agents/`）

每个主角色在部署上可对应 **一个飞书应用 / 机器人**；若你希望「规划」「PRD」等走不同机器人，再拆成多套凭证即可（示例路径见 `.larkagent/secrets/feishu_bot/*.example.env`）。

| 角色 | 文件 | 职责概要 |
|------|------|----------|
| **技术组长** | `agents/tech-lead.md` | 读 Sprint 状态、拆任务、派工、协调子角色与委派 |
| **技术组长 · 规划** | `agents/tech-lead-planner.md` | 技术侧重规划、排期的入口变体 |
| **产品经理** | `agents/product-manager.md` | 需求澄清、优先级与产品侧协调 |
| **产品经理 · PRD** | `agents/product-manager-prd.md` | 侧重 PRD / 需求文档协作的入口变体 |

### 可派生子角色（`skills/roles/<role_name>.md`）

由 **技术组长** 或 **产品经理** 在对话里委派；下表 **标识符** 即调用时的 `role_name`（与技能文件名一致）。

#### A. 专属执行器（深度集成的工程路径）

适合 **写代码、审查、修缺陷、PRD 落盘、进度同步、部署** 等需要定制工具链的任务。

| 标识符 `role_name` | 职责概要 |
|--------------------|----------|
| `developer` | 按任务修改代码、运行测试、产出实现说明 |
| `reviewer` | 代码 / 方案审查与结论 |
| `bug_fixer` | 按审查 Blocker 逐项修复或说明拒绝原因 |
| `prd_writer` | 需求 / PRD 类文档撰写（含 specs 等约定路径） |
| `progress_sync` | 进度与飞书多维表格等同步 |
| `deploy_engineer` | 调用部署脚本与发布相关操作 |

#### B. 工具包驱动（声明式扩展）

子能力以配置声明为主，**加一份技能 Markdown 即可**接入新子角色，无需为每个角色写独立执行器。

| 标识符 `role_name` | 职责概要 |
|--------------------|----------|
| `sprint_planner` | Sprint / 迭代计划辅助 |
| `repo_inspector` | 仓库结构、规范与目录巡检 |
| `researcher` | 调研、信息检索与归纳 |
| `spec_linker` | 规格条目与实现关联 |
| `ux_designer` | 交互与体验侧补充 |
| `qa_tester` | 测试与验收视角 |

> **合计**：4 类飞书主入口 + **12** 类可派生子角色（6 + 6）。扩展时优先在 `skills/roles/` 增加说明文件，并在运行配置中登记该 `role_name`（详见代码侧角色注册与技能描述约定）。

---

<a id="feishu-chat-example"></a>

## 飞书群内对话示例（脱敏节选）

以下为 **群话题内由「技术组长」机器人连续发出的消息** 示意：时间戳、仓库名、域名、分支名、提交哈希、规格目录与 Story 编号均已 **虚构或泛化**。**`developer` / `bug_fixer` 等其它子角色的委派与整段执行过程** 在本节中 **从简省略**（只保留一句起头，避免与「多角色全景」表重复铺陈）；**逐条工具回显、失败与超时** 亦已略去。

**要点**：一条 Story 从推进到合入，常见顺序是 **`developer` 按任务改代码与测试** → **`reviewer`（或固定审查工作流，如示例 `acme:code-review`）** → 若有 Blocker 再 **`bug_fixer`** 等；本节只 **截取「审查完成以后」** 的收尾，便于阅读。通过后常见 **pre-push → commit → push → 开 PR → CI 监听**；最后机器人 **对照 Sprint 与模块任务表** 总结，并 **请你拍板下一步**。

```
技术组长 · 智能体
⏳ 已委派 developer 执行：实现 Story N-N（示例：契约测试与相关类型）。

—— 节选：略去 developer 写码与跑测的全过程；若 review 曾打出 Blocker，还会穿插 bug_fixer 等子角色，此处一并省略 ——

技术组长 · 智能体
⏳ 已委派 reviewer 执行：对 Story N-N（示例：契约测试）做 acme:code-review。读 docs/implementation/N-N-…

—— 节选：略去 reviewer 执行中的逐条工具日志、失败与超时相关提示 ——

技术组长 · 智能体
reviewer ✅ 已完成（约 95s）
行为概要：产出 artifact×1, 读源码×13, 列目录×4

技术组长 · 智能体
✅ pre-push 检查 branch=feat/N-N-example-contract-tests files=0 untracked=2 blockers=0 warnings=0

技术组长 · 智能体
📝 git commit feat/N-N-example-contract-tests@c0ffee42 (2 files): N-N: add implementation note and code review artifact

技术组长 · 智能体
✅ pre-push 检查 branch=feat/N-N-example-contract-tests files=0 untracked=0 blockers=0 warnings=0

技术组长 · 智能体
🚀 git push origin/feat/N-N-example-contract-tests@c0ffee42

技术组长 · 智能体
🔗 PR #42 opened: feat/N-N-example-contract-tests → main
https://github.com/example-org/example-repo/pull/42

技术组长 · 智能体
✅ CI watch PR #42 → success (17s)

技术组长 · 智能体
Sprint 状态里没有 N-N 的独立条目（它是某业务模块下的子任务，不在主 sprint 列表里）。没关系，N-N 交付完成。

---

## Story N-N 交付完成 ✅

- **实现**：由 `developer` 按 Story 完成主要代码与契约测试（话题内委派与工具明细已省略）
- **分支**：`feat/N-N-example-contract-tests`（基于 origin/main）
- **改动**：若干契约测试 + 示例类型的序列化辅助，共 4 个文件
- **审查**：由 `reviewer` 经 1 轮 review 通过（green，0 blockers）；若有 Blocker 则会再委派 `bug_fixer`（本节选未演示）
- **CI**：首轮 green
- **PR #42 可 merge**：`https://github.com/example-org/example-repo/pull/42`
- **Follow-ups**：日期格式可统一为 ISO-8601；某类 outbox 刷新需扩展业务相关 endpoint

---

接下来看 sprint `planned` 列表中的下一个任务。Sprint 里有 `define-sync-interfaces`、`implement-batch-apis` 等 planned 项。同时模块任务表里的 T0xx（示例：某 HTTP API 子任务）也尚未勾选完成。

你希望我继续推进 sprint 中的哪个方向？是继续该模块的下一子任务（如 T0xx），还是转向 sprint planned 里的基础设施类任务（如 `define-sync-interfaces`）？这两个方向差异较大，需要你指个方向。
```

---

## 特性

- **与飞书两种收事件方式对齐**：支持开放平台配置的 **HTTP 回调**（需公网可达地址）或 **长连接**；本地启动命令见「安装」一节。
- **多角色编排**：见 **「多角色全景」**；主角色 + 子角色分工，适合「对话 + 工程任务」而不仅是单轮问答。群内长什么样可参考 [飞书群内对话示例（脱敏节选）](#feishu-chat-example)。
- **进度与表格（可选）**：接上多维表格与项目适配器后，可做进度同步等重度能力。
- **密钥与代码分离**：真实凭证只在本地 `.env` 与 `.larkagent/secrets/`（已 `.gitignore`），仓库内是 `*.example` 模板。
- **轻度 / 重度两套路径**：从「能聊起来」到「全能力」分档写清飞书权限与环境变量，避免一上来全开 scope。
- **大模型接入（文档口径）**：公开文档只约定 **OpenAI 兼容** 一种写法（环境变量见「大模型」与 `.env.example`），兼容常见自建或云厂商网关。
- **记忆与上文（Harness）**：长对话自动 **压缩历史**；把项目笔记、**上次运行摘要**、会话摘要等 **分层记忆** 有序注入上下文；可选 **双路大模型**、**MCP**、会话血缘审计等。详见 [记忆与上文管理（Harness）](#harness-memory)。

---

<a id="harness-memory"></a>

## 记忆与上文管理（Harness）

把「该让模型看见什么、又不能无限膨胀」拆成两层能力：**Tier-1** 面向日常稳定运行，**Tier-2** 面向集成与排障。开关与默认值集中在 **`.env` / 环境变量**；字段定义见 `feishu_agent/config.py`，启动时装配在运行时服务内完成。

**Tier-1（默认即有意义）**

| 能力 | 作用 |
|------|------|
| **上下文压缩** | 对话接近模型上下文上限时，对历史做 **尾部保留式压缩**，降低网关报错与工具 JSON 被截断的概率；可用 `LLM_MAX_CONTEXT_TOKENS`、`LLM_COMPRESSION_*` 等调节或关闭。 |
| **记忆拼装** | 将 **项目笔记**、**上次运行摘要**（默认侧重最近一次非成功运行）、**会话摘要**、运行时基线等拼进系统提示；**定时提醒** 以单独一条用户侧消息注入，与系统提示分开。 |
| **上次运行记忆** | 默认开启；可按项目关闭（`LAST_RUN_MEMORY_ENABLED`）。摘要落在项目根 `.feishu_run_history.jsonl`（若含敏感信息请勿提交，可配 `.gitignore`）。 |
| **双路大模型（可选）** | 主线路异常或限流时，可配置次要网关做重试与回退（`LLM_SECONDARY_*`）。 |

**Tier-2（按需开启）**

| 能力 | 作用 |
|------|------|
| **MCP 工具** | 用 JSONL 声明外部 MCP Server；不配则不在启动时拉起相关子进程。 |
| **会话血缘审计** | 需要深度排障时再开，把会话派生关系落到审计目录（默认关，避免日志膨胀）。 |

开发与 CI 中通过 `feishu_agent/tests/test_harness_integration.py` 校验 Tier-1 的装配形状，减少「环境变量有、代码没接上」类回归。

---

## 架构（简图）

```
┌─────────────┐   HTTP / WS    ┌──────────────────┐   HTTPS      ┌─────────────┐
│ 飞书 IM     │ ◄────────────► │ feishu_opc       │ ───────────► │ 大模型 API  │
│（用户/群）  │                │（FastAPI 等）    │              │ 飞书 OpenAPI │
└─────────────┘                └────────┬─────────┘              └─────────────┘
                                         │
                         ┌───────────────┼───────────────┐
                         ▼               ▼               ▼
                  project-adapters   .larkagent/     skills/
                  （JSON 配置）      secrets（本地）  （角色说明）
```

**流程概要：** 飞书把消息/事件交给本服务 → 校验后交给对应角色 → 模型与工具链执行 → 把结果写回飞书消息或表格（若已启用）。

---

## 前置条件

| 依赖 | 建议 | 自检命令 |
|------|------|----------|
| Python | 3.11+ | `python3 --version` |
| `feishu_fastapi_sdk/` | 与 `feishu_agent/` **并列** 位于仓库根目录的源包（飞书 HTTP 客户端等），随主仓提供、**不再**从 Git 单独安装 | `test -d feishu_fastapi_sdk && echo ok` |
| 飞书企业自建应用 | 已创建并开通「机器人」能力 | 见下文「飞书应用配置」 |
| 大模型（OpenAI 兼容） | 任一兼容 `/v1/chat/completions` 的 HTTPS 端点 + API Key | 见下文「大模型」与 `.env` |

---

## 安装

```bash
git clone <你的 feishu_opc 仓库地址>.git
cd feishu_opc


python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 用编辑器打开 .env，至少完成「轻度使用」必填项（见下表）
```

**启动（二选一，与飞书侧「事件接收方式」一致）**

```bash
# 方式 A：HTTP 回调（需在飞书填写公网可访问的回调 URL，并反代到本机端口）
uvicorn feishu_agent.agent_main:app --host 0.0.0.0 --port 8000

# 方式 B：WebSocket 长连接（无需公网 IP，适合本机或内网直连）
python -m feishu_agent.feishu_ws_main
```

进程无报错、飞书侧能收到事件，即表示链路基本打通。具体 URL、端口以你的部署为准。

---

## 大模型：OpenAI 兼容接口（唯一说明形态）

为减少读者在「多种接入方式」之间迷路，本仓库文档 **只约定一种** 配置口径：在 `.env` 里把来源设为 **`openai_compatible`**，并指向你的 Chat Completions 兼容端点。

**推荐（最简单）**：在仓库根目录 `.env` 填写下列变量即可，**不必** 再维护 `model_sources.json`：

| 变量 | 含义 |
|------|------|
| `TECHBOT_LLM_SOURCE` | 固定为 `openai_compatible` |
| `TECHBOT_LLM_BASE_URL` | 完整 Chat Completions URL，例如 `https://api.openai.com/v1/chat/completions` 或自建网关等价地址 |
| `TECHBOT_LLM_API_KEY` | 该网关的 API Key |
| `TECHBOT_LLM_MODEL` | 模型名，由网关侧定义（如 `gpt-4o-mini`） |

**可选**：若你希望把 Key 放在 `.larkagent/secrets/ai_key/.env`、把 URL/模型写在 JSON，可复制 `model_sources.example.json` 为 `model_sources.json` 并按其中 **仅有** `openai_compatible` 一段填写；不要在该文件中添加其他 `model_source` 类型，以免与本文档不一致。

---

## 飞书应用配置（按顺序做即可）

1. 打开 [飞书开放平台](https://open.feishu.cn/) → **创建企业自建应用** → 填写名称与图标。
2. **添加应用能力** → 勾选 **机器人**。
3. **权限管理**：按下方「轻度 / 重度」表格开启 scope；保存。
4. **事件与回调** → **事件配置**：添加 `im.message.receive_v1`（接收消息）；接收方式选 **长连接** 或与你的 **HTTP 回调 URL** 一致。
5. **（可选）卡片交互**：若使用可点击卡片/菜单，需配置「卡片请求地址」或回调域名；不配置时多数对话能力仍可用，仅交互按钮可能无效（与飞书能力一致）。
6. **凭证与基础信息**：复制 **App ID**、**App Secret**；事件订阅页的 **Verification Token**、**Encrypt Key** 一并写入 `.env`（勿提交 git）。
7. **版本管理与发布** → 创建版本 → 管理员审核通过后，在目标群 **添加机器人** 再测。

权限 scope 在控制台搜索英文标识即可；中文名称以飞书界面为准。

---

## 飞书权限：轻度与重度

### 轻度（最小可用：收发自定义机器人消息）

| scope | 说明 |
|-------|------|
| `im:message` | 获取与发送单聊、群组消息 |
| `im:message:send_as_bot` | 以应用身份发消息 |
| `im:resource` | （建议开）获取消息中的图片等资源，便于多模态解析 |

### 重度（按需追加，勿全开）

| 能力 | 说明 |
|------|------|
| 多维表格 | 在权限管理中搜索 `bitable`，仅开业务用到的读/写项 |
| 真人代发 OAuth | `im:message.send_as_user`、`offline_access`；须配置重定向 URL 与运维脚本（见 `scripts/README.md`） |
| 卡片 / 通讯录 / 云文档 | 仅在实际调用的 OpenAPI 需要时再开，避免过度授权 |

---

## 环境变量说明

复制 `.env.example` 为 `.env` 后按表填写。**更细的注释在 `.env.example` 文件内。**

| 变量 | 轻度 | 默认值 / 说明 |
|------|------|----------------|
| `FEISHU_BOT_APP_ID` | 必填 | 飞书应用 App ID（`cli_` 开头） |
| `FEISHU_BOT_APP_SECRET` | 必填 | App Secret |
| `FEISHU_VERIFICATION_TOKEN` | 必填 | 事件订阅校验 Token |
| `FEISHU_ENCRYPT_KEY` | 必填 | 事件消息加密 Key |
| `TECHBOT_LLM_SOURCE` | 必填 | 固定填 `openai_compatible` |
| `TECHBOT_LLM_API_KEY` | 必填 | OpenAI 兼容网关的 API Key |
| `TECHBOT_LLM_BASE_URL` | 必填 | Chat Completions 完整 URL（含路径） |
| `TECHBOT_LLM_MODEL` | 必填 | 模型名（由网关约定） |
| `SECRET_KEY` | 生产必填 | 随机长字符串，用于会话等签名 |
| `DEBUG` | 可选 | `false` |
| `APP_REPO_ROOT` | 可选 | 不填则自动探测仓库根 |
| 分角色 bot、Bitable、委派、OAuth 等 | 仅重度 | 见 `.env.example` 第二节与 `.larkagent/secrets/**/*.example*` |

---

## 使用模式：轻度与重度（对照）

| 维度 | 轻度 | 重度 |
|------|------|------|
| 目标 | 单群对话、问答、轻量指令 | 多角色、表格同步、委派、CI/部署、代发与卡片等 |
| 飞书应用数 | 通常 1 个应用 + 机器人 | 多应用或多套凭证，见各 `*.example.env` |
| 配置位置 | 多数只需根目录 `.env` | `.env` + `.larkagent/secrets/` 多文件 |
| 运维 | 低：装好依赖与权限即可 | 高：域名证书、token 续期、脚本与监控 |

---

## 本仓库目录（子说明）

| 路径 | 说明 |
|------|------|
| `feishu_fastapi_sdk/` | 飞书 OpenAPI 封装（源包，与 `feishu_agent` 并列；`requirements.txt` 不再含其 Git 依赖） |
| [`feishu_agent/`](feishu_agent/README.md) | 核心服务与路由 |
| [`agents/`](agents/README.md) | 角色入口说明（Markdown） |
| [`skills/`](skills/README.md) | 角色技能与工作流说明 |
| [`project-adapters/`](project-adapters/README.md) | 下游项目 JSON 适配器 |
| [`deploy/`](deploy/README.md) | systemd / Nginx 等模板 |
| [`scripts/`](scripts/README.md) | OAuth 检查、cron、部署辅助 |
| [`spikes/`](spikes/README.md) | 一次性探测脚本 |
| [`.larkagent/`](.larkagent/README.md) | 部署脚本与密钥目录约定 |

---

## 部署与运维（概要）

- **轻度**：用 systemd、Docker 或进程守护工具包住上述 `uvicorn` / `python -m` 命令即可；模板见 `deploy/`。
- **重度**：建议配合 `scripts/` 中 OAuth 与 cron 脚本阅读说明后再上线。

---

## 推送到新远程仓库

```bash
cd feishu_opc
git remote add origin <你的空仓库 URL>
git push -u origin main
```

推送前请确认 `.env`、`.larkagent/secrets/` 未进入暂存区。

---

## 关于本仓库形态

本目录可按 **独立 Git 仓库** 使用：若你看到的提交历史很短或仅有初始提交，属于便于对外发布的 **快照式** 组织，便于推送到新的空远程而不混入其他项目的 commit。日常开发若发生在别的 monorepo，不影响你在此处阅读文档、安装依赖与部署运行。

---

## 许可证

本项目采用 [MIT License](./LICENSE) 发布。若个别文件头部包含 SPDX 标识，以各文件声明为准。
