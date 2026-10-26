from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from data_layer.valuation_dual_read import (
    DualReadMismatchError,
    DualReadResult,
    ValuationDualReadObserver,
    ValuationQuery,
    observer_from_config,
)


class ValuationDualReadService:
    """Lifecycle and health bridge for the optional valuation dual-read observer."""

    def __init__(
        self,
        observer: ValuationDualReadObserver,
        startup_queries: Iterable[ValuationQuery] = (),
    ) -> None:
        self.observer = observer
        self.startup_queries = tuple(startup_queries)
        self._lock = threading.RLock()
        self._started = False
        self._preflight: dict[str, Any] | None = None
        self._results: list[DualReadResult] = []
        self._error: dict[str, str] | None = None
        self._event_paths: list[str] = []

    @property
    def failure_policy(self) -> str:
        return self.observer.config.failure_policy

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._started:
                return self.metadata()
            try:
                preflight = self.observer.preflight()
                self._preflight = {
                    "status": preflight["status"],
                    "shadow_sha256": preflight["shadow_sha256"],
                    "elapsed_ms": preflight["elapsed_ms"],
                }
                self._results = self.observer.compare_many(self.startup_queries)
                self._error = None
                self._started = True
                self._record_event("startup", self.metadata())
                return self.metadata()
            except Exception as exc:
                self._error = {"error_type": type(exc).__name__, "error": str(exc)}
                self._started = True
                self._record_event("startup-failure", self.metadata())
                if self.failure_policy == "closed":
                    raise
                return self.metadata()

    def compare(self, query: ValuationQuery) -> DualReadResult:
        with self._lock:
            if not self._started or self._preflight is None or self._error is not None:
                raise DualReadMismatchError("valuation dual-read service is not ready")
            result = self.observer.compare(query)
            self._results.append(result)
            return result

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            summary = self.observer.summary(self._results)
            return {
                "enabled": True,
                "started": self._started,
                "failure_policy": self.failure_policy,
                "preflight": dict(self._preflight) if self._preflight else None,
                "summary": summary,
                "error": dict(self._error) if self._error else None,
                "startup_query_count": len(self.startup_queries),
                "event_count": len(self._event_paths),
            }

    def health(self):
        from ops_layer.core.models import HealthCheck, LayerHealth

        metadata = self.metadata()
        checks: list[HealthCheck] = []
        preflight = metadata["preflight"]
        error = metadata["error"]
        summary = metadata["summary"]
        if error:
            checks.append(
                HealthCheck(
                    name="preflight",
                    status="fail",
                    detail=f"{error['error_type']}: {error['error']}",
                )
            )
            status = "red"
        elif not metadata["started"] or preflight is None:
            checks.append(HealthCheck(name="preflight", status="fail", detail="未执行启动预检"))
            status = "red"
        else:
            checks.append(
                HealthCheck(
                    name="preflight",
                    status="pass",
                    detail=(
                        f"影子库已绑定 {preflight['shadow_sha256'][:12]}... "
                        f"({preflight['elapsed_ms']:.3f}ms)"
                    ),
                )
            )
            blocking = int(summary["blocking"])
            if blocking:
                checks.append(
                    HealthCheck(
                        name="startup_queries",
                        status="warn" if self.failure_policy == "open" else "fail",
                        detail=f"启动探针 {summary['total']} 项，差异/失败 {blocking} 项",
                        metric=float(blocking),
                        threshold=0.0,
                    )
                )
                status = "yellow" if self.failure_policy == "open" else "red"
            else:
                checks.append(
                    HealthCheck(
                        name="startup_queries",
                        status="pass",
                        detail=f"启动探针 {summary['total']} 项，无阻断结果",
                        metric=0.0,
                        threshold=0.0,
                    )
                )
                status = "green"
        return LayerHealth(
            layer="valuation_dual_read",
            status=status,
            checks=checks,
            summary=(
                "估值双读观察器已就绪"
                if status == "green"
                else "估值双读观察器存在预期差异" if status == "yellow" else "估值双读观察器未就绪"
            ),
        )

    def shutdown(self, timeout: float = 5.0) -> bool:
        return True

    def _record_event(self, event: str, metadata: dict[str, Any]) -> None:
        directory = self.observer.config.evidence_dir
        if directory is None:
            return
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "event": event,
            "recorded_at_ns": time.time_ns(),
            "metadata": metadata,
        }
        text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        target = directory / f"dual-read-service-{payload['recorded_at_ns']}-{event}.json"
        fd, temporary = tempfile.mkstemp(prefix=".dual-read-service-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                raise FileExistsError(target)
            os.link(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        self._event_paths.append(str(target))


def service_from_config(
    formal_db: str | Path,
    config: Any,
    prefix: str = "data.valuation_dual_read",
) -> ValuationDualReadService | None:
    observer = observer_from_config(formal_db, config, prefix=prefix)
    if observer is None:
        return None
    return ValuationDualReadService(observer, _startup_queries_from_config(config, prefix))


def _startup_queries_from_config(config: Any, prefix: str) -> tuple[ValuationQuery, ...]:
    codes_value = config.get(f"{prefix}.probe_codes", [])
    if isinstance(codes_value, str):
        codes = tuple(part.strip() for part in codes_value.split(",") if part.strip())
    else:
        codes = tuple(str(value).strip() for value in codes_value)
    if not codes:
        return ()

    kinds_value = config.get(f"{prefix}.probe_kinds", ["latest", "pit"])
    if isinstance(kinds_value, str):
        kinds = tuple(part.strip() for part in kinds_value.split(",") if part.strip())
    else:
        kinds = tuple(str(value).strip() for value in kinds_value)
    as_of_date = config.get(f"{prefix}.probe_as_of_date")
    start_date = config.get(f"{prefix}.probe_start_date")
    end_date = config.get(f"{prefix}.probe_end_date")

    queries: list[ValuationQuery] = []
    for code in codes:
        for kind in kinds:
            if kind == "range":
                if not start_date or not end_date:
                    raise ValueError("range startup probes require probe_start_date and probe_end_date")
                queries.append(ValuationQuery("range", code, str(start_date), str(end_date)))
            elif kind in ("latest", "pit"):
                if not as_of_date:
                    raise ValueError(f"{kind} startup probes require probe_as_of_date")
                queries.append(ValuationQuery(kind, code, as_of_date=str(as_of_date)))
            else:
                raise ValueError(f"unsupported startup probe kind: {kind!r}")
    return tuple(queries)
