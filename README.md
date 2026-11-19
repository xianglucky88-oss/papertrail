# papertrail — 抓 arXiv/Crossref 元数据，DOI 去重、预印本/正式版本归并、作者消歧。

运行栈：Python 3.11+ 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
通用记录走 `fixtures/input.json`；论文消歧走 `fixtures/papers.json`：
```text
make run         # 通用记录流水线，输出摄取数量和落库行数
make run-papers  # 论文流水线：作者消歧 + 版本链 + 去重，附待审/身份计数
make test        # pytest 全量测试（流水线、幂等、消歧、版本/去重联动）
make smoke       # 零依赖冒烟测试（无 pytest 环境可用）
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只按顺序调用阶段并共享一个
`Context`（内含 sqlite 连接），不在编排层加入业务判断。

```text
loader → normalizer → harvest → author_id → doi_map → version → dedup → keyer → store
```

## 作者消歧（author_id.py）
- **顺序与身份分离**：`bylines[].order` 只记录论文内署名顺序；身份实体用
  稳定 `author_id`（ORCID 锚定或本地 `lid`）单独存放，换序不改变身份。
- **ORCID 硬锚点**：同 ORCID 即同一人，改名时旧名进入 `previous_names`、
  当前名更新；不同 ORCID 撞同名是硬冲突，禁止自动合并，转待人工。
- **证据与双门槛**：无 ORCID 时按同名/同合作者圈定候选，汇总可回放的
  信号证据（名字、曾用名、机构重叠、共同作者、署名位）。
  - `score >= 0.75` 且无冲突 → 自动确认；
  - `score <= 0.45` 或机构冲突且独立佐证锚点不足 2 个 → 待人工；
  - 缺 ORCID + 机构冲突默认「宁漏合并、勿误合并」。
- **匿名署名**：按论文署名位生成隔离的匿名身份，绝不跨论文合并，
  也不进入具名身份的合作者图。
- **人工审核**：`confirm_review(con, review_id, "merge"|"separate")`，
  幂等；确认结果与证据持久化，重放时直接采用。
- **重复导入不变**：身份/待审单以稳定键幂等落库，重复导入只回放，
  不追加证据、不新建身份；旧论文的作者引用保持稳定。

## 版本链与去重如何消费身份（version.py / dedup.py）
- `version` 用 canonical `author_id` 集合、标题、显式 DOI 链接和时序
  汇总版本证据，达到 `AUTO_CHAIN_SCORE=0.75` 才成链，否则只挂候选
  （`pending_link`），证据列表可回放而非裸相似度。
- `dedup` 的指纹由 `doi + 标题 token + 有序 canonical author_id + 版本链`
  派生：同一作者的合并会确定性地改变去重键；任一署名位仍 pending 时
  去重进入 `guarded`，不做硬折叠。

## 持久化键设计（通用 KV，无业务表结构）
| 前缀 | 内容 |
| --- | --- |
| `author_identity:` | 作者身份（ORCID/lid/匿名）、观测历史、采用证据 |
| `author_review:` | 待人工审核单（候选、冲突、原因、确认结果） |
| `version_chain:` | 版本链成员 |
| `dedup:` | 去重指纹 → 记录 id |
| `doi_map:` / `record:` | DOI 索引与版本候选发现索引 |

每条记录处理后即提交，流程在任意一条之后中断，重跑都从持久化状态回放。
