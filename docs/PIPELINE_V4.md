# Diff Analysis v4：离线差异分组与报告管线

> 2026-10-23，当前设计以 Git 最终净差异为唯一变更底账。**本项目只扫描源码和历史、分组并输出报告，不调用 AI、不配置模型 API 或密钥，也不自动上传源码。**

## 1. 执行流程

~~~text
多个项目的固定旧版/新版 SHA
            │
      ┌─────┴────────┐
      ▼              ▼
  最终净 Diff      Git 提交历史
 (文件/Hunk)      (拓扑/说明)
      │              │
      └─────┬────────┘
            ▼
   代码元素匹配/候选来源
            ▼
       需求候选分组
            ▼
  组间交叉检查 / 未修改消费者
            ▼
  输出 report.md + ai_review.md
     + group_reports/G-*.md
     + evidence/D-*.md
     + analysis.json
            │
   用户手动提供给本地 AI
~~~

## 2. 数据职责

### 最终差异：唯一变更范围

以每个项目的 `base_sha` 和 `head_sha` 计算 `git diff`；包括 Java、SQL、配置、前端、删除、重命名以及未支持文件。每个变更 Hunk 或文件事件有固定 `D-*` ID。

中间提交可能新增、修改或完全回退代码；它们是**演进证据**，不能直接叠加成最终变更。差异扫描失败、二进制内容、过大文件要显示解析状态而不是静默忽略。

### Git 历史：解释来源

使用 `git rev-list --parents --topo-order` 收集提交 DAG 与说明，使用 `git log ... -- <file>` 获得可能触及差异文件的提交。来源映射当前只有文件级证据，**不能将 commit 当作确定的逐行修改来源**。

未来按需尝试 RefactoringMiner 和 CodeTracker 提高重命名、移动、提取等演进关联准确性；工具失败时仍输出最终 Diff。

### 需求候选组：保守而可追踪

每条 D-* 仅有一个主展示 G-* 组。仅在文件历史中存在唯一明确任务号时自动合成候选组；缺少任务号、混杂提交以及多任务时不强制合并。分组状态一律是 `CANDIDATE`，不是已批准需求。

### 交叉检查：独立保存

共享文件/符号、分版本静态调用路径及跨项目消费者是待审线索 `X-*`，不能通过这些边强制把两个需求合成一组。未修改、没形成候选组的消费者仍以单独的风险候选显示。

一个项目的内部变化按自身局部业务路径呈现；跨项目时围绕 A 逻辑、调用/共享资源交叉点和 B 相关逻辑呈现。当前 SQL/MQ/Redis 的细粒度资源身份识别仍在计划中，不能伪造关系。

## 3. 离线报告的两种阅读方式

- `report.md`：人工快速查看项目范围、净差异数量、分组依据、组间交叉、未修改消费者及扫描缺口。
- `ai_review.md`：**可直接复制给本地 AI 的自包含 Markdown**，包括任务说明、候选分组、文件级提交历史、每项 D-* 对应的旧新版代码和交叉检查。
- `group_reports/G-*.md`：每组独立 Markdown，包含该组的提交、全部最终变化、代码上下文和相关交叉线索。版本过大时可逐组提交本地 AI。
- `analysis.json`：结构化净差异、版本快照、分组、证据边和不足项。
- `evidence/D-*.md`：单个 Git 变化对应的旧/新版上下文各前后约 30 行。

**底账不会因为某个本地 AI 的输入长度限制而减少记录。** 如果 `ai_review.md` 超出模型上下文，请改为逐组投喂。每组 Markdown 是独立完整证据，不由程序自行调用或限制模型。

## 4. 安全与验收

程序不调用任何模型；没有 API key、endpoint 或远程上传选项。仅用户本人决定是否将报告交给本地 AI。

本地分析产物包含源码和 Commit 消息，属于需要保护的项目资料。让 AI 解释时应要求引用 D-* ID，明确区分源码事实、Git 来源候选、静态影响和人工假设。源代码中的文字不应被当成执行指令。

验收至少包括：

1. 中间提交回退后净差异为空，历史仍可追踪。
2. 同任务号跨提交可形成候选组；混杂任务不强制合组。
3. 共享文件和静态调用形成组间待查线索，不直接认定 Bug。
4. A 修改但 B 未改动时，B 可以作为独立消费者候选展示。
5. 所有 D-* 都能在 `analysis.json`、证据文件及组报告中找到。
6. 生成 `ai_review.md` 和各组 Markdown 时无模型网络请求，不生成模型结论。
7. SQLite 数据库连接正常关闭，Windows 临时文件能删除。
8. 无 Git 历史/无法解析文件时保留限制声明，而不是声称业务覆盖完整。

## 5. 可选工具（当前未集成）

| 项目 | 未来借鉴的机制 |
| --- | --- |
| [SmartCommitCore](https://github.com/Symbolk/SmartCommitCore) | 多种代码片段关系和可解释分组 |
| [CodeTracker](https://github.com/jodavimehran/code-tracker) | 跨重构的代码身份与演进追踪 |
| [RefactoringMiner](https://github.com/tsantalis/RefactoringMiner) | 检测结构变化和混杂的行为修改 |
| [Code Maat](https://github.com/adamtornhill/code-maat) | 历史共同修改的弱关联提示 |

优先验证实际支付/退款相关 MR 的分组质量，再决定是否引入工具。不能将这些工具产生的统计关联直接当作业务逻辑的充分证明。


## 6. Java 重构证据与分组准确率（已实现首批）

### RefactoringMiner CLI 可选适配

通过 `--refactoringminer-bin` 显式指定本地 RefactoringMiner 的可执行文件，调用：

~~~text
RefactoringMiner -bc <repo> <old SHA> <new SHA> -json <temp JSON>
~~~

使用公开 JSON 的 `commits[].refactorings[].type`、`leftSideLocations` / `rightSideLocations`，支持方法 `Rename`、`Move`、`Move And Rename`、`Extract`、`Extract And Move`、`Inline`、`Pull Up`、`Push Down` 及类移动/重命名。归一化为 `evolution_relations` 并附着到最终差异 ID：

- **直接单提交**：如果结果只有本次目标 SHA，可按旧新文件与变更行匹配；这仍是结构映射候选，不表示业务行为等价。
- **包含中间提交的多次重构**：提交前后行号**并不是 base/head 行号**；只做文件路径级关联，状态 `HISTORY_CANDIDATE`。不自动串起虚假的跨版本逻辑链。
- 没有可执行文件时不运行，不要求在使用 Java 8 的业务源码中引入新版本依赖。RefactoringMiner 3.x 本身需要 Java 17 运行。
- 进程运行异常、JSON 不合法、超时、文件过大、事件超过限制：记录 `enrichment.warnings`，保留净 Diff 与基础分组。

### CodeTracker 桥接导入

CodeTracker 主要提供 Java API，本项目**尚未自动调用其 SDK**。新增 `--codetracker-evidence <json-or-directory>` 支持明确定义的 `codetracker-bridge-v1`；这不是 CodeTracker 原生 JSON，需先通过脚本/Java 桥接转换。

~~~json
{
  "schema_version": "codetracker-bridge-v1",
  "project": "rhf/pay",
  "base_sha": "<full old SHA>",
  "head_sha": "<full new SHA>",
  "mappings": [
    {
      "kind": "Extract Method",
      "old": {"path": "src/A.java", "start": 10, "end": 20},
      "new": {"path": "src/B.java", "start": 30, "end": 40}
    }
  ]
}
~~~

路径为项目内的 Git 相对路径，行号为 1 起始的闭区间。导入前校验项目身份、固定 SHA、字段类型和大小上限；未匹配范围仅保留警告与断点。多个项目可传目录，按项目名将 `/` 替换为 `_` 查找 JSON。标准化映射证据标记 `EXTERNAL_TRACKED_CANDIDATE`，不能自动认定重构纯净或无需测试。

### 更严格的需求归属

在 `final_diff_ledger` 对每个有效的新增/修改 Hunk 使用 Git blame 的逐行提交来源（受 120 行预算限制），查询它们是否落在本次旧新版本间，映射 commit message 的唯一任务编号：

1. 新变更行由唯一有任务号提交产生 → `LINE_BLAME_ISSUE_CANDIDATE`，优先用于候选组。
2. 无法取得变更行提交 → 回退至现有文件级候选任务号（只在唯一任务号时归组）。
3. 同一片段出现多个任务号/一个提交有多个任务号 → 不强行合组，列 `AMBIGUOUS_OR_UNASSIGNED`。
4. 仅有方法重命名、移动或提取关系 → 写入演进证据与 `REFACTORING_EVOLUTION_CANDIDATE` **组间检查**；不自动把不同工单的改动合成一个需求。

这种提升主要处理“一个文件中有不同提交/任务的不同 Hunk”。它**不能**解决一个提交同时完成多个需求且没有任务编号的情况，也不能用 blame 证明业务需求语义。

### 回归验收

`tests/test_refactoring_enrichment.py` 包含单次方法重命名、跨类移动、方法提取、跨多提交定位降级、CodeTracker 桥接 SHA 不匹配、安全降级与同一文件不同任务逐行归因。外部 RefactoringMiner 可执行文件不参与基础单测；真实复杂 GitLab 历史仍需本地验收。

已知后续工作：在多提交间构建经过验证的跨重构元素链；为 Extract Method 增加基于 statement mapping 的纯度/行为变化判断；针对 Windows 大仓库建立性能与缓存基准。以上均非当前已完成能力。
