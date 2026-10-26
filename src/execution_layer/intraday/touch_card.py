"""盘中触价行动卡 — R2 报告重塑（设计稿 §2.3，统筹施工令 D1~D8 全批）。

一处触发，并联一路：不改 execute_entry / mark_exit_trigger 的现有行为，
只额外推送一张「用户视角」的行动卡。

定位铁律：QS = 股票推荐系统，非交易系统；主句永远是「你可以做什么」。
色彩语义：绿=可买 / 橙=逼近警示 / 红=止损(熔断) / 蓝=信息(拦截照推)。
措辞表：持仓→推荐跟踪；止损线→参考卖出线。
合规固定段：每张卡结尾「模拟盘研究，非投资建议」。

去重：cron 每 5 分钟一次性进程，delivery_layer 的内存 RateLimiter 跨进程失效，
故用 jsonl 事件台账做跨进程去重（同时充当铁律#12 的第三落点留痕）。
留痕三落点：intraday_checks(既有) + delivery_log(传 db 后通电) + jsonl 事件。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from common.logging import get_logger
from common.utils import bj_date, bj_now
from config_layer import cfg_get
from delivery_layer import DeliveryLayer, Message

logger = get_logger("execution.touch_card")

# 卡片头色 (飞书 card header template) — 色彩语义铁律
HEADER_GREEN = "green"   # 可买
HEADER_ORANGE = "orange"  # 逼近警示
HEADER_RED = "red"        # 止损/熔断
HEADER_BLUE = "blue"      # 信息(拦截照推)

COMPLIANCE_FOOTER = "模拟盘研究，非投资建议"

# 止损逼近阈值：现价距参考卖出线 ≤ 2% 触发橙卡
APPROACH_THRESHOLD_PCT = 2.0
# 逼近卡跨进程冷却（秒）：避免价格滞留线附近时每 5 分钟刷屏
APPROACH_COOLDOWN_S = 1800

# 一次性卡型（每 code 每日一张）vs 冷却卡型
_ONE_SHOT_TYPES = frozenset({"entry", "entry_blocked", "stop_trigger"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _events_dir() -> Path:
    override = os.environ.get("QS_TOUCH_CARD_DIR")
    return Path(override) if override else _repo_root() / "data" / "events"


def _valid_days_left(pool_item: dict) -> int | None:
    """有效期余 N 天（valid_until 为空返回 None，诚实不展示）。"""
    vu = pool_item.get("valid_until")
    if not vu:
        return None
    try:
        end = datetime.strptime(str(vu)[:10], "%Y-%m-%d").date()
        today = datetime.strptime(bj_date(), "%Y-%m-%d").date()
        return (end - today).days
    except Exception:  # noqa: BLE001
        return None


def _stop_of(pool_item: dict) -> float:
    """参考卖出线：动态止损优先，回退固定止损（与 exit_engine layer1 同口径）。"""
    return float(pool_item.get("dynamic_stop_loss") or pool_item.get("stop_loss") or 0)


class TouchCardPusher:
    """触价行动卡推送器：构造中文行动卡并经 delivery_layer intraday_alert 投递。

    复用 intraday_alert 的 FAN_OUT 路由与 delivery_log 留痕；跨进程去重自理（jsonl 台账）。
    一切失败仅 warning，绝不阻断 _scan 主流程。
    """

    def __init__(self, db, stock_pool, config=None):
        self.db = db
        self.stock_pool = stock_pool
        self.config = config
        self._delivery = None
        self._delivery_ready = False

    # ---- delivery 懒加载（传 db 以通电 delivery_log 留痕）----
    def _ensure_delivery(self) -> bool:
        if self._delivery_ready:
            return self._delivery is not None
        try:
            from config_layer import get_config
            cfg = self.config or get_config()
            if not getattr(cfg, "_loaded", False):
                cfg.load()
            self._delivery = DeliveryLayer(config=cfg, db=self.db)
            self._delivery_ready = True
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"TouchCard delivery 初始化失败(跳过推送): {e}")
            self._delivery_ready = True  # 不反复重试
            return False

    # ---- jsonl 事件台账（留痕 + 跨进程去重）----
    def _ledger_path(self) -> Path:
        return _events_dir() / f"touch_card-{bj_date().replace('-', '')}.jsonl"

    def _read_ledger(self) -> dict[tuple[str, str], float]:
        """返回 {(code, card_type): last_ts}。文件不存在/损坏返回空。"""
        out: dict[tuple[str, str], float] = {}
        path = self._ledger_path()
        if not path.exists():
            return out
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    key = (rec.get("code", ""), rec.get("card_type", ""))
                    out[key] = float(rec.get("ts", 0))
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            logger.warning(f"touch_card 台账读取失败(视为空): {e}")
        return out

    def _should_push(self, code: str, card_type: str) -> bool:
        """跨进程去重：一次性卡型当日已推则跳过；逼近卡型按冷却。"""
        last = self._read_ledger().get((code, card_type))
        if last is None:
            return True
        if card_type in _ONE_SHOT_TYPES:
            return False
        return (time.time() - last) >= APPROACH_COOLDOWN_S

    def _append_ledger(self, rec: dict) -> None:
        try:
            d = _events_dir()
            d.mkdir(parents=True, exist_ok=True)
            with self._ledger_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"touch_card 台账写入失败(不影响推送): {e}")

    def _remote_id_of(self, code: str, card_type: str) -> str:
        """R3/D3: 反查台账中该 code 最近一张指定卡型的远端 message_id。

        台账按时间追加, 倒序取第一条带 remote_message_id 的记录。
        找不到返回 ""（调用方据此降级重发新卡）。
        """
        path = self._ledger_path()
        if not path.exists():
            return ""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return ""
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("code") == code and rec.get("card_type") == card_type:
                rid = rec.get("remote_message_id") or ""
                if rid:
                    return rid
        return ""

    # ---- 卡片构造与投递 ----
    @staticmethod
    def _card_content(title: str, body_md: str, header_color: str) -> dict:
        """飞书 interactive 卡片内容（新发与 PATCH 原地更新共用同一构造）。"""
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": header_color,
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": body_md}},
                ],
            },
        }

    def _send_card(self, title: str, body_md: str, header_color: str,
                   code: str, card_type: str) -> bool:
        if not self._ensure_delivery():
            return False
        try:
            content = self._card_content(title, body_md, header_color)
            msg = Message(content=content, content_type="intraday_alert", title=title)
            report_mode = cfg_get("delivery.report_mode", "debug")
            results = self._delivery.send(msg, report_mode=report_mode)
            # R3/D3: 捕获远端 message_id 落台账 — PATCH 原地更新要靠它定位原卡。
            remote_id = ""
            if isinstance(results, list):
                for r in results:
                    rid = getattr(r, "remote_message_id", "")
                    if rid:
                        remote_id = rid
                        break
            self._append_ledger({
                "ts": time.time(), "time": bj_now(), "code": code,
                "card_type": card_type, "title": title,
                "remote_message_id": remote_id,
            })
            logger.info(f"触价卡已推送 [{card_type}] {code}: {title}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"触价卡推送失败 [{card_type}] {code}: {e}")
            return False

    def _patch_card(self, remote_message_id: str, title: str, body_md: str,
                    header_color: str, code: str, card_type: str) -> bool:
        """R3/D3: 原地更新已发卡片（同一张卡状态推进，不重发新卡）。

        成功 → 台账记一条 card_update 留痕（带同一 remote_message_id）；
        任何失败返回 False，调用方降级重发新卡。
        """
        if not remote_message_id or not self._ensure_delivery():
            return False
        try:
            content = self._card_content(title, body_md, header_color)
            result = self._delivery.update(remote_message_id, content)
            if getattr(result, "status", "") != "success":
                logger.warning(
                    f"触价卡原地更新失败 [{card_type}] {code} "
                    f"({getattr(result, 'error_msg', '')}), 降级重发新卡")
                return False
            self._append_ledger({
                "ts": time.time(), "time": bj_now(), "code": code,
                "card_type": f"{card_type}_update", "title": title,
                "remote_message_id": remote_message_id,
            })
            logger.info(f"触价卡已原地更新 [{card_type}] {code}: {title}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"触价卡原地更新异常 [{card_type}] {code}: {e}, 降级重发新卡")
            return False

    def _name_of(self, code: str, pool_item: dict | None) -> str:
        name = (pool_item or {}).get("name") or code
        return f"{name}（{code}）" if name != code else code

    # ---- 入场触价卡（绿）/ D1 拦截照推卡（蓝）----
    def push_entry(self, code: str, price: float, pool_item: dict,
                   trade_result=None, block_reason: str | None = None) -> bool:
        """入场触价行动卡。

        block_reason 非空 → D1 拦截照推（蓝卡，主句不变+标注系统未买入原因）。
        trade_result.success → 绿卡+模拟买入佐证行。
        trade_result 存在但失败 → 绿卡+「系统本笔未买入」佐证。
        """
        item = pool_item or {}
        card_type = "entry_blocked" if block_reason else "entry"
        if not self._should_push(code, card_type):
            logger.debug(f"触价卡去重跳过 [{card_type}] {code}")
            return False

        name = self._name_of(code, item)
        low = item.get("entry_low") or 0
        high = item.get("entry_high") or 0
        stop = _stop_of(item)
        take_profit = item.get("take_profit") or 0
        days_left = _valid_days_left(item)
        reason = item.get("added_reason") or item.get("entry_reason") or ""

        # 主句（用户动作视角，句式宪法）
        head = f"📈 {name} 进入买入区间 [{low:.2f}, {high:.2f}]，可以买"
        title = f"📈 {name} 进入买入区间，可以买"

        lines = [f"**{head}**", "", f"现价 {price:.2f}"]
        meta = []
        if stop > 0:
            dist = (price - stop) / price * 100 if price > 0 else 0
            meta.append(f"参考卖出线 {stop:.2f}（约 -{dist:.1f}%）")
        if take_profit > 0:
            meta.append(f"参考止盈 {take_profit:.2f}")
        if days_left is not None:
            meta.append(f"有效余 {days_left} 天")
        if meta:
            lines.append(" ｜ ".join(meta))
        if reason:
            lines.append(f"理由：{reason}")

        # 佐证行（系统动作降次要位）
        if block_reason:
            lines.append("")
            lines.append(f"系统本笔未买入：{block_reason}，你可自行决策。")
        elif trade_result is not None:
            lines.append("")
            if getattr(trade_result, "success", False):
                qty = getattr(trade_result, "qty", 0) or 0
                at = bj_now().split(" ")[1][:5] if " " in bj_now() else ""
                lines.append(f"系统已于 {at} 按 {price:.2f} 模拟买入 {qty} 股（事后佐证）。")
            else:
                r = getattr(trade_result, "reason", "") or ""
                lines.append(f"系统本笔未买入：{r}，你可自行决策。")

        lines.append("")
        lines.append(f"_{COMPLIANCE_FOOTER}_")

        header = HEADER_BLUE if block_reason else HEADER_GREEN
        return self._send_card(title, "\n".join(lines), header, code, card_type)

    # ---- 止损逼近卡（橙）----
    def push_stop_approach(self, code: str, price: float, stop: float,
                           pool_item: dict) -> bool:
        item = pool_item or {}
        card_type = "stop_approach"
        if not self._should_push(code, card_type):
            logger.debug(f"触价卡去重跳过 [{card_type}] {code}")
            return False
        name = self._name_of(code, item)
        dist = (price - stop) / stop * 100 if stop > 0 else 0
        title = f"⚠️ {name} 逼近参考卖出线"
        lines = [
            f"**⚠️ {name} 现价 {price:.2f} 逼近参考卖出线 {stop:.2f}（约 {dist:.1f}%），留意风险**",
            "",
            "若跌破参考卖出线，系统将标记卖出（T+1 结算）；你可自行决策是否提前处理。",
            "",
            f"_{COMPLIANCE_FOOTER}_",
        ]
        return self._send_card(title, "\n".join(lines), HEADER_ORANGE, code, card_type)

    # ---- 止损/退出触发卡（红）----
    def push_stop_trigger(self, code: str, price: float, reason: str,
                          pool_item: dict) -> bool:
        item = pool_item or {}
        card_type = "stop_trigger"
        if not self._should_push(code, card_type):
            logger.debug(f"触价卡去重跳过 [{card_type}] {code}")
            return False
        name = self._name_of(code, item)
        stop = _stop_of(item)
        title = f"🔴 {name} 触发卖出"
        stop_line = f"参考卖出线 {stop:.2f} ｜ " if stop > 0 else ""
        lines = [
            f"**🔴 {name} 现价 {price:.2f}，{stop_line}系统已标记卖出（T+1 结算）**",
            "",
            f"原因：{reason}",
            "你可以选择跟随卖出，或基于自身判断继续持有。",
            "",
            f"_{COMPLIANCE_FOOTER}_",
        ]
        body_md = "\n".join(lines)
        # R3/D3: 同一张卡「进入区间」→「触发卖出」原地更新（范式 4）。
        # 反查当日 entry 卡的远端 message_id，PATCH 成功即不重发新卡；
        # 找不到原卡/更新失败 → 降级走原有重发新红卡路径。
        entry_rid = self._remote_id_of(code, "entry")
        if entry_rid and self._patch_card(entry_rid, title, body_md,
                                          HEADER_RED, code, card_type):
            return True
        return self._send_card(title, body_md, HEADER_RED, code, card_type)
