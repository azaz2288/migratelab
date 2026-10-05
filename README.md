# MigrateLab

SQLite 升级前的“演练场”：通过只读连接和 SQLite backup API 创建独立副本，迁移只在副本执行。可以先发现 SQL 错误、外键/唯一约束破坏、表被删或行数意外下降，再决定如何修改自己的正式迁移流程。

Python 3.12+，无运行时第三方依赖。

```sh
python examples/demo.py
python -m migratelab source.sqlite migration.sql new-preview --preserve-table players
python -m unittest discover -s tests -v
python -m pip install .
migratelab source.sqlite migration.sql new-preview
```

演示临时创建游戏角色数据库，成功添加等级字段；第二次故意删除角色，行数保护触发后回滚。临时样例不碰你的数据库。

输出新目录包含 `preview.sqlite` 和 `report.json`：SQL摘要、执行数量、迁移前后schema/行数、schema变更和回滚结果。退出码0成功，1迁移被拒且回滚，2输入/预演未完整完成。已存在输出绝不覆盖；失败预演副本保留方便排查，不自动删除。

## 已实现与保护边界

- 源数据库以 `mode=ro` 连接，backup API处理WAL中已提交但未checkpoint的数据。
- 显式事务，语法/约束/行数保护失败回滚。SQL拆分用SQLite completeness parser，支持trigger和字符串里的分号。
- SQLite authorizer禁止事务控制、ATTACH/DETACH、PRAGMA、temp/虚拟表和文件/扩展函数；外键与integrity check前后验证。
- `--timeout` 总预算用于backup与SQLite VM progress handler（默认10秒、上限300秒），SQL上限256KiB。

**仅对你信任的SQL和数据库使用**，不是安全沙箱：SQLite/OS资源耗尽、原生漏洞、不执行VM进度回调的操作并无硬截止保障。大小/CPU/磁盘限额需OS隔离。只读打开WAL数据库可能需要访问/创建SQLite sidecar，原库的逻辑数据不迁移，不能把主文件字节hash当WAL逻辑快照的完整摘要。

行数不变不证明数据值不变；schema和错误文本可能包含敏感字段名/字面量，副本更包含全部原数据，因此目录与报告默认应私有。当前没有加密或版本迁移历史。源库并发更新时backup给出一致SQLite快照，而不是“预览等于未来正式执行”。不自动执行正式迁移。

## v0.2 数据不变量

`--preserve-data-table players` 要求表的列名/顺序与所有typed行值保持不变，检测行数相同但数据被改写，失败时回滚。SQLite分类型、binary collation排序后流式计算摘要，区别NULL/整数/实数/文本/BLOB，不依赖rowid和插入顺序；新增列也会失败。表越大验证成本越高，不包含rowid、index、trigger或访问权限等所有语义。

`--checks checks.json` 可声明最多100个只读scalar SQL验收，例如：

```json
[{"sql":"SELECT count(*) FROM players WHERE level < 1", "expected":0}]
```

每个query必须返回恰好一行一列且类型和值都匹配；bool期望拒绝，以免与SQLite整数混淆。authorizer只允许read/SELECT及受控函数，不能把DELETE RETURNING伪装成验收检查。checks只在副本迁移后、commit前执行，任何失败回滚整个迁移。检查SQL仍须可信，复杂query没有OS硬资源隔离。

## 后续验收

1. 版本迁移图、schema版本与跳级/重复迁移策略。
2. 在现有typed数据摘要和只读scalar checks基础上扩展声明式关系不变量与版本兼容管理。
3. 锁竞争、磁盘空间故障、备份中断的故障注入测试。
4. 大型WAL数据库性能和硬资源隔离选项。
5. 报告隐私控制、GUI审阅与签名版本发布。

新作品集项目，不宣称符合飞书活动原有私有仓库准入。
