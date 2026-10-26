"""计划管理器 — 接收 Strategy 计划 + 动态调整 + Human-in-loop"""


from common.database import Database
from common.logging import get_logger
from common.utils import bj_date
from execution_layer.core.human_in_loop import HumanInLoop
from execution_layer.core.enrich_reason import enrich_reason

logger = get_logger("execution.plan")

# rr 阈值比较容差 (2026-09-25 生产实证)。
# 上游策略按 rr≈1.5 生成入场参数, 阈值 min_rr 默认也是 1.5 —— **阈值正压在
# 生成器设计点上**, 于是 IEEE754 末位噪声决定生死: 积压 35 单全精度实算,
# 10 单落在 [1.4999, 1.5001], 其中 3 单 (601916/002091/300832) 数学上正好
# 等于 1.5 却算出 1.49999999999999xx 被 `rr < min_rr` 误杀 (日志原文荒谬:
# 「风险收益比 1.50 < 阈值 1.5」)。对照 stock_pool 已入池单 min=1.5000
# max=1.5003 亦 2/3 紧贴阈值, 证明这是生成器常态不是积压特例。
_RR_EPS = 1e-9


def entry_valid_until(db, _today: str | None = None,
                      entry_valid_days: int | None = None) -> str:
    """S5 通电: 入场区间有效期截止日 = 今日 + execution.entry_valid_days 个交易日
    (默认 5, 对齐策略A 5~10 交易日复评窗)。日历经 TradingCalendar (common 层)。

    S5 修复 (08-31): 原实现依赖 calendar 前瞻, 而 db 日历集合只有历史
    (daily_klines DISTINCT date), 未来日期被独断 False → next_trading_day
    30 天循环穷尽后返回原日期 → valid_until=挂入当日=零有效窗 (生产 08-28
    深夜挂入 4 笔实证, 002508 46 轮带内触价被拦)。calendar 已修前瞻, 此处
    语义 (挂入日+N 交易日) 不变。_today/entry_valid_days 仅测试注入用。
    """
    from common.calendar import TradingCalendar
    from common.utils import bj_date
    from config_layer import cfg_get
    days = entry_valid_days if entry_valid_days is not None         else int(cfg_get("execution.entry_valid_days", 5))
    cal = TradingCalendar(db=db)
    d = _today if _today is not None else bj_date()
    for _ in range(days):
        d = cal.next_trading_day(d)
    return d


class PlanManager:
    def __init__(self, db: Database, stock_pool, action_record, delivery=None):
        self.db = db
        self.stock_pool = stock_pool
        self.action_record = action_record
        self.human_in_loop = HumanInLoop(db)
        # EX-01: pending 审核推送 (只推送, 交互回调留给 08 层 AG-07 卡)
        self._delivery = delivery
        self._delivery_ready = delivery is not None

    def receive_plan(self, plan: dict):
        plan.get("date", bj_date())
        recommendations = plan.get("recommendations", [])
        for rec in recommendations:
            code = rec.get("code", "")
            name = rec.get("name", "")
            action = rec.get("action", "buy")
            confidence = rec.get("confidence", 0.5)
            reason = rec.get("reason", "")
            # 甲案第二段 (2026-09-25): 入池理由回填 — rationale 有料 → 守卫加工后
            # 真理由; 无料/守卫拒/异常 → 原样 (绝不抛, 铁律#12: 入池主链优先)。
            # 零新增 LLM (只读 agent 层已落库 rationale + 确定性守卫)。
            # 真理由覆盖率天花板 = 当日辩论票数 (≤ deep_top_n), 特性非缺陷。
            # action 传入 = 闸A（动作一致）生效前提: buy 票不许配 avoid/hold 的理由
            reason = enrich_reason(self, code, plan.get("date", bj_date()), reason,
                                   action=action)

            # Human-in-loop 审核
            if action in ("buy", "sell"):
                verdict = self.human_in_loop.evaluate(
                    code=code, decision_type=action,
                    reason=reason, score=rec.get("initial_score", 0),
                    confidence=confidence,
                    entry_low=rec.get("entry_low", 0), entry_high=rec.get("entry_high", 0),
                    stop_loss=rec.get("stop_loss", 0), take_profit=rec.get("take_profit", 0),
                )
                # action_type 必须在 action_records CHECK 白名单内:
                # 拒绝/待审核均属"未执行", 记 skip_entry/skip_exit + reason 区分
                skip_type = "skip_entry" if action == "buy" else "skip_exit"
                if verdict.action == "auto_reject":
                    self.action_record.write(
                        code=code, action_type=skip_type,
                        reason=f"自动拒绝: {verdict.reason}",
                        source="manual",
                    )
                    continue
                if verdict.action == "require_review":
                    if self._auto_approve_enabled():
                        # Phase 1.6: 模拟盘自动批准 — 立即 approve, 消费仍由
                        # daily_update 的 consume_approved 统一收走 (零新机制)。
                        # EX-01 缺参数拦截在消费侧, 此处不绕过。
                        self._auto_approve_and_notify(
                            code, name, action, reason, confidence,
                            review_id=verdict.review_id, rec=rec,
                        )
                    else:
                        self.action_record.write(
                            code=code, action_type=skip_type,
                            reason=f"待人工审核: {verdict.reason}",
                            source="manual",
                        )
                        self._notify_pending_review(
                            code, name, action, reason, confidence,
                            review_id=verdict.review_id, rec=rec,
                        )
                    continue

            if action == "buy":
                # REC 模块2 (2026-09-21): 入池风险收益守卫 — 止损/目标价/买入区间
                # 任一缺失(<=0)即拒入池。此前 rec.get(..., 0) 会把上游缺字段
                # 静默落 0 (生产恰好全给=100% 填充, 但守卫线是防回归载体,
                # 「有注释≠有约束」同构: 有数据≠有约束)。
                # 拒绝必须留痕 (action_record skip_entry + AlertManager),
                # 不静默 (pool_position_guard 960f34c8 同模式)。
                entry_low = rec.get("entry_low", 0) or 0
                entry_high = rec.get("entry_high", 0) or 0
                stop_loss = rec.get("stop_loss", 0) or 0
                take_profit = rec.get("take_profit", 0) or 0
                missing = [k for k, v in (
                    ("entry_low", entry_low), ("entry_high", entry_high),
                    ("stop_loss", stop_loss), ("take_profit", take_profit)
                ) if v <= 0]
                if missing:
                    rr_msg = (f"buy 拒入池: 缺风险收益参数 {missing} "
                              f"(entry_low={entry_low}, entry_high={entry_high}, "
                              f"stop_loss={stop_loss}, take_profit={take_profit})")
                    logger.warning(f"[{code}] {rr_msg}")
                    self.action_record.write(
                        code=code, action_type="skip_entry",
                        reason=rr_msg, source="strategy",
                    )
                    self._send_guard_alert(code, rr_msg)
                    continue
                # 风险收益比走 _rr_ratio 单一定义点 (2026-09-25 三闸卡抽取:
                # consume_approved 审批支也要算同一个 rr, 两处内联 = 改一处
                # 忘另一处 → 防线分裂, 同 688187 字段分裂判例)。
                entry_mid = (entry_low + entry_high) / 2.0
                rr_ok, rr_ratio, min_rr = self._rr_ok(
                    entry_low, entry_high, stop_loss, take_profit)
                if not rr_ok:
                    rr_msg = (f"buy 拒入池: 风险收益比 {rr_ratio:.4f} < 阈值 "
                              f"{min_rr} (tp={take_profit}, mid={entry_mid}, sl={stop_loss})")
                    logger.warning(f"[{code}] {rr_msg}")
                    self.action_record.write(
                        code=code, action_type="skip_entry",
                        reason=rr_msg, source="strategy",
                    )
                    self._send_guard_alert(code, rr_msg)
                    continue
                self.stock_pool.add(
                    code=code, name=name, source="strategy",
                    reason=reason, score=rec.get("initial_score", 0),
                )
                self.stock_pool.update(code,
                    entry_low=entry_low, entry_high=entry_high,
                    stop_loss=stop_loss, take_profit=take_profit,
                    rr_ratio=rr_ratio,
                )
                self.stock_pool.update_status(code, "pending_entry")
                # S5 通电: 初始入场链写有效期 (此前从不写 → valid_until 恒 NULL)
                self.stock_pool.update(code, valid_until=entry_valid_until(self.db))
                self.action_record.write(
                    code=code, action_type="set_entry_range",
                    reason=reason,
                    source="strategy",
                    new_value=entry_high,
                )
            elif action == "hold":
                self.stock_pool.update(code, current_score=rec.get("initial_score", 0))
                self.action_record.write(
                    code=code, action_type="hold",
                    reason=reason,
                    source="strategy",
                )
            elif action == "sell":
                # P0 phantom 卖出修复: sell 只对真持仓生效。原实现不校验
                # status, pending_entry/watch 直接被打成 exit_pending →
                # 从未建仓的股票被 _settle_pending_exits 写出 exit_price
                # (生产实证 12 只: entry_date/entry_price 全 NULL 却有
                # exit_price)。语义对齐 holding.py:26 "mark_exit_trigger
                # 只认 active" 先例:
                #   active        → exit_pending (T+1 结算, 原行为)
                #   pending_entry → cancel_entry (入场计划作废)
                #   watch         → 移除 watch (评分滑出本来就该出 watch)
                current = self.stock_pool.get(code)
                if not current:
                    continue
                status = current.get("status")
                if status == "active":
                    # AUD-026: 走 mark_exit_trigger 正门 —— 旧 update_status 绕过,
                    # trigger_price/trigger_date 全留 NULL。生产那 22 只卡死仓位
                    # 走的就是这条 (sell_signal 22 次 / status_change 52 次的痕迹)。
                    # trigger_price 取最近观测价; 无观测价记 0.0 —— 诚实空值,
                    # 不拿 entry_price 顶替 (那正是 P0 AUD-005 伪造面的做法)。
                    self.stock_pool.mark_exit_trigger(
                        code, float(current.get("last_price") or 0.0), reason)
                    self.action_record.write(
                        code=code, action_type="sell_signal",
                        reason=reason,
                        source="strategy",
                    )
                elif status == "pending_entry":
                    self.stock_pool.update_status(code, "watch")
                    self.action_record.write(
                        code=code, action_type="cancel_entry",
                        reason=f"未建仓收到 sell_signal, 作废入场计划: {reason}",
                        source="strategy",
                    )
                elif status == "watch":
                    self.stock_pool.update_status(code, "exited")
                    self.action_record.write(
                        code=code, action_type="cancel_entry",
                        reason=f"观察池内收到 sell_signal, 移出 watch: {reason}",
                        source="strategy",
                    )

    def _get_delivery(self):
        """EX-01: 惰性初始化 delivery (沿用 paper_broker 模式, 构造失败静默降级)"""
        if not self._delivery_ready:
            try:
                from config_layer import get_config
                from delivery_layer import DeliveryLayer
                cfg = get_config()
                if not cfg._loaded:
                    cfg.load()
                # 卡②: 必须传 db — 否则 executor.log_store=None, HITL 审批/
                # 通知卡发送全程不落 delivery_log (违铁律#12 可观测不可静默)。
                # 对齐 bootstrap.py 的 DeliveryLayer(config, db=database)。
                self._delivery = DeliveryLayer(config=cfg, db=self.db)
                self._delivery.registry.mark_healthy("feishu_dm")
                self._delivery_ready = True
            except Exception:
                return None
        return self._delivery

    def _rr_ratio(self, entry_low: float, entry_high: float,
                  stop_loss: float, take_profit: float) -> float:
        """风险收益比 = (目标价 − 买价) / (买价 − 止损价), 买价取区间中值。

        **单一定义点** (2026-09-25): receive_plan 直接放行支与 consume_approved
        审批支共用同一公式。分母非正 (止损 >= 买价) 返回 0.0, 不抛异常 ——
        这类单必然被阈值拦下, 无需额外分支。
        """
        entry_mid = (entry_low + entry_high) / 2.0
        denom = entry_mid - stop_loss
        return (take_profit - entry_mid) / denom if denom > 0 else 0.0

    def _rr_ok(self, entry_low: float, entry_high: float,
               stop_loss: float, take_profit: float) -> tuple[bool, float, float]:
        """rr 过闸判定 — **公式与比较都在这里** (单一定义点)。

        返回 (是否过闸, rr, 阈值)。比较带 _RR_EPS 容差: 数学上正好等于阈值
        的单必须放行, 浮点末位不该决定生死 (理由见 _RR_EPS 注释)。

        receive_plan 直接放行支与 consume_approved 审批支共用本方法 —— 若只
        抽公式不抽比较, 两处容差不一致照样分裂 (688187 字段分裂判例同族)。
        """
        rr = self._rr_ratio(entry_low, entry_high, stop_loss, take_profit)
        min_rr = self._min_rr_ratio()
        return (rr >= min_rr - _RR_EPS), rr, min_rr

    def _approval_max_age_days(self) -> int:
        """批准单保鲜期 (execution.approval_max_age_days, 缺省 2 个交易日)。

        **不复用 entry_valid_days=5**: 语义不同不共常量 —— entry_valid_days 是
        「入池后等价格回落的主动耐心」, 本值是「批准单在队列里的被动保鲜期」。
        取 2 的依据 = 生产实测漂移拐点: 卡 1~2 个交易日的单漂出入场区间 0%,
        卡 4 个交易日的 25 单漂出 68% (6 涨出 / 11 跌破), 其中 000875 现价距
        止损仅 0.22% —— 消费即开仓即触止损。拐点在 2~4 之间, 取保守下界 2。
        """
        try:
            from config_layer import cfg_get
            return int(cfg_get("execution.approval_max_age_days", 2))
        except Exception:
            return 2

    def _approval_age_days(self, created_at: str) -> int:
        """批准单已积压几个**交易日** (自然日不算 — 周末不该吃掉保鲜期)。

        TradingCalendar.trading_days_between 含端计数, 故减 1 (创建当日不计)。
        取不到日期时返回 0 (按新鲜处理): 闸A 是卫生闸不是安全闸, 判不了时
        不该误杀; 真正的安全底线在闸B/闸C。
        """
        try:
            from common.calendar import TradingCalendar
            day = str(created_at or "")[:10]
            if not day:
                return 0
            n = TradingCalendar(db=self.db).trading_days_between(day, bj_date())
            return max(0, n - 1)
        except Exception:
            return 0

    def _last_close(self, code: str) -> float | None:
        """该票最新收盘价 (无行情返回 None — 由调用方显式留痕, 不伪造 0)。"""
        try:
            row = self.db.get_connection().execute(
                "SELECT close FROM daily_klines WHERE code=? "
                "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass
        return None

    def _min_rr_ratio(self) -> float:
        """REC 模块2: 风险收益比入池阈值 (execution.min_rr_ratio, 缺省 1.5)。

        配置而非硬编码; PlanManager 惰性 config (构造签名无 config),
        读 config_layer Config 单例 (与 _auto_approve_enabled 同风格)。
        """
        try:
            from config_layer import get_config
            v = get_config().get("execution.min_rr_ratio", 1.5)
            return float(v) if v is not None else 1.5
        except Exception:
            return 1.5

    def _send_guard_alert(self, code: str, msg: str) -> None:
        """守卫拒绝的 AlertManager 留痕 (pool_position_guard 同模式)。

        AlertManager 不可用时留痕降级为 logger (守卫拒绝不因告警链故障放大)。
        """
        try:
            from ops_layer.core.models import Alert
            from ops_layer.alert_manager import AlertManager
            am = AlertManager(self.db)
            am.send(Alert(
                severity="warning", layer="execution",
                title=f"入池守卫拒绝: {code}",
                detail=msg,
                action="核查上游推荐链为何缺风险收益参数/赔率不足",
            ))
        except Exception as e:
            logger.warning(f"guard alert 发送失败 (拒入池留痕已在 action_records): {e}")

    def _auto_approve_enabled(self) -> bool:
        """Phase 1.6 开关: execution.hitl_auto_approve (默认 false)。

        读失败/配置未加载一律按关处理 — 宁可多等人审, 不可误放行。
        """
        try:
            from config_layer import cfg_get
            return bool(cfg_get("execution.hitl_auto_approve", False))
        except Exception:
            return False

    def _auto_approve_and_notify(self, code: str, name: str, action: str,
                                 reason: str, confidence: float,
                                 review_id: int = 0, rec: dict | None = None):
        """Phase 1.6: 自动批准 + 通知卡 (带否决按钮) + 台账留痕。

        批准失败 (rowcount=0, 并发/重复) 时降级走人工审核路径, 不静默。
        台账/通知卡失败不阻断主流程 (审计件 best-effort, 留 warning)。
        """
        ok = self.human_in_loop.approve(
            review_id, reviewer="auto-paper", note="模拟盘自动批准")
        if not ok:
            logger.warning(f"auto-approve 未命中 review_id={review_id}, 降级人工审核路径")
            self.action_record.write(
                code=code, action_type="skip_entry" if action == "buy" else "skip_exit",
                reason=f"待人工审核 (auto-approve 未命中): {reason}",
                source="manual",
            )
            self._notify_pending_review(code, name, action, reason, confidence,
                                        review_id=review_id, rec=rec)
            return
        rec = rec or {}
        logger.info(f"[{code}] 模拟盘自动批准 review_id={review_id} "
                    f"({action} confidence={confidence:.2f})")
        # 台账: reviewer=auto-paper, 审计不缺位
        try:
            from delivery_layer.hitl_card_templates import build_signal_summary
            from delivery_layer.hitl_ledger import write_bitable_ledger
            write_bitable_ledger(
                review_id, code, action, "approve", "auto-paper",
                "模拟盘自动批准", "auto_paper",
                build_signal_summary(code, action,
                                     rec.get("initial_score", 0) or 0,
                                     confidence, reason),
            )
        except Exception as e:
            logger.warning(f"auto-paper 台账写入失败 review_id={review_id}: {e}")
        # 通知卡 (非审批卡): 绿头 + 单【否决】按钮, 仍锁死 DM
        self._notify_auto_approved(code, name, action, reason, confidence,
                                   review_id=review_id, rec=rec)

    def _notify_auto_approved(self, code: str, name: str, action: str,
                              reason: str, confidence: float,
                              review_id: int = 0, rec: dict | None = None):
        delivery = self._get_delivery()
        if not delivery:
            return
        rec = rec or {}
        try:
            from delivery_layer import Message
            from delivery_layer.hitl_card_templates import build_notification_card

            card = build_notification_card(
                review_id=review_id, code=code, name=name,
                decision_type=action, reason=reason,
                score=rec.get("initial_score", 0) or 0,
                confidence=confidence,
                entry_low=rec.get("entry_low", 0) or 0,
                entry_high=rec.get("entry_high", 0) or 0,
                stop_loss=rec.get("stop_loss", 0) or 0,
                take_profit=rec.get("take_profit", 0) or 0,
            )
            verb = "买入" if action == "buy" else "卖出"
            results = delivery.send(Message(
                content={"msg_type": "interactive", "card": card},
                content_type="execution_alert",
                title=f"已自动批准[模拟盘]: {code} {verb}",
                force_channel="feishu_hitl_dm",   # Phase 2: 新 app 发出, 回调投 HITL daemon
            ))
            self._persist_card_message_id(review_id, results)
        except Exception as e:
            logger.warning(f"Auto-approved notification failed for {code}: {e}")

    def _notify_pending_review(self, code: str, name: str, action: str,
                               reason: str, confidence: float,
                               review_id: int = 0, rec: dict | None = None):
        """HITL: pending 审核推带按钮的审批卡, 回调由 hitl_ws_daemon 接。

        force_channel="feishu_dm" 是安全硬约束, 不是偏好: 卡片按钮谁点都生效,
        进群等于任何群成员可批信号。锁在代码里, 不交给 push_rules.yaml。
        """
        delivery = self._get_delivery()
        if not delivery:
            return
        rec = rec or {}
        try:
            from delivery_layer import Message
            from delivery_layer.hitl_card_templates import build_approval_card

            card = build_approval_card(
                review_id=review_id, code=code, name=name,
                decision_type=action, reason=reason,
                score=rec.get("initial_score", 0) or 0,
                confidence=confidence,
                entry_low=rec.get("entry_low", 0) or 0,
                entry_high=rec.get("entry_high", 0) or 0,
                stop_loss=rec.get("stop_loss", 0) or 0,
                take_profit=rec.get("take_profit", 0) or 0,
            )
            verb = "买入" if action == "buy" else "卖出"
            results = delivery.send(Message(
                content={"msg_type": "interactive", "card": card},
                content_type="execution_alert",
                title=f"待人工审核: {code} {verb}",
                force_channel="feishu_hitl_dm",   # Phase 2: 新 app 发出, 回调投 HITL daemon
            ))
            self._persist_card_message_id(review_id, results)
        except Exception as e:
            logger.warning(f"Pending review notification failed for {code}: {e}")

    def _persist_card_message_id(self, review_id: int, results) -> None:
        """回填审批卡 message_id — Phase 2 兜底轮询与审计要靠它定位原卡片。

        ws 回调自带 open_message_id, 故此处拿不到不影响 Phase 1 闭环;
        best-effort, 且严格校验类型 (测试里 delivery 是 MagicMock)。
        """
        if not review_id or not isinstance(results, list):
            return
        for r in results:
            remote_id = getattr(r, "remote_message_id", "")
            if isinstance(remote_id, str) and remote_id:
                try:
                    self.human_in_loop.set_message_id(review_id, remote_id)
                except Exception as e:
                    logger.warning(f"回填审批卡 message_id 失败 review_id={review_id}: {e}")
                return

    def adjust_stop_loss(self, code: str, new_stop: float, reason: str, source: str = "execution"):
        pool = self.stock_pool.get(code)
        if not pool:
            return
        old = pool.get("dynamic_stop_loss") or pool.get("stop_loss") or 0
        self.stock_pool.update(code, dynamic_stop_loss=new_stop, dynamic_stop_reason=reason)
        self.action_record.write(
            code=code, action_type="adjust_stop_loss",
            price=pool.get("last_price"), previous_value=old,
            new_value=new_stop, reason=reason, source=source,
        )

    def cancel_entry(self, code: str, reason: str = "手动取消"):
        self.stock_pool.update_status(code, "watch")
        self.action_record.write(code=code, action_type="cancel_entry", reason=reason, source="execution")

    def cancel_exit(self, code: str, reason: str = "手动取消"):
        current = self.stock_pool.get(code)
        if current and current["status"] == "exit_pending":
            self.stock_pool.update_status(code, "active")
            self.action_record.write(
                code=code, action_type="cancel_exit",
                reason=reason, source="execution",
            )

    def consume_approved(self) -> int:
        """F4.6: 消费已批准审核记录 — buy 转 pending_entry 写入场区间, sell 标记退出触发。

        幂等: consumed 标记防重复消费; 已在 active/exit_pending/exited 的 buy 不重复入场。
        返回消费条数。
        """
        consumed = 0
        gate_rejects: list[str] = []
        for rec in self.human_in_loop.get_approved_unconsumed():
            code = rec["code"]
            dtype = rec["decision_type"]
            if dtype == "buy":
                # EX-01: 校验入场参数齐全 — 缺参数的批准单打回标记 invalid 不消费
                # (含存量无参数批准记录; 不删除, 留痕可追溯)
                entry_low = rec.get("entry_low") or 0
                entry_high = rec.get("entry_high") or 0
                stop_loss = rec.get("stop_loss") or 0
                take_profit = rec.get("take_profit") or 0
                if not (entry_low > 0 and entry_high > 0 and stop_loss > 0 and take_profit > 0):
                    self.human_in_loop.mark_invalid(rec["id"], "缺少入场参数")
                    self.action_record.write(
                        code=code, action_type="skip_entry",
                        reason=f"批准记录缺入场参数, 打回作废不消费 (review_id={rec['id']}, "
                               f"entry_low={entry_low}, entry_high={entry_high}, "
                               f"stop_loss={stop_loss}, take_profit={take_profit})",
                        source="manual",
                    )
                    logger.warning(f"[{code}] approved buy 打回: 缺入场参数, 标记 invalid (review_id={rec['id']})")
                    consumed += 1
                    continue
                # ---- 三闸 (2026-09-25): 审批支此前从进队列到入池全程不过闸,
                # 只有 EX-01 参数齐全校验。模块2 的 rr 守卫只活在 receive_plan
                # 的「高信心直接放行」那一支, 审批支零覆盖 —— 而审批支才是量大
                # 的那支 (09-21 一天 25 只)。判例「半截防线=没有防线」。
                #
                # 运行序 A(时效) → B(价格现实) → C(rr), 与"重要性"排序相反,
                # 理由: 陈旧单的 entry 参数本身已失效, 拿失效参数算出的 rr 去
                # 定性「rr 不合格」是误导 —— 它真正的问题是陈旧。先判时效最诚实。
                gate_msg = None

                # 闸A 时效: 队列卫生闸, 不依赖外部行情, 是硬底线
                age = self._approval_age_days(rec.get("created_at", ""))
                max_age = self._approval_max_age_days()
                if age > max_age:
                    gate_msg = (f"approved buy 打回: 批准单已积压 {age} 个交易日 "
                                f"> 保鲜期 {max_age} (created_at={rec.get('created_at')}), "
                                f"入场区间已失效, 不以陈旧参数入池")

                if gate_msg is None:
                    last_close = self._last_close(code)
                    if last_close is None:
                        # 判例「空集必须显式报警不得静默输出零」: 不伪造"通过",
                        # 明说这一闸没能判。不拦截 —— 否则行情断供时再造一片积压
                        # (daily_update 锁饿死 35 单积压的教训); 硬底线由闸A 兜。
                        logger.warning(
                            f"[{code}] 价格现实闸未能判定: 无收盘价可取, 本单按未判"
                            f"放行 (非通过); 时效闸已过 age={age}")
                    elif last_close <= stop_loss:
                        gate_msg = (f"approved buy 打回: 现价 {last_close} 已跌破止损 "
                                    f"{stop_loss}, 消费即开仓即触止损")
                    elif last_close < entry_low or last_close > entry_high:
                        gate_msg = (f"approved buy 打回: 现价 {last_close} 已漂出买入"
                                    f"区间 [{entry_low}, {entry_high}], 原推荐意图不成立")

                if gate_msg is None:
                    # 闸C rr: 补齐审批支覆盖 (本卡存在的理由)
                    rr_ok, rr, min_rr = self._rr_ok(
                        entry_low, entry_high, stop_loss, take_profit)
                    if not rr_ok:
                        gate_msg = (f"approved buy 打回: 风险收益比 {rr:.4f} < 阈值 "
                                    f"{min_rr} (tp={take_profit}, sl={stop_loss})")

                if gate_msg is not None:
                    self.human_in_loop.mark_invalid(rec["id"], gate_msg[:200])
                    self.action_record.write(
                        code=code, action_type="skip_entry",
                        reason=f"{gate_msg} (review_id={rec['id']})",
                        source="manual",
                    )
                    logger.warning(f"[{code}] {gate_msg}")
                    gate_rejects.append(code)
                    consumed += 1
                    continue

                current = self.stock_pool.get(code)
                if current and current["status"] in ("active", "exit_pending", "exited"):
                    logger.info(f"[{code}] approved buy 跳过: 已 {current['status']}")
                    self.human_in_loop.mark_consumed(rec["id"])
                    consumed += 1  # 已标记消费 (跳过入场), 计入处理条数
                    continue
                if not current:
                    self.stock_pool.add(
                        code=code, name=code, source="strategy",
                        reason=rec.get("reason") or "人工审核批准",
                        score=rec.get("score") or 0,
                    )
                self.stock_pool.update(
                    code,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
                self.stock_pool.update_status(code, "pending_entry")
                # S5 通电: 人工批准入场同样写有效期
                self.stock_pool.update(code, valid_until=entry_valid_until(self.db))
                self.action_record.write(
                    code=code, action_type="set_entry_range",
                    reason=f"人工审核批准买入 (review_id={rec['id']}): {rec.get('reason') or ''}",
                    source="manual",
                    new_value=rec.get("entry_high") or 0,
                )
                logger.info(f"[{code}] approved buy → pending_entry [{rec.get('entry_low')}, {rec.get('entry_high')}]")
            elif dtype == "sell":
                current = self.stock_pool.get(code)
                if current and current["status"] == "active":
                    # F4.4 收敛: 只标记触发, T+1 结算
                    self.stock_pool.mark_exit_trigger(
                        code, current.get("last_price") or 0,
                        f"人工批准卖出 (review_id={rec['id']})")
                else:
                    logger.info(f"[{code}] approved sell 跳过: 非 active 持仓")
            else:
                logger.info(f"[{code}] approved {dtype} 暂不支持自动消费, 标记已处理")
            self.human_in_loop.mark_consumed(rec["id"])
            consumed += 1
        if gate_rejects:
            # 聚合告警: 一次消费可能打回几十单 (积压场景), 逐单发告警 = 刷屏。
            # 逐单留痕在 action_records (可查), 告警面只发一条汇总。
            msg = (f"consume_approved 三闸打回 {len(gate_rejects)} 单 "
                   f"(时效/价格现实/风险收益比), 明细见 action_records skip_entry: "
                   f"{gate_rejects[:20]}")
            logger.warning(msg)
            self._send_guard_alert(f"{len(gate_rejects)} 单", msg)
        if consumed:
            logger.info(f"consume_approved: 消费 {consumed} 条已批准审核记录 "
                        f"(其中三闸打回 {len(gate_rejects)} 单)")
        return consumed

    def adjust_take_profit(self, code: str, new_tp: float, reason: str, source: str = "execution"):
        pool = self.stock_pool.get(code)
        if not pool:
            return
        old = pool.get("take_profit") or 0
        self.stock_pool.update(code, take_profit=new_tp)
        self.action_record.write(
            code=code, action_type="adjust_take_profit",
            price=pool.get("last_price"), previous_value=old,
            new_value=new_tp, reason=reason, source=source,
        )

    def adjust_trailing_stop(self, code: str, new_trail: float, reason: str, source: str = "execution"):
        pool = self.stock_pool.get(code)
        if not pool:
            return
        old = pool.get("trailing_stop") or 0
        self.stock_pool.update(code, trailing_stop=new_trail)
        self.action_record.write(
            code=code, action_type="adjust_trailing_stop",
            price=pool.get("last_price"), previous_value=old,
            new_value=new_trail, reason=reason, source=source,
        )

    def adjust_entry_range(self, code: str, new_low: float, new_high: float, reason: str, source: str = "execution"):
        pool = self.stock_pool.get(code)
        if not pool:
            return
        old_low = pool.get("entry_low") or 0
        pool.get("entry_high") or 0
        self.stock_pool.update(code, entry_low=new_low, entry_high=new_high)
        self.action_record.write(
            code=code, action_type="adjust_entry_range",
            previous_value=old_low, new_value=new_high,
            reason=reason, source=source,
        )
