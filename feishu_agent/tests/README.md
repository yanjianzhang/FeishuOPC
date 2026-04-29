# `feishu_agent/tests/` — 单元测试

使用 **pytest** 运行。在仓库根目录执行：

```bash
pip install -r requirements.txt
pytest feishu_agent/tests -q
```

与飞书真实网络无关的测试占多数；涉及 HTTP 的用例一般已 mock。  
**轻度 / 重度**：本地开发或 CI 均可运行；生产服务器上通常不必安装测试依赖。
