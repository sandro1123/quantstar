# 数据源凭据清单(要申请什么、填哪里、缺了会怎样)

> **先说我做不到的事**:我无法代你注册任何账号或申请 API Key。注册需要你的邮箱/手机/实名/支付信息,而且平台条款通常要求由账号持有人本人操作。
>
> 我能做的是把"要申请什么"变成一张可执行清单 —— 每个凭据**解锁本仓库的哪条数据链、填哪个环境变量、缺了会降级成什么**。下面每一条都对着代码里的实际读取点,不是我猜的行业惯例。

---

## 1. 代码已经在读的凭据(现状)

来源:`.env.example` 与 `src/data_layer/sources/` 各适配器。

| 环境变量 | 谁在读 | 必填性 | 缺了会怎样 |
|---|---|---|---|
| `FUYAO_API_KEY` | `sources/fuyao_client.py` | **prod 启动硬校验**(CF-01,缺则拒载) | 涨停梯队/竞价/大宗等**单源独占**能力整段消失 |
| `TUSHARE_TOKEN` | `sources/tushare_source.py` | 可选 | `kline_bulk`/`financial` 少一条主腿,退到 baostock/akshare |
| `MX_APIKEY` | `.env.example` 声明 | 可选 | 见该源文档;代码侧无硬校验 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `delivery_layer/channels/feishu_*`、`hitl_ws_daemon` | **prod 必填** | 三报/复盘/盘中卡全部无法投递;HITL 审批链不可用 |
| `FEISHU_DM_ID` / `FEISHU_GROUP_ID` | 同上 | 可选 | 不配则只有群或有只有 DM,报告送错地方 |
| `QS_API_TOKEN` | `web/api.py` + `scripts/qs_dash_auth_proxy.py` | 对外暴露时必需 | Web/看板裸奔或 401 |
| `MODELSCOPE_API_KEY` / `NVIDIA_API_KEY` / `OPENROUTER_API_KEY` / `SENSENOVA_KEY1..4` | `agent_layer/llm/*` | 可选 | 对应 LLM 腿降级走备链(见 `config/defaults/llm.yaml` 主链) |

**不需要 key 的源**(已集成,零配置):`akshare`、`akshare_sina`、`baostock`、`easyquotation`、`gtimg`、`mootdx`、`eastmoney`、`tonghuashun`(网页抓取)、`zzshare`、`adata`。

这些免费源的问题不是"能不能用",是"**上游限流与接口变动**":`source_health` 的交叉验证就是为了抓这种情况(DL-26)。免费源能跑通日常,但不该是唯一一条腿。

---

## 2. 建议补的凭据(按对数据链的价值排序)

判断依据是本仓库 `DATA_TYPES`(见 `data_layer/sources/protocol.py`)里哪些类型目前只有免费源:

### ① Tushare Pro —— 补 `kline_bulk` / `financial` 的稳定主腿

- **解锁**:全市场按日批量 K 线(回填主力)、财报与估值字段
- **为什么先要**:`kline_bulk` 是回填 250 日历史时的吞吐瓶颈;免费源在这个规模上必然被限流
- **填哪**:`TUSHARE_TOKEN`,代码已在读,**只差一个值**
- **拿**:tushare.pro 注册后到个人主页取 token(积分制,基础接口够日频用;高级接口看积分门槛)

### ② 商业行情主源(你的 `FUYAO_API_KEY` 已经在位)

扶摇已是 prod 必填,是当前唯一"token 型主源"。如果它的额度或覆盖有边界,值得再补一条**同构备链**(宪法#5 单点必有对冲)—— 具体接哪家取决于你实际能拿到的授权,我不替你选。

### ③ 搜索/新闻 API —— 补 `news` 链路

- **解锁**:`news_collector` 的实时资讯、个股新闻归因(`news_attribution`)
- **为什么重要**:现在的新闻主要靠网页抓取,**抓取失败=静默无新闻**,而报告里的"为什么选它"依赖它
- **候选类别**(按接入成本):通用搜索 API(结构化返回,带配额)、中文优化搜索 API、自建 SearXNG(无配额但需服务器)
- **填哪**:目前没有对应的 env 变量 —— **需要先做接入**(`docs/BENCHMARK_DAILY_STOCK_ANALYSIS.md` 的 P1-③ 已排期)

### ④ 港股/美股源 —— 只在真要扩市场时申请

现在不需要。扩市场的前置阻塞在 `common/calendar.py` 的 A 股硬编码节假日,不在凭据。

---

## 3. 申请到手后怎么验证(不靠猜)

```bash
# 1. 源有没有被注册、能力覆盖到哪(本仓 qs data sources,见 PR #22)
qs data sources --json | jq '.summary, .singles, .degraded, .gaps'

# 2. 健康探测(连通性 + 数据层交叉验证)
qs health

# 3. 真实落库对账(避免"任务 success 但零行")
qs recon
```

`data sources` 的四态里:

- `single` = 这个类型只有一条腿,正是需要补 key 的地方;
- `degraded` = 源注册了但当前不可用 —— **最常见的原因就是 key 没填/过期**。

---

## 4. 安全约束(必须遵守)

1. **只在 `.env` 里填,永不进 git**。`.env` 已在 `.gitignore`;`.env.example` 只放空值;
2. CI 有 gitleaks(全分支全 tag 扫描),密钥一旦提交即被拦,但**历史里仍然留下**;
3. 多段 Cursor/Claude 之类的 Agent 会话里粘贴 key 时,注意它可能进入对话记录;
4. `QS_API_TOKEN` 至少 16 位随机:`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`。

---

## 5. 一句话建议

**先补 `TUSHARE_TOKEN`**(代码已在读,零改动,直接补一条 `kline_bulk`/`financial` 主腿),**再评估新闻搜索源**(需要先做接入,但它是"为什么选它"这句话的真实性来源),**港股/美股暂时别申请** —— 现在卡住的是日历和 PIT 财年推导,不是凭据。

---

*本清单的"谁在读"一栏取自仓库实际代码与环境变量声明;各平台的具体额度/门槛以官方文档为准,我未替你做额度承诺。*
