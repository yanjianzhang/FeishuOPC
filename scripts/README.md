# `scripts/` — 运维与排障脚本

Shell / Python 辅助脚本，用于 **OAuth 续期检查、cron 安装、测试卡片发送** 等；不属于 `feishu_agent` 包导入路径。

## 常见脚本

| 脚本 | 说明 |
|------|------|
| `check_impersonation_token.py` | 检查真人代发 token 有效期，可向群内发告警 |
| `setup_impersonation_cron.sh` | 在服务器上安装定时检查、推送 token |
| `setup_oauth_callback.sh` | 部署 OAuth 回调微服务（配合 `deploy/nginx`） |
| `cleanup-stale-worktrees.sh` | 清理代码写入角色遗留的 worktree（重度场景） |
| `dev_send_test_card.py` | 开发时发送测试卡片 |

## 轻度 vs 重度

- **轻度**：一般 **不需要** 运行本目录脚本；除非你要调试卡片或健康检查。  
- **重度**：启用 **真人代发** 时，务必阅读并按需运行 `check_impersonation_token` / `setup_impersonation_cron` / `setup_oauth_callback`，避免 refresh_token 过期导致静默降级。
