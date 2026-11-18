# papertrail — 抓 arXiv/Crossref 元数据，DOI 去重、预印本/正式版本归并、作者消歧。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`author_id`, `version`, `doi_map`, `harvest`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

## 标识层（doi_map）
`doi_map.py` 把同一学术工作的多种写法（URL / 大小写 / 裸前缀 / 代理 / 旧 DOI /
arXiv 新旧 id 与版本号 / Crossref·arXiv 异形字段）归并到稳定身份主键，并把每个
原始写法作为 append-only 证据落在 SQLite（`doi_alias.db`），提供五张事实表：
`identifiers`（身份锚点与冲突计数）、`aliases`（写法→身份的可追踪映射）、
`events`（register/conflict/dead/pending/supersede/invalid 审计）、
`version_chain`（版本与升级链）、`batches`（批量重试/中断恢复进度）。

下游消费同一套稳定键：

* harvest 增量游标 —— `canonical_key_of` / 记录上的 `harvest_cursor`，历史写法与
  别名升级都映射到同一游标位置；
* version 版本链 —— `version_chain_of`，预印本↔正式版（`preprint_of`）、旧 DOI→新
  DOI（`supersede`）、arXiv 版本号（`version`）；
* dedup 合并判断 —— `doi_key` / `doi_work_key` / `doi_same_work` 与 `dedup_group`。

两条明确策略（可重放、不靠覆盖字段）：

1. **失效链接不影响身份确认**。身份由规范字符串决定，链接能否解析只是别名状态。
   `dead_link_policy="legacy"`（默认）下失效别名沿用旧键、仅置 `dead`；
   `"pending"` 下未绑定的失效写法进入待审映射（别名 `key=NULL,status=pending`，
   记录打 `doi_review=["dead_alias"]`），裁决后再绑定。
2. **别名升级保住历史键**。旧写法永久解析到首次确认的键，另登记 `supersede` 链边
   指向新键；`canonical_key` 沿链返回当前代表键，重放任意历史快照解释不变。

同 DOI 元数据冲突不静默覆盖：保留首锚值，冲突字段写入 `events.conflict` 并累加
`identifiers.conflict`，记录打 `doi_conflict=true`。错误 DOI 进入 `review` 阶段、
使用确定性 `invalid:` 键隔离，不参与身份归并但证据可追踪。批量写入按批次提交，
中断后凭 `batches.done` 续跑，`register`/`supersede` 幂等保证重入结果一致。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
标识层测试（`tests/test_doi_map.py`）默认使用内存 mapper，可在无 pytest 环境用
`python3 -I -m unittest discover -s tests` 运行。
