"""异常检测器 — 管道异常 + 健康检查异常 + 统计基线检测

F6.3: 新增 DB 持久化检测路径 (record_health / check_health_red_db /
scan_recent_runs)。cron 每次运行都是新进程, 内存计数器 (_consecutive_*)
跨进程必然清零、永远到不了阈值, 因此生产可观测必须走 DB。
内存版 check_* 保留给长驻进程 (API server) 使用。
"""

import json
import math
import re
from datetime import datetime

from common.database import Database
from common.logging import get_logger

logger = get_logger("ops.anomaly")

_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

# 连续失败/连续红色的告警阈值
CONSECUTIVE_THRESHOLD = 3

# B3 (2026-10-21): 每管道取样窗口。
# 原实现用全局 LIMIT 20 —— 生产单日 130+ 条 pipeline_runs,
# 日频管道在窗口内最多出现 1 次, 连续失败 3 次永远凑不满 => 全体日频失明。
# 改为「按管道各取最近 PER_PIPELINE_WINDOW 条」, N 取 10 足以覆盖 3 次连击
# 且给 partial/success 打断留足余量。
PER_PIPELINE_WINDOW = 10


class AnomalyDetector:
    def __init__(self, db: Database):
        self.db = db
        self._consecutive_failures: dict[str, int] = {}
        self._consecutive_red: dict[str, int] = {}
        self._baselines: dict[str, dict] = {}

    def check_pipeline_duration(self, pipeline_name: str, duration_s: float) -> dict | None:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT duration_s FROM pipeline_runs WHERE pipeline_name=? AND status='success' AND duration_s IS NOT NULL ORDER BY start_time DESC LIMIT 10",
            (pipeline_name,)
        ).fetchall()
        durations = [r["duration_s"] for r in rows if r["duration_s"]]
        if not durations:
            if duration_s > 300:
                return {"anomaly_type": "pipeline_duration", "pipeline": pipeline_name,
                        "detail": f"耗时 {duration_s:.1f}s, 超过默认阈值 300s", "severity": "warning"}
            return None
        avg = sum(durations) / len(durations)
        if duration_s > avg * 2:
            return {"anomaly_type": "pipeline_duration", "pipeline": pipeline_name,
                    "detail": f"耗时 {duration_s:.1f}s, 超过平均 {avg:.1f}s 的2倍", "severity": "warning"}
        return None

    def check_pipeline_failures(self, pipeline_name: str, status: str) -> dict | None:
        if status == "failed":
            self._consecutive_failures[pipeline_name] = self._consecutive_failures.get(pipeline_name, 0) + 1
        else:
            self._consecutive_failures[pipeline_name] = 0
        if self._consecutive_failures.get(pipeline_name, 0) >= 3:
            cnt = self._consecutive_failures[pipeline_name]
            return {"anomaly_type": "consecutive_failures", "pipeline": pipeline_name,
                    "detail": f"连续失败 {cnt} 次", "severity": "critical"}
        return None

    def check_health_red(self, layer: str, status: str) -> dict | None:
        if status == "red":
            self._consecutive_red[layer] = self._consecutive_red.get(layer, 0) + 1
        else:
            self._consecutive_red[layer] = 0
        if self._consecutive_red.get(layer, 0) >= 3:
            cnt = self._consecutive_red[layer]
            return {"anomaly_type": "health_red", "layer": layer,
                    "detail": f"健康检查连续 {cnt} 次为红色", "severity": "critical"}
        return None

    # ---- F6.3: DB 持久化检测 (跨进程安全, 生产可观测主路径) ----

    def record_health(self, overall_status: str, layer_statuses: dict[str, str] | None = None) -> None:
        """把一次健康检查结果写入 health_history, 供跨进程连续判定使用。"""
        try:
            conn = self.db.get_connection()
            conn.execute(
                "INSERT INTO health_history (check_time, overall_status, layer_statuses) "
                "VALUES (?, ?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), overall_status,
                 json.dumps(layer_statuses or {}, ensure_ascii=False)),
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"record_health 写入失败: {e}")

    def check_health_red_db(self, threshold: int = CONSECUTIVE_THRESHOLD) -> dict | None:
        """基于 health_history 判定"连续 N 次整体红色"。跨进程安全。

        返回 anomaly dict 或 None。只在最近 threshold 次全部为 red 时触发。
        """
        try:
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT overall_status FROM health_history "
                "ORDER BY check_time DESC, id DESC LIMIT ?",
                (threshold,),
            ).fetchall()
        except Exception as e:
            logger.warning(f"check_health_red_db 查询失败: {e}")
            return None
        if len(rows) < threshold:
            return None
        if all(r["overall_status"] == "red" for r in rows):
            return {"anomaly_type": "health_red", "layer": "overall",
                    "detail": f"健康检查连续 {threshold} 次整体为红色",
                    "severity": "critical"}
        return None

    def scan_recent_runs(self, window: int = PER_PIPELINE_WINDOW) -> list[dict]:
        """扫描各管道最近 window 条 pipeline_runs, 返回检测到的异常列表。

        B3 (2026-10-21) 修: 原实现是**全局** ``ORDER BY start_time DESC LIMIT 20``,
        生产单日就有 130+ 条记录 => 日频管道在窗口内最多出现 1 次 =>
        连续失败 3 次永远不可达 => 全体日频管道的 consecutive_failures 恒失明
        (实证: #81 的 detail 冻结在 2026-09-01「连续失败3次」, 实际已连败 22 次)。

        现改为**按管道分别取最近 window 条**再算 streak。

        覆盖: consecutive_failures —— 某管道最近连续失败 >= CONSECUTIVE_THRESHOLD。

        V1 例外注记: health_check 是 ``0 10,14 * * 1-5`` + ``6 16 * * 1-5``
        一日三检, 是极少数能在「单日」内形成连击的管道; 按管道取样后它同样
        能被正确检出 (见 test_multi_check_pipeline_streak_still_detected)。
        """
        anomalies: list[dict] = []
        try:
            conn = self.db.get_connection()
            # B3: 按管道分别取最近 window 条 (窗口函数, 单次查询不 N+1)
            rows = conn.execute(
                "SELECT pipeline_name, status, duration_s FROM ("
                "  SELECT pipeline_name, status, duration_s,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY pipeline_name"
                "           ORDER BY start_time DESC, rowid DESC) AS rn"
                "  FROM pipeline_runs"
                ") WHERE rn <= ? ORDER BY pipeline_name, rn",
                (window,),
            ).fetchall()
        except Exception as e:
            logger.warning(f"scan_recent_runs 查询失败: {e}")
            return anomalies
        if not rows:
            return anomalies

        # 按管道聚合最近状态序列 (每管道内已按时间倒序)
        by_pipe: dict[str, list] = {}
        for r in rows:
            by_pipe.setdefault(r["pipeline_name"], []).append(r)

        for name, seq in by_pipe.items():
            # 连续失败计数 (从最新一条起)
            streak = 0
            for r in seq:
                if r["status"] == "failed":
                    streak += 1
                else:
                    break
            if streak >= CONSECUTIVE_THRESHOLD:
                anomalies.append({
                    "anomaly_type": "consecutive_failures", "pipeline": name,
                    "detail": f"最近连续失败 {streak} 次", "severity": "critical",
                })
        return anomalies

    def check_data_lag(self, pipeline_name: str, update_time: str, max_lag_minutes: int = 30) -> dict | None:
        try:
            update_dt = datetime.strptime(update_time, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            lag_min = (now - update_dt).total_seconds() / 60
            if lag_min > max_lag_minutes:
                return {"anomaly_type": "data_lag", "pipeline": pipeline_name,
                        "detail": f"数据延迟 {lag_min:.0f} 分钟, 超过阈值 {max_lag_minutes} 分钟",
                        "severity": "critical" if lag_min > max_lag_minutes * 2 else "warning"}
        except Exception:
            pass
        return None

    def build_baseline(self, metric_name: str, values: list[float]):
        if not values:
            return
        mean = sum(values) / len(values)
        std = math.sqrt(sum((x - mean) ** 2 for x in values) / len(values)) or 1.0
        self._baselines[metric_name] = {"mean": mean, "std": std, "count": len(values)}
        logger.info(f"Baseline built for {metric_name}: mean={mean:.2f}, std={std:.2f}")

    def check_baseline(self, metric_name: str, value: float, z_threshold: float = 3.0) -> dict | None:
        baseline = self._baselines.get(metric_name)
        if not baseline:
            return None
        z = (value - baseline["mean"]) / baseline["std"]
        if abs(z) > z_threshold:
            direction = "偏高" if z > 0 else "偏低"
            return {"anomaly_type": "baseline_deviation", "metric": metric_name,
                    "detail": f"指标 {metric_name} 当前值 {value:.2f}, Z-score={z:.2f} ({direction})",
                    "severity": "warning", "z_score": round(z, 2)}
        return None

    def check_data_volume_anomaly(self, table_name: str, current_count: int) -> dict | None:
        if not table_name or not _IDENTIFIER_RE.match(table_name):
            return None
        self._baselines.get(f"volume_{table_name}")
        conn = self.db.get_connection()
        try:
            rows = conn.execute(
                f"SELECT DATE(created_at) as day, COUNT(*) as cnt FROM {table_name} "
                "WHERE created_at >= datetime('now', '-7 days', '+8 hours') "
                "GROUP BY day ORDER BY day"
            ).fetchall()
        except Exception:
            rows = []
        if not rows:
            return None
        daily_counts = [r["cnt"] for r in rows]
        if len(daily_counts) < 3:
            return None
        mean = sum(daily_counts) / len(daily_counts)
        std = math.sqrt(sum((x - mean) ** 2 for x in daily_counts) / len(daily_counts)) or 1.0
        z = (current_count - mean) / std
        if abs(z) > 3:
            return {"anomaly_type": "data_volume", "table": table_name,
                    "detail": f"表 {table_name} 数据量异常: 当前 {current_count}, 均值 {mean:.0f}, Z={z:.2f}",
                    "severity": "warning"}
        return None

    def check_data_source_staleness(self, source_name: str, last_update: str | None, max_hours: int = 24) -> dict | None:
        if not last_update:
            return {"anomaly_type": "source_stale", "source": source_name,
                    "detail": f"数据源 {source_name} 从未更新", "severity": "warning"}
        try:
            last = datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            hours = (now - last).total_seconds() / 3600
            if hours > max_hours:
                return {"anomaly_type": "source_stale", "source": source_name,
                        "detail": f"数据源 {source_name} 已 {hours:.0f} 小时未更新",
                        "severity": "critical" if hours > max_hours * 2 else "warning"}
        except Exception:
            pass
        return None

    # ---- #80: duration 族基线维护 + 比对 (metric_baselines 通电) ----

    MIN_BASELINE_SAMPLES = 10  # <10 样本不建基线 (宁可 NULL 不可假值)
    Z_THRESHOLD = 3.0

    def refresh_duration_baselines(self, pipelines: list[str],
                                    window_days: int = 20,
                                    enabled: bool | None = None) -> dict:
        """#80: 14 作业白名单 × pipeline_runs 历史窗 duration → upsert 基线。

        - 门控: enabled=None 时读 ops.anomaly_detection.enabled (该键此前全库
          零读者 — 有名无实, 本方法即其首个读点; 默认 True 兜底)
        - 样本 < MIN_BASELINE_SAMPLES 跳过 (首夜多管道不足 10 天属预期)
        - ON CONFLICT(metric_name) DO UPDATE: 只更新 value/std/count/kind/
          updated_at — **不碰 needs_reset (人工停比位) 与 created_at (首建时间)**
        - 返回 {upserted, skipped, window_days} 供维护段上报
        """
        stats = {"upserted": 0, "skipped": 0, "window_days": window_days}
        if enabled is None:
            try:
                from config_layer import cfg_get
                enabled = bool(cfg_get("ops.anomaly_detection.enabled", True))
            except Exception:
                enabled = True
        if not enabled:
            stats["skipped"] = len(pipelines)
            return stats
        try:
            from ops_layer.core.tables import _ensure_metric_baselines_columns
            conn = self.db.get_connection()
            _ensure_metric_baselines_columns(conn)
            for name in pipelines:
                rows = conn.execute(
                    "SELECT duration_s FROM pipeline_runs "
                    "WHERE pipeline_name=? AND status='success' AND duration_s IS NOT NULL "
                    "AND date >= date('now', ?, '+8 hours') "
                    "ORDER BY start_time DESC",
                    (name, f"-{window_days} day"),
                ).fetchall()
                values = [r["duration_s"] for r in rows if r["duration_s"]]
                if len(values) < self.MIN_BASELINE_SAMPLES:
                    stats["skipped"] += 1
                    continue
                mean = sum(values) / len(values)
                std = math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))
                conn.execute(
                    """INSERT INTO metric_baselines
                           (metric_name, baseline_value, baseline_std,
                            sample_count, metric_kind, updated_at)
                       VALUES (?, ?, ?, ?, 'pipeline_duration',
                               datetime('now', 'localtime'))
                       ON CONFLICT(metric_name) DO UPDATE SET
                           baseline_value=excluded.baseline_value,
                           baseline_std=excluded.baseline_std,
                           sample_count=excluded.sample_count,
                           metric_kind=excluded.metric_kind,
                           updated_at=excluded.updated_at""",
                    (name, round(mean, 3), round(std, 3), len(values)),
                )
                stats["upserted"] += 1
            conn.commit()
        except Exception as e:
            logger.warning(f"refresh_duration_baselines 失败 (fail-safe): {e}")
        return stats

    def check_duration_baseline(self, pipeline_name: str, duration_s: float) -> dict | None:
        """#80: 最近一次 duration 对基线做 z 检验 — |z|>3 → baseline_deviation。

        保守面: 基线不存在 / std=0 (全同值无法区分) / needs_reset=1 (人工停比)
        一律返回 None 不告警 — 宁可静默不可误报。
        """
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT baseline_value, baseline_std, needs_reset FROM metric_baselines "
                "WHERE metric_name=? AND metric_kind='pipeline_duration'",
                (pipeline_name,),
            ).fetchone()
            if not row or not row["baseline_std"] or row["needs_reset"]:
                return None
            z = (duration_s - row["baseline_value"]) / row["baseline_std"]
            # G4 (2026-10-23): 耗时判据改为**单侧** —— 只报「变慢」。
            #
            # 原实现 abs(z) > 3 双向判，把「变得更快」也报成异常。实证 #123:
            #   "morning_brief 耗时 16.0s, 基线 56.9s±11.1, Z=-3.69 (偏低)"
            # 而 16s 正是把「60s 超时截断」修好后的正常结果。
            # 真病: 基线取自病态期，好转被判成异常。
            #
            # 耗时方向性明确: 变慢 = 可能有问题; 变快 = 几乎总是好事。
            # (快得离谱但产出为空/不全 —— 由「输出是否为空」类判据管, 不归耗时管。)
            if z > self.Z_THRESHOLD:
                return {"anomaly_type": "baseline_deviation", "pipeline": pipeline_name,
                        "detail": (f"{pipeline_name} 耗时 {duration_s:.1f}s, "
                                   f"基线 {row['baseline_value']:.1f}s±{row['baseline_std']:.1f}, "
                                   f"Z={z:.2f} (偏高/变慢)"),
                        "severity": "warning", "z_score": round(z, 2)}
        except Exception as e:
            logger.warning(f"check_duration_baseline 失败 (fail-safe): {e}")
        return None
