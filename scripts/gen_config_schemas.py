"""P0-7 Stage-2: 从现有 config/*.yaml 结构反推生成 JSON Schema (一次性 dev 工具)。

用法: python scripts/gen_config_schemas.py
输出: config/schemas/<name>.schema.json (提交入库, 供 pytest 门禁消费)

设计:
- 固定键 (顶层 section / 嵌套已知键) → properties + required, 锁类型
- 动态 map (如 ops.pipelines: 管道名→配置) → additionalProperties 宽松
- additionalProperties: true 于固定对象 → 新增键不破坏门禁 (前向兼容),
  但改类型 / 删键会失败。这正是"锁结构不锁扩展"的门禁语义。
"""
import json
import os

import yaml

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
SCHEMA_DIR = os.path.join(CONFIG_DIR, "schemas")

# 动态 map 判定: 值全为 dict 且键数 >= 此阈值 → 视为 名称→配置 映射
MAP_KEY_THRESHOLD = 3


def infer_schema(value):
    if value is None:
        return {"type": ["null", "string", "number", "boolean"]}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return infer_object_schema(value)
    return {}


def infer_object_schema(d):
    if not d:
        return {"type": "object"}
    values = list(d.values())
    # 动态 map: 多个键且值全为 dict → 名称→配置映射, 宽松放行
    if len(d) >= MAP_KEY_THRESHOLD and all(isinstance(v, dict) for v in values):
        return {"type": "object", "additionalProperties": {"type": "object"}}
    props = {k: infer_schema(v) for k, v in d.items()}
    return {
        "type": "object",
        "properties": props,
        "required": sorted(d.keys()),
        "additionalProperties": True,
    }


def gen_for_file(yaml_path):
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        data = {}
    schema = infer_object_schema(data)
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    schema["$comment"] = f"auto-generated from {os.path.basename(yaml_path)} by gen_config_schemas.py"
    return schema


def main():
    os.makedirs(SCHEMA_DIR, exist_ok=True)
    targets = []
    defaults_dir = os.path.join(CONFIG_DIR, "defaults")
    for f in sorted(os.listdir(defaults_dir)):
        if f.endswith(".yaml"):
            targets.append(os.path.join(defaults_dir, f))
    for extra in ("settings.yaml", os.path.join("profiles", "dev.yaml"), os.path.join("profiles", "prod.yaml")):
        p = os.path.join(CONFIG_DIR, extra)
        if os.path.exists(p):
            targets.append(p)

    for path in targets:
        name = os.path.basename(path).replace(".yaml", "")
        # profiles 下重名防护
        if os.path.dirname(path).endswith("profiles"):
            name = "profile_" + name
        schema = gen_for_file(path)
        out = os.path.join(SCHEMA_DIR, f"{name}.schema.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  {os.path.basename(path):24s} -> {os.path.basename(out)}")
    print(f"Done: {len(targets)} schemas in {SCHEMA_DIR}")


if __name__ == "__main__":
    main()
