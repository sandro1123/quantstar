"""QuantStar Web API — 健康检查/快照/持仓/监控"""
import datetime as _dt
import hmac
import logging
import os
import re

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
_log = logging.getLogger(__name__)

_app_instance = None

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _api_token() -> str:
    return os.environ.get("QS_API_TOKEN", "")


def _get_app():
    global _app_instance
    if _app_instance is None:
        from bootstrap import bootstrap_app
        _app_instance = bootstrap_app()
    return _app_instance


_PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP")


def _require_auth():
    """Bearer token 鉴权。

    未配置 QS_API_TOKEN 时只允许真正的本机直连: 带反向代理转发头的请求一律拒绝,
    因为经 cloudflared / nginx 转发进来的外网请求 remote_addr 也是 127.0.0.1。
    """
    token = _api_token()
    if token:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if hmac.compare_digest(auth.encode(), expected.encode()):
            return None
        return jsonify({"error": "unauthorized"}), 401
    if any(request.headers.get(h) for h in _PROXY_HEADERS):
        return jsonify({"error": "forbidden: set QS_API_TOKEN when serving behind a proxy"}), 403
    if request.remote_addr in ("127.0.0.1", "::1"):
        return None
    return jsonify({"error": "forbidden"}), 403


def _cors_origins() -> set[str]:
    """显式信任的浏览器来源 (QS_CORS_ORIGINS, 逗号分隔); 默认不开跨域。"""
    return {
        origin.strip()
        for origin in os.environ.get("QS_CORS_ORIGINS", "").split(",")
        if origin.strip()
    }


@app.after_request
def _cors_headers(resp):
    origin = request.headers.get("Origin")
    if origin and origin in _cors_origins():
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.vary.add("Origin")
    return resp


def _internal_error(exc: Exception, where: str):
    _log.exception("web api error in %s", where)
    return jsonify({"status": "error", "message": "internal error"}), 500


@app.before_request
def _auth_wrapper():
    if request.method == "OPTIONS":
        return None
    return _require_auth()


@app.route("/health")
def health():
    try:
        app_qs = _get_app()
        if hasattr(app_qs, "ops") and app_qs.ops:
            report = app_qs.ops.check_health()
            return jsonify({
                "status": report.overall_status,
                "layers": [{"name": layer.layer, "status": layer.status, "summary": layer.summary} for layer in report.layers],
                "date": report.date,
            })
        return jsonify({"status": "error", "message": "Ops not initialized"}), 500
    except Exception as e:
        return _internal_error(e, "health")


@app.route("/metrics")
def metrics():
    """Prometheus 格式的指标端点"""
    try:
        from common.database import Database
        conn = Database().get_connection()
        klines = conn.execute("SELECT COUNT(*) FROM daily_klines").fetchone()[0]
        factors = conn.execute("SELECT COUNT(*) FROM factor_daily").fetchone()[0]
        db_size = os.path.getsize(Database().db_path)
        total_runs = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        failed_runs = conn.execute("SELECT COUNT(*) FROM pipeline_runs WHERE status='failed'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM stock_pool WHERE status='pending_entry'").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM stock_pool WHERE status='active'").fetchone()[0]

        lines = [
            "# HELP quantstar_klines_total Total number of kline records",
            "# TYPE quantstar_klines_total gauge",
            f"quantstar_klines_total {klines}",
            "# HELP quantstar_factors_total Total number of factor records",
            "# TYPE quantstar_factors_total gauge",
            f"quantstar_factors_total {factors}",
            "# HELP quantstar_db_size_bytes Database file size",
            "# TYPE quantstar_db_size_bytes gauge",
            f"quantstar_db_size_bytes {db_size}",
            "# HELP quantstar_pipeline_runs_total Total pipeline runs",
            "# TYPE quantstar_pipeline_runs_total counter",
            f"quantstar_pipeline_runs_total {total_runs}",
            "# HELP quantstar_pipeline_failed_total Failed pipeline runs",
            "# TYPE quantstar_pipeline_failed_total counter",
            f"quantstar_pipeline_failed_total {failed_runs}",
            "# HELP quantstar_pipeline_success_rate Pipeline success rate",
            "# TYPE quantstar_pipeline_success_rate gauge",
            f"quantstar_pipeline_success_rate {(total_runs - failed_runs) / max(total_runs, 1) * 100:.1f}",
            "# HELP quantstar_stock_pool_pending Pending entry stocks",
            "# TYPE quantstar_stock_pool_pending gauge",
            f"quantstar_stock_pool_pending {pending}",
            "# HELP quantstar_stock_pool_active Active positions",
            "# TYPE quantstar_stock_pool_active gauge",
            f"quantstar_stock_pool_active {active}",
        ]
        return Response("\n".join(lines) + "\n", mimetype="text/plain")
    except Exception:
        _log.exception("web api error in metrics")
        return Response("# Error: internal error\n", status=500, mimetype="text/plain")


@app.route("/api/snapshot/<date>")
def snapshot(date: str):
    if not _DATE_RE.match(date):
        return jsonify({"error": "invalid date, expected YYYY-MM-DD"}), 400
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        return jsonify({"error": "invalid date, not a real calendar date"}), 400
    try:
        app_qs = _get_app()
        if hasattr(app_qs, "data") and app_qs.data:
            snap = app_qs.data.snapshot(date)
            return jsonify({
                "date": date,
                "klines": len(snap.klines) if hasattr(snap, "klines") and snap.klines else 0,
                "valuations": len(snap.valuations) if hasattr(snap, "valuations") and snap.valuations else 0,
                "financials": len(snap.financials) if hasattr(snap, "financials") and snap.financials else 0,
                "is_ready": getattr(snap, "readiness", None) is not None,
            })
        return jsonify({"error": "Data layer not available"}), 500
    except Exception as e:
        return _internal_error(e, request.path)


@app.route("/api/portfolio")
def portfolio():
    try:
        app_qs = _get_app()
        if hasattr(app_qs, "execution") and app_qs.execution:
            snap = app_qs.execution.get_portfolio_snapshot()
            return jsonify({
                "active": snap.active,
                "pending_entry": snap.pending_entry,
                "pending_exit": snap.pending_exit,
                "watch": snap.watch,
                "exited": snap.exited,
                "total_invested": snap.total_invested,
                "total_pnl": snap.total_pnl,
                "total_pnl_pct": snap.total_pnl_pct,
                "portfolio_nav": snap.portfolio_nav,
                "today_pnl": snap.today_pnl,
                "date": snap.date,
            })
        return jsonify({"error": "Execution layer not available"}), 500
    except Exception as e:
        return _internal_error(e, request.path)


@app.route("/api/positions")
def positions():
    try:
        app_qs = _get_app()
        if hasattr(app_qs, "execution") and app_qs.execution:
            active = app_qs.execution.stock_pool.get_by_status("active")
            return jsonify({
                "count": len(active),
                "positions": [{
                    "code": p["code"],
                    "entry_price": p.get("entry_price", 0),
                    "last_price": p.get("last_price", 0),
                    "pnl_pct": p.get("unrealized_pnl_pct", 0),
                    "holding_days": p.get("holding_days", 0),
                    "stop_loss": p.get("stop_loss", 0),
                    "score": p.get("current_score", 0),
                } for p in active[:50]],
            })
        return jsonify({"error": "Execution layer not available"}), 500
    except Exception as e:
        return _internal_error(e, request.path)


def start(host: str = "127.0.0.1", port: int = 8512, debug: bool = False):
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    start()
