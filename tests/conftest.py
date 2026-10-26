"""P0-2: 测试套件独立 test DB — 隔离生产库 data/qs.db (8.4G)。

在任何测试模块导入前, 把 no-arg Database() 的默认路径重定向到会话级临时库,
避免测试读写生产库造成: ① 多会话并发 pytest 砸同一 SQLite 锁竞争
② worktree 内 test_common 的 Database() 误建空 data/qs.db stub,
   使依赖相对路径生产库的 test_data_quality_gates 由 skip 变误报失败。

生产环境从不设置 QS_TEST_DB, 故此改动不影响生产 Database() 路径解析。
"""
import atexit
import os
import shutil
import tempfile

import pytest

# 会话级临时库目录 — 每个 pytest 进程独立 mkdtemp, 并发互不干扰
_TEST_DB_DIR = tempfile.mkdtemp(prefix="qs_test_db_")
os.environ["QS_TEST_DB"] = os.path.join(_TEST_DB_DIR, "test_qs.db")
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)

# DL-64: 测试日志隔离 — 把 setup_logging 的默认文件 handler 重定向到临时目录,
# 防 pytest 窗口把测试日志 (测试消息/MagicMock 等) 写进生产 logs/quantstar.log。
# 血案: quantstar.log 里已混入 "测试消息" (test_logging.py) 与 MagicMock 告警,
# 污染铁律 #16 的日志取证。common/logging.setup_logging 读取此变量 (与 QS_TEST_DB 同款)。
os.environ["QS_TEST_LOG_FILE"] = os.path.join(_TEST_DB_DIR, "test_quantstar.log")

# 9router 接入回归防护 (2026-08-26): key_source 机制能从生产 9router db 解析出真 key,
# 若无此门, 单测里 has_available_llm()=True → 裸 AgentLayer() 测试真调
# localhost:20128 (30s 超时 + 烧免费额度; 血案: 套件 166s + test_ag03 failed)。
# LLMClient._resolve_api_key 读取此变量阻断 key_source (与 QS_TEST_DB 同款; 生产从不设置)。
os.environ["QS_LLM_OFFLINE"] = "1"

# P0-2 ①: outbox 落地隔离 — 既有 executor 测试 (t_bad 等全败链路) 会触发链尾
# 落地钩子, 若不隔离则测试垃圾写进生产 data/outbox/, 次日早报水位段误亮。
# delivery_layer.outbox.default_outbox_dir 每次调用读取此变量 (与 QS_TEST_DB 同款)。
os.environ["QS_OUTBOX_DIR"] = os.path.join(_TEST_DB_DIR, "outbox")

# LLM 用量账本 (provider_manager._ledger_write) 默认写 data/qs.db, 测试同样重定向
os.environ["QS_LEDGER_DB"] = os.environ["QS_TEST_DB"]


def pytest_addoption(parser):
    parser.addoption(
        "--prod-db", action="store_true", default=False,
        help="运行需要真实生产库数据体量的测试 (needs_production_db)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_production_db: 需要真实生产库数据体量, 默认 skip "
        "(用 --prod-db 或 -m needs_production_db 启用)",
    )


def pytest_collection_modifyitems(config, items):
    markexpr = config.getoption("-m") or ""
    run_prod = config.getoption("--prod-db") or "needs_production_db" in markexpr
    if run_prod:
        return
    skip_prod = pytest.mark.skip(reason="需要生产库 (用 --prod-db 启用)")
    for item in items:
        if "needs_production_db" in item.keywords:
            item.add_marker(skip_prod)


@pytest.fixture(scope="session")
def test_db_path():
    """会话级临时 test DB 路径 (QS_TEST_DB)。"""
    return os.environ["QS_TEST_DB"]


@pytest.fixture
def test_db(test_db_path):
    """指向临时 test DB 的 Database 实例 (schema 走 init_db, 不拷贝生产数据)。"""
    from common.database import Database
    db = Database(test_db_path)
    db.init_db()
    yield db


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    """TH-01: 每个测试前重置 Config 单例, 防跨测试污染。

    get_config() 缓存进程级单例; 若某测试 (如 P0-1 prod 模拟) 造出 prod Config,
    后续测试复用该单例时 CF-01 必填校验误发 (tests/unit 单跑 25 failed + 43 errors
    全在 execution_layer)。每测重置, 各测试从当前环境重建全新实例。
    reset_config 为 src/config_layer 现成 API, 无需改 src。
    """
    from config_layer import reset_config
    reset_config()
    yield


@pytest.fixture(autouse=True)
def _isolate_env_and_cwd():
    """CF-02: 每个测试前后快照/恢复 os.environ 与 cwd, 治跨测试环境泄漏。

    根因实证 (2026-08-26, REPRO 对照): 双泄漏合谋致全量 25F+43E——
    ① test_morning_brief_picks 模块级 import build_morning_brief, 该 cron 直驱
       入口 import 期 load_dotenv(.env) 注入 CONFIG_ENV=prod + os.chdir;
    ② test_required_secrets 裸 os.environ.pop 删 7 个秘密键不恢复。
    reset_config() 只清单例不清 os.environ, 泄漏的 CONFIG_ENV=prod 让后续
    cfg_get() 重建 prod Config → CF-01 必填校验硬抛。
    本 fixture 在 yield 后无条件恢复快照 (测试失败也恢复), 兜底一切
    模块级/import 期/测试体内的 env 与 cwd 改动, 防快照机制自身成为新泄漏源。
    """
    env_snapshot = dict(os.environ)
    cwd_snapshot = os.getcwd()
    yield
    os.environ.clear()
    os.environ.update(env_snapshot)
    os.chdir(cwd_snapshot)


# 微卡③ (09-05): 全局 autouse 重定向子管道计时 jsonl — 防 pytest 写生产 logs/。
# 病灶实证: 生产 logs/child_pipeline_timing.jsonl 里 08-31 09:22 / 09-02 23:36 /
# 09-04 23:16:02 三批历史日期 fuyao_financial 帧 = 某测试绕过
# test_child_pipeline_timing.py 自己的 _redirect_timing_jsonl fixture
# 直接调 _run_data_pipeline。钩子 QS_CHILD_PIPELINE_TIMING 早已存在
# (orchestrator.py:64 供测试), 病 = 有钩子未被全局用。与下方 _PROD_LOG
# fd 探针 (DL-64) 同族: 光提供重定向机制不够, 必须全局兜底。
# tmp_path 是 function 级 — 每测试独立文件, 不互串; 生产进程不受影响
# (env 只在 pytest 进程内)。
@pytest.fixture(autouse=True)
def _redirect_child_pipeline_timing(tmp_path):
    os.environ["QS_CHILD_PIPELINE_TIMING"] = str(
        tmp_path / "child_pipeline_timing.jsonl")
    yield
    os.environ.pop("QS_CHILD_PIPELINE_TIMING", None)


# DL-64 负向对照 (铁律 #18): 套件跑完后断言 pytest 进程未持有生产 logs/quantstar.log
# 的打开句柄。光提供 QS_TEST_LOG_FILE 重定向机制不够 — 必须有一条"能证明失败"的检查,
# 否则下一个人新写的测试绕过隔离也不会被发现 (DL-68 教训反过来用)。
# 门禁4红收口 (2026-08-27): 原实现对比会话前后生产日志 size+mtime 指纹, 无法区分
# "测试写的" 与 "生产 cron 并发写的" — 门禁 24min 窗口内 api_health_check 等 cron
# 合法写日志 → 误报红 (test_cors_headers teardown error)。改进程级探针: 只断言
# 本进程不持有生产日志 fd, 对其他进程 (cron) 的写入免疫。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROD_LOG = os.path.join(_PROJECT_ROOT, "logs", "quantstar.log")


def _pytest_holds_prod_log_fd():
    """进程级探针: 当前 (pytest) 进程是否持有生产日志的打开 fd。

    扫 /proc/self/fd readlink 匹配 _PROD_LOG。绕过 QS_TEST_LOG_FILE 直接
    setup_logging 的测试会打开 RotatingFileHandler, handler fd 存活至会话末,
    teardown 时必被抓住 (负向对照价值保留)。返回命中的 fd 号, 未命中返回 None;
    非 Linux (/proc 不可用) 返回 None 放行 (探针不可用不阻塞)。
    """
    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        return None
    real = os.path.realpath(_PROD_LOG)
    for fd in os.listdir(fd_dir):
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if target == real:
            return fd
    return None


@pytest.fixture(scope="session", autouse=True)
def _guard_production_log():
    """会话级守卫: pytest 进程不得持有生产 logs/quantstar.log 的打开句柄。

    旧实现 (size+mtime 指纹前后对比) 在生产 cron 并发写日志时误报 — 文件指纹
    是全局状态, 任何进程都能改; fd 探针只看本进程, 精确对应"测试绕过隔离"
    这一真实失效模式 (handler fd 常驻), 对 cron 免疫, 门禁不再需要人肉解读。
    """
    yield
    held = _pytest_holds_prod_log_fd()
    assert held is None, (
        f"DL-64 隔离失效: pytest 进程持有生产日志 {_PROD_LOG} 的打开句柄 "
        f"(fd={held})。检查是否有测试绕过 QS_TEST_LOG_FILE 重定向直接 setup_logging。"
    )


# ---------------------------------------------------------------------------
# 跨域污染根治案 (09-12, sm-be69213): LLM 跨测试状态卫生
#
# 病根: _llm_expert_opinion 的 ProviderManager 缓存挂在**函数属性** (_pm) 上,
# 跨测试永驻 — 字母序下 ag10/decision_hub 等先行测试一旦把 pm 初始化成
# 可用态 (9router 20128 生产端口活 + api_key_env 形态 key 在环境, 不受
# QS_LLM_OFFLINE 门管 — 该门只管 key_source 路径), agcompose 的降级断言
# (期望离线 rules) 就被真 LLM 意见击穿 → tier='llm' 3 红。单域绿/合跑炸
# =时敏污染家族第 7 形态 (跨测试进程级缓存驻留)。
#
# 修 (状态面单一定义点): 每个测试前把 _pm 重置为"未初始化", 测试间状态
# 零泄漏; 同时保 QS_LLM_OFFLINE=1 在位 (防先行测试 delenv 后异常路径漏恢复)。
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _llm_state_hygiene():
    """每测试重置 LLM 跨测试驻留态 (pm 函数属性缓存 + offline 门 env)。"""
    os.environ["QS_LLM_OFFLINE"] = "1"
    try:
        from agent_layer.debate import experts as _experts_mod
        _fn = getattr(_experts_mod, "_llm_expert_opinion", None)
        if _fn is not None and hasattr(_fn, "_pm"):
            _fn._pm = None
        # 09-13 根治补丁: 仅清不够 — 清后重建在含 .env 生产密钥的树上
        # (QS_LLM_OFFLINE 只挡 key_source, api_key_env 形态裸奔) 会再激活 pm。
        # 离线哨兵: 置 _pm=False 让本测试进程内 pm 恒判不可用, 与 conftest
        # 的 QS_LLM_OFFLINE=1 语义对齐 (测试进程=永远离线态)。
        if _fn is not None:
            _fn._pm = False
    except Exception:
        pass  # agent_layer 未装/不可导入的测试环境下无态可清
    yield
