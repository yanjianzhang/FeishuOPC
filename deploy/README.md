# `deploy/` — 部署模板

本目录存放 **systemd、Nginx** 等 **模板文件**，供你在自有服务器上渲染后使用；不包含任何密钥。

## 内容说明

| 文件或子路径 | 用途 |
|--------------|------|
| `systemd/*.tmpl` | systemd 单元模板（占位符需替换为实际路径、用户、端口） |
| `nginx/oauth.example.com.conf` | OAuth 回调微服务前的 Nginx 示例（域名请改为自己的，并与 `scripts/setup_oauth_callback.sh` 中变量一致） |

## 轻度 vs 重度

- **轻度**：可不使用本目录；用进程管理器或 `uvicorn` 前台运行即可。  
- **重度**：建议用 systemd + 反向代理 + HTTPS；若启用 **真人代发 OAuth**，需按 `scripts/setup_oauth_callback.sh` 与根目录 `README.md` 部署回调域名与证书。

具体命令与顺序以 `scripts/README.md`、`.larkagent/README.md` 为准。
