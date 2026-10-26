# QuantStar 部署指南

## 快速部署

```bash
cd /path/to/qs
bash deploy/install.sh
```

## 手动部署

### 1. systemd 服务

```bash
sudo cp deploy/quantstar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quantstar
sudo systemctl start quantstar
```

### 2. crontab 定时任务

> **注意**：`deploy/quantstar-crontab` 是 2026-08-23 线上 `crontab -l` 的快照（GO-7 后，36 非注释行，17 行带 flock_guard 闸）。
> 权威源是运行时（`crontab -l`），本文件仅供重建。修改排程后必须重新落库（铁律 #20）。
>
> `quantstar-crontab.apply.pre-go7` / `quantstar-crontab.proposed.pre-go7` 为 GO-7 前草稿，**已作废，勿使用**，仅留作排程演化记录。


```bash
crontab -l | grep -v "# QuantStar"; cat deploy/quantstar-crontab | crontab -
```

### 3. 常用命令

| 命令 | 说明 |
|------|------|
| `systemctl status quantstar` | 查看服务状态 |
| `systemctl restart quantstar` | 重启服务 |
| `journalctl -u quantstar -f` | 查看实时日志 |
| `python3 -m src run` | 手动运行流水线 |
| `python3 -m src run --mode strategy` | 只运行策略 |
| `python3 -m src run --date 2024-01-15` | 指定日期运行 |
| `python3 -m src backtest --code 000001` | 回测指定股票 |
| `python3 -m src health` | 健康检查 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CONFIG_ENV` | `dev` | 配置环境 (dev/prod) |
| `PYTHONPATH` | — | 需设为 `src` 目录 |

## 日志

- 应用日志: `logs/` 目录
- Cron 日志: `logs/cron.log`
- 健康检查日志: `logs/health.log`
