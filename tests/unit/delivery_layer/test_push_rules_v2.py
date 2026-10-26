# -*- coding: utf-8 -*-
"""投递层 push_rules 三端语义+FAN_OUT 收口 TDD RED 底稿 (第四波主力卡)。

依据: 统筹派单 sm-b35cfb1e + 09-10 病理单 (16 条 intraday_alert 双发/
三端无语义分工/双轨后门) + 宪法 #12 白名单闸。
hermetic: yaml 断言纯文件读, 零网络零生产库。

RED 语义 (施工前必须红):
- P1 三端语义: delivery.yaml 按 白名单闸形态重写 — 每消息类型显式映射
  (技术报告→feishu_group 主通道/运维件→ops_archive 归档/盘中警示→feishu_dm
  单通道); 未知类型默认拒 (default_deny: true)。
- P2 FAN_OUT 收口: intraday_alert production 收口 [feishu_dm] 单通道;
  fan_out_types 清单保留 (广播语义保留但通道清单单端化)。
- P3 后门删除: Router._load_rules 无 push_rules.yaml 文件直读 fallback
  (配置单源铁律: registry 无数据=显式空, 禁双轨)。
- P4 运维件分离: ops_report production→[ops_archive] 归档通道 (新
  channel 类, 写 outbox/归档不 DM 用户); dev_verify 同归档面。
"""
import inspect

import yaml


def _load_rules():
    with open("config/defaults/delivery.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["delivery"]


class TestP1ThreeTerminalSemantics:
    def test_default_deny_gate(self):
        d = _load_rules()
        assert d.get("default_deny") is True, (
            "白名单闸: 未知消息类型默认拒 (宪法#12)")

    def test_channel_semantics_map_present(self):
        d = _load_rules()
        sem = d.get("channel_semantics", {})
        assert sem.get("feishu_group") == "技术报告主通道"
        assert sem.get("feishu_dm") == "用户个人直达"
        assert "ops_archive" in sem, "运维归档通道须有语义定义"

    def test_tech_reports_route_group_primary(self):
        rules = _load_rules()["push_rules"]
        for ct in ("morning_brief", "premarket_brief", "midday_brief",
                   "evening_report", "weekly_summary"):
            assert rules[ct]["production"] == ["feishu_group"], (
                f"{ct} 技术报告=群主通道单端 (群失败自动降级 dm 链由 "
                "report_mode 层管, 配置面单端收口)")

    def test_unknown_type_dropped_by_router(self):
        from delivery_layer.router import Router
        from delivery_layer.message import Message
        r = Router()
        msg = Message(content={"x": 1}, content_type="nonexistent_type",
                      title="t")
        ch = r.resolve(msg, report_mode="production")
        assert ch == [], "白名单闸: 未注册类型 production 默认拒"


class TestP2FanOutCollapse:
    def test_intraday_alert_single_channel(self):
        rules = _load_rules()["push_rules"]
        assert rules["intraday_alert"]["production"] == ["feishu_dm"], (
            "盘中警示=用户个人面, DM 单通道 (病理单: 16 条双发噪音根除)")

    def test_alert_critical_stays_dm(self):
        rules = _load_rules()["push_rules"]
        assert rules["alert_critical"]["production"] == ["feishu_dm"]

    def test_fan_out_types_semantics_kept(self):
        d = _load_rules()
        assert "intraday_alert" in d.get("fan_out_types", []), (
            "fan_out 语义保留 (广播模式本身不删, 删的是通道清单双端)")


class TestP3BackdoorRemoval:
    def test_router_no_yaml_file_fallback(self):
        from delivery_layer.router import Router
        src = inspect.getsource(Router)
        # 断言锚在代码面非注释面 (docstring 说明「后门已删」合法提及):
        # 旧后门三特征 = yaml 文件 open 直读 + safe_load + os.path.exists
        assert "yaml.safe_load" not in src, (
            "配置单源铁律: Router 禁 yaml 文件直读 (双轨后门)")
        assert "os.path.exists(path)" not in src, (
            "Router 不再做 push_rules.yaml 存在性探测")
        assert src.count("_load_rules") >= 2, "单源读取路径须在"
        assert "get_section" in src, "registry 单源面须在"

    def test_router_unknown_mode_returns_empty_not_dm(self):
        from delivery_layer.router import Router
        from delivery_layer.message import Message
        r = Router()
        msg = Message(content={"x": 1}, content_type="intraday_alert",
                      title="t")
        # rules 缺 mode 键 → 空而非默认 dm 兜底 (白名单闸语义)
        ch = r._resolve_from_rules({"intraday_alert": {}}, msg, "production")
        assert ch == []


class TestP4OpsSeparation:
    def test_ops_report_routes_to_archive(self):
        rules = _load_rules()["push_rules"]
        assert rules["ops_report"]["production"] == ["ops_archive"], (
            "运维日报归档面, 不直灌用户 DM (宪法#12 施工面)")
        assert rules["dev_verify"]["production"] == ["ops_archive"]

    def test_ops_archive_channel_exists(self):
        from delivery_layer.channels.ops_archive import OpsArchiveChannel
        ch = OpsArchiveChannel()
        assert ch.name == "ops_archive"

    def test_ops_archive_send_writes_no_network(self):
        """归档通道 send() = 落地仓/outbox 记录, 零网络零 DM。"""
        from delivery_layer.channels.ops_archive import OpsArchiveChannel
        import inspect as _i
        src = _i.getsource(OpsArchiveChannel.send)
        assert "requests" not in src and "webhook" not in src.lower(), (
            "归档通道零网络面")
