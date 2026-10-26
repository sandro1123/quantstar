"""P0-7 配置两段门禁 — Stage-1 yamllint 结构 lint + Stage-2 JSON Schema 校验。

接进 pytest 门禁 (不另起 CI): config/ 下所有 yaml 先过语法/结构 lint,
再过 schema 校验 (schema 由 scripts/gen_config_schemas.py 从现有结构反推,
提交入库 config/schemas/)。

Stage-2 用 jsonschema 库 (check-jsonschema 的同一校验引擎); 手工/ad-hoc
可用 `check-jsonschema --schemafile config/schemas/<name>.schema.json <file>`。

门禁语义: 锁结构不锁扩展 — 改类型/删已知键 → 失败; 新增键 → 放行
(additionalProperties: true)。有意结构变更需重跑 gen_config_schemas.py。
"""
import json
import os

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
SCHEMA_DIR = os.path.join(CONFIG_DIR, "schemas")


def _config_yaml_files():
    """defaults/*.yaml + settings.yaml + profiles/*.yaml + local.yaml (若存在)"""
    files = []
    defaults_dir = os.path.join(CONFIG_DIR, "defaults")
    if os.path.isdir(defaults_dir):
        files += [os.path.join(defaults_dir, f) for f in sorted(os.listdir(defaults_dir)) if f.endswith(".yaml")]
    for extra in ("settings.yaml", "local.yaml"):
        p = os.path.join(CONFIG_DIR, extra)
        if os.path.exists(p):
            files.append(p)
    prof_dir = os.path.join(CONFIG_DIR, "profiles")
    if os.path.isdir(prof_dir):
        files += [os.path.join(prof_dir, f) for f in sorted(os.listdir(prof_dir)) if f.endswith(".yaml")]
    return files


def _schema_for(path):
    """config 文件 → 对应 schema 路径"""
    name = os.path.basename(path).replace(".yaml", "")
    if os.path.dirname(path).endswith("profiles"):
        name = "profile_" + name
    return os.path.join(SCHEMA_DIR, f"{name}.schema.json")


class TestStage1Yamllint:
    """Stage-1: yamllint 结构完整性 (语法错误/重复键), 风格项已在 .yamllint.yaml 关闭"""

    def test_all_config_yaml_pass_lint(self):
        from yamllint import linter
        from yamllint.config import YamlLintConfig

        conf = YamlLintConfig(file=os.path.join(REPO_ROOT, ".yamllint.yaml"))
        errors = []
        for path in _config_yaml_files():
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for p in linter.run(content, conf, filepath=path):
                if p.level == "error":
                    errors.append(f"{os.path.relpath(path, REPO_ROOT)}:{p.line}: {p.message}")
        assert not errors, "yamllint 结构错误:\n" + "\n".join(errors)


class TestStage2Schema:
    """Stage-2: JSON Schema 锁结构/类型"""

    def test_schemas_exist_for_all_defaults(self):
        defaults_dir = os.path.join(CONFIG_DIR, "defaults")
        missing = []
        for f in sorted(os.listdir(defaults_dir)):
            if f.endswith(".yaml") and not os.path.exists(_schema_for(os.path.join(defaults_dir, f))):
                missing.append(f)
        assert not missing, f"缺 schema (跑 scripts/gen_config_schemas.py 生成): {missing}"

    @pytest.mark.parametrize("path", _config_yaml_files(), ids=lambda p: os.path.basename(p))
    def test_config_matches_schema(self, path):
        import jsonschema

        schema_path = _schema_for(path)
        if not os.path.exists(schema_path):
            # local.yaml 无独立 schema, 走 section 级校验 (见下)
            if os.path.basename(path) == "local.yaml":
                pytest.skip("local.yaml 走 test_local_yaml_sections_known")
            pytest.fail(f"缺 schema: {schema_path}")

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)

        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as e:
            loc = "/".join(str(x) for x in e.absolute_path) or "<root>"
            pytest.fail(f"{os.path.basename(path)} schema 校验失败 @ {loc}: {e.message}")

    def test_local_yaml_sections_known(self):
        """local.yaml 是覆盖层: 每个顶层 section 必须能在某个 defaults schema 中找到"""
        local_path = os.path.join(CONFIG_DIR, "local.yaml")
        if not os.path.exists(local_path):
            pytest.skip("local.yaml 不存在")
        with open(local_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        known_sections = set()
        for f in sorted(os.listdir(SCHEMA_DIR)):
            if not f.endswith(".schema.json"):
                continue
            with open(os.path.join(SCHEMA_DIR, f), encoding="utf-8") as fh:
                schema = json.load(fh)
            known_sections |= set(schema.get("properties", {}).keys())

        unknown = [k for k in data.keys() if k not in known_sections]
        assert not unknown, f"local.yaml 含未知顶层 section (拼写错误?): {unknown}"
