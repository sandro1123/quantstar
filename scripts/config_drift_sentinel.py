"""P0-7 配置漂移哨兵 — deepdiff 每日对比生效配置 vs 基线, 漂移即告警。

治"谁改了配置没人知道": config/defaults/*.yaml + settings.yaml +
profiles/{profile}.yaml + local.yaml 逐层合并出**生效配置**, 与基线快照
deepdiff 对比。合法变更 (如 22:05 窗口改 ops.yaml/local.yaml) 后用
`--rebase` 重打基线; 其余任何漂移 → 落档 + 飞书告警 + 退出码 1。

合并顺序与 config_layer/loader.py:load_profile 完全一致:
  defaults/*.yaml (sorted) → settings.yaml → profiles/{profile}.yaml → local.yaml

用法:
  python scripts/config_drift_sentinel.py            # 检查模式 (cron 每日)
  python scripts/config_drift_sentinel.py --rebase   # 重打基线 (合法变更后)
  python scripts/config_drift_sentinel.py --json     # 只打印生效配置 (调试)

落档 (data/config_snapshots/, gitignored):
  effective_baseline.json          基线 (唯一漂移参照)
  history/effective_<ts>.json      每次运行的生效配置快照
  history/override_diff_<ts>.json  defaults→生效 的覆盖差异 (谁覆盖了谁)
  drift_<ts>.json                  漂移报告 (仅漂移时)
"""
import argparse
import json
import os
import sys
from datetime import datetime

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
SNAP_DIR = os.path.join(REPO_ROOT, "data", "config_snapshots")
HISTORY_DIR = os.path.join(SNAP_DIR, "history")
BASELINE_PATH = os.path.join(SNAP_DIR, "effective_baseline.json")


def deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并: dict 深合并, 标量/列表 overlay 覆盖 base"""
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_defaults_merged() -> dict:
    """defaults/*.yaml 按文件名排序逐层合并 (loader.py 同款顺序)"""
    merged = {}
    defaults_dir = os.path.join(CONFIG_DIR, "defaults")
    if os.path.isdir(defaults_dir):
        for f in sorted(os.listdir(defaults_dir)):
            if f.endswith(".yaml"):
                merged = deep_merge(merged, _load_yaml(os.path.join(defaults_dir, f)))
    return merged


def load_effective_config(profile: str | None = None) -> dict:
    """生效配置 = defaults → settings → profile → local (与 loader.py 一致)"""
    profile = profile or os.environ.get("CONFIG_ENV", "prod")
    effective = load_defaults_merged()
    effective = deep_merge(effective, _load_yaml(os.path.join(CONFIG_DIR, "settings.yaml")))
    effective = deep_merge(effective, _load_yaml(os.path.join(CONFIG_DIR, "profiles", f"{profile}.yaml")))
    effective = deep_merge(effective, _load_yaml(os.path.join(CONFIG_DIR, "local.yaml")))
    return effective


def compute_diff(old: dict, new: dict) -> dict:
    """deepdiff 结构化差异 (ignore_order); 返回可 JSON 序列化的 dict"""
    from deepdiff import DeepDiff
    # 注意: 不能传 verbose_level=0 — deepdiff 9.x 下会吞掉 values_changed (实测漏检布尔翻转)
    diff = DeepDiff(old, new, ignore_order=True)
    return json.loads(diff.to_json()) if diff else {}


def _ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def send_alert(title: str, body: str):
    """飞书告警 — best-effort, 失败静默 (哨兵不能因告警失败而哑)"""
    try:
        from config_layer import get_config
        from delivery_layer import DeliveryLayer, Message
        cfg = get_config()
        if not cfg._loaded:
            cfg.load()
        delivery = DeliveryLayer(config=cfg)
        delivery.registry.mark_healthy("feishu_dm")
        content = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
            },
        }
        delivery.send(Message(content=content, content_type="config_drift_alert",
                              title=title, force_channel="feishu_dm"))
    except Exception as e:
        print(f"[drift-sentinel] 告警发送失败 (静默降级): {e}", file=sys.stderr)


def cmd_rebase(effective: dict) -> int:
    old = {}
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH, encoding="utf-8") as f:
            old = json.load(f)
    diff = compute_diff(old, effective)
    _write_json(BASELINE_PATH, effective)
    print(f"[drift-sentinel] 基线已重打 -> {os.path.relpath(BASELINE_PATH, REPO_ROOT)}")
    if diff:
        print(f"  与旧基线差异 {len(diff)} 类:")
        for k, v in diff.items():
            n = len(v) if isinstance(v, (dict, list)) else 1
            print(f"    {k}: {n}")
    else:
        print("  与旧基线无差异")
    return 0


def cmd_check(effective: dict, alert: bool = True) -> int:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    ts = _ts()
    # 每次运行落档生效配置 + 覆盖差异
    _write_json(os.path.join(HISTORY_DIR, f"effective_{ts}.json"), effective)
    override_diff = compute_diff(load_defaults_merged(), effective)
    _write_json(os.path.join(HISTORY_DIR, f"override_diff_{ts}.json"), override_diff)

    if not os.path.exists(BASELINE_PATH):
        print("[drift-sentinel] 无基线, 首次运行自动打基线 (本次不判漂移)")
        _write_json(BASELINE_PATH, effective)
        return 0

    with open(BASELINE_PATH, encoding="utf-8") as f:
        baseline = json.load(f)
    drift = compute_diff(baseline, effective)
    if not drift:
        print("[drift-sentinel] 无漂移 (生效配置 == 基线)")
        return 0

    report = {"ts": ts, "drift": drift, "effective": effective}
    report_path = os.path.join(SNAP_DIR, f"drift_{ts}.json")
    _write_json(report_path, report)

    lines = [f"**配置漂移告警** — 生效配置偏离基线 ({len(drift)} 类差异)", ""]
    for kind, items in drift.items():
        n = len(items) if isinstance(items, (dict, list)) else 1
        lines.append(f"- `{kind}`: {n} 处")
        if isinstance(items, dict):
            for key in list(items.keys())[:5]:
                lines.append(f"  - `{key}`")
    lines += ["", f"详情: `{os.path.relpath(report_path, REPO_ROOT)}`",
              "合法变更请跑 `python scripts/config_drift_sentinel.py --rebase` 重打基线"]
    body = "\n".join(lines)
    print(body)
    if alert:
        send_alert("⚠️ QS 配置漂移", body)
    return 1


def main():
    parser = argparse.ArgumentParser(description="QS 配置漂移哨兵")
    parser.add_argument("--rebase", action="store_true", help="重打基线 (合法变更后)")
    parser.add_argument("--profile", default=None, help="覆盖 CONFIG_ENV (默认 prod)")
    parser.add_argument("--no-alert", action="store_true", help="漂移不发飞书 (测试用)")
    parser.add_argument("--json", action="store_true", help="只打印生效配置 JSON")
    args = parser.parse_args()

    effective = load_effective_config(args.profile)
    if args.json:
        print(json.dumps(effective, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.rebase:
        return cmd_rebase(effective)
    return cmd_check(effective, alert=not args.no_alert)


if __name__ == "__main__":
    sys.exit(main())
