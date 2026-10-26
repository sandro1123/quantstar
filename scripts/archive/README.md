# scripts/archive — 已归档代码存放区

本目录存放**不再被生产代码引用、但按审计决策保留历史**的模块与测试。
归档 ≠ 删除: 文件保留 git 历史与原始内容, 仅移出 `src/` 与 `tests/` 收集路径。

## valuation_dual_read (2026-08-18 归档, DL-19)

**来历**: S2 adjust_type 迁移期间的影子对账工具 (dual-read observer)。
- `0268f1f` — add fail-closed valuation dual-read observer
- `80bd654` — hook valuation dual-read into health reporting

**归档原因**: 该观察器为 S2 迁移一次性对账设施, 配置开关
`data.valuation_dual_read.enabled` 在所有环境配置中均未启用 (default-off),
迁移完成后无活跃调用方。DL-19 用户预决策: 归档不删除。

**归档内容**:
- `valuation_dual_read.py` — 原 `src/data_layer/valuation_dual_read.py`
- `valuation_dual_read_service.py` — 原 `src/data_layer/valuation_dual_read_service.py`
- `tests/test_valuation_dual_read.py` — 原 `tests/unit/data_layer/`
- `tests/test_valuation_dual_read_service.py` — 原 `tests/unit/data_layer/`
- `tests/test_p209b_bootstrap_hook.py` — 原 `tests/unit/`
- `tests/test_p209b_report_hook.py` — 原 `tests/unit/report_layer/`

**同步清理**: `src/bootstrap.py` 与 `src/report_layer/__init__.py` 中的
接线 (启动钩子 / health 检查 / Application 字段) 已一并移除, 全仓无失效 import。
若未来需要恢复, 从本目录移回原位并按 git 历史重建接线即可。
