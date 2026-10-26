"""9router 接入 — strict_priority 链序 + key_source(9router db 只读引用) 单元测试

统筹放行件 (2026-08-25): key 不落 .env, 单一事实源留 9router db apiKeys 表。
llm.yaml 只写 key_source 引用 (path+query, 非秘密), key 值永不进 git。
"""
import sqlite3

import pytest

from agent_layer.llm.client import LLMClient, RateLimiter


def _make_client(providers, rotation=None):
    """构造带指定 providers 的 client, 绕过配置文件加载."""
    client = LLMClient(providers_path="/nonexistent/path.yaml")
    client._providers = providers
    for p in providers:
        client._rate_limiters[p["name"]] = RateLimiter(1000)
    if rotation is not None:
        client._strict_priority = (rotation == "strict_priority")
    return client


# ── strict_priority 链序 ──

def test_strict_priority_ignores_rotation_index():
    provs = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    client = _make_client(provs, rotation="strict_priority")
    client._rotation_index = 2  # 轮换模式下会从 b 开始
    order1 = [p["name"] for p in client._ordered_providers()]
    order2 = [p["name"] for p in client._ordered_providers()]
    assert order1 == ["a", "b", "c"]
    assert order2 == ["a", "b", "c"]
    assert client._rotation_index == 2  # strict 不推进轮换指针


def test_rotation_default_advances_index():
    provs = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    client = _make_client(provs)  # 默认非 strict
    client._rotation_index = 0
    order1 = [p["name"] for p in client._ordered_providers()]
    assert order1 == ["a", "b", "c"]
    assert client._rotation_index == 1
    order2 = [p["name"] for p in client._ordered_providers()]
    assert order2 == ["b", "c", "a"]


def test_strict_priority_flag_loaded_from_config(tmp_path):
    yaml_text = (
        "llm:\n"
        "  rotation: strict_priority\n"
        "  providers:\n"
        "    - name: p1\n"
        "      url: http://x\n"
        "      api_key_env: K\n"
        "      model: m\n"
    )
    path = tmp_path / "llm.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    from config_layer import reset_config
    reset_config()
    client = LLMClient(providers_path=str(path))
    assert client._strict_priority is True


def test_rotation_default_when_flag_absent(tmp_path):
    yaml_text = (
        "llm:\n"
        "  providers:\n"
        "    - name: p1\n"
        "      url: http://x\n"
        "      api_key_env: K\n"
        "      model: m\n"
    )
    path = tmp_path / "llm.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    from config_layer import reset_config
    reset_config()
    client = LLMClient(providers_path=str(path))
    assert client._strict_priority is False


# ── key_source: 9router db 只读引用 ──

def _make_ninerouter_db(tmp_path, key="sk-test-12345"):
    db_path = tmp_path / "data.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE apiKeys (id INTEGER PRIMARY KEY, key TEXT, name TEXT, isActive INTEGER)")
    conn.execute("INSERT INTO apiKeys (key, name, isActive) VALUES (?, 'Agent', 1)", (key,))
    conn.commit()
    conn.close()
    return db_path


def test_key_source_sqlite_resolves_key(monkeypatch, tmp_path):
    monkeypatch.delenv("QS_LLM_OFFLINE", raising=False)  # 根 conftest 设了离线门, 本测显式豁免
    db_path = _make_ninerouter_db(tmp_path)
    prov = {
        "name": "n9r",
        "url": "http://localhost:20128/v1/chat/completions",
        "model": "tokenrouter/qwen/qwen3.8-max-free",
        "key_source": {
            "type": "sqlite",
            "path": str(db_path),
            "query": "SELECT key FROM apiKeys WHERE name='Agent' AND isActive=1 LIMIT 1",
        },
    }
    client = _make_client([prov])
    assert client._resolve_api_key(prov) == "sk-test-12345"


def test_key_source_readonly_does_not_write(monkeypatch, tmp_path):
    monkeypatch.delenv("QS_LLM_OFFLINE", raising=False)
    db_path = _make_ninerouter_db(tmp_path)
    mtime_before = db_path.stat().st_mtime_ns
    prov = {
        "name": "n9r", "url": "http://x", "model": "m",
        "key_source": {"type": "sqlite", "path": str(db_path),
                       "query": "SELECT key FROM apiKeys WHERE name='Agent' LIMIT 1"},
    }
    client = _make_client([prov])
    client._resolve_api_key(prov)
    assert db_path.stat().st_mtime_ns == mtime_before  # mode=ro 不写


def test_key_source_missing_db_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("QS_LLM_OFFLINE", raising=False)
    prov = {
        "name": "n9r", "url": "http://x", "model": "m",
        "key_source": {"type": "sqlite", "path": str(tmp_path / "nope.sqlite"),
                       "query": "SELECT key FROM apiKeys LIMIT 1"},
    }
    client = _make_client([prov])
    assert client._resolve_api_key(prov) == ""


def test_key_source_empty_result_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("QS_LLM_OFFLINE", raising=False)
    db_path = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE apiKeys (id INTEGER PRIMARY KEY, key TEXT, name TEXT, isActive INTEGER)")
    conn.commit()
    conn.close()
    prov = {
        "name": "n9r", "url": "http://x", "model": "m",
        "key_source": {"type": "sqlite", "path": str(db_path),
                       "query": "SELECT key FROM apiKeys WHERE name='Agent' LIMIT 1"},
    }
    client = _make_client([prov])
    assert client._resolve_api_key(prov) == ""


def test_api_key_env_still_resolves(monkeypatch):
    monkeypatch.setenv("QS_TEST_KEY", "env-secret")
    prov = {"name": "p", "url": "http://x", "model": "m", "api_key_env": "QS_TEST_KEY"}
    client = _make_client([prov])
    assert client._resolve_api_key(prov) == "env-secret"


def test_env_takes_precedence_over_key_source(monkeypatch, tmp_path):
    db_path = _make_ninerouter_db(tmp_path, key="db-secret")
    monkeypatch.setenv("QS_TEST_KEY", "env-secret")
    prov = {
        "name": "p", "url": "http://x", "model": "m",
        "api_key_env": "QS_TEST_KEY",
        "key_source": {"type": "sqlite", "path": str(db_path),
                       "query": "SELECT key FROM apiKeys LIMIT 1"},
    }
    client = _make_client([prov])
    assert client._resolve_api_key(prov) == "env-secret"


def test_offline_gate_blocks_key_source(monkeypatch, tmp_path):
    """QS_LLM_OFFLINE=1 时 key_source 不解析 (测试套件离线铁律, 仿 QS_TEST_DB 先例).

    防止单测经 key_source 拿到生产 9router 真 key 后真调 localhost:20128。
    生产环境从不设此变量, 故不影响生产 key 解析。
    """
    db_path = _make_ninerouter_db(tmp_path)
    monkeypatch.setenv("QS_LLM_OFFLINE", "1")
    prov = {
        "name": "n9r", "url": "http://x", "model": "m",
        "key_source": {"type": "sqlite", "path": str(db_path),
                       "query": "SELECT key FROM apiKeys LIMIT 1"},
    }
    client = _make_client([prov])
    assert client._resolve_api_key(prov) == ""
