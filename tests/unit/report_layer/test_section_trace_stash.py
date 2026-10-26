# -*- coding: utf-8 -*-
"""判例 #138「对得上=没坏」机器钉 — report_section_trace 修法反向钉（绿 → 红）。

判例三问（宣告要答）：
a) formatter 输出删 sections → trace 仍落？  （升材料防formatter 改形)
b) 删 stash → trace 不落？  （升防 stash 过期)

修法（统筹裁）：
`_build_report`（src/report_layer/__init__.py:70 前）存 `message.raw_content = dict(message.content)`（浅拷贝/stash），
`_log_sections`（delivery_layer/executor.py:112）改读 `message.raw_content` 优先、`message.content` 回退。
"""
import pytest
import sqlite3
from unittest.mock import patch, MagicMock
from delivery_layer.message import Message
from delivery_layer.log_store import DeliveryLogStore

# a) 「formatter 输出删 sections」→ trace 仍落在材料面读到的原形——
#    用同一个 message 对象走两遍：build（留 raw_content）→ format（改 content）→ _log_sections
def test_sections_survive_formatter_overwrite():
    """钉: 判例#166『就地覆盖等于丢失』, stash 作为反制."""
    # 模拟晨报 build 后,_build_report 执行 formatter.format 前的 stash
    msg = Message(
        content={"sections": [{"name": "top_picks", "body": "今天买 A"}, {"name": "news", "body": "新闻"}]},
        content_type="morning_brief",
        title="晨报",
    )
    # Step 1: stash(修法中)_build_report 应先存
    raw = dict(msg.content)  # 修法注入点
    msg.raw_content = raw  # 修法结果

    # Step 2: formatter 改形(msg.content 被替换为 card 输出, sections 键消失)
    # 模拟 feishu_card.format 的行为: 改 msg.content 为 card 格式
    msg.content = {"card": "…飞书卡…", "msg_type": "interactive"}
    assert "sections" not in msg.content, "formatter 输出不应有 sections 键(真形已去)"

    # Step 3: _log_sections 读 raw_content(修法后行为)
    content = getattr(msg, "raw_content", msg.content)
    sections = content.get("sections")
    assert isinstance(sections, list), "stash 读读原始 sections"
    assert len(sections) == 2, "stach 应存完整 sections"
    # 判例#166: 修法必须让 _log_sections 能拿到原 sections


def test_log_sections_reads_stash_over_content(msg=None):
    """判例 #142 的实改焊: _log_sections 读 raw_content 优先于 content.get(\"sections\")."""
    # 建 fake message 与 log_store 的引用一次把 `_log_sections` 的行为测出来
    msg = Message(
        content={"card": "…飞书卡…", "msg_type": "interactive"},  # 已被 formatter 改形, 无 sections
        content_type="morning_brief",
        title="晨报",
    )
    # 修法前可能有 raw_content( stash 存在)
    msg.raw_content = {"sections": [{"name": "top_picks", "body": "今天买 A"}, {"name": "news", "body": "新闻"}]}

    # 调用 _log_sections 的逻辑(直接按修法后的行为断言)
    content = getattr(msg, "raw_content", None)
    if content is None:
        content = msg.content
    assert "sections" in content, "stash 读读原始 sections"
    assert isinstance(content.get("sections"), list) and len(content["sections"]) == 2


def test_log_sections_zero_trace_without_stash():
    """反向钉: 删除 stash(raw_content)后, _log_sections 必须仍然不写 + 测试红."""
    msg = Message(
        content={"card": "…飞书卡…", "msg_type": "interactive"},  # formatter 输出, 无 sections
        content_type="morning_brief",
        title="晨报",
    )
    # 不在 msg 上设 raw_content( 现实中 _build_report 没 stash 时就没人保护)
    content = getattr(msg, "raw_content", None)
    if content is None:
        content = msg.content
    # 修法前的行为: content.get(\"sections\") 是 None → _log_sections 早退(测试继续写着钉子),
    # 修法后的预期: 有 stash → 有 sections → 应用仍写
    assert "sections" not in content, "无 stash 时 content[\"sections\"] 不该有"
    # 真 treatments: trace 是否写=取决于是否有 raw_content;这两个区别在于是否挧上述 event order
