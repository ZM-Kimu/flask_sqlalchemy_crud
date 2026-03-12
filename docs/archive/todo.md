# sqlalchemy_crud_tx TODO（当前有效）

> 本文件只保留与 SQLAlchemy 2.x 主路径一致的后续事项。  
> 旧 `CRUDQuery/query/paginate` 路线已移除，不再作为维护方向。

---

## 1. 2.x 查询类型推导（长期）

- [ ] 继续收紧 `CRUD.select(...)` 多重重载（实体参数 0..8）在 Pyright strict 下的边界行为。
- [ ] 为 `execute/scalars/scalar` 增加更多类型契约样例，覆盖聚合、列投影、空结果分支。
- [ ] 明确 `Row` 命名属性访问仅为运行时能力，静态保证点保持在 tuple 位置类型。

## 2. 事务与 Session 类型建模（中期）

- [ ] 进一步收紧 `SessionLike` 协议，只保留库内部真实调用的方法。
- [ ] 为 `CRUD.transaction` 的 `ParamSpec` 契约补充跨模块测试，确保装饰后签名不退化。
- [ ] 评估是否提供独立 async 入口（若做，采用新 API 而非复用同步接口）。

## 3. 工程化收尾（中期）

- [ ] 持续补齐公共 API docstring 的参数/返回语义。
- [ ] 维护“常见误用排错”文档（缺失 configure、外部事务冲突、status_only 行为）。
- [ ] 维持 `pytest + pyright` 门禁，避免回流旧查询核心符号。

