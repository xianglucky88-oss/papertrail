# papertrail — 抓 arXiv/Crossref 元数据，DOI 去重、预印本/正式版本归并、作者消歧。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run           # 旧入口：扁平 fixtures，输出摄取数量和落库行数
make run-sources   # 多来源分页采集（fixtures/sources.json），输出每源摘要
make test          # 运行全部测试
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`author_id`, `version`, `doi_map`, `harvest`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 多源采集：游标与恢复点
`harvest.py` 负责分页采集，`store.py` 提供唯一的恢复点。

- **游标只在提交确认后前进。** `store.commit_harvest_page` 把本页记录、
  版本状态、来源游标、请求边界、响应版本、页指纹和请求轨迹放进
  **同一个事务**。事务 `committed=True` 之前，`harvest` 绝不更新游标；
  提交失败则回滚，重启从上一个已确认的恢复点继续，不会越过未落库论文。
- **空页的两种含义。** 空页 + 分页证据（`next` 边界、`total` 大于已见
  条数、`page < total_pages`）→ `empty_anomaly`（可重试，游标不动）；
  空页 + 证据表明枯竭 → `empty_end`（`status=complete`）。二者绝不混淆。
- **没有更多数据 ≠ 请求失败。** 只有有证据的空页 / 枯竭的 `next` 才产生
  `complete`；超时、HTTP 错误、提交失败只产生 `partial`/`failed` 并保留
  可继续的游标。不可重试的 4xx 记 `failed`，可重试失败记 `partial`。
- **重复页 / 重放不重复写入。** 页内容指纹落在 `harvest_pages`；已提交页
  再次出现记 `duplicate_page` 并整体跳过写入。`next` 成环记
  `pagination_loop`，`total` 说还有数据但没有 `next` 记 `truncated`——
  两种情况都保留本页数据、游标不前进、状态如实为 `partial`。
- **单源失败可继续。** 各来源状态独立（`harvest_state`）；一个来源失败
  不影响其他来源，进程重启后各源从自己的恢复点续采。

### 资源上限
- **限速**：`RateLimiter` 按来源最小间隔请求，可注入 `clock`/`sleep` 离线验证。
- **缓存**：`PageCache` 线程安全、容量硬上限（LRU 淘汰）+ TTL 过期；
  异常页会被作废，避免把坏响应带进重启。
- **并发**：`max_workers` 封顶并发来源数；每个来源独立连接，SQLite 以
  WAL + busy_timeout 排队写入，缓存由所有来源共享并受同一上限约束。
- **字段漂移**：`items/results/data`、`next/next_cursor/nextPageToken` 等
  别名表读取；DOI 的 `doi/DOI/Doi` 由 `doi_map` 归一。

### 恢复点涉及的模块
- `doi_map.py`：把任意 DOI 写法归一成 `doi:<lowercase>` 规范键，供去重与主键使用。
- `version.py`：判定 `published / preprint / unknown` 及秩；同一 DOI 上正式版
  永不预印本降级，版本状态与记录同事务落 `record_versions`。
- `store.py`：记录、版本状态、游标、请求轨迹的原子提交与提交确认。

## 持久化与测试
数据通过项目内 SQLite 存储（`records / harvest_state / harvest_pages /
harvest_requests / record_versions`），使用稳定键执行幂等写入。离线测试覆盖
超时、空页、重复页、字段漂移、缓存过期、并发与部分成功后重启的请求轨迹。
