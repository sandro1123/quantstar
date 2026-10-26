# AI Agent 协同与开发纪律指南 (Agent Collaboration & Disciplines)

本文档面向使用 AI 编程助手（Claude Code、Cursor、GitHub Copilot、Codex、Kimi 等）参与 QuantStar 协同开发与代码贡献的工程师。

---

## 核心开发纪律

### 1. 断言必须证明自己会红（变异验证）

一个**从不报红的测试断言与没有测试等价**。在编写任何核心业务、风控或状态机测试时，必须遵循以下纪律：

- **反向变异验证**：任何顺序、时序或边界断言，必须附带一条验证：“当刻意注入错误或改回错误实现时，断言必须能够精准报错变红”；
- **工具支持**：框架在 `tests/unit/_mutation.py` 中提供了变异验证工具：
  - `assert_mutation_caught(mutate, check, what)`：验证注入变异后 `check` 必须报红，对永绿断言直接报错；
  - `assert_no_mutation_effect(mutate, check, what)`：验证注入无关变更后测试依然正常通过（防止断言过刚误伤）；
  - `assert_order(calls, expected)`：验证严格调用顺序。
- **纪律载体**：任何关键组件重构或新断言引入，必须经受 `assert_mutation_caught` 的敏感性考验。

---

## 架构与测试底线

1. **单向分层依赖**：严格遵守十层架构规范（`docs/ARCHITECTURE.md`），严禁跨层直接操作数据库；
2. **时点数据 (PIT) 语义**：财务与行情数据严禁未来函数，严格绑定真实披露日（`announced_at`）；
3. **三态律规范**：严格区分 `NONE`（查不到/故障）、`EMPTY`（事实为空/正常）与 `ERROR`（异常），禁止用 0 或 None 静默掩盖异常；
4. **代码规范与凭据安全**：
   - 提交前必须通过 `ruff check src/` 静态扫描；
   - 严禁提交任何真实的生产 API Key、Token 或个人绝对路径。
