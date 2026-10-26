"""HITL WebSocket daemon — 飞书卡片回调长连接主通道

纯出站 wss 客户端: 零监听端口, NAT 后即可, 与 curl 出站同性质
(不违反「禁常驻 web」红线 — 红线针对入站监听)。

数据流:
    用户点卡片按钮
      -> 飞书 ws 网关下推 EVENT 帧 (event_type=card.action.trigger)
      -> on_card_action
      -> HumanInLoop.approve/reject (SQLite 原子 UPDATE ... WHERE status='pending')
      -> Bitable 台账行
      -> PATCH 原卡片为结果卡 (按钮消失)
      -> 返回 toast

SDK 事实 (lark-oapi 1.7.3, 已核实源码):
  * ws/client.py 对 MessageType.CARD 帧直接 return — 丢弃不处理。
    因此 lark_oapi.card.CardActionHandler 只服务 HTTP webhook 模式,
    长连接下挂它永远不触发。卡片回调实际走 EVENT 帧,
    经 EventDispatcherHandler._callback_processor_map 的
    "p2.card.action.trigger" 键派发。
  * ws.Client.__init__ 无 card_handler 参数, 只有 event_handler。

运维:
    systemctl --user status|restart qs-hitl-ws
回滚:
    systemctl --user disable --now qs-hitl-ws
"""

import contextlib
import json
import os
import sys
import threading
import time
import traceback

# --- 路径与环境: 必须在业务 import 之前 ---
_HERE = os.path.dirname(os.path.abspath(__file__))          # <root>/src/delivery_layer
QS_ROOT = os.path.dirname(os.path.dirname(_HERE))           # <root>
_SRC = os.path.join(QS_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# CF-01: 非 src/__main__.py 入口必须自带 load_dotenv, 否则读不到飞书凭据
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(QS_ROOT, ".env"))

import requests  # noqa: E402
from lark_oapi.core.enum import LogLevel  # noqa: E402
from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E402
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler  # noqa: E402
from lark_oapi.ws import Client as WsClient  # noqa: E402

from common.database import Database  # noqa: E402
from common.logging import get_logger  # noqa: E402
from common.utils import bj_now  # noqa: E402
from delivery_layer.channels.feishu_dm import FEISHU_API_BASE, _get_cached_token  # noqa: E402
from delivery_layer.hitl_card_templates import (  # noqa: E402
    build_result_card,
    build_signal_summary,
)
from delivery_layer.hitl_ledger import (  # noqa: E402
    ledger_selfcheck,
    write_bitable_ledger,
)
from execution_layer.core.human_in_loop import HumanInLoop  # noqa: E402

logger = get_logger("delivery.hitl_ws")

HEARTBEAT_PATH = os.path.join(QS_ROOT, "logs", "hitl_ws_heartbeat.jsonl")
HEARTBEAT_MAX_BYTES = 5 * 1024 * 1024   # 5MB
HEARTBEAT_GENERATIONS = 3               # .1 .2 .3
HEARTBEAT_INTERVAL_S = 60
RSS_WARN_MB = 300                       # systemd MemoryMax=384M 为硬顶
# 稳态 ~175MB 几乎全是 lark_oapi 的 import 开销 (见 unit 文件注释),
# 原值 80 低于地板, 会让每一次心跳都误报内存超限。

# Phase 2 app 分离: HITL 专用凭据优先, 缺则回退共用键 (回滚=删 .env 新键)
FEISHU_APP_ID = os.environ.get("FEISHU_HITL_APP_ID") or os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_HITL_APP_SECRET") or os.environ.get("FEISHU_APP_SECRET", "")
# Bitable 台账配置由 hitl_ledger.write_bitable_ledger 自行读 env (Phase 1.6 抽取)

_START_TIME = time.time()
_hb_lock = threading.Lock()


# ---------------------------------------------------------------- 可观测

def _rotate_heartbeat():
    """5MB 自转, 保留 3 代"""
    for i in range(HEARTBEAT_GENERATIONS - 1, 0, -1):
        src = HEARTBEAT_PATH + "." + str(i)
        dst = HEARTBEAT_PATH + "." + str(i + 1)
        if os.path.exists(src):
            os.replace(src, dst)
    os.replace(HEARTBEAT_PATH, HEARTBEAT_PATH + ".1")


def write_heartbeat(event: str, **kwargs):
    """心跳/回调/重连事件写 jsonl — 铁律#12 可观测不可静默"""
    try:
        with _hb_lock:
            os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
            if (os.path.exists(HEARTBEAT_PATH)
                    and os.path.getsize(HEARTBEAT_PATH) > HEARTBEAT_MAX_BYTES):
                _rotate_heartbeat()
            entry = {"ts": bj_now(), "event": event, "pid": os.getpid()}
            entry.update(kwargs)
            with open(HEARTBEAT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"心跳写入失败 ({event}): {e}")


def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def _start_heartbeat_thread():
    def _loop():
        while True:
            time.sleep(HEARTBEAT_INTERVAL_S)
            rss = round(_rss_mb(), 1)
            write_heartbeat("heartbeat", rss_mb=rss,
                            uptime_s=int(time.time() - _START_TIME))
            if rss > RSS_WARN_MB:
                logger.warning(f"HITL ws 内存偏高: RSS={rss}MB > {RSS_WARN_MB}MB")
                write_heartbeat("memory_warn", rss_mb=rss)

    threading.Thread(target=_loop, daemon=True, name="hitl-heartbeat").start()


# ---------------------------------------------------------------- 飞书出站

def _token() -> str:
    return _get_cached_token(FEISHU_APP_ID, FEISHU_APP_SECRET)


def patch_card(message_id: str, card: dict) -> bool:
    """PATCH 更新应用已发出的卡片。

    注意: 必须是 PATCH /im/v1/messages/{id} (更新消息卡片);
    PUT 是「编辑消息」, 只支持 text/post, 对 interactive 会失败。
    """
    if not message_id:
        return False
    token = _token()
    if not token:
        return False
    try:
        resp = requests.patch(
            FEISHU_API_BASE + "/im/v1/messages/" + message_id,
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"},
            json={"content": json.dumps(card, ensure_ascii=False)},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            return True
        logger.warning(f"PATCH 卡片失败 message_id={message_id}: "
                       f"code={data.get('code')} msg={data.get('msg')}")
    except Exception as e:
        logger.warning(f"PATCH 卡片异常 message_id={message_id}: {e}")
    return False


# ---------------------------------------------------------------- 状态机

@contextlib.contextmanager
def hitl_session():
    """每次回调开一条短命连接。

    回调是人点按钮触发的稀疏事件, 不值得为它常驻一条 SQLite 连接;
    短命连接同时规避长驻连接被回收后的 closed-database 类故障。
    """
    db = Database()
    try:
        yield HumanInLoop(db)
    finally:
        conn = getattr(db, "_conn", None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def _toast(kind: str, content: str) -> P2CardActionTriggerResponse:
    resp = P2CardActionTriggerResponse()
    resp.toast = CallBackToast({"type": kind, "content": content})
    return resp


def _approver_allowlist() -> set[str]:
    """HITL_APPROVER_OPEN_IDS: 逗号分隔的飞书 open_id / user_id 白名单。"""
    return {x.strip() for x in os.environ.get("HITL_APPROVER_OPEN_IDS", "").split(",") if x.strip()}


_ALLOWLIST_WARNED = False


def _approver_allowed(operator) -> bool:
    """白名单未配置时默认拒绝 (fail-closed); 配置后只认名单内的人。

    确需放开 (单人私聊等) 时显式设 HITL_ALLOW_ANY_APPROVER=1。
    """
    global _ALLOWLIST_WARNED
    allow = _approver_allowlist()
    if not allow:
        if os.environ.get("HITL_ALLOW_ANY_APPROVER", "").strip().lower() in ("1", "true", "yes"):
            if not _ALLOWLIST_WARNED:
                logger.warning("HITL_ALLOW_ANY_APPROVER=1: 任何能点到卡片的人都可审批")
                _ALLOWLIST_WARNED = True
            return True
        if not _ALLOWLIST_WARNED:
            logger.error("HITL_APPROVER_OPEN_IDS 未配置: 审批一律拒绝 (配置白名单, "
                         "或显式设 HITL_ALLOW_ANY_APPROVER=1)")
            _ALLOWLIST_WARNED = True
        return False
    if operator is None:
        return False
    ids = {getattr(operator, "open_id", None), getattr(operator, "user_id", None)}
    return bool(ids & allow)


def _review_expired(review: dict) -> bool:
    """待审批单超过 HITL_REVIEW_TTL_MIN 分钟 (默认 1440, 0=不限) 不再接受批准。"""
    try:
        ttl = int(os.environ.get("HITL_REVIEW_TTL_MIN", "1440"))
    except ValueError:
        ttl = 1440
    if ttl <= 0:
        return False
    created = review.get("created_at")
    if not created:
        return False
    try:
        import datetime as _dt
        ts = _dt.datetime.fromisoformat(str(created).replace("T", " "))
    except ValueError:
        return False
    return (_dt.datetime.now() - ts).total_seconds() > ttl * 60


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """卡片按钮回调 — 幂等: 状态转换靠 WHERE status='pending' 收口"""
    t0 = time.time()
    try:
        event = data.event
        if event is None or event.action is None:
            return _toast("error", "回调数据为空")

        value = event.action.value or {}
        action_type = str(value.get("action") or "")
        review_id_raw = value.get("review_id")

        if action_type not in ("approve", "reject", "veto") or review_id_raw in (None, ""):
            logger.warning(f"卡片回调参数无效: {value}")
            write_heartbeat("callback_invalid", value=str(value)[:200])
            return _toast("error", "回调参数无效")

        try:
            review_id = int(review_id_raw)
        except (TypeError, ValueError):
            return _toast("error", f"review_id 非法: {review_id_raw}")

        # 审批人: user_id 优先 (可读), 退 open_id
        operator = event.operator
        reviewer = "unknown"
        if operator is not None:
            reviewer = operator.user_id or operator.open_id or "unknown"
        if not _approver_allowed(operator):
            logger.warning(f"非白名单审批人尝试操作 review_id={review_id}: {reviewer}")
            write_heartbeat("callback_forbidden", review_id=review_id, reviewer=str(reviewer)[:64])
            return _toast("error", "无审批权限")

        # 原卡片 message_id: 回调自带最可靠, 落库值作兜底
        message_id = event.context.open_message_id if event.context else ""

        with hitl_session() as hitl:
            review = hitl.get(review_id)
            if not review:
                logger.warning(f"卡片回调指向不存在的 review_id={review_id}")
                return _toast("error", f"审批单 {review_id} 不存在")

            stored_mid = review.get("message_id") or ""
            if stored_mid and message_id and stored_mid != message_id:
                logger.warning(f"review_id={review_id} 回调卡片与落库卡片不一致, 拒绝")
                return _toast("error", "卡片与审批单不匹配")
            if action_type == "approve" and _review_expired(review):
                return _toast("error", "审批单已过期, 请重新发起")

            code = review.get("code") or ""
            decision_type = review.get("decision_type") or ""

            if action_type == "approve":
                ok = hitl.approve(review_id, reviewer=reviewer, note="")
            elif action_type == "reject":
                ok = hitl.reject(review_id, reviewer=reviewer, note="")
            else:  # veto: 事后否决已自动批准的记录
                ok = hitl.veto(review_id, reviewer=reviewer, note="人工否决")

            if not ok:
                # 已被另一通道/重复点击处理 — 幂等返回, 不改台账
                current = (hitl.get(review_id) or {}).get("status", "?")
                logger.info(f"review_id={review_id} 已处理 (status={current}), 跳过")
                write_heartbeat("callback_noop", review_id=review_id,
                                status=current, action=action_type)
                return _toast("info", f"该审批已处理 (当前: {current})")

            consumed = bool(review.get("consumed"))
            summary = build_signal_summary(
                code, decision_type,
                float(review.get("score") or 0),
                float(review.get("confidence") or 0),
                review.get("reason") or "",
            )
            if not message_id:
                message_id = review.get("message_id") or ""

        # 库外做网络 IO: 不让 HTTP 往返占着 SQLite 连接
        write_bitable_ledger(review_id, code, decision_type, action_type,
                             reviewer, "", "ws_callback", summary,
                             reviewed_at=bj_now())

        veto_effect = ""
        if action_type == "veto":
            veto_effect = _apply_veto_side_effects(code, review_id, consumed)

        patched = patch_card(
            message_id,
            build_result_card(code, decision_type, action_type, reviewer, bj_now()),
        )

        latency_ms = int((time.time() - t0) * 1000)
        write_heartbeat("callback", review_id=review_id, action=action_type,
                        reviewer=reviewer, card_patched=patched, latency_ms=latency_ms,
                        **({"veto_effect": veto_effect} if veto_effect else {}))
        logger.info(f"审批完成 review_id={review_id} action={action_type} "
                    f"reviewer={reviewer} patched={patched} latency={latency_ms}ms"
                    + (f" veto_effect={veto_effect}" if veto_effect else ""))

        toasts = {"approve": "已批准", "reject": "已拒绝", "veto": "已否决"}
        return _toast("success", toasts.get(action_type, "已处理"))

    except Exception as e:
        logger.error(f"卡片回调处理异常: {e}\n{traceback.format_exc()}")
        write_heartbeat("callback_error", error=str(e)[:300])
        return _toast("error", "处理失败, 详见 daemon 日志")


def _apply_veto_side_effects(code: str, review_id: int, consumed: bool) -> str:
    """Phase 1.6 否决三态联动 (状态翻转已在 hitl.veto 完成):

    * consumed=0: 无联动 — consume_approved 只认 status='approved', 翻转即拦下。
    * consumed=1 且 stock_pool=pending_entry: 撤入场回 watch (cancel_entry 语义)。
    * consumed=1 且 active: mark_exit_trigger 走既有 T+1 结算, 不发明新结算路径。
    * 其余状态 (exit_pending/exited/watch): 如实记录, 不动。
    联动失败不抛 — 状态翻转已生效, 联动缺口留 warning + 心跳可查。
    """
    if not consumed:
        return "not_consumed"
    try:
        from execution_layer.core.action_record import ActionRecord
        from execution_layer.core.stock_pool import StockPool

        db = Database()
        try:
            pool = StockPool(db)
            current = pool.get(code)
            if not current:
                return "no_pool_row"
            status = current.get("status")
            ar = ActionRecord(db)
            if status == "pending_entry":
                pool.update_status(code, "watch")
                ar.write(code=code, action_type="cancel_entry",
                         reason=f"人工否决自动批准 (review_id={review_id})",
                         source="manual")
                return "entry_cancelled"
            if status == "active":
                pool.mark_exit_trigger(
                    code, current.get("last_price") or 0,
                    f"人工否决自动批准 (review_id={review_id})")
                return "exit_triggered"
            return f"pool_status_{status}"
        finally:
            conn = getattr(db, "_conn", None)
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
    except Exception as e:
        logger.warning(f"否决联动失败 review_id={review_id} code={code}: {e}")
        return "side_effect_error"

# ---------------------------------------------------------------- 主循环

def build_handler() -> EventDispatcherHandler:
    """长连接模式下 encrypt_key/verification_token 留空 (握手已鉴权)"""
    return (
        EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )


def main() -> int:
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        logger.error("FEISHU_APP_ID / FEISHU_APP_SECRET 未设置, 退出")
        write_heartbeat("fatal", reason="missing_credentials")
        return 1

    logger.info("HITL ws daemon 启动")
    # [:9] 区分新旧 app (cli_aab vs cli_aa8); [:6] 都是 cli_aa 分不清
    write_heartbeat("start", app_id_prefix=FEISHU_APP_ID[:9] + "***")

    # 启动真写探测 (教训#4): 能读证明不了能写 — 91403 事故里新 app 有
    # 查看权限没编辑权限, 台账静默断链 13 行。这里启动时对台账表真写一行
    # 探针再立即删除, 权限缺 (403/permission) 直接红牌退出, 不带病上岗。
    # "未配置台账"同样红牌: Phase 1.6 起台账是验收过的标配, 配置回退成
    # 跳过只会把断链从"权限死"变成"配置死", 同样不可见。
    probe = ledger_selfcheck()
    write_heartbeat("startup_probe", ok=probe["ok"], detail=probe["detail"][:300])
    if not probe["ok"]:
        logger.error("台账写探测失败, daemon 拒绝带病上岗: %s", probe["detail"])
        return 1
    logger.info("台账写探测通过: %s", probe["detail"])

    _start_heartbeat_thread()

    handler = build_handler()

    # SDK 内置自动重连; 外层守护进程级异常, systemd Restart=always 兜最后一层
    while True:
        try:
            client = WsClient(
                FEISHU_APP_ID,
                FEISHU_APP_SECRET,
                event_handler=handler,
                log_level=LogLevel.INFO,
                auto_reconnect=True,
            )
            # SDK 暴露的重连生命周期观察点 — 接进心跳流
            client.on_reconnecting = lambda: write_heartbeat("reconnecting")
            client.on_reconnected = lambda: write_heartbeat("reconnected")

            logger.info("ws.Client 启动, 等待飞书网关连接")
            write_heartbeat("connecting")
            client.start()   # 阻塞
            # 正常返回也视为掉线, 重建
            write_heartbeat("client_exited")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("收到 SIGINT, 退出")
            write_heartbeat("shutdown", reason="sigint")
            return 0
        except Exception as e:
            logger.error(f"ws.Client 异常: {e}\n{traceback.format_exc()}")
            write_heartbeat("client_error", error=str(e)[:300])
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
