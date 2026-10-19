# 架构设计 v2.0：版本级影响分析、业务契约与差异执行

> 设计文档，非功能完成声明。更新：2026-10-19。当前运行实现参见 [P0 使用文档](P0_USAGE.md)。  
> 核心闭环：**版本变更清单 → 跨项目业务影响 → 契约确认 → 回归场景 → 隔离 V1/V2 执行 → 副作用差异 → 人工评审**。

## 1. 需求与边界

### 1.1 系统要回答的问题

- 一次版本由哪些需求和 GitLab MR 构成？这些 MR 合并后实际改变了哪些方法、接口、配置与业务行为？
- 如果代码改变交易状态或 MQ 消息，哪些**没有提交 MR 的程序**依赖这些资源？
- 新功能的输入、输出、副作用预期是什么？旧功能哪些行为必须不变，哪些经业务确认允许变化？
- 在相同初始条件下，V1 与 V2 的实际结果有什么差别？它是否超出了批准的变更范围？
- 目前哪些风险仍无法判定？缺失的是代码依赖、场景契约、外部模拟、环境还是执行证据？

### 1.2 测试哲学

- **单元测试仍有价值**：金额计算、签名验签、状态机转移、输入校验等相对纯粹的逻辑适合快速、细粒度断言。
- **不能直接从实现生成正确答案**：AI 可根据需求提出待审核场景草案，但验收期望必须来自独立业务约束、审核的需求或既有经确认的契约。
- **Mock 看边界，不看数量**：银行/支付公司接口、短信等不可真实调用的外部系统可以模拟；系统内部 Service、Mapper 和关键 DB 状态流转应尽量真实执行。
- **回归资产以业务场景为核心**：`scenario_id` 跨版本持续复用；版本引用测试集，不按 MR 创建无法长期维护的“临时用例目录”。
- **历史行为不是绝对真理**：旧版本行为仅作为差异线索；如果历史存在缺陷，应按业务批准的迁移契约判断新版，而不是强迫新版复现旧 Bug。

### 1.3 非目标（至少前四阶段）

不自动合并生产 MR、不接真实支付渠道进行回放、不保证全量未知依赖自动发现、不将无差异作为上线安全证明、不在本阶段构建通用 AI 代码编辑器。

## 2. 新架构

~~~text
输入：需求/验收说明（可选，但契约必须确认） + 多 GitLab MR + 目标环境
                              │
                              ▼
      [A] Release Coordinator：版本清单与候选合并状态
          ├─ GitLab 固定 SHA / 合并树 / 依赖 Jar / 配置
          └─ base (production)、MR diff-base、candidate (release)
                              │
                              ▼
      [B] Change & Dependency Analyzer
          ├─ Java AST/语义 Diff、接口兼容及资源写入变化
          ├─ 旧图 ∪ 新图（方法、Dubbo、DB 列、MQ、Redis）
          └─ 受影响入口、关联未修改项目、未覆盖证据
                              │
                              ▼
      [C] Contract & Scenario Planner
          ├─ 需求与业务场景对应、人工确认预期变化
          ├─ NEW / CHANGED / UNCHANGED / UNKNOWN
          └─ 已有场景重用 + 缺失场景建议 + 优先级
                              │
                      ┌───────┴─────────┐
                      ▼                 ▼
            [D1] 人工评审清单    [D2] Differential Runner（后续）
                                      ├─ V1 独立隔离环境
                                      ├─ V2 独立隔离环境
                                      ├─ 相同规范化输入/初始数据
                                      ├─ 可控外部银行渠道模拟
                                      └─ 返回、DB、MQ、Redis、外部调用事件
                                               │
                                               ▼
                                      [E] Semantic Comparator
                                      ├─ 字段/状态/金额/次数
                                      ├─ 允许差异/噪声归一化
                                      ├─ 非预期变化/欠缺预期变化
                                      └─ 不可比较/执行失败/未知
                                               │
                                               ▼
                                   [F] 有证据的版本评审报告
~~~

实施方式：保留现有 Python CLI 和 JDK AST 扫描器作为起点；新增清晰的纯业务数据模型与 JSON 接口，测试执行器作为独立 Worker/Agent。以后可使用内网服务、数据库和 Web UI 包装，**不要求为了实现设计先重写整个原型**。

## 3. 三种基线必须分开

### 3.1 Git/MR 比较基线

对每个 MR 锁定 `diff_refs.base_sha` 与 `head_sha`，用于分析“这个 MR 引入了什么”。如果目标分支改变，重新检查合并基线。

### 3.2 生产版本基线（Release Baseline / V1）

记录生产当前部署的每一个服务及前端的 Commit、API Jar 版本、数据库 schema 与迁移版本、必要配置哈希、MQ/Redis/ES 资源名称及拓扑。生产状态可能与某个 MR 的 target 分支不同，**不可将 MR diff-base 当作生产运行版本**。

### 3.3 候选版本（Candidate / V2）

所有 MR 按已定义顺序生成候选合并树，并锁定最后 Commit/树哈希、二进制依赖版本、迁移脚本及配置。对于同仓库多个 MR，冲突、不兼容目标分支和不可合并必须明确列为阻塞；对跨仓库项目记录兼容矩阵与部署顺序。

未来输入示例（**目标接口，当前 CLI 尚不接受此 JSON**）：

~~~json
{
  "release_id": "2026-10-R01",
  "requirements": ["PAY-SIGN-23", "REFUND-15"],
  "mrs": [
    {"url": "https://gitlab.example.cn/rhf/spms-back/-/merge_requests/101"},
    {"url": "https://gitlab.example.cn/rhf/adpts/-/merge_requests/52"}
  ],
  "baseline": {
    "name": "prod-2026-10-18",
    "repositories": {
      "rhf/spms-back": {"commit": "<full-commit-sha>"},
      "rhf/adpts": {"commit": "<full-commit-sha>"}
    },
    "api_dependency_lock": "<artifact-manifest-hash>",
    "schema_version": "<schema-manifest-hash>",
    "config_hash": "<config-manifest-hash>"
  },
  "candidate": {
    "policy": "merge_in_defined_order",
    "config_hash": "<candidate-config-hash>"
  }
}
~~~

业务行为差异比较应在“版本清单可复现”后进行，不应把不同配置、不同 API 依赖的结果误认为纯代码造成的差异。

## 4. 依赖图谱：从调用方法扩展到可观察资源

### 4.1 节点和边

- 代码节点：应用/仓库、模块、Java 类/方法、HTTP/Dubbo API、MQ/XXL-JOB 入口、前端页面。
- 资源节点：数据库实例/Schema/表/字段、MQ vhost/exchange/queue/routing key、Redis Key 模式、ES 索引与字段、配置项。
- 场景节点：业务场景、契约、测试执行、观测事件及未验证风险。

关键关系：`CALLS`、`PROVIDES_API`、`CONSUMES_API`、`WRITES`、`READS`、`READS_CONDITION`、`PUBLISHES`、`ROUTES_TO`、`CONSUMES`、`USES_CONFIG`、`COVERS`。

重要：**对 SQL WHERE、JOIN、HAVING 等判断字段单独建立 `READS_CONDITION`**。方法没有调用关系仍可能通过交易状态、消息或缓存互相影响。

### 4.2 如何找未知 D

~~~text
C.updateState()
   └─ WRITES [mysql:orders.trans_state]
                     ├─ READS_CONDITION ← D.ReconciliationJob
                     └─ READS           ← E.RefundQuery
~~~

如果 D/E 未出现在当前 MR 中，也要进入“需要确认的关联消费者”清单。不能根据同一表名、同一个 Redis Key 前缀就证明存在实际因果；需保存跨环境资源标识、代码证据与运行证据。

### 4.3 证据模型

每条关系包含：
`source_id`、`target_id`、`relation_type`、`snapshot_id`、`environment`、`resource_identity`、`evidence_type`（STATIC/RUNTIME/CONFIG/MANUAL/INFERRED）、`evidence_location`、`observed_at`、`confidence_state`（VERIFIED/CANDIDATE/UNKNOWN）。

保留旧图与新图的并集，以免遗漏被删除的调用或资源关系；动态未观测到的关系不直接删掉。缓存按仓库 SHA + 扫描器版本命中，更新规则应失效重建。SQL 动态拼接、反射、消费者不可见等明确标为未知。

### 4.4 采集阶段与约束

1. 先做 **Java 静态类型和 API/Dubbo 声明**，确保比单纯方法名/参数数量匹配更可信。
2. 再做 MyBatis XML 与 SQL 的表/列读写/条件，优先从支付状态、退款、对账高风险数据路径开始。
3. MQ、Redis、ZooKeeper 用代码分析 + 管理 API/配置，结合已有 Dubbo Filter/traceId 的实际证据。
4. ES、XXL-JOB、前端依赖随后扩展。不得要求现有 Dubbo 2.6.4、ES 6.4.2 或 Java 8 业务服务必须升级。

## 5. 业务契约：决定“应该变化什么”

### 5.1 契约分类

| 类别 | 规则 | 行为比较 |
| --- | --- | --- |
| `NEW` | 需求新增业务，V1 可能没有该入口 | 用经批准的新期望验证 V2；V1 不适用 |
| `CHANGED` | 需求明确改变原场景输出或副作用 | V2 必须满足新契约；V1/V2 应展示差异及理由 |
| `UNCHANGED` | 经确认旧业务仍需保持原有契约 | 对 V1/V2 做语义差异检测；发现差异须解释 |
| `UNKNOWN` | 需求不明确或发现了未纳管影响 | 生成审批任务/待确认项，不自动认定通过 |

**不是所有未列在需求中的变化都自动归为 UNCHANGED**：先结合影响图定位，再由历史已审批契约或人工确认；证据不足使用 UNKNOWN。变更分类及审批记录与需求版本绑定。

### 5.2 业务场景元数据示例

以下是**拟议数据格式**，不是当前 CLI 受支持的配置：

~~~yaml
scenario_id: payment.success.then.refund.v1
domain: refund
business_entry: dubbo:RefundApi.refund
scenario_version: 1
tags: [payment, refund, idempotency, money-risk]
preconditions:
  - original_payment.status == SUCCESS
  - refundable_amount == 100.00
input_fixture: fixtures/refund/partial-refund.json
expected:
  response:
    result_code: SUCCESS
  database:
    - table: refund_trans_record
      operation: INSERT
      fields:
        refund_amount: "20.00"
        refund_status: SUCCESS
  external_intents:
    - provider: bank_refund
      operation: refund
      count: 1
policies:
  generated_fields: [trace_id, db_id, created_at]
  eventually_consistent: false
ownership:
  reviewer: payment-reviewer
~~~

注意：预期金额和状态由**业务契约审核**确认，不应自动从待测方法中的常量生成。参数、状态变化、外部调用、退款次数等属于可观察结果。

### 5.3 一个版本如何组织回归集合

~~~text
长期场景库（独立于 MR）
  payment.create / payment.query / refund.partial / notify.retry / reconcile.daily
                            │
                            ▼
Release 2026-10-R01
  NEW:      签约完成后的首次支付
  CHANGED:  部分退款金额边界
  UNCHANGED: 历史支付查询、失败重试、对账
  UNKNOWN:  另一个未提交 MR 的 MQ 消费者
~~~

选择场景以“变更→依赖→业务入口→契约”为依据；优先覆盖资金、重复执行、状态、对账风险。场景库中已有用例复用；对于缺失契约先创建人工补充任务，而非随意生成期望数据。

## 6. V1/V2 差异执行：隔离与证据

### 6.1 执行环境约束

- 两个版本使用**独立的 DB schema/实例、Redis namespace/实例、MQ vhost/队列、ES 测试索引**，防止互相污染。
- 业务数据使用**脱敏快照、合成数据或可复现 fixture**；不使用生产真实支付授权信息。
- 银行/第三方支付渠道采用受控 mock server，规定同一外部条件和返回序列；捕获**调用意图**（接口、金额、交易号、次数、幂等键）而非触发实际扣款/退款。
- 内部调用尽量保留真实链路；只有不可控、昂贵或不可访问的边界允许 Mock，且每个 Mock 都必须登记覆盖缺口和风险。
- 固定/注入时钟、随机源、业务日、配置开关、渠道响应脚本；明确并发顺序、异步消费超时与最终一致性窗口。
- 不要为了获得比较结果而共享同一套会被修改的 DB/MQ/Redis。异步任务执行前需明确初始消息及隔离范围。

### 6.2 采集什么

每次 `ScenarioExecution` 至少记录：

| 类型 | 采集内容 | 判定要点 |
| --- | --- | --- |
| 请求和返回 | 入口、标准化入参、响应码/业务响应/异常 | 语义字段及兼容性 |
| DB | 执行前后业务表行/列变化或事务事件 | 金额、状态、记录数、关键关联键 |
| Redis | Key 模式、操作、最终业务状态、必要 TTL 区间 | 幂等、锁、缓存兼容 |
| MQ | exchange、routing key、消息摘要、数量及消费结果 | 未发、重复发送、字段兼容 |
| 外部渠道 | URL/方法、业务操作、金额、幂等键、次数、模拟结果 | 禁止多次支付/退款 |
| 运行上下文 | release SHA、配置哈希、fixture、模拟脚本、时钟、观测窗口 | 可复现性与可比性 |

不采集/回传原始银行卡号、密钥、敏感证件信息。尽量存规范化字段、散列和引用；留有安全审计记录。

### 6.3 对比算法

1. **先判断可比性**：是否相同业务初始状态、输入语义、环境配置和外部模拟响应？如果不是，返回 `INCOMPARABLE`。
2. **规范化**：映射自增主键/UUID/traceId/时间戳为稳定逻辑标识；金额统一精度与单位；可忽略明确批准的非业务字段。
3. **语义对齐**：按业务唯一键和事件类型匹配 DB/MQ/Redis/外部调用；仅在没有因果要求时忽略独立事件顺序。关键状态转移和渠道调用**次数/顺序不得随意抹平**。
4. **契约判断**：对于 NEW 检查新期望；CHANGED 检查批准变更；UNCHANGED 检查非预期差异；UNKNOWN 只列证据待审。
5. **产生报告**：`EXPECTED_CHANGE`、`UNEXPECTED_CHANGE`、`MISSING_EXPECTED_CHANGE`、`NO_OBSERVED_CHANGE`、`INCOMPARABLE`、`EXECUTION_FAILED`、`NOT_EXECUTED`；所有结论带置信边界。

对 MQ、对账和补偿等异步流程，必须在合理的**完成条件或观测窗口**后做断言，不能以瞬时快照证明最终一致。失败重试要比较外部调用次数、最终业务状态和补偿结果。

### 6.4 示例：状态扩展造成对账遗漏

~~~text
变更：PaymentService 新增状态 SUCCESS
  └─ WRITES trans_record.trans_state
       ├─ RefundService 识别 SUCCESS
       └─ ReconciliationJob WHERE trans_state = '2'
~~~

分析阶段先出具“疑似遗漏”并要求确认：实际落库是否使用 `SUCCESS`？如果不是，该风险不能直接定为 Bug。

业务合同：旧状态支付的对账行为需保持；新增状态交易如果代表成功，应能进入对账（由需求批准）。

执行阶段：在 V1/V2 分别构造等价交易和对账作业初始条件；比较被对账交易集合、状态、金额、外部通知/文件输出。若 V1 没有新状态业务，需按 NEW/CHANGED 契约评价 V2，不能简单把两边行数不同认定为失败。

## 7. 数据模型和模块契约

### 7.1 核心数据表（拟议，不表示已建表）

| 表 | 关键字段 / 约束 |
| --- | --- |
| `release_manifest` | `release_id`、`baseline_manifest_hash`、`candidate_manifest_hash`、环境、创建时间 |
| `release_repository` | `release_id`、项目 ID、base/head/merged SHA、API 版本、配置哈希 |
| `change_item` | `release_id`、源码符号、修改类型、代码位置、effect 候选 |
| `dependency_node` / `dependency_edge` | versioned node、typed edge、resource identity、evidence |
| `business_scenario` | `scenario_id`、`scenario_version`、入口、输入 fixture、标签 |
| `behavior_contract` | `scenario_id`、`release_id`、NEW/CHANGED/UNCHANGED/UNKNOWN、预期副作用、审核状态 |
| `release_scenario` | 版本下需要执行的场景、来源影响路径、优先级 |
| `scenario_execution` | V1/V2、fixture hash、环境 hash、runner 版本、状态 |
| `effect_event` | execution ID、事件类型、业务键、规范化内容摘要、时间序号 |
| `behavior_diff` | 对齐键、两侧证据、结果类别、契约判断、是否可比 |
| `review_finding` | 风险级别、代码证据、测试证据、人工结论与审批记录 |

多租户/多环境资源 ID 要带 cluster、schema、vhost 等边界。`effect_event` 的内容应配置脱敏保留策略及最大采样量。

### 7.2 计划中的可组合接口

~~~text
ReleaseResolver.resolve(MR list, target environment) -> ReleaseManifest
SemanticChangeExtractor.diff(V1, V2) -> ChangeItem[]
ImpactGraph.trace(ChangeItem[], evidence snapshots) -> ImpactPath[]
ScenarioPlanner.select(ImpactPath[], approved requirements) -> ScenarioPlan
ContractRegistry.approve(ScenarioPlan, reviewer decisions) -> BehaviorContract[]
ExecutionOrchestrator.run(ScenarioPlan, V1/V2 env specs) -> ScenarioExecution[]
EffectCollector.collect(executionId) -> EffectEvent[]
BehaviorComparator.compare(executions, contracts) -> BehaviorDiff[]
ReviewReporter.render(release, findings, diffs, gaps) -> Markdown/JSON
~~~

接口先在 Python/JSON 层冻结语义和 Schema，再根据公司部署方式决定是否引入 Spring Boot 管理端。

## 8. 输出报告升级设计

建议顺序：

1. 版本清单、MR 和 Git SHA、候选合并状态及扫描完整性。
2. **主要业务行为变化**：对应需求、代码位置、数据及外部接口变化。
3. **依赖传播链**：直接/间接影响、未修改项目、证据性质与采集缺口。
4. **契约矩阵**：NEW、CHANGED、UNCHANGED、UNKNOWN，审核者及依据。
5. **建议版本回归场景**：复用已有场景、需新增场景、未覆盖场景。
6. **执行结果**（若具备 runner）：V1/V2 响应 + DB/MQ/Redis/渠道调用意图差异；是否符合契约。
7. **人工重点评审清单**：优先 5～20 条高价值事项，每条能跳转到 SHA/文件/方法/测试记录。
8. 风险缺口：不可比较、超时、未纳管依赖、无法验证的环境与 Mock 覆盖。

**没有执行器时**报告只给“推荐验证/尚未执行”，不可伪造比较结果或声称“版本回归通过”。

## 9. 技术路线与工程安全

- 现有 Python CLI/JDK AST 继续复用；Java 8 业务仓库不执行构建脚本。扫描 AST 可优先独立用 JDK 17，已支持 JDK 8 相关工具链检查。
- 起步用 JSON 文件、SQLite 或 MySQL 存元数据；图查询规模需要时再引入 Neo4j，不作为先决条件。
- Java 精确调用链可评估 jQAssistant / CodeQL；SQL 和中间件资源依赖需要自定义规则和运行证据，不假设开源工具能直接涵盖存量业务。
- 执行 V1/V2 的容器/虚拟化 Worker 必须沙箱化、最小权限、网络出站白名单；不能自动使用生产账号、真实渠道密钥或生产 DB。
- 独立保留 Scanner 版本、需求批准版本、fixture 哈希和比较策略版本，保证同一结论可追溯。
- 所有 AI 结论必须引用实际代码或已批准契约；推断性依赖保持 CANDIDATE / UNKNOWN，不越权改动仓库和生产环境。

## 10. 度量与验收

- **影响识别**：已知人工植入的隐藏 D 程序依赖检出率；误报率；未解析/未纳管范围比例。
- **契约质量**：高风险受影响场景中有经批准 NEW/CHANGED/UNCHANGED 判断的比例；UNKNOWN 处理时长。
- **差异执行**：可复现率、可比率、异常副作用检出能力、Mock 覆盖缺口。
- **评审价值**：代码证据链接正确率、历史回归缺陷重现率、人工处理关键风险所需时间。
- **安全**：测试不能产生真实银行支付、退款或生产数据变更；失败必须可审计。

详细分阶段开发任务见 [DEVELOPMENT_PLAN_V2.md](DEVELOPMENT_PLAN_V2.md)。

## 11. 开源与既有工作参考

- [jQAssistant](https://github.com/jqassistant/jqassistant)：Java 静态依赖结构。
- [CodeQL](https://github.com/github/codeql)：数据流/语义分析。
- [Testcontainers](https://testcontainers.com/)：隔离中间件环境的可选实现。
- [Playwright](https://github.com/microsoft/playwright)：后续前端行为/视觉验证。
- 已有 SPI/traceId、Dubbo 2.6.4、ZK、MyBatis、RabbitMQ、Redis、MySQL/Oracle、ES 6.4.2、XXL-JOB：优先复用现有平台能力。

如果考虑专利，需要另行检索相近权利要求并做新颖性分析，不能将“静态图 + AI + 测试选择”当作天然可专利创新。
