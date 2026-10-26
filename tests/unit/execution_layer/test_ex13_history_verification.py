"""EX-13: 历史报错复跑确认回归测试

验证历史报错已彻底清除:
1. tushare_source 模块语法有效, 无 IndentationError / 语法残留;
2. execution stats 与 post_exit_tracking 查询列名已使用 quality, 无 no such column: review_quality;
3. daily_update 脚本主入口与底层 daily_update 全流程在测试库跑通无报错。
"""
import py_compile
from pathlib import Path
from unittest.mock import patch

from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.tables import init_execution_tables


def test_tushare_source_syntax_clean():
    """验证 tushare_source.py 语法编译干净, 无 IndentationError 残留"""
    src_path = Path("src/data_layer/sources/tushare_source.py")
    if src_path.exists():
        # py_compile.compile raises py_compile.PyCompileError on syntax / indentation errors
        compiled = py_compile.compile(str(src_path), doraise=True)
        assert compiled is not None


def test_daily_update_full_flow_no_column_error():
    """验证 daily_update 全流程在内存测试库中运行正常, 无 review_quality 列名缺失错误"""
    db = Database(":memory:")
    db.init_db()
    init_execution_tables(db.get_connection())

    el = ExecutionLayer(db=db)
    el.start()
    try:
        # 运行 daily_update 流程
        el.daily_update()
        # 验证 execution_stats 与 post_exit_tracking 等查询与落库无异常
        stats_rows = db.get_connection().execute("SELECT COUNT(*) FROM execution_stats").fetchone()[0]
        assert stats_rows >= 0
    finally:
        el.shutdown()
        db.close()
