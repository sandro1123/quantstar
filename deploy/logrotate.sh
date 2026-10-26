#!/bin/bash
# QuantStar 日志轮转
# 用途: 保留最近 30 天的日志, 压缩旧日志
# 用法: deploy/logrotate.sh

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOGS_DIR="$BASE_DIR/logs"
RETENTION_DAYS=30

if [ ! -d "$LOGS_DIR" ]; then
    echo "No logs directory"
    exit 0
fi

find "$LOGS_DIR" -name "*.log" -mtime +$RETENTION_DAYS -exec rm -f {} \;
find "$LOGS_DIR" -name "*.log.*" -mtime +$RETENTION_DAYS -exec rm -f {} \;

echo "Log rotation completed: $(date)"