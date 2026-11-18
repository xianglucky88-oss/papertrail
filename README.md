# papertrail — 抓 arXiv/Crossref 元数据，DOI 去重、预印本/正式版本归并、作者消歧。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、版本归并与幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`author_id`, `version`, `doi_map`, `harvest`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 版本归并（version.py）
在“论文身份（paper）”之下保存版本节点（node）、节点前后关系（edge）、撤回事实
与正式版选择依据（formal_basis）。所有决定以只追加事件写入 `vx_events`，
其余 `vx_*` 表是可由事件流重算的物化状态（`version.rebuild(con)`）。

### 接链规则（证据由强到弱）
强证据自动并入同一条版本链：
- `R1_DOI`：相同非空 DOI（标题/作者冲突仍归并，另记 `metadata_conflict`，两版元数据互不覆盖）
- `R2_ARXIV`：相同非空 arXiv id（`v1/v2` 后缀是版本标签，不拆分身份）
- `R3_TITLE_AUTHORS`：标题指纹相同且作者指纹集合完全一致、且无冲突标识；日期冲突只记冲突事件

证据不足时**保留候选、暂不归并**（`vx_candidates`，双方各自保持独立链）：
- `W1_TITLE_AUTHOR`：同标题且有共有作者，但标识冲突或双方都缺可锚定标识
- `W2_TITLE_ONLY`：仅标题相同，无共有作者佐证
- `W3_IDENT_CONFLICT`：标题/作者相似但 DOI 或 arXiv 标识明确不同，身份存疑

一条同时携带两条链标识的新记录构成桥接证据，触发 `paper_merged` 并链；
论文获得更强锚点（如 arXiv 链迎来 DOI）时触发 `paper_renamed`，旧 id 经
`vx_paper_aliases` 永久可解析。候选在并链后标记为已解决。

### 正式版（VOR）选择
只看证据、不看到达先后，因此“期刊先于预印本到达”与乱序导入结果一致：
1. 仅在 `published` 节点中选择；没有正式稿时正式版为空，当前版本回退最新预印本，依据写明 `no_published_version`
2. 排序：`node_date` 降序 → `version_label` 降序 → `node_id` 升序（确定性）
3. 撤回不改变正式版归属，依据标注 `retracted` 并关联 `vx_retractions` 撤回事实
4. 每次选择落 `formal_selected` 事件保存完整依据；规则变更后可凭事件流历史重算与比对

### 幂等与半批失败
- 确定性 `event_id`：重复运行不产生重复节点/事件；镜像记录判 `duplicate` 并留 `duplicate_record` 事件
- `version.session(con)`：正常退出统一 finalize 并 commit；任何异常整体 rollback，
  不留半截物化状态，重跑即收敛

### 主要查询接口
`current_formal` / `current_version` / `version_chain` / `retractions` /
`candidates` / `history` / `rebuild`，均接受任意历史 paper_id（自动经别名链解析）。

### 与下游阶段的关系
- `doi_map`：同一 paper 的预印本与正式稿共享主标识，DOI 解析指向当前正式版
- `dedup`：复用 paper 身份与节点角色判重；弱候选绝不判重，保持独立论文

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
