# 架构设计 v3.0：相邻 MR 版本基线、双台账、业务主链交叉点

> 已确认设计：按项目 MR 版本递进 / 用户维护项目目录 / Windows Python + SQLite + Web / 先静态评审，后单场景行为差异执行。  
> 本文是**设计目标**，非当前功能完成声明。旧文档 v2 仅供参考，冲突处以 v3 为准。

## 1. 设计结论与不做的事情

### 1.1 必须满足

1. **基线跟随项目版本而不是生产部署**：`V1.1 → V1.0` 的 MR，首次对真实 V1.0 源码创建基线，分析 V1.0 → V1.1；后续 `V1.2 → V1.1` 复用已验证的 V1.1，若实际 Git SHA 不一致必须提示调和。
2. **项目全集由人工维护**：不从关联图推测“公司全部程序”或自动添加项目。扫描报告公开列出 catalog 总数、成功数、失败数、暂停数与未纳管边界。
3. **扫描不保证理解全部业务**：Java AST、SQL/Mapper、Dubbo、配置等分层解析；关系必须提供证据，不能从同名类/表就创建已确认业务依赖。
4. **UI 围绕一条变化的主逻辑呈现**：A 程序主链 + 外部交叉媒介 + B 程序相关链；仅有 A 则画 A 的局部流程；不把全部节点倒到一张巨图。
5. **两张独立台账**：代码 Diff 处理覆盖台账负责“源码改动没漏处理”；影响路径台账负责“已识别到哪些程序/逻辑、哪段关系缺证据”。不可用前者宣称业务完整。
6. **首期做静态影响报告与人工证据审核**，同时冻结场景/契约 Schema；后续受控 V1/V2 只选择一条资金场景验证。

### 1.2 明确不保证

不保证识别项目清单之外的程序；不保证反射、动态 SQL、临时配置路由等所有隐藏边；不把静态资源同名当作逻辑必然关联；不假定历史 V1 行为就是正确需求；不将发现零条高风险项当成版本安全证明。

## 2. 项目目录（Project Catalog）

### 2.1 配置示例（拟议格式；当前 CLI 尚不支持）

~~~yaml
catalog_id: rhf-payments
catalog_revision: 5
projects:
  - project_id: spms-back
    gitlab_project_path: rhf/spms-back
    included: true
    ownership: payment-team
    version_policy: sequential_mr
    baseline_source: target_ref
    scanners: [java, mybatis, config]
    entry_tags: [http, dubbo]
  - project_id: reconciliation-job
    gitlab_project_path: rhf/reconciliation-job
    included: true
    ownership: clearing-team
    version_policy: sequential_mr
    scanners: [java, mybatis, config]
    entry_tags: [xxl-job]
  - project_id: refund-service
    gitlab_project_path: rhf/refund-service
    included: true
    scanners: [java, mybatis]
catalog_checked_by: reviewer
~~~

项目目录可以手动增减，但任何改动产生新的 `catalog_revision`。一次 Review Task 固定项目目录版本，避免分析过程中范围变更。一个项目暂时无法访问，不能把它从统计分母偷偷移走。

首次运行建议扫描清单里的每个项目的**已指定旧版本 Git 快照**（无法指定版本则标为待配置，不默认等于生产）。运行时保存完整 SHA、tree hash、语言文件清单、扫描器版本、数据源/配置关联的来源。

### 2.2 多项目版本独立递进

同一联合分析 ReviewBatch 可以同时包含：

- spms-back：V1.0 → V1.1
- refund-service：V3.7 → V3.8
- reconciliation-job：没有 MR，停留在 V2.4
- 其他纳管项目：在这个批次没有改动，但保留各自已确认快照

版本标签**只用于展示与递进校验**；跨项目影响应绑定一组 `project@commit` 的 ReviewBatch 版本清单，不要求所有项目版本名一致。

## 3. MR 版本基线状态机

### 3.1 实际状态

~~~text
NO_BASELINE
  ├─ 获取 MR targetRef 对应完整 SHA + tree
  ├─ 首次扫描 + 记录缺口
  └─ BASELINE_PROVISIONAL
        │ 人工核对项目/版本、关键扫描结果
        ▼
   BASELINE_VERIFIED (仅在指定范围/规则下)
        │ MR 固定 head + 候选合并
        ▼
   CANDIDATE_ANALYZED
        │ 发生实际 merge / rebase / squash，重新读取 GitLab 合并结果
        ▼
   MERGED_UNVERIFIED
        │ 校验最终结果 commit/tree，重算变化与扫描
        ▼
   BASELINE_PROMOTED (下一版本)
~~~

安全约束：
- **不是代码扫描完就提升基线**；必须 MR 真正合并、识别最终 commit/tree、与候选差异解释完、保存最终快照后才能提升。
- 分支名 `V1.0` / `V1.1` 不是 Git SHA；“MR 来源 V1.1 合入目标 V1.0”之后，是否形成新分支、标签，取决于企业 Git 工作流。项目必须配置 `version_name ↔ actual_ref ↔ final_commit` 映射。**不能假设 MR 的 head SHA 就是最终 V1.1**。
- 若目标分支在建基线后被他人更新，候选必须重新计算；失败标为 `TARGET_MOVED`。
- 下一 MR 目标名是 V1.1 时，系统核对其 Git 对象与之前晋升的 V1.1；不一致标为 `BASELINE_DIVERGED`，人工确认是热修复/cherry-pick/分支重建还是数据错误，禁止静默覆盖。
- 多 MR 同仓库：按业务规定顺序构造合并候选和冲突报告；不能直接把多个独立 diff 拼接为最终版本。
- GitLab 版本递进为主，不把“生产版本”偷换为默认主分支。若后续需要对比生产部署版本，则是另外一条可选的 deployment manifest。

### 3.2 首次没有基线怎么办？

输入 `MR(spms-back, source=V1.1, target=V1.0)`：

1. 通过 GitLab API 读取 MR 身份、source/target、base/head 与 target 当前 SHA；锁定 SHA 和树，不仅存分支名。
2. 查询 SQLite `baseline_snapshot(project, version=V1.0)`，不存在则固定扫描 V1.0 target 的 Git 树。
3. 对全部 catalog 项目生成各自的 pinned snapshot，形成 `catalog_snapshot_id`；无已定义旧版本的项目列 `VERSION_UNKNOWN`，允许部分分析但不声称范围完整。
4. 生成文件索引/AST/调用/SQL 等扫描结果和覆盖错误，状态 `PROVISIONAL`；重点资源由人工核对后才变成 `VERIFIED`。
5. 基于当前 target 与 source 构造 V1.1 candidate，生成双台账；MR 尚未合并时**不能写入 V1.1 已完成基线**。
6. MR 合并后（可能 squash/rebase），重新取得目标分支实际最新 commit/tree，核验本次合并归属，重建候选；成功才晋升 V1.1。
7. 下次 V1.2 → V1.1，先校验 target 的 tree hash 和上次保存的 V1.1，只有一致或经人工调和才复用缓存。

基线校验优先级：
`REFERENCE_MATCH`（SHA/树一致）、
`SOURCE_COVERAGE`（文件已枚举且失败显式登记）、
`ANALYZER_VERSION`（扫描器规则与缓存匹配）、
`RESOURCE_EVIDENCE`（关键数据库/消息边证据经过复核）；
最后一项是持续核验，而非首次执行的硬阻塞条件。

## 4. 为什么默认扫描难以发现隐藏依赖

### 4.1 一段 Java 代码不等于 SQL 写入事实

例如：

~~~java
transaction.setTransState("SUCCESS");
transactionMapper.update(transaction);
~~~

从这个 Java AST 可以确知方法调用以及 setter 参数的语法信息；但要确认“这个调用写了某个 MySQL 字段”，还必须解析 mapper 类型、映射 XML/注解 SQL、参数绑定、数据源以及可能的动态条件。没解析到就标 `WRITE_MAPPING_UNKNOWN`。

再例如 SQL：

~~~sql
SELECT * FROM trans_record WHERE trans_state = '2'
~~~

能确定 WHERE 中依赖一个字段/常量，但是否与 A 使用**同一个 DB 实例.Schema.表**，还需根据数据源配置核对；动态 schema / 路由查不到则停在 `RESOURCE_IDENTITY_UNKNOWN`。

因此，**关系引擎设计为“按证据拼接的分段路径”**。每段连接有来源、是否已验证、上下文范围，不将 AI 语义猜测伪装成静态扫描结论。

### 4.2 分层识别器

| 层 | 识别对象 | 常用证据 | 无法确认时 |
| --- | --- | --- | --- |
| Java | 类/方法、调用与接口实现、入口、条件分支 | AST、绑定类型、签名 | UNRESOLVED_CALL |
| Framework | Spring 注入、Dubbo 服务声明/消费 | 注解、XML、Maven 依赖、ZK 可选 | INJECTION_OR_PROXY_UNKNOWN |
| SQL | Mapper XML/注解、表列读写、WHERE/JOIN/HAVING 条件 | SQL AST、参数映射 | DYNAMIC_SQL_UNKNOWN |
| Resource | 数据库实例.Schema/表、MQ vhost/exchange/queue、Redis 命名空间 | 配置来源、管理 API、部署资源清单 | RESOURCE_IDENTITY_UNKNOWN |
| Runtime (later) | 实际调用/访问 | traceId、已有 Dubbo Filter、脱敏 SQL/消息记录 | NOT_OBSERVED（不视为无边） |

先做静态分层，后逐步接运行证据。用户也可以在关键链路上传入 **人工别名映射/业务规则**（记录审查者与生效版本）来弥补无法自动识别的资源，并与算法发现的事实清楚区分。

## 5. Diff Coverage Ledger：源码变更全量登记

### 5.1 主键和来源

`change_hunk(project, base_sha, candidate_sha, path, old_range, new_range, patch_hash)` 唯一标记原始 hunk。删除、新增、重命名、二进制资源及 Git 子模块变更必须有记录；没有 hunk 的整个新增文件也要落一个 file-level 事件。

状态：`SEMANTIC_MAPPED` / `NON_BEHAVIORAL` / `UNSUPPORTED` / `PARSE_FAILED` / `NEEDS_REVIEW`。语义分类可以多标签：
`CONTROL_FLOW`、`STATE_TRANSITION`、`API_CONTRACT`、`PERSISTENCE`、`MQ`、`REDIS`、`CONFIG`、`FRONTEND`、`OTHER`。

对每个 Hunk 保留完整 Git 补丁位置、所属方法/SQL/配置（可多个）、语义变化摘要和处理状态。每个原始变更都必须能在台账找到；按 MR 和最终合并树分别记录以解释合并时的覆盖差异。

### 5.2 不要伪造单一覆盖率

`git_diff_inventory_complete` 与 `semantic_resolution_coverage` 是两个指标。前者证明实际 Git 树和补丁清单已经枚举，后者只是有多少变更成功解析为已支持的语义类型。

**即使两项都是 100%，也不能推出业务关联没有遗漏。** 漏报评测只有在拥有带人工答案的样例集后才可能定量评估，首期不设置虚假的“业务召回率 99%”目标。

## 6. Impact Trace Ledger：业务主链与交叉点

### 6.1 数据结构

~~~text
ChangeHunk (*) → ChangeItem (多对多)
ChangeItem → SourceLogicSpine（A 的入口/分支/关键操作）
SourceLogicSpine → EffectCandidate（函数调用/接口/资源写入）
EffectCandidate → CrossingPoint（调用边或共享资源匹配）
CrossingPoint → ConsumerLogicSpine（B 的入口/分支/资源消费）
ConsumerLogicSpine → ImpactFinding（潜在后果和待核实信息）
~~~

并非每条路径都完整。**缺一个环节就保留一个可点击断点**，例如：

~~~text
支付入口 → PaymentService#setState() → [WRITE_MAPPING_UNKNOWN]
                                   │
                                   └─ 人工确认 mapper 与表字段归属
~~~

每段证据保存 `project_id`、`snapshot_id`、`symbol/SQL/resource_id`、`edge_type`、`source_file/lines`、`evidence_origin`（STATIC/CONFIG/RUNTIME/MANUAL/AI_HYPOTHESIS）、`verification_state`、`broken_reason`、`reviewer`、`reviewed_at`。

### 6.2 业务主链生成算法（先保守实现）

1. 从 `ChangeItem` 找所在方法/分支/副作用；以 **变更点**为锚。
2. 沿反向调用关系找可能的 HTTP/Dubbo/MQ/XXL-JOB/内部方法入口，形成 A 的候选入口路径；无法解析的调用单独列出。
3. 从修改点沿正向调用和资源操作找关键终点（返回、DB 写入、消息发出、缓存变更、外部接口），形成 A 主链的主要节点。
4. 以有证据的接口、数据库字段、SQL 条件、MQ 路由或 Redis key 归一化为 `CrossingPoint`，在**人工 catalog 的全部项目快照**中反查消费者。
5. 对消费者 B 从已知入口推导至交叉点的相关路径，截取相交的**局部业务脉络**，不展示 B 全部代码。
6. 根据变化类型检查潜在不兼容（状态值/条件、字段结构、次数、异常分支等）；不能证明业务后果就归类 `POSSIBLE_IMPACT`，不直接称为 Bug。
7. 分组去重：同一变化、同一资源、相同受影响入口可合并为一个 Finding，下挂多个代码证据和若干可选路径。

**A 和 B 没有已证实的桥时**，图只画到断点，B 不应被画成一条实线关联。若用户只选择 A，直接显示本项目的局部条件分支、状态转移及副作用变化。

### 6.3 分类方式与图规模控制

五级检索/折叠：
1. 业务域与场景（支付、退款、通知、对账、签约…）；
2. 业务入口/主要处理阶段（校验、决策、持久化、消息、返回/异常、补偿）；
3. 变化类型和交叉媒介（CALL/RPC/DB_FIELD/MQ/REDIS/CONFIG）；
4. 关联项目与最终消费逻辑；
5. 证据状态和人工确认。

对每个变化点，UI 默认只画**主链 + 最相关的 1～3 个交叉点**，其余显示数量、过滤条件和展开入口。底层 Impact Trace Ledger **不因 UI 截断而丢失**；计算预算触顶时生成 `ANALYSIS_TRUNCATED` 而不是静默略过。

### 6.4 概念示例：跨程序 vs 单程序

**跨程序（示意关联，需代码/SQL/资源证据证实）：**

~~~text
[A: PaymentService，基线 V1.0 / 候选 V1.1]
HTTP支付回调 ──→ 核验渠道结果 ──→ 写 trans_state=SUCCESS ──→ 返回
                                    │
                                    ▼
             [交叉点 DB: pay_schema.trans_record.trans_state]
                                    │ 可能改变被消费的状态取值
                                    ▼
[B: ReconciliationJob，当前 V2.4]
XXL-JOB 入口 ──→ 查询待对账交易 ──→ WHERE trans_state='2' ──→ 生成对账单
                                          ▲
                                      风险：旧筛选条件
~~~

**同程序：**

~~~text
[spms-back: RefundService]
退款请求 → 查询原交易 → 【修改了成功判断】
                         ├─ TRUE → 渠道退款 → 记录退款状态
                         └─ FALSE → 拒绝退款
            V1.0: state == 2
            V1.1: state in (2, SUCCESS)
                  → 需核对幂等、累计金额及异常分支
~~~

不把“可能风险”写成“已证实缺陷”；在实际测试验证之前只显示静态事实及合理候选。

## 7. 本地 Web/SQLite 方案

### 7.1 页面与使用流程

~~~text
项目清单 → 创建批次（选择 MR/版本） → 发现或复用 V1.0 基线
        → 扫描/覆盖审核 → V1.1 候选变化
        → 业务主链/交叉点可视化 → 人工确认/补充映射
        → 报告导出 → 合并事件后确认 V1.1 晋升
~~~

- 首页：当前批次、项目数、基线状态、未覆盖项目/文件、待确认重要风险。
- 项目清单：GitLab 地址、项目版本、扫描范围、来源校验、用户备注/人工别名规则。
- 版本谱系：项目 v1.0 / v1.1 等 SHA、候选与真实合并、人工校验、完整性记录。
- 变化分类台账：可筛项目/MR/文件/业务类型，打开代码 diff 和解析断点。
- 逻辑交叉点视图：以一个 `ChangeItem` 为中心，分别展开 A/B 流程；选择媒介与消费项目；节点点击显示文件/行号、SQL、验证方式。
- 结论处理：`CANDIDATE` / `CONFIRMED_IMPACT` / `NO_IMPACT_WITH_EVIDENCE` / `UNKNOWN`，保留人审日志和需求/业务契约草案。
- 静态报告：Markdown、JSON、HTML，均从同一持久化中间数据生成，不让 AI 重建证据图。

### 7.2 推荐技术栈（待开发）

`FastAPI` + `SQLite`（WAL、事务、迁移脚本）+ `Cytoscape.js`（从带分组的子图渲染），静态资源本地打包以允许企业内网离线运行。保留现有 Python CLI、GitLab/Git/Java AST 模块，扫描运行在独立工作线程/子进程以避免阻塞 UI；后续迁移任务队列。

Windows 启动拟议形式（**尚不存在该命令**）：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis.web --host 127.0.0.1 --port 8765
~~~

绝不默认监听 `0.0.0.0`。只读 GitLab Token 从环境变量加载，不持久化进 SQLite、日志或前端状态。首次仓库全量扫描应可以查看进度、失败项并重试；配置、证据和报告持久化。

### 7.3 拟议的数据表

| 表 | 重要字段 |
| --- | --- |
| `project_catalog` / `project_catalog_member` | catalog_revision、project_id、GitLab 路径、版本策略、纳管/暂停状态 |
| `project_version` / `baseline_snapshot` | version_name、commit_sha、tree_sha、parent_snapshot_id、scanner_version、config_hash、verification_status |
| `mr_review` / `review_batch` | MR URL、source/target、head/base SHA、候选/真实 merge SHA、合并顺序、状态 |
| `source_inventory` / `scan_status` | tree_file_id、文件类型、解析结果、跳过原因、扫描时间 |
| `diff_hunk` / `semantic_change` / `hunk_change_map` | Git Diff 清单、AST/SQL 语义对象、处理状态 |
| `dependency_node` / `dependency_edge` / `evidence` | 多版本多类型关系、资源标识和证据 |
| `logic_spine` / `logic_spine_step` | 入口、变更点、条件、调用、外部效应、退出 |
| `crossing_point` / `impact_trace_segment` | 媒介、跨项目消费者、证据路径、断点原因、合并组 |
| `impact_finding` / `review_decision` | 风险、状态、人工结论及审计历史 |
| `business_scenario` / `behavior_contract` | 长期可复用的场景与需求确认契约（预留） |
| `scenario_execution` / `effect_event` / `behavior_diff` | 后续一条资金业务的隔离执行结果（预留） |

### 7.4 API 建议（设计，当前未实现）

~~~text
POST /api/catalogs
POST /api/reviews       {catalog_revision, mr_urls[], version_labels}
GET  /api/reviews/{id}/baseline
POST /api/reviews/{id}/scan
GET  /api/reviews/{id}/changes
GET  /api/changes/{change_id}/main-spine
GET  /api/changes/{change_id}/crossings
GET  /api/crossings/{id}/evidence
POST /api/findings/{id}/decision
POST /api/reviews/{id}/promote-merged-version
GET  /api/reviews/{id}/report
~~~

版本晋升需要检查 MR 已合并和最终 SHA，不能由前端点击直接跳过安全检查。

## 8. 证据分级与“尽可能不漏”

| 级别 | 判断 | 示例 |
| --- | --- | --- |
| F1 源码事实 | 可以由 Git/AST/SQL 明确验证 | 某条件从 `=2` 变为 `=SUCCESS` |
| F2 连接事实 | 链路跨层证据齐全 | Java mapper 调用与 SQL 映射、同实例.Schema.表 |
| F3 疑似业务影响 | 事实链支持风险但业务后果未证实 | B 的旧过滤可能漏掉新状态 |
| F4 业务确认/执行证据 | 人工需求批准或受控运行证明后果 | 确认新状态应参与对账且旧 SQL 漏单 |
| UNKNOWN | 缺少代码/配置/映射/项目 | 只显示断点和下一步核查手段 |

用户维护 catalog 是**全集声明**，不是系统能自行证明公司没有其他系统。优先统计“已纳管项目及版本是否覆盖”“每个 Hunk 有无处理状态”“关系断点在哪里”“人审确认多少”。已知案例召回率、真实生产漏报率需要长期累积，不作为首期自动保证。

## 9. 业务契约与差异执行（后续而非当期）

首期就可以建立 `BusinessScenario`/`BehaviorContract` JSON Schema，分类 `NEW`/`CHANGED`/`UNCHANGED`/`UNKNOWN`；当前阶段不执行自动回归。

未来只选一条资金业务场景（例如部分退款或对账）：
- A/B 两版本独立服务 + DB/Redis/MQ（具体依赖按选定场景最小化）。
- 同一脱敏 fixture、固定时钟/随机因素、相同经批准的渠道模拟脚本。
- 真实执行内部业务路径，外部银行/支付接口模拟并记录调用意图。
- 比较返回值、DB 行/字段、MQ/Redis、渠道金额/次数/幂等；先判定是否可比，再按批准契约判定是否预期变化。
- 不可比较/未执行/测试环境异常明确记状态，不把“无差异”当作“满足需求”。

## 10. 可实现性与风险

| 部分 | 可实现性 | 首要不确定性 |
| --- | --- | --- |
| 固定 SHA 建版本快照与逐次晋升 | 高 | 企业 MR 分支命名、合并策略及 SHA 变化 |
| 项目清单/首次全量代码扫描 | 高 | 仓库权限、巨大项目、Java 解析失败 |
| Diff Coverage Ledger | 高 | 语义解析能力决定 `UNSUPPORTED` 数量 |
| Java/Dubbo/SQL/MQ/Redis 依赖 | 分阶段可行 | 大量动态配置/调用需要补证据 |
| A/B 主链交叉点 UI | 可行 | 业务入口抽取、子图分组、路径爆炸 |
| 自动断言“业务影响全部识别” | 不可保证 | 项目范围及隐藏动态依赖 |
| 一条隔离资金业务差异执行 | 可行但成本较高 | 环境可复制性、渠道 Mock 边界 |

## 11. 验收样例

1. 首次输入 V1.1 → V1.0 MR，生成 V1.0 基线和可解释扫描缺口；同项目下次 V1.2 → V1.1 能复用。
2. V1.1 发生 squash merge，候选 head 与最终合并结果不同；版本晋升必须使用实际最终 Git 树，不错误沿用源 HEAD。
3. 任一纳管项目扫描失败：UI 显示明确问题，不算 100% 覆盖。
4. Java 修改 setter 但 SQL 映射未扫描到：停在 `WRITE_MAPPING_UNKNOWN`，不伪造 DB 写入事实。
5. 同一共享字段的跨 A/B SQL 消费路径：绘制主链及交叉媒介，B 未提交 MR 仍进入候选；没有数据源身份时不能标确定。
6. 单项目只有局部条件改变：只展示其内部判断/副作用路径，不画无关 B。
7. 一个交叉点有 200 个消费者：默认折叠分组，能逐层展开全部，计算未完成要有明确 `TRUNCATED`。
8. 用户提供 catalog 10 项、仅 9 项成功：分母为 10，报告显示 9/10，不借“没有发现影响”掩盖第 10 项。
9. 完整 Diff Hunk 均归类，但某 SQL 类型 unsupported：代码账本完整，业务关系账本仍未知，报告不称风险已排除。

下一步实现拆分见 [DEVELOPMENT_PLAN_V3.md](DEVELOPMENT_PLAN_V3.md)。
