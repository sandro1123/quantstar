#!/bin/bash
set -euo pipefail

QS_DIR="${QS_HOME:-$HOME/qs}"
DEPLOY_DIR="$QS_DIR/deploy"

echo "=== QuantStar 部署安装 ==="

# 1. 创建日志目录
echo "创建日志目录..."
mkdir -p "$QS_DIR/logs"
echo "  ✅ $QS_DIR/logs"

# 1.5. 安装 Python 依赖
echo "安装 Python 依赖..."
cd "$QS_DIR" && /usr/bin/python3 -m pip install -e ".[dev]" --quiet
echo "  ✅ 依赖安装完成"

# 2. 安装 crontab (幂等: 用 start/end 标记整块替换, 重复执行不会重复)
echo "安装 crontab 条目..."
TMP_CRON="$(mktemp)"
crontab -l 2>/dev/null | sed '/^# QuantStar start$/,/^# QuantStar end$/d' > "$TMP_CRON" || true
cat "$DEPLOY_DIR/quantstar-crontab" >> "$TMP_CRON"
crontab "$TMP_CRON"
rm -f "$TMP_CRON"
echo "  ✅ crontab 已安装 (幂等: # QuantStar start/end 块替换)"

# 3. 安装 systemd service
echo "安装 systemd 服务..."
sudo cp "$DEPLOY_DIR/quantstar.service" /etc/systemd/system/quantstar.service
sudo systemctl daemon-reload
sudo systemctl enable quantstar.service
sudo systemctl start quantstar.service
echo "  ✅ systemd 服务已启动"

echo ""
echo "=== 安装完成 ==="
echo ""
echo "常用命令:"
echo "  systemctl status quantstar   # 查看服务状态"
echo "  systemctl restart quantstar  # 重启服务"
echo "  journalctl -u quantstar -f   # 查看日志"
echo "  crontab -l                   # 查看定时任务"
echo ""
echo "手动运行:"
echo "  cd $QS_DIR && python3 -m src run"
echo "  cd $QS_DIR && python3 -m src health"
