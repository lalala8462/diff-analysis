# 开发计划 v3.0：版本递进基线、主链交叉点与分层人工评审

> 2026-10-19｜本文件是**待实施的里程碑计划**。已经实现的部分仍以 [P0 CLI 使用说明](P0_USAGE.md)为准。  
> 决策已确认：版本基线来自 MR 的相邻版本、所有关联项目由用户提供、Windows Python + Web + SQLite、先静态评审再做单场景差异执行。

## 0. 实现策略

先实现一个能**正确记录用户项目清单、版本快照及证据断点**的本地系统，再提高跨项目扫描能力；**图谱的自动推理能力不应该成为首次基线生成的阻塞项**。

阶段关系：

~~~text
P0-S CLI 实测稳定性
   ↓
P1-B 项目清单 + 版本谱系 + 首次基线 → [最小只读 Web UI 可并行]
   ↓
P2-L Git Diff 台账 + 语义变化点 + A 单程序主链
   ↓
P3-X 跨项目交叉点 + 证据链 + 断点 → P4-U 正式交互 UI
   ↓
P5-C 业务场景与行为契约 → P6-E 单场景隔离 V1/V2 执行
   ↓
P7-A AI 证据评审/更广的动态依赖和多用户部署
~~~

每个阶段必须有**不依赖后续阶段也能独立使用的产物**。不以未知业务漏报率作为首期验收门槛，优先验收扫描覆盖、证据质量、人工复核可用性。

## P0-S 当前原型确认（当前代码已具备基础功能）

### 已有

- [x] `review-local` / `review-mrs` 命令及 GitLab API/MR URL 处理。
- [x] 固定 base/head 的 Git 对象 Diff、Java AST 和基础反向调用。
- [x] 初步 API 版本提醒、额外 `--catalog` 固定本地项目候选匹配。
- [x] Markdown/JSON 报告、Windows Python/JDK8 兼容修复；用户已报告本地基础测试全通过。

### 尚需验收

- [ ] P0-S01：在公司 GitLab 使用真实多个 MR 验证 fetch、SHA 固定和完整报告；保留脱敏测试记录。
- [ ] P0-S02：CI runner 问题排查并让 Windows/Linux job **实际跑完**测试步骤。
- [ ] P0-S03：补充失败边界样例：认证失败、MR 已合并、fork、提交不可读、Diff 过大、扫描不支持/失败。
- [ ] P0-S04：对当前静态候选明确展示证据级别，不能仅用方法名同名得出确定跨项目依赖。

**出口**：真实 MR → 完整 Git 差异与基础 Java 分析；失败/未知可审计。

## P1-B 项目清单及相邻版本基线（最高优先级）

### 工作包

- [ ] B01：SQLite 初始化/迁移；项目目录表及 catalog_revision，录入 GitLab project URL、纳管状态、版本规则、扫描配置、负责人。
- [ ] B02：GitLab 项目/MR 列表和 URL 解析统一：source/target/version label、MR base/head、target tip、merge SHA 都分别记录。
- [ ] B03：基线发现器：`MR(V1.1→V1.0)` 时，若 `project@V1.0` 无快照，则固定**旧目标分支 Commit/树**生成首次源码清单；不要求已有业务图。
- [ ] B04：对人工 catalog 全项目生成版本绑定快照；没有版本映射、权限不足、拉取失败时保存显式状态 `VERSION_UNKNOWN` / `UNREACHABLE`。
- [ ] B05：完成 `BASELINE_PROVISIONAL`、`VERIFIED`、`CANDIDATE_ANALYZED`、`MERGED_UNVERIFIED`、`PROMOTED`、`DIVERGED` 状态机及审核日志。
- [ ] B06：合并事件/人工触发后读取实际 GitLab final commit/tree；检查候选与最终代码的偏差，成功后晋升为下一版本基线。
- [ ] B07：下次 V1.2→V1.1 检查上一轮晋升的 SHA/tree 与当前目标分支的一致性，不一致要人工调和，禁止静默复用。
- [ ] B08：多项目版本清单 Batch：每个项目独立版本标签与 pinned SHA；同仓库多个 MR 要按顺序模拟合并并记录冲突。
- [ ] B09：按 `project+SHA/tree+scanner version+config hash` 进行源码/扫描缓存；首次全扫，后续增量。版本或扫描规则不一致触发重扫。
- [ ] B10：初期最小 API：catalog CRUD、创建批次、获取版本图、建立/重扫基线、查询错误；不能让浏览器直接操作 GitLab Token。

### 关键自动化验收案例

| 编号 | 输入 | 必须输出 |
| --- | --- | --- |
| B-A1 | 首次 V1.1→V1.0 | V1.0 pinned baseline + V1.1 candidate，互不覆盖 |
| B-A2 | 第二次 V1.2→V1.1 | 复用已晋升 V1.1，SHA 一致、缓存命中 |
| B-A3 | 合并时 squash/rebase | 保存实际最终 commit/tree，不能直接用源 HEAD 晋升 |
| B-A4 | 目标分支中途前进 | 显示 TARGET_MOVED / DIVERGED，旧结果不可冒充最新 |
| B-A5 | catalog 有 10 项，1 项无权限 | 目录仍为 10，失败项目可见，不显示全量覆盖 |
| B-A6 | 一个 Batch 中项目版本号不同 | 正确存储每项目各自 Vn→Vn+1 |
| B-A7 | 项目清单增删 | 新 catalog_revision；旧报告仍指向旧版本目录 |

### 交付

`catalog.sqlite`、版本谱系 JSON Schema、基线锁定/晋升 API/CLI、状态可追踪的首次扫描报告。**未要求资源级业务依赖或完整 Web 图谱。**

## P2-L 双台账第一部分：Git Diff Ledger 与本项目主链

### 工作包

- [ ] L01：`source_inventory` 枚举 Git 对象里的所有文件，包括删除、重命名、不可解析格式与子模块，保存语言/类型/失败原因。
- [ ] L02：把每个 Git 变更 Hunk/文件级操作记为 `DiffHunk`，确保有一条处理记录；每项关联零个或多个 `SemanticChange`。
- [ ] L03：语义分类器：JAVA_METHOD、CONDITION、STATE_TRANSITION、API_CONTRACT、SQL、CONFIG、MQ、REDIS、FRONTEND、UNKNOWN 等多标签。
- [ ] L04：识别非行为变化但要保留证据：注释/文档/格式；无法判断使用 NEEDS_REVIEW。
- [ ] L05：识别 A 项目修改方法与候选入口的反向路径，再找其局部条件、状态迁移和副作用出口；无法绑定调用关系使用 UNRESOLVED_CALL。
- [ ] L06：单项目局部图：入口 → 决策/变更节点 → 分支 → 副作用/返回；变化前/后对照，节点附文件/行号/commit。
- [ ] L07：扫描统计分开呈现：`source_inventory_complete`、`hunk_accounting_complete`、`semantic_coverage`、`entry_resolution_coverage`，不能合并为“业务覆盖率”。

### 验收

- [ ] L-A1：100 个 Hunk 中 3 个 unsupported，页面显示 100 条已登记且 3 个未知，不出现“业务 100%”。
- [ ] L-A2：方法只改 Java 局部分支，图只画 A 一条主链与前后分支差异。
- [ ] L-A3：重命名/删除、整文件新增、文件解析失败均有覆盖账目。
- [ ] L-A4：Java 方法无法找到业务入口时显示“仅有局部方法级脉络”，不虚构 HTTP/Dubbo 入口。

### 交付

`Diff Coverage Ledger` 持久化结构、单项目主链片段 JSON、可导出的覆盖及异常清单。

## P3-X 跨项目交叉点与证据路径（业务价值核心）

### 工作包（按影响识别价值排序）

- [ ] X01：建立 `Impact Trace Ledger` 结构：SourceLogicSpine、EffectCandidate、CrossingPoint、ConsumerLogicSpine、ImpactFinding、EvidenceGap。
- [ ] X02：Java 调用准确率：类全名、签名、接口实现/注入和组件映射，不能仅靠简单方法名/参数个数形成确定关系。
- [ ] X03：识别 Dubbo 提供/消费接口声明及项目依赖 Jar 坐标，缺少实现/运行证据时标为 CANDIDATE。
- [ ] X04：MyBatis XML/注解 SQL：Java mapper → SQL statement → 实例.Schema.表.字段，识别 READS、WRITES、READS_CONDITION（WHERE/JOIN/HAVING）。
- [ ] X05：配置资源归属：允许管理员维护 datasource/schema 别名映射和证据；无身份时生成 RESOURCE_IDENTITY_UNKNOWN 断点，不根据同名表硬连。
- [ ] X06：跨项目主链连接：A 有证据 effect → 中间资源/接口 → B 对应消费逻辑，再找 B 到已知入口的局部路径。
- [ ] X07：影响规则首批：状态值变化 vs SQL 过滤、API 参数变化 vs 消费者、MQ 字段变化 vs 消费者；先候选，支持人审升级。
- [ ] X08：按用户 catalog 反向扫描所有纳管项目当前快照；未纳管项目只能展示边界，不虚构未知 D。
- [ ] X09：消息与缓存第二批：MQ exchange/routing/queue/consumer、Redis Key 模式和值结构；跨环境同名不能自动关联。
- [ ] X10：图裁剪/分组：按业务入口、主链阶段、媒介、关联项目及证据状态逐层展开；默认只展示 1～3 个交叉点，但完整 Ledger 可检索。
- [ ] X11：证据断点模型：WRITE_MAPPING_UNKNOWN、DYNAMIC_SQL_UNKNOWN、RESOURCE_IDENTITY_UNKNOWN、UNRESOLVED_CALL、UNSCANNED_PROJECT、ANALYSIS_TRUNCATED 等。
- [ ] X12：支持人工“确认关系/排除关系/补充规则”，带项目版本、证据、审核者和失效策略，不永久覆盖自动检测。

### 验收

| 案例 | 验收条件 |
| --- | --- |
| A 改写状态，B 仍按旧 SQL 过滤 | A 主链→DB 字段→B 查询主链，有代码/SQL 证据；业务风险仍待确认 |
| A mapper 不可解析 | A 只连到证据断点，不将 B 误标为确定消费者 |
| 同表名不同 Schema | 不建立已确认交叉关联 |
| Dubbo 接口变更 | 能关联消费项目及 API/方法，运行时调用缺失仍保持候选 |
| 只有 A 的局部逻辑影响 | 只绘 A 内部流程，不制造跨项目路径 |
| 一个资源有 200 条候选消费者 | 默认折叠；总数/过滤/展开/导出完整，超预算显式提示 |
| B 没有 MR | 仍可从 catalog 的 B 基线扫描出受影响路径 |

### 交付

`Impact Trace Ledger`、可分组的 anchored graph JSON、人工审核与证据断点结构。**不要求此阶段给出绝对业务漏报率。**

## P4-U Windows 本地 Web/SQLite 界面

### 技术选择

`FastAPI` + `SQLite` + `Cytoscape.js` + 本地打包静态前端，默认 `127.0.0.1`。主分析引擎保留 CLI/服务共用逻辑；后台 Worker 处理扫描任务。SQLite 使用事务、迁移和索引；初期单用户，后续迁移多人服务另行设计。

### 实施顺序

- [ ] U01：**可提前做**最小 UI：项目目录、版本列表、创建 MR 批次、扫描进度、错误和 Markdown 报告查看。
- [ ] U02：Diff 台账页面：按项目/类别/处理状态筛选，查看对应 GitLab Diff 和 AST 映射。
- [ ] U03：主链/交叉点页面：以一个变更点为中心，左侧 A 主处理链、中间媒介、右侧 B 相关逻辑；仅 A 时局部流程。
- [ ] U04：聚合风险：同资源/相同消费入口只展示一个 Finding，下挂多个代码位置与路径；证据按钮展开 SQL/配置、固定 SHA 链接。
- [ ] U05：人工确认工作流和审核历史、标记已验证/候选/未知；不能在前端把“确认关系”和“验证业务无风险”混为一谈。
- [ ] U06：Markdown/JSON/HTML 均来自同一 SQLite 模型；离线资源打包，企业内网无需 CDN。
- [ ] U07：安全：Token 在进程环境变量，不进 SQLite 或浏览器；只绑定 localhost；日志自动脱敏；限制源码/API 导出权限。

### UI 验收

从“spms-back 状态变化”卡片进入：

~~~text
当前变更：PaymentService#setState()
左边：A 主流程 [请求→渠道回调→状态更新→返回]
中间：DB trans_record.trans_state [打开 mapper 与 schema 证据]
右边：B 对账流程 [XXL-JOB→查询 WHERE=2→对账输出]
下方：未确认的业务假设、SQL/配置证据、审核按钮、其他消费者分组（可展开）
~~~

任何节点都有来源位置或明确 `UNKNOWN` 说明，绝不画出未经证实的“确定调用”。

## P5-C 业务场景与行为契约（先模型后生成用例）

- [ ] C01：`BusinessScenario`：稳定 ID、domain、入口、前置条件、输入、标签、已有用例链接、owner。
- [ ] C02：`BehaviorContract`：NEW/CHANGED/UNCHANGED/UNKNOWN、需求依据、已批准期望行为、审核人及版本；**期望不可从待测实现直接认定**。
- [ ] C03：影响链映射测试场景；高风险支付、退款、对账场景允许手工强制纳入版本回归。
- [ ] C04：展示“保留行为/批准变化/待确认”清单，先在 Web 上审批、不执行任何业务系统。
- [ ] C05：第一批手工场景：状态更新→查询/退款/对账、部分退款超额、未知交易状态恢复。先与业务负责人确认输入、输出与副作用约束。

**验收**：只给 MR 和一个批准的需求描述，也能形成待测场景清单及 UNKNOWN 待确认项；没有执行结果就显示 NOT_EXECUTED。

## P6-E 一个资金业务的受控 V1/V2 执行

- [ ] E01：根据已批准场景选择一个最小纵切面（推荐对账或部分退款，先确认哪种环境更容易隔离）。
- [ ] E02：两套可复现程序/Schema 数据、独立中间件空间；相同脱敏 fixture、时钟/渠道响应。
- [ ] E03：保留真实内部 Service/Mapper，外部银行通道只使用受控 Mock，记录请求意图、次数、金额、幂等键。
- [ ] E04：收集返回值、关键 DB 数据状态，以及选定场景涉及的 MQ/Redis 事件（未覆盖明确标记）。
- [ ] E05：业务键/主键映射、动态字段规范化、异常分支和异步完成条件比较。
- [ ] E06：区分 EXPECTED_CHANGE、UNEXPECTED_CHANGE、MISSING_EXPECTED_CHANGE、INCOMPARABLE、NOT_EXECUTED。
- [ ] E07：人工确认测试覆盖与 Mock 缺口；不连接真实银行渠道或生产库。

**验收**：测试故意多次发起退款/漏写状态/对账漏单（取决于选定场景），检测器能指出实际可观察差异并链接到业务契约；测试无法比较时不会显示 PASS。

## P7-A AI、长期运行和评测

- [ ] A01：AI 按需阅读**已确认的主链上下文**，生成业务摘要与待核实原因，不能添加不存在的有证据关系。
- [ ] A02：长周期从人工反馈、历史故障、构造的金标准场景统计漏报/误报；不对未知项目数编造漏报率。
- [ ] A03：逐步扩展运行证据、ZooKeeper/MQ/Redis/Oracle/ES/XXL-JOB 适配器。
- [ ] A04：如需要公司多人使用，再增加共享数据库、角色权限、任务队列及审计。
- [ ] A05：GitLab 审阅评论和发布前风险清单可后续接入；默认人工最终批准。

## 最小数据模型与接口位置（计划）

建议先保留当前结构：

~~~text
diff_analysis/cli.py
diff_analysis/gitlab.py
diff_analysis/gitops.py
diff_analysis/scanner.py
diff_analysis/review.py
diff_analysis/report.py
diff_analysis/java/JavaAstScanner.java
~~~

拟增：

~~~text
diff_analysis/
  catalog.py
  versioning.py
  baseline.py
  storage/
    schema.sql
    sqlite_store.py
  semantic/
    hunk_ledger.py
    java_changes.py
    sql_mapping.py
  impact/
    logic_spine.py
    crossing_point.py
    evidence.py
  web/
    app.py
    api_catalog.py
    api_review.py
    static/...
  contracts/
    schema.py
  execution/           # P6-E 时再建
tests/
  fixtures/
    version_lineage/
    local_logic/
    cross_project_sql/
    incomplete_scan/
~~~

已确认设计不要求立即重构成 Spring Boot、安装 MySQL/Neo4j，也不要求每次跑全部场景。代码从 v3 第一个模块开始，每完成一个阶段更新 README 的**已实现状态**，不要只勾选计划框。

## 下一次开发的第一张任务卡（建议直接开工）

**P1-B01 + B02 + B03：项目清单和首次 V1.0 基线**

输入：手工配置的 3 个项目、一个 `V1.1→V1.0` MR。

输出：
1. SQLite 中保存 catalog_rev、每项目 Git URL/项目 ID；
2. 固定目标 V1.0 的真实 commit/tree；
3. 保存 V1.0 的源文件清单、扫描状态和未解析文件；
4. 生成 V1.1 的候选来源信息（不自动晋升）；
5. JSON/API 返回两个可分别查询的 `baseline` 与 `candidate`。

最小验收：首次无基线也能完成；重新执行可复用；若目标 SHA 不匹配则报错；任何项目无法扫描均显示 UNKNOWN。

**这个阶段完成后，才开始 P2-L 的台账和界面逻辑，避免依赖图建立在不可复现的版本之上。**
