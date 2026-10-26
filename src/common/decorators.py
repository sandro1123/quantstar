import atexit
import concurrent.futures
from functools import wraps

from common.errors import ErrorSeverity, QuantStarError
from common.logging import get_logger

_BACKGROUND_MAX_WORKERS = 8
_background_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_BACKGROUND_MAX_WORKERS,
    thread_name_prefix="timeout_bg",
)
_TIMED_OUT_FUTURES: set[concurrent.futures.Future] = set()


def _cleanup_ghost_futures():
    """解释器退出前等待已超时但仍在运行的 ghost 任务完成, 避免 DB 写入中途丢失。

    旧实现 shutdown(wait=False, cancel_futures=True) 会在进程退出时掐断 ghost 线程,
    导致其内部嵌套提交 (如 update_factor_daily) 报 "cannot schedule new futures",
    factor_daily 数据静默丢失 (2026-08-17 事故根因之一)。现改为有界等待。
    """
    if not _TIMED_OUT_FUTURES:
        return
    running = {f for f in _TIMED_OUT_FUTURES if f.running()}
    if running:
        get_logger("timeout").warning(f"等待 {len(running)} 个 ghost 任务完成后再退出 (最多 300s)")
        _, not_done = concurrent.futures.wait(running, timeout=300)
        if not_done:
            get_logger("timeout").error(f"{len(not_done)} 个 ghost 任务 300s 内未完成, 强制退出 (数据可能丢失)")
    _background_executor.shutdown(wait=False, cancel_futures=True)
    _TIMED_OUT_FUTURES.clear()


def reset_timeout_executor():
    """超时后/关闭后重建 executor — 防止解释器 partial shutdown 后无法提交"""
    global _background_executor
    if _background_executor._shutdown:
        _background_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_BACKGROUND_MAX_WORKERS, thread_name_prefix="timeout_bg"
        )


atexit.register(_cleanup_ghost_futures)


def catch_quantstar_error(layer_name: str, fallback_return=None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            getattr(args[0], '_trace_id', '') if args and hasattr(args[0], '_trace_id') else ''
            try:
                return func(*args, **kwargs)
            except QuantStarError as e:
                get_logger(layer_name).error(f"QuantStarError in {func.__name__}: {e}")
                return fallback_return
            except Exception as e:
                get_logger(layer_name).error(f"Unexpected error in {func.__name__}: {e}")
                return fallback_return
        return wrapper
    return decorator


def with_timeout(timeout_s: int, fallback_return=None, raise_on_timeout: bool = False, cancel_on_timeout: bool = False,
                 config_key: str | None = None):
    """B9 knife2: config_key 给定时，调用期读配置预算（延迟 import 防循环：
    config_layer 反向依赖本模块）。无键/非法值回落 timeout_s 字面量。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            trace_id = getattr(args[0], '_trace_id', '') if args and hasattr(args[0], '_trace_id') else ''
            budget = timeout_s
            if config_key is not None:
                try:
                    from config_layer import cfg_get
                    budget = cfg_get(config_key, timeout_s)
                except Exception:
                    budget = timeout_s
                try:
                    budget = int(budget)
                    if budget <= 0:
                        budget = timeout_s
                except (TypeError, ValueError):
                    budget = timeout_s
            # 若上次 ghost/shutdown 导致 executor 不可用，重建
            reset_timeout_executor()
            try:
                future = _background_executor.submit(func, *args, **kwargs)
            except (RuntimeError, concurrent.futures.BrokenExecutor) as e:
                # 解释器关闭期间 executor 不再接受新任务（cannot schedule new futures）。
                # 注意: 这是 concurrent.futures 的模块级 _shutdown 标志, reset_timeout_executor
                # 重建新实例也无法绕过。降级为同步内联执行: 调用方(ghost)线程的 DB 连接仍存活,
                # sqlite3 在解释器关闭期间仍可写入, 避免嵌套写库 (如 update_factor_daily)
                # 静默丢数据 (2026-08-17 事故根因)。内联执行失去超时保护, 但优于静默丢失。
                get_logger("timeout").warning(
                    f"{func.__name__} submit failed ({e}); 降级为同步内联执行以避免数据丢失"
                )
                try:
                    return func(*args, **kwargs)
                except Exception as inline_err:
                    get_logger("timeout").error(
                        f"{func.__name__} inline fallback also failed: {inline_err}"
                    )
                    if raise_on_timeout:
                        raise QuantStarError(
                            layer="timeout",
                            severity=ErrorSeverity.WARNING,
                            trace_id=trace_id,
                            recoverable=True,
                        ) from None
                    return fallback_return
            try:
                return future.result(timeout=budget)
            except concurrent.futures.TimeoutError:
                get_logger("timeout").error(f"{func.__name__} timed out after {budget}s")
                if future.running():
                    # 线程已开始运行则 cancel() 无效：记录为 ghost work，避免静默丢数据
                    _TIMED_OUT_FUTURES.add(future)
                    get_logger("timeout").warning(
                        f"{func.__name__} 超时但任务已在后台线程继续运行 (ghost work)"
                    )
                elif cancel_on_timeout:
                    cancelled = future.cancel()
                    get_logger("timeout").warning(
                        f"{func.__name__} 超时后取消: {'成功' if cancelled else '失败(线程仍在运行)'}"
                    )
                if raise_on_timeout:
                    raise QuantStarError(
                        layer="timeout",
                        severity=ErrorSeverity.WARNING,
                        trace_id=trace_id,
                        recoverable=True,
                    ) from None
                return fallback_return
        return wrapper
    return decorator
