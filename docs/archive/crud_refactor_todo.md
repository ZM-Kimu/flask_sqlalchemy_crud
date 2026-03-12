# CRUD / 事务重构 TODO 列表（当前有效）

> 本文只保留尚未完成且仍有价值的事项。  
> 已完成的迁移工作（移除 `CRUDQuery/query/paginate`、2.x 主路径落地）不再重复列出。

## 1. 模块化与可选适配（P1）

- [ ] 评估是否需要提供独立 `flask_integration.py`（仅作为可选 glue 层，不影响核心 SQLAlchemy 路径）。
- [ ] 若新增适配层，明确公共入口与最小维护边界（不回流到核心模块）。

## 2. 文档与示例补强（P1）

- [ ] 增加“外部已有事务”的策略示例，覆盖 `existing_txn_policy` 的关键差异。
- [ ] 在 README 增加最小排错段：`session_provider` 缺失、事务冲突、`status_only` 使用建议。

## 3. 类型与文档同步（P2）

- [ ] 与 `docs/archive/todo.md` 对齐 2.x 类型路线：以 `select/execute/scalars/scalar` 为核心，不再扩展旧 Query 路径。
- [ ] 补充 `@CRUD.transaction` 的签名契约示例，防止未来改动导致类型退化。
