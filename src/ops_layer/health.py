"""ops_layer/health.py — 健康检查聚合（带超时控制）"""

import concurrent.futures

from common.logging import get_logger
from common.utils import bj_date
from ops_layer.core.models import AttentionItem, LayerHealth, OpsReport

logger = get_logger("ops.health")

TIMEOUT_PER_LAYER = 5  # 每层健康检查最多 5 秒（聚合超时 = 层数 × 每层）


class OpsHealth:
    def __init__(self, layers: dict[str, object]):
        self.layers = layers

    def check_all(self) -> OpsReport:
        today = bj_date()
        layer_healths = []
        needs_attention = []

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(self.layers) or 1)
        future_map = {
            executor.submit(self._check_layer, name, layer): name
            for name, layer in self.layers.items()
        }
        pending = set(future_map)
        try:
            completed = concurrent.futures.as_completed(
                future_map,
                timeout=TIMEOUT_PER_LAYER * len(self.layers),
            )
            for future in completed:
                pending.discard(future)
                name = future_map[future]
                try:
                    h = future.result()
                    if not isinstance(h, LayerHealth):
                        raise TypeError(f"health() returned {type(h).__name__}, expected LayerHealth")
                    layer_healths.append(h)
                    if h.status in ("red", "yellow"):
                        needs_attention.append(AttentionItem(
                            severity="critical" if h.status == "red" else "warning",
                            layer=name,
                            message=h.summary or f"{name} health is {h.status}",
                        ))
                except Exception as e:
                    layer_healths.append(self._failed_health(name, e))
                    needs_attention.append(AttentionItem(
                        severity="critical", layer=name,
                        message=f"健康检查异常: {e}",
                    ))
        except concurrent.futures.TimeoutError:
            for future in pending:
                name = future_map[future]
                future.cancel()
                layer_healths.append(LayerHealth(
                    layer=name, status="red", checks=[],
                    summary=f"健康检查超时 (> {TIMEOUT_PER_LAYER}s)",
                ))
                needs_attention.append(AttentionItem(
                    severity="critical", layer=name,
                    message="健康检查超时",
                ))
        finally:
            # Do not wait for a broken probe after its deadline; otherwise health() can hang forever.
            executor.shutdown(wait=False, cancel_futures=True)

        overall = "green"
        for h in layer_healths:
            if h.status == "red":
                overall = "red"
                break
            elif h.status == "yellow":
                overall = "yellow"

        return OpsReport(date=today, overall_status=overall, layers=layer_healths, needs_attention=needs_attention)

    @staticmethod
    def _failed_health(name: str, error: Exception) -> LayerHealth:
        return LayerHealth(
            layer=name,
            status="red",
            checks=[],
            summary=f"健康检查失败: {error}",
        )

    def _check_layer(self, name: str, layer: object) -> LayerHealth:
        if layer is None:
            return LayerHealth(layer=name, status="red", checks=[], summary="层未初始化")
        if not hasattr(layer, "health") or not callable(layer.health):
            return LayerHealth(layer=name, status="red", checks=[], summary="缺少健康检查接口")
        return layer.health()
