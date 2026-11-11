# -*- coding: utf-8 -*-
"""#15 llm.yaml 去私有化 — 开源默认不得含作者私有配置

Issue #15 原文:
  「默认配置中含作者本地路由 (10Router/9router) 设置, 开源用户无法直接使用。
    改为中性默认 + local.yaml 覆盖示例。」

P0 把 9router 改成 10router —— **这只是换了私有路径的名字, 没去私有化**。
本卡让默认配置真正做到「任何用户开箱可用」, 私有配置迁到 local.yaml.example。

判据是**扫描式**: 直接扫 config/defaults/ 全部文件 + 相关源码常量,
不允许出现作者本机路径/网关名。这样以后有人往默认配置里加私有路径也会被抓。
"""
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DEFAULTS_DIR = REPO / "config" / "defaults"

# 断言要抓的私有痕迹 — 扩充须谨慎 (只加真实私有物, 不加通用词)
PRIVATE_MARKERS = [
    r"\.9router",          # 作者旧网关目录
    r"\.10router",         # 作者新网关目录
    r"Hermes-Agent",       # 作者网关里的 key 名
]


class TestNoPrivatePathsInDefaults:
    def test_no_private_path_in_any_default_yaml(self):
        """扫描: config/defaults/*.yaml 不得出现作者私有路径/网关名"""
        offenders = []
        for f in sorted(DEFAULTS_DIR.glob("*.yaml")):
            text = f.read_text(encoding="utf-8")
            for pat in PRIVATE_MARKERS:
                for m in re.finditer(pat, text):
                    line = text[: m.start()].count("\n") + 1
                    offenders.append(f"{f.name}:{line}: {pat}")
        assert not offenders, (
            "默认配置含作者私有痕迹 (开源用户无法使用):\n  "
            + "\n  ".join(offenders)
            + "\n请移到 config/local.yaml.example")

    def test_llm_providers_are_neutral(self):
        """llm.yaml 的 providers 必须能用环境变量配置, 且指向中性端点"""
        cfg = yaml.safe_load((DEFAULTS_DIR / "llm.yaml").read_text(encoding="utf-8"))
        providers = cfg["llm"]["providers"]
        assert providers, "至少要有一个默认 provider"
        for p in providers:
            assert "url" in p and p["url"].startswith("http"), p
            assert p.get("model"), p
            # key 必须走环境变量名 (不在 yaml 里放真 key)
            assert p.get("api_key_env"), f"{p['name']} 缺 api_key_env"
            # 不得再用 sqlite key_source 指向作者本机库
            assert "key_source" not in p, (
                f"{p['name']} 仍用 key_source (作者本机私有库) — 默认配置须中性")

    def test_default_llm_has_no_literal_secret(self):
        """默认配置不得出现任何形似真 key 的字面量"""
        text = (DEFAULTS_DIR / "llm.yaml").read_text(encoding="utf-8")
        cfg = yaml.safe_load(text)
        for p in cfg["llm"]["providers"]:
            # 允许 api_key_env 指定变量名, 不允许 api_key 直接给值
            assert "api_key" not in p, f"{p['name']} 直接写了 api_key, 应改用 api_key_env"


class TestQuotaDefaultsNeutral:
    def test_ops_quota_source_path_is_empty_by_default(self):
        """ops.yaml 的配额真源路径默认为空 = 未启用, 不猜作者本机路径"""
        cfg = yaml.safe_load((DEFAULTS_DIR / "ops.yaml").read_text(encoding="utf-8"))
        quota = cfg["ops"]["agent"]["quota"]
        assert quota["source_path"] == "", (
            "默认 source_path 必须为空 (未启用), 而非指向作者本机路径")

    def test_ops_quota_api_key_hint_empty(self):
        cfg = yaml.safe_load((DEFAULTS_DIR / "ops.yaml").read_text(encoding="utf-8"))
        assert cfg["ops"]["agent"]["quota"]["api_key_hint"] == ""

    def test_quota_manager_unconfigured_is_honest(self):
        """未配置时 QuotaManager 报 degraded 且说清是"未配置"不是"坏了" """
        from agent_layer.llm.quota_manager import QuotaManager, _DEFAULT_SOURCE_PATH

        assert _DEFAULT_SOURCE_PATH == "", "降级兜底常量不得留作者路径"
        qm = QuotaManager()
        qm.configure(source_path="", api_key_hint="") if hasattr(qm, "configure") else None
        with pytest.raises(LookupError) as ei:
            qm._today_usage()
        msg = str(ei.value)
        assert "未配置" in msg, f"文案须说清是未配置: {msg}"


class TestLocalExampleExists:
    def test_example_file_present_and_documents_override(self):
        """必须提供 local.yaml.example, 且含覆盖说明与示例"""
        ex = REPO / "config" / "local.yaml.example"
        assert ex.exists(), "缺 config/local.yaml.example (issue #15 明确要求覆盖示例)"
        text = ex.read_text(encoding="utf-8")
        assert "providers" in text
        assert "local.yaml" in text
        assert "cp " in text, "示例须给出复制命令"

    def test_example_has_no_real_secret(self):
        """示例文件也不许有真 key (它是提交物)"""
        text = (REPO / "config" / "local.yaml.example").read_text(encoding="utf-8")
        # 形如 sk-xxxx / gho_xxxx 的真 key 模式
        assert not re.search(r"\b(sk-[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{16,})", text), \
            "示例含疑似真实 key"

    def test_local_yaml_is_gitignored(self):
        """local.yaml 必须被 gitignore, 否则用户真配置会被提交"""
        gi = (REPO / ".gitignore").read_text(encoding="utf-8")
        assert "local.yaml" in gi, "config/local.yaml 须在 .gitignore 中"
