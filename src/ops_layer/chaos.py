"""混沌工程 — 模拟故障测试系统韧性"""

import time

from common.database import Database
from common.logging import get_logger
from common.utils import validate_identifier

logger = get_logger("ops.chaos")


class ChaosEngine:
    _ALLOWED_TABLES = {"chaos_test_table", "temp_data", "test_results"}

    def __init__(self, db: Database, orchestrator=None, alert_manager=None):
        self.db = db
        self.orchestrator = orchestrator
        self.alert_manager = alert_manager
        self._faults = []

    def simulate_pipeline_failure(self, pipeline_name: str) -> dict:
        logger.warning(f"Chaos: simulating failure for pipeline {pipeline_name}")
        self._faults.append({"type": "pipeline_failure", "target": pipeline_name, "time": time.time()})
        raise RuntimeError(f"Chaos: simulated failure for {pipeline_name}")

    def simulate_data_source_unavailable(self, source_name: str) -> dict:
        logger.warning(f"Chaos: simulating data source unavailable: {source_name}")
        self._faults.append({"type": "source_down", "target": source_name, "time": time.time()})
        return {"status": "unavailable", "source": source_name, "detail": "模拟数据源不可用"}

    def simulate_high_latency(self, target: str, latency_ms: int = 5000) -> dict:
        logger.warning(f"Chaos: simulating {latency_ms}ms latency for {target}")
        self._faults.append({"type": "high_latency", "target": target, "time": time.time()})
        time.sleep(latency_ms / 1000.0)
        return {"status": "completed", "target": target, "detail": f"模拟延迟 {latency_ms}ms"}

    def simulate_database_corruption(self, table_name: str) -> dict:
        logger.warning(f"Chaos: simulating database corruption in {table_name}")
        if not validate_identifier(table_name, "table"):
            return {"status": "failed", "table": table_name, "detail": "Invalid table name"}
        if table_name not in self._ALLOWED_TABLES:
            return {"status": "failed", "table": table_name, "detail": f"Table not in whitelist: {table_name}"}
        self._faults.append({"type": "db_corruption", "target": table_name, "time": time.time()})
        conn = self.db.get_connection()
        try:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}_backup")
            conn.execute(f"CREATE TABLE {table_name}_backup AS SELECT * FROM {table_name}")
            conn.execute(f"DELETE FROM {table_name}")
            conn.commit()
            self._faults.append({"type": "db_corruption_executed", "target": table_name, "time": time.time()})
            return {"status": "corrupted", "table": table_name, "detail": f"模拟 {table_name} 数据损坏"}
        except Exception as e:
            return {"status": "failed", "table": table_name, "error": str(e)}

    def simulate_oom_condition(self, process_name: str) -> dict:
        logger.warning(f"Chaos: simulating OOM for {process_name}")
        self._faults.append({"type": "oom", "target": process_name, "time": time.time()})
        return {"status": "oom_simulated", "process": process_name, "detail": "模拟内存不足, 触发降级"}

    def simulate_network_partition(self, service_name: str) -> dict:
        logger.warning(f"Chaos: simulating network partition for {service_name}")
        self._faults.append({"type": "network_partition", "target": service_name, "time": time.time()})
        return {"status": "partitioned", "service": service_name, "detail": "模拟网络分区, 触发熔断"}

    def run_chaos_test(self, scenarios: list[str] | None = None) -> dict:
        results = []
        if scenarios is None:
            scenarios = ["pipeline_failure", "source_down", "high_latency", "oom", "network_partition"]
        for scenario in scenarios:
            try:
                if scenario == "pipeline_failure":
                    self.simulate_pipeline_failure("chaos_test_pipeline")
                elif scenario == "source_down":
                    self.simulate_data_source_unavailable("chaos_source")
                elif scenario == "high_latency":
                    self.simulate_high_latency("chaos_service", 2000)
                elif scenario == "db_corruption":
                    self.simulate_database_corruption("chaos_test_table")
                elif scenario == "oom":
                    self.simulate_oom_condition("chaos_worker")
                elif scenario == "network_partition":
                    self.simulate_network_partition("chaos_api")
                results.append({"scenario": scenario, "status": "completed"})
            except Exception as e:
                results.append({"scenario": scenario, "status": "caught", "error": str(e)})
        return {"results": results, "total_faults": len(self._faults)}
