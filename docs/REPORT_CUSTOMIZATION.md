# 报告定制与多平台推送配置指南 (Report & Delivery Guide)

QuantStar 采用了“内容组件化拼装（Report Layer）+ 渠道多态投递（Delivery Layer）”的解耦架构。

**重要说明**：本仓库中**不包含、也不存储任何预先生成的静态报告文件**。所有盘前速报、晨报、午报、晚报与周总结，均是在定时任务触发时，由代码读取数据库最新截面数据，在内存中动态组装为 `Message` 对象，随后路由投递至指定的接收端。

本文档站在使用者角度，说明如何定制专属报告结构、如何切换输出格式，以及如何配置不同的推送通道。

---

## 目录

1. [报告层工作机制](#1-报告层工作机制)
2. [如何定制专属报告内容](#2-如何定制专属报告内容)
   - [第一步：编写自定义报告段 (Section)](#第一步编写自定义报告段-section)
   - [第二步：组装或创建自定义报告生成器 (ReportBuilder)](#第二步组装或创建自定义报告生成器-reportbuilder)
   - [第三步：注册到报告层门面](#第三步注册到报告层门面)
3. [支持的输出格式与排版风格](#3-支持的输出格式与排版风格)
4. [支持的平台与通道配置](#4-支持的平台与通道配置)
   - [飞书 / Lark (原生富文本卡片)](#1-飞书--lark-原生富文本卡片)
   - [纯文本与本地日志归档](#2-纯文本与本地日志归档)
   - [企业微信 / 钉钉 / Webhook (基于 Apprise 扩展)](#3-企业微信--钉钉--webhook-基于-apprise-扩展)
5. [投递路由与模式切换 (`delivery.yaml`)](#5-投递路由与模式切换-deliveryyaml)

---

## 1. 报告层工作机制

报告生成的完整链路如下：

```
 [定时任务触发]
       │
       ▼
 ┌───────────────┐     注入上下文 (DB / 策略 / 行情)
 │ ReportContext │ ───────────────────────────────────┐
 └───────────────┘                                    │
                                                      ▼
 ┌───────────────┐     sections()                     ┌───────────────┐
 │ ReportBuilder │ ─────────────────────────────────▶ │    Section    │ ──▶ 生成 SectionResult
 └───────────────┘                                    └───────────────┘     (标题、正文、数据源)
       │
       ▼ 组装各段落
 ┌───────────────┐
 │    Message    │ (含 content_type, title, raw_content)
 └───────────────┘
       │
       ▼ 格式化 (Formatter)
 ┌───────────────┐
 │ DeliveryLayer │ ──▶ 匹配 delivery.yaml 路由规则 ──▶ 推送至飞书/Webhook/终端
 └───────────────┘
```

每个报告由若干个独立的 `Section` 组成。如果某个段落所需的数据缺失，该段落会自动返回 `None` 或显式标注降级，其他段落正常拼装，避免因局部数据异常导致整份报告崩溃。

---

## 2. 如何定制专属报告内容

系统内置了标准报告生成器（位于 `src/report_layer/reports/`），例如：
- `morning_brief.py`：早报（指数概览、候选池标的、大模型辩论综述）；
- `midday_brief.py`：午报（盘中持仓表现、异动情况）；
- `evening_report.py`：晚报（全天操作复盘、持仓净值归因）；
- `weekly_summary.py`：周报（周度胜率、盈亏比、回测与实盘偏离度）。

如果你希望增加自己的专属段落（例如加入大盘资金流向、自定义宏观情绪等），只需三步：

### 第一步：编写自定义报告段 (Section)

在 `src/report_layer/sections/` 下创建新文件（例如 `macro_summary.py`），继承 `Section` 基类：

```python
from report_layer.sections.base import Section, SectionResult

class MacroSummarySection(Section):
    @property
    def name(self) -> str:
        return "macro_summary"

    def build(self, context) -> SectionResult | None:
        # context 中包含 data_layer, strategy_layer, db 等上下文实例
        db = getattr(context.data_layer, "db", None) if context.data_layer else None
        if not db:
            # 数据不可得时安全跳过，绝不阻塞整份报告
            return None

        # 从数据库读取你关心的自定义统计指标
        conn = db.get_connection()
        # row = conn.execute("SELECT ...").fetchone()
        
        title = "🌐 宏观与资金截面"
        body = (
            "今日北向资金：+25.3 亿元\n"
            "市场涨跌比：3200 : 1800\n"
            "两市成交额：1.12 万亿元"
        )
        return SectionResult(name=self.name, title=title, body=body)
```

### 第二步：组装或创建自定义报告生成器 (ReportBuilder)

你可以直接修改既有的生成器，或者继承 `ReportBuilder` 创建一个全新的报告类型：

```python
from report_layer.reports.base import ReportBuilder
from report_layer.sections.macro_summary import MacroSummarySection
from report_layer.sections.tomorrow_picks import TomorrowPicksSection

class MyCustomReportBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "my_custom_report"

    @property
    def title(self) -> str:
        return "📊 我的量化自选分析内参"

    def sections(self) -> list:
        # 按期望展示的顺序拼装段落列表
        return [
            MacroSummarySection(),
            TomorrowPicksSection(),
        ]
```

### 第三步：注册到报告层门面

在 `src/report_layer/__init__.py` 的 `ReportLayer` 类中暴露该方法，即可通过 CLI 或定时任务调用：

```python
def build_my_custom_report(self, date: str, report_mode: str = "debug"):
    from report_layer.reports.my_custom_report import MyCustomReportBuilder
    return self._build_report(MyCustomReportBuilder(), date, report_mode)
```

---

## 3. 支持的输出格式与排版风格

框架支持通过 Formatter 将组装好的 `Message` 渲染为不同呈现形式（位于 `src/report_layer/formatters/`）：

1. **富文本交互卡片 (`feishu_card`)**：
   - 生成符合飞书开放平台 JSON 格式的高级交互卡片；
   - 带有彩色顶通（红/绿/蓝/橙，表征盈亏或预警级别）、字段分栏与折叠面板。
2. **纯文本 / Markdown 格式 (`text`)**：
   - 去除平台专有排版语法，生成结构清晰的纯文本内容；
   - 适用于命令行直接打印（`stdout`）、写入本地文件或发送邮件正文。

---

## 4. 支持的平台与通道配置

投递层（`delivery_layer`）负责将报告推送到使用者指定的终端。

### 1. 飞书 / Lark (原生富文本卡片)

适合团队协作、交易盯盘与手机即时推送。

#### 配置步骤：
1. 前往[飞书开放平台](https://open.feishu.cn/)创建自建应用，获取 App ID 和 App Secret；
2. 开启机器人能力，并将机器人拉入你的投研预警群；
3. 获取群的 `chat_id`（通常形如 `oc_xxxx`）；
4. 在 `.env` 中填写配置：
   ```bash
   FEISHU_APP_ID="cli_xxxxxxxx"
   FEISHU_APP_SECRET="yyyyyyyy"
   FEISHU_GROUP_ID="oc_zzzzzzzz"        # 技术报告主群
   FEISHU_DM_ID="oc_user_xxxx"          # 个人私聊直达（用于紧急止损与风控告警）
   ```

### 2. 纯文本与本地日志归档

如果不需要即时通讯工具推送，仅希望系统在本地静默运行并将报告存盘：
1. 在 `config/defaults/delivery.yaml` 中将模式切换为静默：
   ```yaml
   delivery:
     report_mode: "silent"
   ```
2. 运行生成报告命令，通过 CLI 输出并重定向到本地文件：
   ```bash
   python -m src.cli.main report morning_brief --dry-run > logs/morning_brief.txt
   ```

### 3. 企业微信 / 钉钉 / Webhook (基于 Apprise 扩展)

框架内置集成了第三方通知库 `apprise`。使用者可以通过统一的 URI 格式轻松接入数十种主流通知平台：

- **钉钉自定义机器人**：`dingtalk://access_token/`
- **企业微信机器人**：`wechat://key/`
- **通用 Webhook**：`json://my.webhook.server/endpoint`
- **电子邮件**：`mailto://user:pass@smtp.example.com`

#### 使用示例：
在 `src/delivery_layer/channels/` 中可以直接通过 Apprise 实例派发消息：
```python
import apprise

a = apprise.Apprise()
# 添加目标 Webhook
a.add("json://https://my-webhook-url.com/notify")
# 发送纯文本正文
a.notify(title=message.title, body=message.content["text"])
```

---

## 5. 投递路由与模式切换 (`delivery.yaml`)

推送行为由 `config/defaults/delivery.yaml` 集中管理：

### 1. 运行模式 (`report_mode`)
- **`production`**：生产模式。严格根据白名单规则路由推送（如早报发群、紧急止损告警发私聊），未在 `push_rules` 中声明的未注册消息默认拒绝发送（`default_deny: true`），避免产生骚扰信息；
- **`debug`**：调试模式。所有消息默认降级发送至个人私聊（`feishu_dm`）或终端输出，供开发者单独观察；
- **`silent`**：静默模式。阻断所有外部网络发送动作，仅落盘日志。

### 2. 路由矩阵 (`push_rules`)
```yaml
delivery:
  push_rules:
    morning_brief:
      production: [feishu_group]
      debug: [feishu_dm]
    evening_report:
      production: [feishu_group]
      debug: [feishu_dm]
    intraday_alert:
      production: [feishu_dm]      # 盘中预警直达个人，不发大群
      debug: [feishu_dm]
    alert_critical:
      production: [feishu_dm, feishu_group] # 严重故障双发
```

使用者可以根据自身组织结构和沟通习惯，自由分配各类报告的投递渠道与接收人员。