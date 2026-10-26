#!/usr/bin/env bash
# scripts/chronic.sh — P0-3③: chronic 降噪包装器 (moreutils chronic 等价自研)
#
# 用法 (crontab 中, 包在最外层):
#   ... scripts/chronic.sh scripts/cron_watchdog.sh <key> <cmd...> >> logs/x.log 2>&1
#
# 语义: 缓冲命令全部输出 (stdout+stderr), 退出码非 0 才吐出去;
#       成功则静默 — cron 日志只在失败时才有内容, pipeline.log 降一个量级。
# 可观测性不受损: 成功路径的留痕本就走结构化通道 (pipeline_runs 表 /
#       watchdog.jsonl / 五回执 jsonl), stdout 噪音只是冗余 (铁律#12 不违反)。
# 红线: 退出码原样透传 (含 124/137 超时语义), 不得吞码。
set -u

if [ "$#" -eq 0 ]; then
    echo "usage: chronic.sh <cmd...>" >&2
    exit 2
fi

TMP="$(mktemp)" || exit 2
trap 'rm -f "$TMP"' EXIT

"$@" > "$TMP" 2>&1
RC=$?

if [ "$RC" -ne 0 ]; then
    cat "$TMP"
fi
exit "$RC"
