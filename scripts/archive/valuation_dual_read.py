from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

VALUATION_COLUMNS = (
    "code",
    "date",
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "market_cap",
    "total_shares",
    "dividend_yield",
)
NUMERIC_COLUMNS = VALUATION_COLUMNS[2:]
VALUATION_SCHEMA = (
    ("code", "TEXT", 1, None, 1),
    ("date", "TEXT", 1, None, 2),
    ("pe_ttm", "REAL", 0, "0", 0),
    ("pb_mrq", "REAL", 0, "0", 0),
    ("ps_ttm", "REAL", 0, "0", 0),
    ("market_cap", "REAL", 0, "0", 0),
    ("total_shares", "REAL", 0, "0", 0),
    ("dividend_yield", "REAL", 0, None, 0),
)

QueryKind = Literal["range", "latest", "pit"]
CompareMode = Literal["sample", "full"]
FailurePolicy = Literal["open", "closed"]


class DualReadError(RuntimeError):
    """Base error for valuation dual-read checks."""


class DualReadMismatchError(DualReadError):
    """Raised when fail-closed mode observes a mismatch."""


class DualReadTimeoutError(DualReadError):
    """Raised when either read exceeds the configured timeout."""


class DualReadSchemaError(DualReadError):
    """Raised when the valuation schema is not safe to compare."""


class DualReadSafetyError(DualReadError):
    """Raised when immutable reads would be unsafe."""


@dataclass(frozen=True)
class DualReadConfig:
    enabled: bool = False
    mode: CompareMode = "sample"
    failure_policy: FailurePolicy = "open"
    sample_modulus: int = 100
    sample_buckets: tuple[int, ...] = (0,)
    timeout_seconds: float = 2.0
    preflight_timeout_seconds: float = 300.0
    relative_tolerance: float = 1e-12
    absolute_tolerance: float = 1e-12
    evidence_dir: Path | None = None
    expected_shadow_sha256: str | None = None
    max_detail_items: int = 50

    def __post_init__(self) -> None:
        if self.mode not in ("sample", "full"):
            raise ValueError(f"invalid dual-read mode: {self.mode}")
        if self.failure_policy not in ("open", "closed"):
            raise ValueError(f"invalid failure policy: {self.failure_policy}")
        if self.sample_modulus < 1:
            raise ValueError("sample_modulus must be positive")
        if not self.sample_buckets:
            raise ValueError("sample_buckets must not be empty")
        if any(bucket < 0 or bucket >= self.sample_modulus for bucket in self.sample_buckets):
            raise ValueError("sample bucket outside modulus")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.preflight_timeout_seconds <= 0:
            raise ValueError("preflight_timeout_seconds must be positive")
        if self.relative_tolerance < 0 or self.absolute_tolerance < 0:
            raise ValueError("numeric tolerances must be non-negative")
        if self.expected_shadow_sha256 is not None and (
            len(self.expected_shadow_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.expected_shadow_sha256.lower())
        ):
            raise ValueError("expected_shadow_sha256 must be a 64-character hex digest")
        if self.max_detail_items < 0:
            raise ValueError("max_detail_items must be non-negative")


@dataclass(frozen=True)
class ValuationQuery:
    kind: QueryKind
    code: str
    start_date: str | None = None
    end_date: str | None = None
    as_of_date: str | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.code.isdigit() or len(self.code) != 6:
            raise ValueError(f"invalid security code: {self.code!r}")
        if self.kind == "range":
            if not self.start_date or not self.end_date:
                raise ValueError("range query requires start_date and end_date")
            start = _canonical_date(self.start_date)
            end = _canonical_date(self.end_date)
            if start > end:
                raise ValueError("range start_date exceeds end_date")
        elif self.kind in ("latest", "pit"):
            if not self.as_of_date:
                raise ValueError(f"{self.kind} query requires as_of_date")
            _canonical_date(self.as_of_date)
        else:
            raise ValueError(f"invalid query kind: {self.kind}")

    def stable_key(self) -> str:
        return "|".join(
            str(value or "")
            for value in (self.kind, self.code, self.start_date, self.end_date, self.as_of_date)
        )


@dataclass
class DualReadResult:
    status: Literal["DISABLED", "SKIPPED", "MATCH", "MISMATCH", "ERROR", "TIMEOUT"]
    query: dict[str, Any]
    compared: bool
    mismatch_classes: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    evidence_path: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in ("DISABLED", "SKIPPED", "MATCH")


class ReadOnlySQLite:
    """SQLite adapter that rejects WAL-backed or structurally unsafe valuation inputs."""

    def __init__(self, path: str | Path):
        supplied = Path(path)
        if supplied.is_symlink():
            raise DualReadSafetyError(f"database path must not be a symlink: {supplied}")
        self.path = supplied.resolve()

    def file_state(self) -> dict[str, Any]:
        return {
            "db": self._one_file_state(self.path),
            "wal": self._one_file_state(Path(str(self.path) + "-wal")),
            "shm": self._one_file_state(Path(str(self.path) + "-shm")),
        }

    @staticmethod
    def _one_file_state(path: Path) -> dict[str, Any]:
        state: dict[str, Any] = {"path": str(path), "exists": path.exists()}
        if path.exists():
            stat = path.stat()
            state.update(size=stat.st_size, inode=stat.st_ino, mtime_ns=stat.st_mtime_ns)
        return state

    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file() or self.path.is_symlink():
            raise DualReadSafetyError(f"database path is not a regular file: {self.path}")
        wal = Path(str(self.path) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise DualReadSafetyError(f"non-empty WAL makes immutable read unsafe: {wal}")
        uri = f"file:{self.path.as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        actual = tuple(
            (row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in conn.execute("PRAGMA table_info(stock_valuation)")
        )
        if actual != VALUATION_SCHEMA:
            conn.close()
            raise DualReadSchemaError(f"stock_valuation schema mismatch: {actual!r}")
        return conn

    def query(self, query: ValuationQuery, deadline: float | None = None) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            if deadline is not None:
                conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            columns = ",".join(VALUATION_COLUMNS)
            try:
                if query.kind == "range":
                    rows = conn.execute(
                        f"SELECT {columns} FROM stock_valuation "
                        "WHERE code=? AND replace(date,'-','') BETWEEN replace(?,'-','') AND replace(?,'-','') "
                        "ORDER BY replace(date,'-',''), date",
                        (query.code, query.start_date, query.end_date),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT {columns} FROM stock_valuation "
                        "WHERE code=? AND replace(date,'-','')<=replace(?,'-','') "
                        "ORDER BY replace(date,'-','') DESC, date DESC LIMIT 1",
                        (query.code, query.as_of_date),
                    ).fetchall()
            except sqlite3.OperationalError as exc:
                if deadline is not None and time.monotonic() >= deadline and "interrupted" in str(exc).lower():
                    raise DualReadTimeoutError("SQLite query interrupted at deadline") from exc
                raise
            return [dict(row) for row in rows]
        finally:
            conn.set_progress_handler(None, 0)
            conn.close()


def _explicit_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in ("true", "1", "yes", "on"):
            return True
        if normalised in ("false", "0", "no", "off", ""):
            return False
    raise ValueError(f"invalid explicit boolean: {value!r}")


def observer_from_config(
    formal_db: str | Path,
    config: Any,
    prefix: str = "data.valuation_dual_read",
) -> ValuationDualReadObserver | None:
    """Build the observer only when explicitly enabled; disabled mode touches no shadow path."""
    enabled = _explicit_bool(config.get(f"{prefix}.enabled", False)) if config is not None else False
    if not enabled:
        return None
    shadow_db = config.get(f"{prefix}.shadow_db")
    if not shadow_db:
        raise ValueError("enabled valuation dual-read requires shadow_db")
    buckets_value = config.get(f"{prefix}.sample_buckets", [0])
    if isinstance(buckets_value, str):
        buckets = tuple(int(part.strip()) for part in buckets_value.split(",") if part.strip())
    else:
        buckets = tuple(int(value) for value in buckets_value)
    evidence_value = config.get(f"{prefix}.evidence_dir")
    observer_config = DualReadConfig(
        enabled=True,
        mode=str(config.get(f"{prefix}.mode", "sample")),
        failure_policy=str(config.get(f"{prefix}.failure_policy", "open")),
        sample_modulus=int(config.get(f"{prefix}.sample_modulus", 100)),
        sample_buckets=buckets,
        timeout_seconds=float(config.get(f"{prefix}.timeout_seconds", 2.0)),
        preflight_timeout_seconds=float(config.get(f"{prefix}.preflight_timeout_seconds", 300.0)),
        relative_tolerance=float(config.get(f"{prefix}.relative_tolerance", 1e-12)),
        absolute_tolerance=float(config.get(f"{prefix}.absolute_tolerance", 1e-12)),
        evidence_dir=Path(evidence_value) if evidence_value else None,
        expected_shadow_sha256=config.get(f"{prefix}.shadow_sha256"),
        max_detail_items=int(config.get(f"{prefix}.max_detail_items", 50)),
    )
    return ValuationDualReadObserver(formal_db, shadow_db, observer_config)


class ValuationDualReadObserver:
    def __init__(
        self,
        formal_db: str | Path,
        shadow_db: str | Path,
        config: DualReadConfig | None = None,
    ):
        self.formal = ReadOnlySQLite(formal_db)
        self.shadow = ReadOnlySQLite(shadow_db)
        if self.formal.path == self.shadow.path:
            raise DualReadSafetyError("formal and shadow databases must be different files")
        self.config = config or DualReadConfig()
        if self.config.enabled and not self.config.expected_shadow_sha256:
            raise ValueError("enabled dual-read requires expected_shadow_sha256")
        self._shadow_hash_verified = False
        self._verified_shadow_state: dict[str, Any] | None = None

    def should_compare(self, query: ValuationQuery) -> bool:
        if not self.config.enabled:
            return False
        if self.config.mode == "full":
            return True
        digest = hashlib.sha256(query.stable_key().encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") % self.config.sample_modulus
        return bucket in self.config.sample_buckets

    def preflight(self) -> dict[str, Any]:
        """Pin the shadow before request handling under one interruptible deadline."""
        started = time.monotonic()
        deadline = started + self.config.preflight_timeout_seconds
        formal_before = self.formal.file_state()
        shadow_before = self.shadow.file_state()
        for adapter in (self.formal, self.shadow):
            self._check_preflight_deadline(deadline, f"before quick_check: {adapter.path}")
            connection = adapter.connect()
            try:
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                try:
                    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
                except sqlite3.OperationalError as exc:
                    if time.monotonic() >= deadline and "interrupted" in str(exc).lower():
                        raise DualReadTimeoutError(
                            f"dual-read preflight exceeded {self.config.preflight_timeout_seconds:.3f}s "
                            f"during quick_check: {adapter.path}"
                        ) from exc
                    raise
                if quick != "ok":
                    raise DualReadSafetyError(f"quick_check failed: {adapter.path}")
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
        expected = str(self.config.expected_shadow_sha256).lower()
        digest = hashlib.sha256()
        with self.shadow.path.open("rb") as handle:
            while True:
                self._check_preflight_deadline(deadline, "during shadow SHA verification")
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise DualReadSafetyError(
                f"shadow SHA mismatch: expected={expected} actual={actual}"
            )
        formal_after = self.formal.file_state()
        shadow_after = self.shadow.file_state()
        if formal_after != formal_before:
            raise DualReadSafetyError("formal file state changed during preflight")
        if shadow_after != shadow_before:
            raise DualReadSafetyError("shadow file state changed during preflight")
        self._verified_shadow_state = shadow_after
        self._shadow_hash_verified = True
        return {
            "status": "PASS_DUAL_READ_PREFLIGHT",
            "shadow_sha256": actual,
            "formal_state": formal_after,
            "shadow_state": shadow_after,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        }

    def _check_preflight_deadline(self, deadline: float, stage: str) -> None:
        if time.monotonic() >= deadline:
            raise DualReadTimeoutError(
                f"dual-read preflight exceeded {self.config.preflight_timeout_seconds:.3f}s {stage}"
            )

    def _require_preflight(self) -> None:
        if not self._shadow_hash_verified or self._verified_shadow_state is None:
            raise DualReadSafetyError("dual-read preflight has not completed")
        if self.shadow.file_state() != self._verified_shadow_state:
            raise DualReadSafetyError("shadow file state changed after preflight")

    def compare(self, query: ValuationQuery) -> DualReadResult:
        started = time.monotonic()
        if not self.config.enabled:
            return DualReadResult("DISABLED", asdict(query), compared=False)
        if not self.should_compare(query):
            return DualReadResult("SKIPPED", asdict(query), compared=False)

        before = {"formal": self.formal.file_state(), "shadow": self.shadow.file_state()}
        try:
            self._require_preflight()
            formal_rows, shadow_rows = self._read_both(query)
            mismatch_classes, detail = compare_rows(
                formal_rows,
                shadow_rows,
                self.config.relative_tolerance,
                self.config.absolute_tolerance,
                self.config.max_detail_items,
            )
            status: Literal["MATCH", "MISMATCH"] = "MATCH" if not mismatch_classes else "MISMATCH"
            result = DualReadResult(
                status,
                asdict(query),
                compared=True,
                mismatch_classes=mismatch_classes,
                detail={
                    **detail,
                    "formal_row_count": len(formal_rows),
                    "shadow_row_count": len(shadow_rows),
                },
            )
        except DualReadTimeoutError as exc:
            result = DualReadResult(
                "TIMEOUT",
                asdict(query),
                compared=True,
                mismatch_classes=["timeout"],
                detail={"error": str(exc)},
            )
        except DualReadSchemaError as exc:
            result = DualReadResult(
                "ERROR",
                asdict(query),
                compared=True,
                mismatch_classes=["schema_mismatch"],
                detail={"error_type": type(exc).__name__, "error": str(exc)},
            )
        except DualReadSafetyError as exc:
            result = DualReadResult(
                "ERROR",
                asdict(query),
                compared=True,
                mismatch_classes=["unsafe_input"],
                detail={"error_type": type(exc).__name__, "error": str(exc)},
            )
        except Exception as exc:
            result = DualReadResult(
                "ERROR",
                asdict(query),
                compared=True,
                mismatch_classes=["query_error"],
                detail={"error_type": type(exc).__name__, "error": str(exc)},
            )

        after = {"formal": self.formal.file_state(), "shadow": self.shadow.file_state()}
        if before != after:
            result.status = "ERROR"
            if "source_file_changed" not in result.mismatch_classes:
                result.mismatch_classes.append("source_file_changed")
            result.detail["source_state_before"] = before
            result.detail["source_state_after"] = after
        result.elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        result.evidence_path = self._write_evidence(result)

        if not result.passed and self.config.failure_policy == "closed":
            if result.status == "TIMEOUT":
                raise DualReadTimeoutError(json.dumps(asdict(result), sort_keys=True))
            raise DualReadMismatchError(json.dumps(asdict(result), sort_keys=True))
        return result

    def compare_many(self, queries: Iterable[ValuationQuery]) -> list[DualReadResult]:
        return [self.compare(query) for query in queries]

    @staticmethod
    def summary(results: Sequence[DualReadResult]) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        class_counts: dict[str, int] = {}
        for result in results:
            status_counts[result.status] = status_counts.get(result.status, 0) + 1
            for mismatch_class in result.mismatch_classes:
                class_counts[mismatch_class] = class_counts.get(mismatch_class, 0) + 1
        blocking = sum(1 for result in results if not result.passed)
        return {
            "total": len(results),
            "compared": sum(int(result.compared) for result in results),
            "blocking": blocking,
            "ready": blocking == 0,
            "status_counts": dict(sorted(status_counts.items())),
            "mismatch_class_counts": dict(sorted(class_counts.items())),
            "max_elapsed_ms": max((result.elapsed_ms for result in results), default=0.0),
        }

    def _read_both(self, query: ValuationQuery) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        deadline = time.monotonic() + self.config.timeout_seconds
        formal_rows = self.formal.query(query, deadline=deadline)
        if time.monotonic() >= deadline:
            raise DualReadTimeoutError(
                f"dual read exceeded {self.config.timeout_seconds:.3f}s after formal read"
            )
        shadow_rows = self.shadow.query(query, deadline=deadline)
        return formal_rows, shadow_rows

    def _write_evidence(self, result: DualReadResult) -> str | None:
        directory = self.config.evidence_dir
        if directory is None or result.status in ("DISABLED", "SKIPPED"):
            return None
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        query_hash = hashlib.sha256(
            json.dumps(result.query, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        timestamp = time.time_ns()
        target = directory / f"dual-read-{timestamp}-{query_hash}.json"
        payload = json.dumps(_json_safe(asdict(result)), indent=2, sort_keys=True, allow_nan=False) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".dual-read-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
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
        return str(target)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "+Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_safe(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _canonical_date(value: str) -> str:
    text = str(value)
    formats = ("%Y-%m-%d", "%Y%m%d")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"invalid date: {value!r}")


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["date"] = _canonical_date(str(result["date"])).replace("-", "")
    return result


def _duplicate_keys(rows: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    duplicates: set[tuple[str, str]] = set()
    for row in rows:
        normal = _normalise_row(row)
        key = (str(normal["code"]), str(normal["date"]))
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return sorted(duplicates)


def compare_rows(
    formal_rows: Sequence[dict[str, Any]],
    shadow_rows: Sequence[dict[str, Any]],
    relative_tolerance: float = 1e-12,
    absolute_tolerance: float = 1e-12,
    max_detail_items: int = 50,
) -> tuple[list[str], dict[str, Any]]:
    classes: list[str] = []
    detail: dict[str, Any] = {}
    formal_duplicates = _duplicate_keys(formal_rows)
    shadow_duplicates = _duplicate_keys(shadow_rows)
    if formal_duplicates or shadow_duplicates:
        classes.append("duplicate_key")
        detail["formal_duplicate_key_count"] = len(formal_duplicates)
        detail["shadow_duplicate_key_count"] = len(shadow_duplicates)
        detail["formal_duplicate_keys"] = formal_duplicates[:max_detail_items]
        detail["shadow_duplicate_keys"] = shadow_duplicates[:max_detail_items]

    formal_map = {
        (str(row["code"]), str(row["date"]).replace("-", "")): _normalise_row(row)
        for row in formal_rows
    }
    shadow_map = {
        (str(row["code"]), str(row["date"]).replace("-", "")): _normalise_row(row)
        for row in shadow_rows
    }
    missing_in_shadow = sorted(set(formal_map) - set(shadow_map))
    extra_in_shadow = sorted(set(shadow_map) - set(formal_map))
    if missing_in_shadow:
        classes.append("missing_in_shadow")
        detail["missing_in_shadow_count"] = len(missing_in_shadow)
        detail["missing_in_shadow_digest"] = _stable_digest(missing_in_shadow)
        detail["missing_in_shadow"] = missing_in_shadow[:max_detail_items]
    if extra_in_shadow:
        classes.append("extra_in_shadow")
        detail["extra_in_shadow_count"] = len(extra_in_shadow)
        detail["extra_in_shadow_digest"] = _stable_digest(extra_in_shadow)
        detail["extra_in_shadow"] = extra_in_shadow[:max_detail_items]

    value_mismatches = []
    nonfinite = []
    for side, rows in (("formal", formal_map), ("shadow", shadow_map)):
        for key, row in sorted(rows.items()):
            for column in NUMERIC_COLUMNS:
                value = row.get(column)
                if value is not None and not math.isfinite(float(value)):
                    nonfinite.append({"side": side, "key": key, "column": column, "value": value})
    for key in sorted(set(formal_map) & set(shadow_map)):
        left = formal_map[key]
        right = shadow_map[key]
        differences = {}
        for column in NUMERIC_COLUMNS:
            a, b = left.get(column), right.get(column)
            same = (a is None and b is None) or (
                a is not None
                and b is not None
                and math.isclose(float(a), float(b), rel_tol=relative_tolerance, abs_tol=absolute_tolerance)
            )
            if not same:
                differences[column] = {"formal": a, "shadow": b}
        if differences:
            value_mismatches.append({"key": key, "differences": differences})
    if value_mismatches:
        classes.append("value_mismatch")
        detail["value_mismatch_count"] = len(value_mismatches)
        detail["value_mismatch_digest"] = _stable_digest(value_mismatches)
        detail["value_mismatches"] = value_mismatches[:max_detail_items]
    if nonfinite:
        classes.append("nonfinite_value")
        detail["nonfinite_value_count"] = len(nonfinite)
        detail["nonfinite_value_digest"] = _stable_digest(nonfinite)
        detail["nonfinite_values"] = nonfinite[:max_detail_items]
    return classes, detail
