# -*- coding: utf-8 -*-
"""合规免责句 — 延续 Kimi 落地措辞（feishu_card.py DISCLAIMER 同源），双位置：

1. 卡片头 subtitle（formatters/feishu_card.py 已落，Kimi 6e808b7b）
2. 风险提示段（sections/risk_compliance.py COMPLIANCE_LINE 已落，Kimi 6e808b7b）
3. 模块1b新增：每条推荐正文段尾（stock_picks / tomorrow_picks 接入本处）

单一来源定义于本模块，其余处引用（防措辞漂移——Kimi 卡与 1b 卡共用一个串）。
"""
DISCLAIMER = "本系统为模拟盘研究性质，非投资建议"
