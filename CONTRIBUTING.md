# Contributing to QuantStar

感谢对 QuantStar 项目的关注与贡献。为了保持代码库的工程纪律与架构完整性，请遵循以下规范：

---

## 一、提交规范 (Conventional Commits)

Commit Message 必须采用标准 Conventional Commits 格式：

- `feat(layer): 新功能描述`（例如 `feat(strategy): add momentum breakout factor`）
- `fix(layer): Bug修复描述`（例如 `fix(data): skip suspended rows in kline pipeline`）
- `docs: 文档更新`
- `refactor(layer): 重构`
- `test(layer): 测试新增或更新`
- `chore: 依赖与工程维护`

---

## 二、架构契约与开发纪律

1. **单向依赖原则**：
   严格遵守十层单向依赖架构，杜绝反向依赖或跨层直接操作数据库（禁止在 Strategy/Agent 层直接写 SQL 操作数据表）；
2. **时点数据 (PIT) 语义**：
   数据管道与财务数据计算严禁使用未来数据，财报数据必须严格按 `announced_at`（真实披露日）对齐；
3. **三态律规范**：
   业务查询与数据检查必须严格区分 `NONE`（系统查不到，故障）、`EMPTY`（查到了事实确实为空，正常）与 `ERROR`（驱动或网络异常），严禁用 0 或 None 静默抹平异常；
4. **反向变异测试 (Mutation Testing)**：
   为关键风控逻辑或数据检查编写测试时，必须同时配备反向变异验证（故意破坏核心逻辑后测试必须当场报红）；
5. **严禁硬编码敏感信息**：
   任何测试代码或文档中严禁包含真实的生产 API Key、Token 或个人绝对文件路径。

---

## 三、本地验证流水线

在向 `main` 分支发起 Pull Request 前，请务必在本地运行以下三道门禁并保证 100% 通过：

```bash
# 1. 运行核心单元测试
pytest tests/unit/ -q

# 2. 运行跨层契约集成测试
PYTHONPATH=src pytest tests/integration/ -v

# 3. 运行代码规范检查 (必须 0 警告 0 错误)
ruff check src/
```

---

## 四、AI Agent 协同指引

如果你使用 Claude Code、Cursor、GitHub Copilot、Kimi 等 AI 工具辅助开发，可以直接使用 [README.md 第九节](README.md#九ai-agent-协同指引-prompts-for-ai-agents) 提供的标准化 Prompt，引导你的 Agent 按照本项目的架构契约与测试规范提交 Pull Request。
