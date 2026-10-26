"""QuantStar 启动引导"""

from __future__ import annotations

import os
from dataclasses import dataclass

from common.logging import get_logger, setup_logging
from config_layer import Config


@dataclass
class Application:
    config: Config | None = None
    data: object = None
    strategy: object = None
    agent: object = None
    llm: object = None
    backtest: object = None
    execution: object = None
    report: object = None
    delivery: object = None
    ops: object = None

    def shutdown(self, timeout: float = 30.0):
        layers = [self.ops, self.delivery, self.report, self.execution,
                  self.strategy, self.agent, self.backtest,
                  self.data, self.config]
        for layer in layers:
            if layer and hasattr(layer, "shutdown"):
                try:
                    layer.shutdown(timeout)
                except Exception:
                    pass


def bootstrap_app(profile: str | None = None) -> Application:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    if profile is None:
        profile = os.environ.get("CONFIG_ENV", "dev")

    setup_logging(profile)
    logger = get_logger("bootstrap", trace_id="boot")
    logger.info("QuantStar 启动中...", extra={"metrics": {"profile": profile}})

    from common.database import Database
    from ops_layer.startup import StartupManager
    db = Database()
    from ops_layer.alert_manager import AlertManager
    from ops_layer.pipeline_logger import PipelineLogger
    startup_mgr = StartupManager(db, PipelineLogger(db), AlertManager(db))
    layer_instances = {}

    # Phase 0: Config Layer (critical, retry once)
    config = None
    for attempt in range(2):
        try:
            from config_layer import get_config
            config = get_config(profile)
            config.load()
            logger.info("Config 加载完成", extra={"metrics": {"profile": profile}})
            break
        except Exception as e:
            logger.error(f"Config 加载失败 (attempt {attempt+1}): {e}")
            if attempt == 1:
                raise

    # 数据库初始化
    db_path = config.get("data.db_path", "data/qs.db") if config else "data/qs.db"
    # 测试隔离: tests/conftest.py 设置 QS_TEST_DB (生产从不设置), 防测试写生产 data/qs.db
    db_path = os.environ.get("QS_TEST_DB") or db_path
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    database = Database(db_path)
    database.init_db()
    logger.info("数据库初始化完成", extra={"metrics": {"db_path": db_path}})

    # Phase 1: Data Layer (critical, retry once)
    data_layer = None
    for attempt in range(2):
        try:
            from data_layer import DataLayer
            data_layer = DataLayer(database, config)
            data_layer.start()
            logger.info("Data Layer 启动完成")
            break
        except Exception as e:
            logger.error(f"Data Layer 启动失败 (attempt {attempt+1}): {e}")
            if attempt == 1:
                raise
    layer_instances["data"] = data_layer

    # Phase 2: Agent Layer (non-critical, 需要提前创建以供给 Strategy/Execution)
    agent_layer = None
    try:
        from agent_layer import AgentLayer
        agent_layer = AgentLayer(database, config)
        agent_layer.start()
        logger.info("Agent Layer 启动完成")
    except Exception as e:
        logger.error(f"Agent Layer 启动失败 (继续): {e}")
    layer_instances["agent"] = agent_layer

    # Phase 2b: LLM Service (复用 Agent Layer 的 ProviderManager)
    llm_service = None
    try:
        # AG-10: 经统一入口取门内叙述柜台 (共享 ProviderManager 单池), 不伸手 _pm
        llm_service = agent_layer.get_llm_service() if agent_layer else None
        logger.info("LLM Service 启动完成")
    except Exception as e:
        logger.error(f"LLM Service 启动失败 (继续): {e}")
    layer_instances["llm"] = llm_service

    # Phase 3: Strategy Layer (non-critical)
    strategy_layer = None
    try:
        from strategy_layer import StrategyLayer
        strategy_layer = StrategyLayer(config, data_layer, agent_layer=agent_layer)
        logger.info("Strategy Layer 启动完成")
    except Exception as e:
        logger.error(f"Strategy Layer 启动失败 (继续): {e}")
    layer_instances["strategy"] = strategy_layer

    # Phase 3: Execution Layer (non-critical)
    execution_layer = None
    try:
        from execution_layer import ExecutionLayer
        execution_layer = ExecutionLayer(database, config, data_layer, strategy_layer, agent_layer=agent_layer)
        execution_layer.start()
        logger.info("Execution Layer 启动完成")
    except Exception as e:
        logger.error(f"Execution Layer 启动失败 (继续): {e}")
    layer_instances["execution"] = execution_layer

    # Phase 4: Backtest Layer (non-critical)
    backtest_layer = None
    try:
        from backtest_layer import BacktestLayer
        backtest_layer = BacktestLayer(data_layer=data_layer, strategy_layer=strategy_layer)
        logger.info("Backtest Layer 启动完成")
    except Exception as e:
        logger.error(f"Backtest Layer 启动失败 (继续): {e}")
    layer_instances["backtest"] = backtest_layer

    # Phase 5: Report Layer (non-critical)
    report_layer = None
    try:
        from report_layer import ReportLayer
        report_layer = ReportLayer(
            config, data_layer, strategy_layer, execution_layer,
            agent_layer=agent_layer, backtest_layer=backtest_layer,
            llm_service=llm_service,
        )
        logger.info("Report Layer 启动完成")
    except Exception as e:
        logger.error(f"Report Layer 启动失败 (继续): {e}")
    layer_instances["report"] = report_layer

    # Phase 3: Delivery Layer (non-critical)
    delivery_layer = None
    try:
        from delivery_layer import DeliveryLayer
        # F5.2c: 传入 database 启用 delivery_log 推送日志持久化
        delivery_layer = DeliveryLayer(config, db=database)
        logger.info("Delivery Layer 启动完成")
    except Exception as e:
        logger.error(f"Delivery Layer 启动失败 (继续): {e}")
    layer_instances["delivery"] = delivery_layer

    # Phase 5: Ops Layer (non-critical)
    ops_layer = None
    try:
        from ops_layer import OpsLayer
        ops_layer = OpsLayer(database, config, llm_service=llm_service)
        ops_layer.start()
        ops_layer.register_layers({
            "data": data_layer,
            "strategy": strategy_layer,
            "agent": agent_layer,
            "execution": execution_layer,
            "backtest": backtest_layer,
            "report": report_layer,
            "delivery": delivery_layer,
        })
        logger.info("Ops Layer 启动完成")
        # 注入 ops_layer 到 report_layer, 使 ops_report/alerts 可见系统状态
        if report_layer is not None:
            report_layer.ops_layer = ops_layer
    except Exception as e:
        logger.error(f"Ops Layer 启动失败 (继续): {e}")
    layer_instances["ops"] = ops_layer

    # Generate StartupReport
    startup_report = startup_mgr.startup(layer_instances)
    if not startup_report.success:
        logger.warning(f"启动部分失败: {startup_report.failures}")
    else:
        logger.info("所有层启动成功")

    app = Application(
        config=config,
        data=data_layer,
        strategy=strategy_layer,
        agent=agent_layer,
        llm=llm_service,
        backtest=backtest_layer,
        execution=execution_layer,
        report=report_layer,
        delivery=delivery_layer,
        ops=ops_layer,
    )
    # 将 app 引用注入 orchestrator，避免每次管道运行都重新 bootstrap
    if ops_layer and hasattr(ops_layer, 'orchestrator'):
        ops_layer.orchestrator.set_app(app)
    logger.info("QuantStar 启动完毕")
    return app
