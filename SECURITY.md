# 安全政策

## 支持的版本
| 版本 | 支持 |
|---|---|
| v0.20.0-beta.x | ✅ |
| < v0.20.0 | ❌（仅安全修复酌情回移） |

## 报告漏洞
**请勿通过公开 issue 报告安全漏洞。** 使用 GitHub 私有漏洞报告（Security → Advisories → Report a vulnerability），
或邮件联系仓库所有者。48 小时内确认，修复窗口最长 30 天。

## 已知边界（诚实声明）
- 本系统为**参考实现**，未经过第三方安全审计；
- 面向个人模拟盘/试运行场景设计，未针对多用户/公网暴露加固；
- `web.api` 仅假设绑定内网；生产部署请自行置于防火墙之后；
- 未设置 `QS_API_TOKEN` 时 `web.api` 只放行本机直连。裸 nginx `proxy_pass` 默认不加 `X-Forwarded-For`，外网请求会看起来像本机直连，因此**只要经任何反向代理/隧道暴露，就必须设置 `QS_API_TOKEN`**（至少 16 位，过短会直接拒绝服务）；
- HITL 审批默认 fail-closed：未配置 `HITL_APPROVER_OPEN_IDS` 时审批一律拒绝；
- 数据源与 LLM 的 API key 通过 `.env` 注入，**任何情况下不要提交**（CI 有 gitleaks 扫描兜底）。
