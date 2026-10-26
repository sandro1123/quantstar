# -*- coding: utf-8 -*-
"""10router (端口 20129) + codebuddy-cn hy3 配置守卫。

背景 (2026-10-21, 用户令): 9router 作废, 全切 10router; 主链改 hy3。

本文件钉的是四个曾真实发生过的坑, 每个坑都让 LLM 链静默瘫痪:
  (1) 网关端口/服务: 9router(20128) 已停服, 指向它就全线 Connection refused
  (2) key 名: 'Agent' 这把 key 已失效 (实测 Invalid API key),
      但配置里 13 条腿全指向它 -> 即使端口改对也全部认证失败
  (3) 死模型: nvidia 4 款官方 EOL (HTTP 410), 留着只烧重试时延
  (4) reasoning 模型 token 预算: hy3 的 reasoning 吃大头 token,
      max_tokens 给小会 "HTTP 200 但 content 空" -> 假成功

钉子只读配置, 不联网 (联网验证另由统筹逐条实跑)。
"""
import os
import sqlite3

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
YAML_PATH = os.path.join(ROOT, "config", "defaults", "llm.yaml")


@pytest.fixture(scope="module")
def cfg():
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["llm"]


@pytest.fixture(scope="module")
def providers(cfg):
    return cfg["providers"]


def test_all_providers_point_to_10router_port_20129(providers):
    """钉(1): 不许再有任何一条腿指向 9router(20128)。"""
    bad = [p["name"] for p in providers if ":20128" in p["url"]]
    assert not bad, f"仍有腿指向已停服的 9router(20128): {bad}"
    for p in providers:
        assert "localhost:20129" in p["url"], (
            f"{p['name']} 未指向 10router(20129): {p['url']}")


def test_key_source_uses_valid_key_name_not_dead_agent(providers):
    """钉(2): key 必须取自 Hermes-Agent; 'Agent' 已失效。"""
    for p in providers:
        ks = p.get("key_source")
        assert ks, f"{p['name']} 缺 key_source"
        assert ".10router/db/data.sqlite" in ks["path"], (
            f"{p['name']} key 源路径未切到 10router: {ks['path']}")
        q = ks["query"]
        assert "Hermes-Agent" in q, f"{p['name']} 未用有效 key(Hermes-Agent): {q}"
        assert "name='Agent'" not in q, (
            f"{p['name']} 仍引用已失效的 'Agent' key")


def test_no_410_eol_models_remain(providers):
    """钉(3): 官方已 EOL(410)的模型不许留在链上烧重试。"""
    dead = ("meta/llama-3.1-8b-instruct",
            "meta/llama-3.1-70b-instruct",
            "nvidia/llama-3.3-nemotron-super-49b-v1",
            "deepseek-ai/deepseek-v4-pro")
    hit = [(p["name"], p["model"]) for p in providers if p["model"] in dead]
    assert not hit, f"仍留有 HTTP 410 EOL 死模型: {hit}"


def test_reasoning_token_budget_declared():
    """钉(4): reasoning 模型的 token 预算约束必须写在配置里。

    实测 hy3 一次回答 reasoning 吃掉 177/201 token;
    预算给小会返回 HTTP 200 但 content 为空 = 假成功。
    """
    with open(YAML_PATH, encoding="utf-8") as f:
        raw = f.read()
    assert "reasoning" in raw.lower(), (
        "llm.yaml 未声明 reasoning 模型的 token 预算约束")


def test_primary_chain_is_codebuddy_cn_hy3(providers):
    """主链必须是用户指定的 codebuddy-cn hy3。"""
    primary = [p for p in providers if p["priority"] == 1]
    assert len(primary) == 1, f"主链应唯一, 实有 {len(primary)}"
    assert primary[0]["model"] == "cbcn/hy3", (
        f"主链应为 cbcn/hy3, 实为 {primary[0]['model']}")


def test_strict_priority_and_discovery_off(cfg):
    """链序语义不得被改: 严格优先级 + 不自动发现。"""
    assert cfg["rotation"] == "strict_priority"
    assert cfg["enable_discovery"] is False


def test_every_key_source_query_resolves_non_empty(providers):
    """钉(2)加强: 每条腿的 key_source 查询在真实库上必须解出一把非空 key。

    (只读打开 10router 的 db; 库不存在则跳过, 允许 CI 无此文件)
    """
    for p in providers:
        ks = p["key_source"]
        path = ks["path"]
        if not os.path.exists(path):
            pytest.skip("key 库不存在(非本机): %s" % path)
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            row = conn.execute(ks["query"]).fetchone()
        finally:
            conn.close()
        assert row and row[0], f"{p['name']} 的 key_source 查询解不出 key"
