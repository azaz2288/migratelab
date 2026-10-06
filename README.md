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

## v0.3 受控版本与迁移链

单次SQL可声明版本边：

```sh
migratelab source.sqlite migration.sql new-preview --from-version 2 --to-version 3
```

以backup完成后的副本 `PRAGMA user_version` 为准。起点不匹配时SQL一条也不执行，报告拒绝并回滚；目标版本由工具在同一事务内设置，用户SQL仍不能执行PRAGMA。既有无版本参数的SQL模式不变，报告增加迁移前后 `user_version`，不隐式改版本。

批量升级把 `migration` 输入切换成JSON（不自动读取任何工程内策略）：

```json
[
  {"from_version": 2, "to_version": 3, "sql": "ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;"},
  {"from_version": 3, "to_version": 5, "sql": "UPDATE players SET level=2;"}
]
```

```sh
migratelab source.sqlite chain.json new-preview --chain --preserve-table players
python examples/version_chain.py
```

Python API为 `preview_chain(source, output, migrations, ...)`，支持原有保留表/typed数据/只读checks/timeout选项。链要求1至100条，版本必须是0至2147483647的严格整数（bool拒绝），每条递增、相邻边起终点衔接，SQL总量与JSON文件均限256KiB。允许明确声明3到5的边；不猜跳级路径、不排序或自动选择分支。重复边、降级、断链、不完整版本参数在创建输出前拒绝。

整条链是**一个事务**；中途SQL或最终不变量失败，先前结构、数据和版本一起回滚。`migration_chain` 报告每条边、SQL摘要和成功执行的语句数（不等于已提交）；`after.user_version` 是实际提交/回滚后的版本。多步顶层摘要是规范JSON链的SHA256，单步仍为原始SQL SHA256。版本只是应用自报整数，不证明对应schema正确；请同时使用不变量。当前不是版本图自动寻路，也不是正式库的持久迁移历史账本。

## 后续验收路线

1. 已实现显式迁移图审阅；后续增加受控持久版本历史，不能自动猜正式迁移路径。
2. 在现有typed数据摘要和只读scalar checks基础上扩展声明式关系不变量与版本兼容管理。
3. 已覆盖基础锁竞争、空间不足、备份/报告中断；继续进程强杀、OS限额与大型WAL故障验收。
4. 大型WAL数据库性能和硬资源隔离选项。
5. 报告隐私控制、GUI审阅与签名版本发布。

新作品集项目，不宣称符合飞书活动原有私有仓库准入。

## v0.3.1 报告发布与故障验收

报告先写入同目录独占临时文件，完整序列化、flush和fsync后关闭，再用hardlink独占发布`report.json`。最终名字不暴露写入中的部分JSON，不覆盖竞争writer的文件；仅清理本次临时文件，始终保留副本。文件系统不支持hardlink时失败关闭，不降级为覆盖式rename。这个协议**不保证目录fsync或断电恢复**，输出目录仍须由可信使用者控制，并非抵御恶意同权限进程的沙箱。

CLI只有报告完整发布后才退出0或1。报告输出失败退出2/`complete:false`；这时副本可能已经成功commit，不能把报告失败解释为迁移已回滚。若有竞争writer，现存报告不是本次结果，不应仅凭目录中存在JSON判成功。不要复用失败输出目录；检查后用新目录重试。

新增故障测试仅使用合成临时数据库：真实源EXCLUSIVE锁与副本写锁、多个backup chunk中途注入I/O失败、SQLite副本`max_page_count`产生真实`SQLITE_FULL`、JSON写入/文件同步/发布失败、竞争writer及成功/拒绝报告发布。没有填满宿主磁盘或读取用户数据库。`SQLITE_FULL`可能由SQLite自动回滚；工具保持保守退出2，不将未执行的显式回滚认证为成功。备份中断也不给不完整副本出具通过报告。

```sh
python -m unittest discover -s tests -p test_faults.py -v
```

当前timeout是SQLite回调的协作预算，锁获取与OS I/O可能超出；这些注入不等同断电、文件系统崩溃、真实磁盘满或进程强杀验证。后续仍需要OS硬资源隔离与恢复流程。

## v0.4 迁移图审阅与明确选择

同一JSON格式现在也可声明分支、断开的版本分量和跳级边，不必组成一条连续链。先审阅目录，无需提供或打开数据库：

```sh
python -m migratelab.review catalog.json
migratelab-review catalog.json --route 2 3 5
python examples/graph_review.py
```

审阅只输出规范图摘要、版本、起点/终点、分支、每条边的SQL摘要与语句数量，以及可选的明确路径；不输出SQL正文、不执行SQL、不读取schema。`complete:true`仅表示声明/版本/SQL拆分检查完成，**不是SQL语义正确、schema兼容或安全批准**。未选边也要通过同样的输入检查。错误退出2，仅输出不含输入正文的失败消息，不给出部分审阅结果。

```json
[
  {"from_version":2,"to_version":3,"sql":"ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;"},
  {"from_version":3,"to_version":5,"sql":"UPDATE players SET level=2;"},
  {"from_version":2,"to_version":5,"sql":"DROP TABLE players;"}
]
```

明确选择2→3→5，不会执行未选的2→5；即使只有一条可能路径也不自动选择：

```sh
migratelab source.sqlite catalog.json new-preview --graph --route 2 3 5 --preserve-table players
```

可加 `--expect-graph-sha256` 并传入审阅输出的64位小写 `graph_sha256`，防止审阅后目录改变。不同摘要在数据库访问和输出创建前拒绝，连未选边的SQL变化也会拒绝。摘要覆盖按from/to排序的完整边目录（字段from_version/to_version/sql），用 `json.dumps(..., ensure_ascii=True, sort_keys=True, separators=(',', ':'))` 的UTF-8字节计算SHA256；输入边排序不影响它，SQL空白变化会影响它。它不是身份签名、来源认证或人工审批，不能还原SQL，请自行保留原目录。此绑定是可选项，默认不代表已有人审阅。

图上限100条边/SQL合计256KiB，CLI JSON另限256KiB；版本范围和严格类型沿用链模式，重复版本对即使SQL相同也拒绝，不作覆盖。递增边天然无环，不枚举所有可能路径；可以显式选择某个分量的一段2至101个版本的路径，起点必须与backup副本版本一致。所有选择的边仍在一个事务中执行，最终不变量失败会整体回滚。既有SQL与 `--chain` 保持兼容；`--graph` 不可混用 `--chain` 或单边版本参数。

Python接口：`from migratelab.graph import review_graph, preview_graph`；后者必须传 `route=[2,3,5]`，可选 `expected_graph_sha256=review['graph_sha256']`，其余保留表/typed数据/checks/timeout策略与链模式一致。预演报告schema版本1增加 `graph_review` 元数据，`migration_chain` 仅列选中的执行边；旧读取器应忽略额外字段。报告发布故障仍退出2，副本可能已commit，不能称为回滚。

没有自动路由、持久历史账本、正式库执行、GUI或签名审批。审阅输出隐藏SQL正文不等于全部产物不敏感：原目录含SQL，预演报告schema/错误文本和副本仍可能含私密信息。哈希本身也不是匿名化保证；资源隔离与协作timeout局限不变。
