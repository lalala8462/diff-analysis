# 分析管线 v4：最终差异底账 → 提交演进 → 需求候选组 → 跨组交叉 → 模型按组推理

> 2026-10-23。保留已有 Python/JDK AST/SQLite/Windows CLI；本设计以 net Git diff 为唯一最终变更范围。  
> 本文明确区分**已编码的保守首批机制**和**尚未接入的精密工具**。不把历史推断、重构检测或 LLM 文本写为已验证业务事实。

## 1. 核心架构与职责

~~~text
固定项目旧版 SHA / 新版 SHA（按项目独立）
                  │
           ┌──────┴─────────┐
           │                │
           ▼                ▼
    Net diff inventory    Commit topology + messages
    files/hunks/rename    merge parents/revert history
    before/after context  file-level commit candidates
           │                │
           └───────┬────────┘
                   ▼
       Source identity / refactoring hints
      (已有 Java AST; 精细映射待引入)
                   │
                   ▼
       Requirement candidate grouping
       (强任务号优先; 模糊变化不强合)
                   │
                   ▼
       Cross-group intersections
       (共享资源/调用/影响链单独记录)
                   │
                   ▼
     Group-limited model inputs / opt-in LLM
                   │
                   ▼
     人工评审报告 + 结构化证据 + 审核缺口
~~~

**两个正交维度**：
- `BelongsTo(group, diff_id)` 是候选需求归属；每个 diff_id 有一个默认主组，共享影响通过独立关系引用。
- `Intersects(group_a, group_b, evidence)` 是需要联合检查的影响线索；绝不以交叉边为理由把两组无条件合并。
- `Evolves(element_old, element_new, commit/transform)` 是演进映射；当前仅保存提交与文件级候选，CodeTracker 等精确节点待开发。

### 参照工具，不强制引入依赖

| 参考项目 | 要提炼的机制 | 集成节点 | 当前 |
| --- | --- | --- | --- |
| [SmartCommitCore](https://github.com/Symbolk/SmartCommitCore) | Diff 片段多种关系和任务候选分组 | 在分组规则接口可稳定替换 | 未接入 |
| [CodeTracker](https://github.com/jodavimehran/code-tracker) | 重命名、移动与跨提交的元素身份追踪 | 不明确的演进片段按需调用 | 未接入 |
| [RefactoringMiner](https://github.com/tsantalis/RefactoringMiner) | Java 重构检测、映射及部分类型纯度验证 | 在构造元素关系时标出纯重构/夹杂行为变化/未知 | 未接入 |
| [Code Maat](https://github.com/adamtornhill/code-maat) | 历史共同修改、噪声过滤和统计窗口 | 基础分组稳定后添加弱提示 | 未接入 |

## 2. 最终差异底账：不因解析器能力而丢失

输入一组 `(project_id, repository, base_sha, head_sha)`。分析的唯一变更范围是 `git diff base head`。具体采集：

- `git diff --name-status -z --find-renames`：文件增删、改名、复制、二进制/非源代码对象。
- 对每个受影响文件，解析净变化 Hunk 的旧/新区间，生成稳定 `D-*` 标识，存旧/新 Git SHA、路径、文件状态及类别。
- 旧版和新版各取变化点前后 30 行作为独立代码证据文件。文件缺失、二进制、超出大小限制和提取失败均保留原因。
- Java AST 提供变更范围的候选方法符号。SQL/XML/配置/前端仅登记变化类别，缺少对应语义扫描器时使用 `NEEDS_REVIEW`。
- 没有文本 Hunk 的重命名、Gitlink 和二进制变化仍记一条文件级事件，标记 `UNSUPPORTED`。
- 单文件 Hunk 超限时会保留 `HUNK_LIMIT / ANALYSIS_TRUNCATED`，不得说已经完整解析全部 Hunk。

状态：`SEMANTIC_MAPPED`、`NEEDS_REVIEW`、`UNSUPPORTED`。未来可扩展 `PARSE_FAILED`、`NON_BEHAVIORAL` 等。**底账覆盖和业务影响识别是两个不同的指标**。

当前 net-diff 仍受到 Git diff 算法和复制检测配置约束；输出固定 SHA 与处理选项以便复算。不能以已登记文件数等于“业务影响没有遗漏”。

## 3. 演进采集与来源归因

- 全局提交拓扑使用 `git rev-list --parents --topo-order --reverse base..head`，保存父提交和说明。
- 如 base 不是 head 的祖先，`history.status=BASE_NOT_ANCESTOR`；最终差异依然可分析，但不能按单条线性演进解释来源。
- 对每个变更文件用 `git log base..head -- path` 获取**文件触及提交候选**，记录 `FILE_TOUCHED_CANDIDATE`，并保留可能的多个提交。**不能声称每个 Hunk 的精确作者或目标提交已经确定**。
- 暂不逐次拼接中间提交的 Hunk。全部回退但最终不产生 net diff 的片段仅留历史，不形成待发布变化组。
- 对提交记录数量设定上限；超限标记 `TRUNCATED`，不静默抛弃影响追踪缺口。

下一阶段可用 CodeTracker/RefactoringMiner 或 `git blame` 对有歧义的部分做按需精确映射；需要解决 rename、move、extract、inline、split 和 squash。映射应允许一对多、多对一与 UNKNOWN。

## 4. 候选需求分组（当前首批规则）

一条最终变化 `D-*` 只有一个主要 `G-*` 展示归属，其他关联通过独立交叉关系保存。

保守策略：
1. 从文件触及提交说明提取工单编号（如 `PAY-123`）。
2. **当该变化文件的候选来源只有一个独立工单编号时**，允许将其作为候选组键；多个文件/多个提交但同一工单可汇总。
3. 发现多个任务号、没有任务号、提交来源不完整时，每个差异单独成组，不根据相同文件/方法一律合并。
4. 所有组 `status=CANDIDATE`，人工可调整归属；未来按方法位置的 blame/重构追踪提高准确率。

限制：当前工单号规则仅识别大写字母数字组合加连字符和数字（`PAY-123` 形式）。文件级候选对提交混杂的场景准确性有限；不保证自动把一个混杂 commit 拆成真正的两个需求。

## 5. 交叉检查（独立于分组）

当前首批实现：
- `SHARED_FILE_WEAK`：两个不同候选组修改同一个文件；只提示检查，不自动合组。
- `CROSS_PROJECT_CALL_CANDIDATE`：P0 的调用名称/参数数量候选若在两个变化组都有映射，形成跨项目组间检查（并非确认的 Dubbo 调用）。
- `consumer_candidates`：如果 B 没有改变也没形成分组，但当前 P0 检测到 B 调用 A 的候选，仍把 B 作为**未变更消费程序**单独列在报告中，等待人工核实。
- `SHARED_SYMBOL_CANDIDATE`：两个组修改同一 AST 方法符号；仍须审查变化是否独立。
- `CALL_PATH_BASE_CANDIDATE` / `CALL_PATH_HEAD_CANDIDATE`：沿**分别属于旧版/新版**的静态调用图，发现两个组共同涉及的路径。
- 预算触顶时 `ANALYSIS_TRUNCATED`，不能将遗漏的组对视为没有关系。

后续加入跨 Git 提交线路、API/SQL/MQ/Redis、Db instance.Schema.Table.Field 等不同媒介的更丰富交叉点。对于不存在共同祖先的分叉，后续需保存 ancestor、left/right 和真实合并树，不仅看最终 SHA。

**证据图与人类展示分开**：底层允许多对多关系；界面/Markdown 围绕改动 A 主链与 B 相关逻辑的交叉点展开；仅内部 A 时展示局部流程。不直接在一张巨图中罗列所有代码节点。

## 6. 模型：按组输入、显式调用、无远程默认值

每组生成 `model_inputs/G-*.json`：
- 本组的 `D-*`、净变化上下文、文件/方法线索、候选提交说明与组间检查项。
- 单组约 24,000 字符的输入预算；超过部分记录 `unprovided_change_ids`，而不是让模型在不完整输入上假称已覆盖。
- 模型必须返回 JSON，并引用当前组已提供的 diff_id。调用器检查引用、把置信度降为 `HYPOTHESIS`，总是保留人工确认状态。
- 无 endpoint 时 `NOT_INVOKED`；有 endpoint 但失败则 `MODEL_FAILED`，静态结果照常完成；部分组超出预算则 `NOT_ANALYZED_BUDGET`。
- 默认只允许 localhost HTTP(S)，显式 `--allow-remote-model` 才允许其他 HTTPS 主机。禁用跨主机 HTTP redirect。不在文件中保存 API key。

模型不应生成未经证实的业务事实、重写人工代码证据或替代版本审核。程序还不能证明模型输出全部可由代码支持；它只能强制所引用的 ID 在输入内。

## 7. 输出和持久化

~~~text
.diff-analysis/runs/20261023T040000Z-01234567/
  report.md              按候选需求组排序的人审报告
  analysis.json          最终差异、来源演进、分组/组间线索、模型状态
  evidence/D-*.md        旧版和新版源码各前后 30 行
  model_inputs/G-*.json  本组输入
  model_inputs/G-*.result.json  实际调用模型后的原始结构化结果
~~~

SQLite `analysis_run` 表只存任务 ID、报告目录、分析摘要和模型状态，**与 `baseline_snapshot` 隔离**。运行目录用 UTC 时间 + 随机短 ID，避免覆盖同一分钟内的结果。原有 `report.md` / `review.json` 路径仍可按旧参数输出，供自动化脚本兼容。

## 8. 验收覆盖的金标准案例

1. 两个提交共同实现同一显式任务号：产生一个**候选**组。
2. 一个提交说明同时包含两个任务号：禁止无条件合并；保留待审。
3. 提交改变代码又回退到原值：最终净差异为空，但提交拓扑仍保留修正过程。
4. 文件改名但没有代码修改：至少产生一个重命名文件事件，不能静默忽略。
5. 不同组修改同一文件两处：仅产生 `CHECK_TOGETHER`，不强制合并。
6. base 不是 head 祖先：输出 `BASE_NOT_ANCESTOR`，仍保留净差异。
7. 模型返回没有引用已提供差异 ID 的事实：拒绝相关输出并标记失败。
8. 模型不能连接/预算用尽：产生基本报告，模型状态为错误/未分析，不删减差异。
9. Java 修改在旧版路径与新版路径不同：分别显示证据并留未来 RefactoringMiner 映射待办。
10. 真实 GitLab MR 含 SQL、配置、前端和 Java：所有最终变更均列入报告，未解析类型不得标分析完成。

## 9. 后续开发里程碑与集成策略

| 顺序 | 任务 | 首期交付 |
| --- | --- | --- |
| Phase 1 | Net Diff 台账、源上下文、提交历史证据、时间戳目录 | 首批代码已写；Windows 实测待确认 |
| Phase 2 | 分组和版本内交叉、方法元素身份、混杂提交拆组 | 只有保守分组/交叉首版；精细映射待开发 |
| Phase 3 | 可选 LLM 模块、逐组和跨组推断、人审结果 | 已写可选组调用；跨组 LLM 与人工交互待开发 |
| Phase 4 | RefactoringMiner + CodeTracker 可选适配 | 待开发，必须失败降级且保留净差异 |
| Phase 5 | Code Maat 共同修改候选与 SmartCommitCore 复杂关系 | 待开发，固定窗口、过滤噪声和不合组原则 |
| 后续 | SQL/消息/缓存依赖、可交互 Web、V1/V2 受控行为验证 | 待开发 |

**最有价值的下一张任务卡**：为混杂提交构建 Hunk→真实来源 commit 的可验证映射，然后引入 Java 重构关系；证明不会因为历史来源错误，把不同需求错合成一组。
