"""execution_layer/core/protection_gate.py — M2: 风控三件套闸门 (账本派生状态版)

统筹定案 (2026-08-27, 参数全裁):
1. Cooldown = 5 交易日: 卖出同股后 5 交易日内禁买回 (对齐策略A复评窗)。
2. MaxDrawdown = pause -8% / resume -5%, **观察期累计口径** (锚定初始本金):
   净值回撤 <= -8% → 停开新仓; 净值回升到 -5% 以内才恢复开仓。
   -8%~-5% 迟滞带内保持暂停 (防边界抖动)。
3. Stoploss = 当日 3 次止损 → 当日停止新开仓 (日粒度, cron 跨进程安全)。

病灶 (05底稿项7): 旧 strategy_layer/risk/protections.py 三守卫全 src 零实例化,
且状态全在内存 (_sold/_paused/_count) — cron 模式每进程重启即清零, 形同虚设。
本闸门状态全部从既有账本派生 (action_records + pnl_tracker 净值曲线):
零新表零写库(除留痕), cron 进程间天然一致。

触发留痕 + 告警 (铁律#12 不可静默):
- 拦截 → skip_entry 留痕 (per-code)
- 回撤熔断首触/恢复 → volatility_alert 留痕 (code='PORTFOLIO') + 飞书 DM 告警
"""
from common.logging import get_logger
from common.utils import bj_date

logger = get_logger("execution.protection_gate")

# 回撤熔断留痕的 reason 前缀 — 措辞即协议, 不可随意改!
# 暂停/恢复状态由这两个前缀从 action_records 反向派生 (_drawdown_pause_active),
# 改动前缀文案 = 历史留痕全部失配 = 熔断状态机无声失效 (改告警文案前先改这里并迁移存量数据)。
_DD_PAUSE_PREFIX = "回撤熔断:"
_DD_RESUME_PREFIX = "回撤熔断解除:"


class ProtectionGate:
    """风控三件套闸门 — entry_engine.check_entry 前置调用。

    check(code, current_time) -> (can_trade: bool, reason: str)。
    任一守卫拦截即返回 (False, reason); 全过返回 (True, "")。
    守卫自身异常 honest 放行 (保护闸门故障不应堵死交易主链路, 只 warning)。
    """

    def __init__(self, db, stock_pool, config=None):
        self.db = db
        self.stock_pool = stock_pool
        self.config = config
        self._delivery = None
        self._delivery_ready = False

    # -- 参数 (config 可覆盖, 默认=统筹定案值) --------------------------------

    def _cfg(self, key: str, default):
        if self.config is None:
            return default
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    @property
    def cooldown_days(self) -> int:
        return int(self._cfg("strategy.risk.cooldown_days", 5))

    @property
    def drawdown_pause_at(self) -> float:
        limits = self._cfg("strategy.risk.drawdown_limits", {}) or {}
        return float(limits.get("pause_at", -0.08))

    @property
    def drawdown_resume_at(self) -> float:
        limits = self._cfg("strategy.risk.drawdown_limits", {}) or {}
        return float(limits.get("resume_at", -0.05))

    @property
    def stoploss_daily_max(self) -> int:
        return int(self._cfg("strategy.risk.stoploss_daily_max", 3))

    @property
    def initial_capital(self) -> float:
        return float(self._cfg("execution.initial_capital", 300000.0))

    # -- 主入口 ----------------------------------------------------------------

    def check(self, code: str, current_time: str) -> tuple[bool, str]:
        for guard in (self._check_cooldown, self._check_daily_stoploss,
                      self._check_drawdown):
            try:
                reason = guard(code)
            except Exception as e:  # noqa: BLE001 — 守卫故障放行+告警, 不堵主链路
                logger.warning(f"protection guard {guard.__name__} failed: {e}")
                continue
            if reason:
                self._record_block(code, reason)
                return False, reason
        return True, ""

    # -- 守卫①: 冷却期 (卖出同股后 N 交易日禁买回) ----------------------------

    def _check_cooldown(self, code: str) -> str:
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT MAX(created_at) as last_exit FROM action_records "
            "WHERE code=? AND action_type='exit'", (code,),
        ).fetchone()
        last_exit = row["last_exit"] if row else None
        if not last_exit:
            return ""
        trading_days = self._trading_days_between(str(last_exit)[:10], bj_date())
        if trading_days < self.cooldown_days:
            return (f"冷却期: {code} 于 {str(last_exit)[:10]} 卖出, "
                    f"未满 {self.cooldown_days} 交易日 (已过 {trading_days})")
        return ""

    @staticmethod
    def _trading_days_between(start: str, end: str) -> int:
        """start 之后到 end 的交易日数 (工作日口径, 节假日保守多算=保护方向安全)。"""
        from datetime import datetime, timedelta
        try:
            d0 = datetime.strptime(start, "%Y-%m-%d")
            d1 = datetime.strptime(end, "%Y-%m-%d")
        except (ValueError, TypeError):
            return 999  # 解析失败=不拦 (保守放行, 冷却期不应因日期脏数据误杀)
        n = 0
        d = d0 + timedelta(days=1)
        while d <= d1:
            if d.weekday() < 5:
                n += 1
            d += timedelta(days=1)
        return n

    # -- 守卫②: 当日连损 (当日 N 次止损 → 当日停开新仓) ----------------------

    def _check_daily_stoploss(self, code: str) -> str:
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT COUNT(*) as n FROM action_records "
            "WHERE action_type='exit' AND reason LIKE '%止损%' "
            "AND created_at LIKE ?", (f"{bj_date()}%",),
        ).fetchone()
        n = row["n"] if row else 0
        if n >= self.stoploss_daily_max:
            return (f"连损熔断: 当日已 {n} 次止损 (>= {self.stoploss_daily_max}), "
                    f"当日停止新开仓")
        return ""

    # -- 守卫③: 回撤熔断 (观察期累计, 锚定初始本金, -8%停/-5%恢复迟滞带) ------

    def _current_portfolio_nav(self) -> float | None:
        """刀一 (2026-10-09, P0 熔断假态根治): 全组合口径 nav
        = get_available_cash() + Σ(last_price×position_qty WHERE status IN 三态)。

        事故根因 (审计官逐位复算): 旧实现取曲线末组 nav — 曲线按 last_price_date
        分组且 cash 用当前全组合 — 两个不同时点的量拼一个点 (10-08: 6,848+3,404
        =10,252 → -96.58% 假态; 真值 6,848+279,315=286,163 → -4.61%)。
        与 R-5 真值同源 (包A status IN 三值 + get_available_cash 含包D clamp)。
        """
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT COALESCE(SUM(last_price*position_qty),0) AS pv FROM stock_pool "
                "WHERE status IN ('active','exit_pending','pending_entry') "
                "AND last_price > 0 AND position_qty > 0").fetchone()
            pv = float(row["pv"] or 0.0)
            cash = float(self.stock_pool.get_available_cash(self.initial_capital))
        except Exception:
            return None
        return cash + pv

    def _peak_nav(self) -> float | None:
        """刀二: peak_nav = 历史每日组合市值的 max (单调不降); 无历史 → None。

        数据源 = paper_positions_snapshot 按日聚合 Σmv (与曲线同源, 直查表
        不绕 PnLTracker — 守卫是判定面, 依赖越少越稳, 判例 #143 反面教材)。
        无历史 → None → _current_drawdown 返 None → 显式放行+留痕
        (绝不拿 initial_capital 当分母)。"""
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT COALESCE(MAX(daily_mv),0) AS pk FROM ("
                "  SELECT date, SUM(mv) AS daily_mv FROM paper_positions_snapshot "
                "  GROUP BY date)").fetchone()
            pk = float(row["pk"] or 0.0)
        except Exception:
            return None
        return pk if pk > 0 else None

    def _current_drawdown(self, cash: float | None = None) -> float | None:
        """组合当前回撤 = 当前全组合 nav / peak_nav - 1。

        无法判定 → None (不造假不触发):
        - 当前三态持仓与 cash 均不可得 → None
        - 曲线空且无历史 peak → None (分母绝不回落 initial_capital —
          10-07/10-08 假态事故的直接教训: 30 万分母 + 1 万分子 = 惊天假回撤)
        """
        nav = self._current_portfolio_nav()
        if nav is None:
            return None
        # 包B 置位延续 (2026-10-11): 末端锚 = 新数据源三态行 max(last_price_date)。
        # 旧实现随 curve 置位 _last_curve_end; 刀一弃 curve 后若不在新源重接,
        # _stale_nav_reason 恒空 → 生产链「陈旧放行」话术静默消失 (pack_b 回归,
        # 双树对照 2026-10-11 定谳)。NULL/无三态行 → None → 不报陈旧 (不造假)。
        try:
            _r = self.db.get_connection().execute(
                "SELECT MAX(last_price_date) AS e FROM stock_pool "
                "WHERE status IN ('active','exit_pending','pending_entry') "
                "AND last_price > 0 AND position_qty > 0").fetchone()
            self._last_curve_end = _r["e"] if _r else None
        except Exception:
            self._last_curve_end = None
        peak = self._peak_nav()
        if peak is None or peak <= 0:
            # 无历史 peak (snapshot 空, 10-08 后停写) → 首日为基:
            # peak=当前 nav → dd=0 → 不触发 (统筹批「首日 nav 为基」;
            # 且 _check_drawdown 侧仍落「无 NAV 依据」留痕, 放行可追溯)
            self._peak_unavailable = True
            return 0.0
        self._peak_unavailable = False
        peak = max(peak, nav)
        return nav / peak - 1

    def _check_drawdown(self, code: str) -> str:
        dd = self._current_drawdown()
        peak_unavailable = getattr(self, "_peak_unavailable", False)
        if dd is None or peak_unavailable:
            # 包E: active=0 && exit_pending>0 → 显式放行 (truthy reason = block);
            # 陈旧 → _stale_nav_reason; 其他 → 放行 (reason="" 走 paused 逻辑)。
            # 判例 #138/#153: 显式放行必须可见且落 action_records。
            reason = self._stale_nav_reason()
            if not reason:
                conn = self.db.get_connection()
                active_cnt = conn.execute(
                    "SELECT COUNT(*) c FROM stock_pool WHERE status='active'").fetchone()["c"]
                pending_cnt = conn.execute(
                    "SELECT COUNT(*) c FROM stock_pool WHERE status='exit_pending'").fetchone()["c"]
                if active_cnt == 0 and pending_cnt > 0:
                    reason = "无 活 跃 持 仓 显式放行 (判 例 包E)"
            if reason:
                try:
                    self.stock_pool.action_record.write(
                        code="PORTFOLIO", action_type="guard_stale",
                        reason=reason, source="execution")
                except Exception:
                    pass
            else:
                # 刀二事故复现语义: dd=None 或「无历史 peak(首日为基)」→
                # 守卫无法用历史分母判定 → 放行但必须留痕 (绝不拿 initial_capital)
                reason = ""
                tag = ("无 NAV 依据 (曲线空且无历史 peak)" if dd is None
                       else "无历史 peak (首日为基, dd=0)")
                try:
                    self.stock_pool.action_record.write(
                        code="PORTFOLIO", action_type="guard_stale",
                        reason=f"{tag} → 无法按历史分母判定, 显式放行 "
                               "(P0 熔断假态根治 2026-10-09)",
                        source="execution")
                except Exception:
                    pass
            return reason
        paused = self._drawdown_pause_active()
        if dd <= self.drawdown_pause_at:
            if not paused:
                _nav = self._current_portfolio_nav()
                _pv = (_nav - self.stock_pool.get_available_cash(self.initial_capital)
                       ) if _nav is not None else None
                self._record_drawdown_transition(
                    _DD_PAUSE_PREFIX,
                    f"组合回撤 {dd:.2%} <= {self.drawdown_pause_at:.0%}, 停止开新仓",
                    nav=_nav, dd=dd, pv=_pv, denom=gate_peak if (gate_peak := self._peak_nav()) else None)
            return f"回撤熔断: 组合回撤 {dd:.2%} (阈值 {self.drawdown_pause_at:.0%})"
        if paused:
            if dd >= self.drawdown_resume_at:
                self._record_drawdown_transition(
                    _DD_RESUME_PREFIX,
                    f"组合回撤回升至 {dd:.2%} (回到 {self.drawdown_resume_at:.0%} 以内), 恢复开仓")
                return ""
            return (f"回撤熔断迟滞带: 组合回撤 {dd:.2%} 未回升到 "
                    f"{self.drawdown_resume_at:.0%} 以内, 维持停开新仓")
        return ""

    def _drawdown_pause_active(self) -> bool:
        """派生暂停态: 最近一次熔断留痕是 pause 且其后无 resume。零新表。

        判 例 #148+判 例 #154: paused 熔断闩锁 带 时效 — pause 留痕 已 超 5 个 交易 日 (N=5
        对 齐 cooldown_days 量 级) → 闩锁过期 → paused=False + log.warning + resume
        留痕 (判 条 #137 silent 截 不 载 同 族)。

        判 例 #154 判 据 (「闩锁 解除条件不得 依赖被闩锁 的 对象」): paused 解除
        条件 = 「NAV 回升」,而 NAV = f(守 卫 判定),守 卫 被 paused 后不再判定 →
        行集合冻结 → NAV 冻结 → 永 不 回升 → 永 不 解除。「熔断 等 着 它 自己造成 的 结果」
        = 判 例 家 族 第 22 条。
        """
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT reason, created_at FROM action_records "
            "WHERE code='PORTFOLIO' AND action_type='volatility_alert' "
            "AND (reason LIKE ? OR reason LIKE ?) "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (f"{_DD_PAUSE_PREFIX}%", f"{_DD_RESUME_PREFIX}%"),
        ).fetchone()
        if not row:
            return False
        if str(row["reason"]).startswith(_DD_PAUSE_PREFIX):
            # 判 例 #148+判 例 #154: 闩锁 带 时效 — pause 留 痕 已 超 5 个 交易 日 (N=5) → 闩 锁 过 期
            # → paused=False + log.warning + resume 留痕
            created_at = str(row["created_at"] or "")
            days_lapse = self._days_lapse(created_at)
            if days_lapse > 5:
                logger.warning(
                    f"[判 例 #154] pause 留痕 已 超 5 个 交易 日 "
                    f"(created_at={created_at}, days_lapse={days_lapse}) → "
                    f"闩锁过期 → paused=False + resume 留痕")
                try:
                    self._record_drawdown_transition(
                        _DD_RESUME_PREFIX,
                        "闩锁过期:pause 留痕 已 超 5 交易 日,自 动 解锁 (+落 resume 留痕)")
                except Exception:  # fail-safe 不 吞主链
                    pass
                return False
            return True
        return False

    def _curve_stale_days(self) -> int | None:
        """曲线末端日期距今天数（NAV 曲线末端陈旧天数）。"""
        end = getattr(self, "_last_curve_end", None)
        if not end:
            return None
        from datetime import datetime
        from common.utils import bj_date
        try:
            return (datetime.strptime(bj_date(), "%Y-%m-%d")
                    - datetime.strptime(end, "%Y-%m-%d")).days
        except (ValueError, TypeError):
            return None

    def _stale_nav_reason(self) -> str:
        """包B (Day 4): 守卫读数陈旧 → 显式 reason，不得返回空串 (判例 #138/#153)。
        判据：curve 末端 距 bj_date() > 5 自然日 （对齐包C 闩锁时效量 级）→ 显式 放行。
        否则 返回空串。"""
        days = self._curve_stale_days()
        if days is None:
            return ""
        if days > 5:
            return f"守卫读数陈旧 (NAV末端已{days}天, 阈值5) → 显式放行 (判例#148/#153)"
        return ""

    def _days_lapse(self, created_at: str) -> int:
        """计 算 留痕 距 今 多 少 天 (自然 日 期) — 判 条 #148 锚 5 天 限期"""
        from datetime import datetime
        from common.utils import bj_date
        try:
            d = datetime.strptime(str(created_at)[:10], "%Y-%m-%d")
            t = datetime.strptime(bj_date(), "%Y-%m-%d")
            return max((t - d).days, 0)
        except (ValueError, TypeError):
            return 0

    def _record_drawdown_transition(self, prefix: str, summary: str,
                                    nav: float | None = None,
                                    dd: float | None = None,
                                    pv: float | None = None,
                                    denom: float | None = None):
        """丙 (P0 根治): 熔断状态迁移留痕 + 数值四件 (nav/dd/pv/分母)。

        10-07/10-08 实测 132+198 条留痕 action_detail/price/previous_value/
        new_value 全 NULL — 熔断判定不留数值证据, 事后无法复算当时 NAV。
        修后: price=nav, previous_value=pv, new_value=denom(peak),
        action_detail=可复算摘要 (判例 #138: 留痕必须能重放判定)。"""
        detail = None
        if nav is not None and dd is not None and denom:
            detail = (f"nav={nav:.2f}, pv={pv if pv is not None else 'n/a'}, "
                      f"denom(peak)={denom:.2f}, dd={dd:.4f}")
        try:
            self.stock_pool.action_record.write(
                code="PORTFOLIO", action_type="volatility_alert",
                action_detail=detail,
                price=nav, previous_value=pv, new_value=denom,
                reason=prefix + summary, source="execution")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"drawdown transition record failed: {e}")
        self._notify("⚠️ 风控熔断" if prefix == _DD_PAUSE_PREFIX else "✅ 风控恢复",
                     summary)

    # -- 留痕与告警 -------------------------------------------------------------

    def _record_block(self, code: str, reason: str):
        try:
            self.stock_pool.action_record.write(
                code=code, action_type="skip_entry",
                reason=f"风控闸门拦截: {reason}", source="execution")
        except Exception as e:  # noqa: BLE001 — 留痕失败不阻断拦截本身
            logger.warning(f"protection block record failed: {e}")

    def _notify(self, title: str, body: str):
        """飞书 DM 告警 (paper_broker 同款: dry_run 只打日志, 初始化失败静默降级)。"""
        try:
            if self.config is not None and self._cfg("execution.dry_run", True):
                logger.info(f"[DRY RUN] 风控通知: {title} {body}")
                return
            if not self._delivery_ready:
                from config_layer import get_config
                from delivery_layer import DeliveryLayer, Message
                cfg = get_config()
                if not cfg._loaded:
                    cfg.load()
                self._delivery = DeliveryLayer(config=cfg)
                self._delivery.registry.mark_healthy("feishu_dm")
                self._delivery_ready = True
            from delivery_layer import Message
            content = {
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": title},
                               "template": "red"},
                    "elements": [{"tag": "div",
                                  "text": {"tag": "lark_md", "content": body}}],
                },
            }
            self._delivery.send(Message(content=content,
                                        content_type="execution_alert", title=title))
        except Exception as e:  # noqa: BLE001 — 告警失败不阻断守卫判定
            logger.warning(f"protection notify failed: {e}")
