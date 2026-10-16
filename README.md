# Diff Analysis — 多 GitLab MR 代码变更影响分析与 AI 辅助评审

> 状态：方案设计 / 待开发  
> 文档版本：v1.0（2026-10-16）  
> 定位：**输入一组自建 GitLab Merge Request，自动分析跨项目代码改动、依赖影响与疑似遗漏，输出有代码证据的人工评审报告。**

## 1. 背景与核心诉求

大模型让开发迭代更快，但同时引入几个问题：

- AI 修改范围可能很大，出现过度抽象、复杂语法或与项目惯例不一致的实现，人工逐行评审成本增加。
- AI 可能缺少历史业务上下文，只修改已知的 A → B → C 流程，遗漏依赖 C 产生的数据或消息的未知程序 D。
- 一个需求往往涉及多个微服务、多仓库和多个 MR；只评审单个 MR 容易遗漏跨项目联动。
- 内部调用图、ZK 注册关系、MQ 消费者、Redis Key、数据库字段依赖等信息分散，而且长期维护的依赖清单不可避免地不完整。

**本项目优先解决“影响范围和遗漏改动评审”，而非替代人工做最终质量判断。**

### 1.1 必须实现的能力

1. **多 MR 输入**：接收同一需求/版本涉及的多个 GitLab MR URL。
2. **真实代码读取与 Diff**：使用配置的只读 GitLab 权限拉取仓库、锁定 base/head commit，获取完整 Git Diff；处理 MR API 差异截断。
3. **语义变更提取**：识别 Java 方法、API Jar、配置、SQL、消息、Redis、前端组件及 CSS 的变更，而不是仅依赖文本 Diff。
4. **跨仓库依赖反查**：从变更方法和外部可观察行为出发，利用静态图谱、历史基线、运行时证据及中间件配置定位上下游。
5. **遗漏改动检查**：检查关联消费者是否同步适配，找出未包含在输入 MR 中、但可能受影响的项目与业务路径。
6. **人工重点评审报告**：按风险排序，给出“变了什么、影响谁、可能遗漏什么、为何判断、应审查何处”；提供可定位的 GitLab commit/文件/行号链接。
7. **证据和不确定性管理**：严格区分确定规则违规、可验证的兼容性问题、疑似遗漏和未知依赖；不可把“未找到依赖”当作“没有依赖”。

### 1.2 暂不作为第一阶段目标

- 自动修改业务代码、自动合并 GitLab MR。
- 全公司实时、绝对完整的业务知识图谱。
- 全量自动化测试平台、生产交易回放或直接发布门禁。
- 仅凭 AI 推理结果判定“可以安全上线”。

后续可扩展自动选择回归用例、新旧版本差异测试及 MR 评论回写，但必须和基础影响分析解耦。

## 2. 适用环境与既有资产

| 范围 | 当前特点 | 设计要求 |
| --- | --- | --- |
| 源码 | 自建 GitLab、多项目、多 MR | 支持私有仓库与自定义域名；只读访问优先 |
| 后端 | Java 8、Spring、Dubbo 2.6.4 | 代码扫描器可以独立运行在更新的 JDK，业务系统不必升级 |
| 服务注册 | ZooKeeper / Dubbo Admin | 收集 providers、consumers、订阅关系，结合实际调用证据 |
| 服务追踪 | 已有自研 SPI / Filter / traceId | 优先复用，不要求重新部署 SkyWalking |
| 消息 | RabbitMQ | 分析生产者、Exchange、RoutingKey、Binding、Queue、消费者 |
| 缓存 | Redis | 分析读/写方法、Key 模式、值结构及跨服务引用 |
| 数据库 | MySQL、Oracle；重点关注 adpts、spms 跨项目数据访问 | SQL 表级/字段级、条件依赖和写入副作用 |
| 搜索日志 | Elasticsearch 6.4.2、日志/监控程序、adpts、Canal | 分析索引、字段映射、查询及消费方；不强制升级生产 ES |
| 作业 | XXL-JOB | 分析任务入口、调用链和访问资源 |
| 前端 | 各管理平台仓库 | 组件/样式引用和页面影响，后续配合 Playwright 验证 |

重要业务约束：**对外 API 类的任何修改按公司规则要求升级 API 版本**，但还需判断消费者兼容性；版本升级并不能自动证明兼容。

## 3. 总体架构

```text
多个 GitLab MR URL + 需求说明（可选） + 环境/基线版本
                          │
                          ▼
              MR Loader / Workspace
  GitLab API → clone/fetch → 锁定 SHA → 合并候选树 → git diff
                          │
                          ▼
               Semantic Diff Analyzer
    Java AST / API / SQL / 配置 / MQ / Redis / JS/CSS
                          │
                          ▼
                 Dependency Center
   ┌──────────────────┬──────────────────┬────────────────┐
   │ 静态代码依赖快照 │ 运行时 SPI 事件  │ 中间件/配置快照│
   │ 类/方法/资源引用 │ traceId/读写事件 │ ZK/MQ/DB/ES   │
   └──────────────────┴──────────────────┴────────────────┘
                          │
                          ▼
               Change Impact Analyzer
      版本图谱差异 + 变更类型传播 + 反向查询 + 证据缺口
                          │
                          ▼
               AI Review Reasoner
     按需加载相关方法/上下文 → 业务语义推理 → 遗漏候选
                          │
                          ▼
               Review Report Generator
   改动概览 / 主要影响点 / 疑似遗漏 / 风险 / 代码证据
             （后续可回写 GitLab MR 评论）
```

第一版建议采用**单体分析服务 + 扫描 Worker + MySQL**，保持模块边界清晰；有大量复杂图查询需求时再评估 Neo4j，不以部署图数据库作为 MVP 前置条件。

### 3.1 模块划分

| 模块 | 职责 | 优先级 |
| --- | --- | --- |
| `review-orchestrator` | 接收多个 MR，记录任务、进度、目标版本、最终报告 | P0 |
| `gitlab-loader` | 认证、获取 MR 元信息、拉取源码、锁定提交、生成 Diff | P0 |
| `semantic-diff` | 识别 Java/API/SQL/配置/前端等具体变更类型 | P0 |
| `dependency-scanner` | 扫描完整修改项目，建立内部调用和外部资源引用图 | P0 |
| `dependency-center` | 保存版本化基线、节点、边、证据、运行事实 | P0 |
| `impact-engine` | 反向影响传播、联合多 MR 关联、疑似遗漏规则 | P0 |
| `ai-review` | 有边界的代码上下文检索、业务推理、证据化结论 | P1 |
| `report-generator` | HTML/Markdown/JSON 报告、GitLab 代码链接 | P0 |
| `runtime-evidence-adapters` | SPI、ZK、RabbitMQ、Redis、DB、ES 证据接入 | P1/P2 |
| `regression-adapter` | 测试用例关联、覆盖/回放/视觉差异（后续） | P3 |

## 4. 输入、版本与 Diff 规则

### 4.1 分析任务输入（示例）

```json
{
  "review_name": "支付签约功能",
  "target_environment": "uat",
  "merge_requests": [
    "https://gitlab.example.com/rhf/spms-back/-/merge_requests/101",
    "https://gitlab.example.com/rhf/adpts/-/merge_requests/52",
    "https://gitlab.example.com/rhf/spms-front/-/merge_requests/86"
  ],
  "requirement": "可选：需求描述、约束及预期行为变化",
  "baseline_policy": "target_branch_or_release_manifest"
}
```

### 4.2 基线与候选代码

- **生产基线**：当前生产实际部署的每个仓库 commit、API 依赖版本、必要配置哈希；用于发布影响分析。
- **MR 目标基线**：各 MR 目标分支与 merge-base；用于精确识别 MR 引入的变更。
- **候选版本**：锁定每个 MR 的 head SHA，并尽量通过临时合并树验证“合入后”的效果；若合并冲突或目标分支漂移，显式报告。
- **多项目版本清单（Release Manifest）**：同一次评审中列出所有项目的基线与目标 SHA，即使项目没有被修改。
- **全程可复现**：GitLab API、扫描器和模型只针对固定 SHA 输出结论；MR 发生新提交后，报告必须标记为过期并重新分析。

使用 `git diff <base>...<head>` 识别 MR 相对共同祖先的修改；需要验证候选合并结果时，还应比较候选合并树与目标分支。两者不能混淆。对多 MR 同仓库输入需明确合并顺序并检测冲突；被 GitLab API 截断的 Diff 必须从完整 Git 对象重建，不能静默跳过。

## 5. 版本化依赖关系模型

### 5.1 三层依赖

1. **代码结构关系**：方法调用、接口实现、继承、注入、反射候选、API/组件引用。
2. **跨服务资源关系**：Dubbo、MQ、Redis、DB 字段、ES 索引、配置项和任务入口。
3. **业务与测试关系**：HTTP/Dubbo/MQ/XXL-JOB 入口、业务场景、已有测试及业务契约。

### 5.2 节点

示例：`APPLICATION`、`MODULE`、`METHOD`、`API`、`ENTRY`、`MQ_EXCHANGE`、`MQ_QUEUE`、`REDIS_KEY_PATTERN`、`DB_TABLE`、`DB_COLUMN`、`ES_INDEX_PATTERN`、`FRONT_COMPONENT`、`STYLE_RULE`、`TEST_CASE`。

统一标识必须包括所需维度：**环境、服务/仓库、版本、资源实例/集群、数据库 schema 或 MQ vhost**，避免同名资源错误合并。稳定逻辑实体 ID 与版本快照 ID 分离。

### 5.3 关系

| 关系 | 例子 |
| --- | --- |
| `CALLS` / `IMPLEMENTS` | Java 方法调用、接口实现 |
| `CONSUMES_API` / `PROVIDES_API` | API Jar 使用、接口对外提供 |
| `DUBBO_SUBSCRIBES` / `RPC_CALLS` | 服务订阅与实际 RPC |
| `PUBLISHES` / `ROUTES_TO` / `CONSUMES` | RabbitMQ 生产、Binding、消费 |
| `READS` / `WRITES` | 读写 Redis Key 模式、数据表或字段 |
| `READS_CONDITION` | SQL WHERE/JOIN/HAVING 等条件字段依赖 |
| `USES_CONFIG` | 配置项影响某个功能 |
| `RENDERS` / `USES_STYLE` | 前端页面、组件、CSS |
| `COVERS` | 测试用例覆盖入口、方法、场景 |

**边要保存证据，不能只有源节点和目标节点。** 至少记录：`relation_type`、`snapshot_id`、`evidence_type`、`evidence_ref`、`first_seen`、`last_seen`、`verify_status`、`source_location`。

证据来源：`STATIC`、`RUNTIME`、`CONFIG`、`MANUAL`、`INFERRED`。同一边可以有多条证据。状态区分 `VERIFIED`、`CANDIDATE`、`UNKNOWN`；**潜在依赖不等于已证实的因果关系**。

### 5.4 推荐存储表（初版）

- `review_task`：评审任务、目标环境、状态。
- `review_task_mr`：各 MR 的仓库、base/head/merge SHA 和快照引用。
- `release_manifest`：多仓库版本基线。
- `dependency_snapshot`：仓库版本、扫描器版本、扫描结果哈希。
- `dependency_node`：节点与逻辑唯一标识。
- `dependency_edge`：有类型、有方向的关系。
- `dependency_evidence`：代码位置、SQL 摘要、trace 或配置快照。
- `change_item`：Git Diff/AST 提取的语义变更。
- `impact_finding`：影响路径、风险等级、证据与人工结论。
- `review_report`：报告版本与来源 commit。

## 6. 依赖采集与反向补全

### 6.1 程序内部

- 首次对纳管项目完整扫描，建立可重建的版本基线。
- 每次 MR 分析：**完整重新扫描发生修改的项目**，与历史基线比较；不只扫描 Git Diff 所在方法。
- 按需扫描未修改的下游项目，并定期全量核验依赖基线是否过期。
- 组合方法调用图、接口多实现、Spring 注入、MyBatis XML、配置引用、反射字符串/类名关键字检索。
- Java 分析引擎优先调研 jQAssistant；复杂数据流按需研究 CodeQL。两者不保证识别所有动态调用。

### 6.2 跨服务依赖

| 中间件 | 初期发现策略 | 边界与补充 |
| --- | --- | --- |
| Dubbo/ZK | ZK 注册的 providers/consumers，Dubbo Admin，代码引用，现有 Filter 运行记录 | 订阅关系不等于实际调用；需关联应用身份与接口方法 |
| RabbitMQ | Management API 的 connection/consumer/exchange/queue/binding，代码端 producer/listener 配置 | 仅靠客户端 IP 不可靠；需识别 vhost、路由键及历史生产者 |
| Redis | Manager/RedisTemplate/Jedis 调用点、Key 前缀与模式、操作类型 | Key 归一化需规则配置；`READS`/`WRITES` 同模式仅能表示潜在关系 |
| MySQL/Oracle | MyBatis/JDBC SQL AST，表/字段读写及条件字段 | adpts、spms 为跨程序库重点；其他库仍需验证例外和直接连接 |
| ES | adpts、Canal、日志工具、监控程序及索引查询/映射 | 区分日志索引写入、业务查询、监控规则和映射兼容 |
| XXL-JOB | 任务 Handler、配置和实际执行入口 | 不能只依赖 HTTP 请求链 |

现有 SPI/traceId 系统可逐步新增可选的资源访问事件；采集仅作依赖证据，不影响原业务。异步有界队列、敏感数据脱敏、只存 SQL 摘要与 Redis Key 模式，不记录支付敏感参数。运行时没有观测到某条边，**不能因此删除静态发现的边**。

### 6.3 如何发现未知 D

核心从“谁调用了修改的方法”扩展为“修改可能改变哪些可观察资源，谁依赖这些资源”。

例如：

```text
C.updateTransaction()
  └─ WRITES → trans_record.trans_state
                    └─ READS_CONDITION ← D.ReconciliationJob
                                                  ↑
                                             XXL-JOB 入口
```

即使 D 不在输入 MR 中，也要检查其消费条件是否兼容。对于代码未纳管、SQL 无法归属、MQ 历史消费者缺失等情况，形成 `UNKNOWN` 风险项，不直接推断安全。

## 7. 变更影响与遗漏检查算法

### 7.1 主流程

```text
锁定多 MR SHA
    ↓
提取 Git Diff → AST/SQL/配置语义改动
    ↓
识别直接修改点、可能变化的外部行为
    ↓
对比旧版图谱 ∪ 新版图谱（包含新增和删除边）
    ↓
按变更类型与关系规则进行反向传播
    ↓
定位业务入口、未修改项目、消费者和证据缺口
    ↓
按需提取关联代码上下文供 AI 评审
    ↓
输出确定违规 / 疑似遗漏 / 未验证影响 / 已验证兼容
```

**不能仅用固定的 BFS 层数、风险评分阈值切断高风险资金链路。** 传播停止须有明确的不可影响证明或通过的契约/行为验证；无法确认则保留不确定性。

### 7.2 变更类型与检查规则

| 变更类型 | 影响传播 | 典型遗漏检查 |
| --- | --- | --- |
| Java 方法逻辑 | 反向调用方、业务入口、资源副作用 | 异常、幂等、退款/通知/对账分支是否遗漏 |
| 对外 API 类 | API Jar 使用者、Dubbo/HTTP 消费者 | 是否升级 API 版本、消费者是否适配 |
| SQL/交易状态 | 表/字段读写、`READS_CONDITION`、定时任务 | 状态值改变后，旧查询条件是否漏数据 |
| RabbitMQ 消息 | producer → exchange → binding → queue → consumer | 消息字段/路由键修改后，消费者是否兼容 |
| Redis Key/Value | Key 模式、读写方、序列化格式 | Key 改名或结构变化是否影响旧读取方 |
| ES 索引或字段 | 索引查询、模板、监控规则、Canal | 映射或字段变化是否影响查询 |
| 前端 CSS/组件 | 样式规则 → 组件 → 页面 | 公共组件复用、布局遮挡和滚动问题 |

对“仅修改页面高度”的情形，优先定位匹配 CSS 规则的组件和页面，建议视觉回归；不应无差别扩展到支付后端。无法纯静态确认的布局行为交给浏览器测试。

### 7.3 发现结果分级

- **确定违规**：可由规则直接判定，如 API 修改未升级版本。
- **高可信疑似遗漏**：有明确消费者代码，例如新增状态值，但消费者仍只查询旧值。
- **需要确认**：共享数据或消息资源存在关联，但缺少因果/兼容证明。
- **未知依赖**：采集证据不完整、外部项目不可见或程序归属不明。

“未修改某个消费者项目”**不等于**“一定遗漏”；必须解释为什么需要适配或验证。

### 7.4 AI 的职责与防幻觉要求

AI 负责总结业务变化、发现可能的隐含约束、指出应进一步查询的代码，以及生成面向人工的评审解释。Git Diff、调用边、资源关系及文件位置必须来自真实扫描或检索证据。

采用按需上下文加载：先提取变更节点 → 查询关联项目与方法 → 只加载必要的调用方、消费者、SQL 和配置 → 形成结论。不得因为上下文窗口有限，就忽略未读取的受影响项目。

每条 AI finding 必须有：
`severity`、`category`、`claim`、`reason`、`evidence[]`、`affected_projects[]`、`suggested_review`、`uncertainty`。
无证据结论必须降级为“待核实线索”。

## 8. 评审报告规范

### 8.1 报告应回答的问题

1. 本次多个 MR 合起来究竟改变了哪些业务行为？
2. 哪些模块、接口、消息、数据库字段、Redis Key 和页面受到潜在影响？
3. 是否有**未包含在本次 MR** 的相关项目或消费者？
4. 是否存在同步修改遗漏、接口版本不一致、兼容性问题或未考虑的业务路径？
5. 人工需要优先看哪些代码、确认哪些业务场景？

### 8.2 报告结构

```text
联合 MR 影响评审报告
├─ 任务信息（MR URL、项目、base/head SHA、扫描覆盖情况）
├─ 本次主要业务改动摘要（按业务场景聚合）
├─ 影响范围（直接变更 / 上游调用 / 下游消费者 / 共享资源）
├─ 重点风险清单（严重度、证据、代码位置、建议）
├─ 可能遗漏的关联改动（包含未修改项目）
├─ 尚未确认的未知依赖与数据缺口
├─ 推荐人工重点评审点 / 建议回归场景
└─ 分析方法、已知局限、结果生成时间与版本
```

示例 finding：

> **高风险（待确认）**：`PaymentService.isSuccess()` 支持新状态 `SUCCESS`，但 `ReconciliationJob` 中 SQL 仍过滤 `trans_state='2'`。若新状态会持久化至数据库，对账任务可能遗漏交易。证据：修改处 GitLab SHA/文件/行号链接 + 消费处 GitLab SHA/文件/行号链接。建议确认落库规则并回归对账流程。

优先提供 5～20 条高价值评审事项，并保留可展开的完整证据和依赖路径。所有统计项（改动数、风险数）均基于真实扫描，禁止编造。

## 9. 安全、性能和可靠性

- GitLab 使用最小权限，初期只需要读取仓库与 MR 的授权；不要求写入/合并权限。
- 在内网拉取私有代码；严控凭据、源码缓存、日志和报告访问。外部 LLM 是否可读源码须单独配置与审批。
- 不执行来自未知 MR 的构建脚本、测试脚本或用户代码；需要执行时使用隔离的受限 Worker。
- 可恢复地处理 GitLab 不可用、API 限流、仓库权限缺失、Diff 截断、合并冲突、扫描失败和部分项目不可见。
- 输出每个项目的 **扫描成功/失败/未覆盖** 状态；部分失败不能显示“全量分析完成”。
- 按仓库 SHA 缓存完整静态图，重复 MR 可复用；扫描器规则升级时使旧缓存失效。
- 初期 MySQL 保存结构化关系，历史报告与证据按任务留存，避免把整个项目源码永久冗余入库。

## 10. 开源方案参考（技术参考，非强制依赖）

| 工具 | 用途 |
| --- | --- |
| [jQAssistant](https://github.com/jqassistant/jqassistant) | Java 静态结构及架构关系图，结合自定义扫描规则 |
| [CodeQL](https://github.com/github/codeql) | 高风险 Java/JS 数据流与语义查询 |
| [change-impact-analyzer](https://github.com/thepavansai/change-impact-analyzer) | Git Diff → 依赖影响路径与风险报告的原型参考 |
| [Apache SkyWalking](https://github.com/apache/skywalking) | 参考运行时依赖采集模型；实际优先复用现有 SPI |
| [DataHub](https://github.com/datahub-project/datahub) | 字段级数据血缘与影响查询的设计参考 |
| [Playwright](https://github.com/microsoft/playwright) | 后续前端视觉、交互回归验证 |

注意：普通字段血缘通常不足以覆盖 `WHERE/JOIN/HAVING` 等条件依赖，本项目必须单独建模 `READS_CONDITION`。

相关公开专利检索参考（仅作技术对照，不表示已完成法律评估）：

- [CN121880205A](https://patents.google.com/patent/CN121880205A/zh)：代码变更影响识别与自动化测试执行。
- [CN120578704A](https://patents.google.com/patent/CN120578704A/zh)：跨应用接口变更与数据血缘分析。
- [CN121882226A](https://patents.google.com/patent/CN121882226A/zh)：跨层级数据血缘及缺失关系补全。

如计划申请专利，应进一步比对权利要求，优先考虑异构中间件证据缺口、条件依赖传播和自动验证闭环等更具体的技术改进。

## 11. 开发路线和待办事项

### P0 — 多 MR 联合分析 MVP（优先）

- [ ] 建立基础工程：分析服务、Worker、MySQL、配置与日志。
- [ ] 实现 GitLab URL 解析、凭据配置、仓库权限检测与 MR 元数据获取。
- [ ] 支持多 MR 输入，固定 base/head SHA，构建分析工作区；处理合并冲突和 Diff 截断。
- [ ] Java 方法级 Git Diff/AST 变更检测、方法签名和代码链接。
- [ ] 实现修改项目完整静态扫描与版本化依赖基线。
- [ ] 实现 Java 调用关系反向查询、入口定位、旧新依赖边对比。
- [ ] 实现对外 API 类变更后的版本升级强制检查。
- [ ] 生成 Markdown/HTML 报告：改动摘要、影响链、遗漏候选、证据、未覆盖范围。
- [ ] 建立示例仓库与自动化验收集。

**P0 验收**：输入多个有效 MR，系统可基于固定 SHA 完整重建 Diff，并列出 Java 内部调用影响及重点人工评审点；报告中的文件、方法、行号可以打开验证。失败或未知范围被明确报告。

### P1 — 跨项目中间件和 AI 评审

- [ ] Dubbo/ZooKeeper provider/consumer 图谱，集成已有 SPI 运行证据。
- [ ] RabbitMQ producer / exchange / binding / queue / consumer 关系。
- [ ] MyBatis/JDBC SQL AST，表级及关键字段级、条件依赖。
- [ ] Redis Manager/Key 模式与读写关系。
- [ ] 根据关联依赖按需检索未修改项目的完整方法上下文。
- [ ] AI 生成证据化遗漏候选与业务变化摘要；支持人工确认/误报反馈。
- [ ] 支持同一需求多仓库版本清单及跨 MR 联动规则。

**P1 验收**：在 A → B → C 且 D 通过 MQ/DB/Redis 使用 C 产出资源的预设案例中，即使 D 未提交 MR，也应将 D 列为关联项目，并给出相应证据和不确定性。

### P2 — 前端、存量适配和精度提升

- [ ] API Jar 与接口契约兼容检查。
- [ ] Oracle、ES/Canal、XXL-JOB 依赖适配。
- [ ] 前端 JS/TS/Vue、组件与 CSS 引用关系。
- [ ] 字符串反射、动态 SQL、配置开关、共享组件等候选依赖分析。
- [ ] 生产/测试环境差异、历史依赖证据时效与定期基线核验。
- [ ] 风险项去重、优先级排序、误报分析及报告易读性优化。

### P3 — 扩展测试与流程集成

- [ ] 受影响入口与现有用例映射；结合 traceId/testRunId 补充覆盖证据。
- [ ] 回归场景推荐与新旧行为差异验证接口。
- [ ] Playwright 页面视觉、滚动及关键交互回归建议。
- [ ] 将报告摘要或行级证据评论回写 GitLab MR（明确授权后开启）。
- [ ] 可选 CI 门禁；默认仍由人工最终决策。

## 12. 质量指标与边界

需要持续衡量：

- **已知影响检出率**：人工植入跨服务依赖、历史故障场景，能否准确识别。
- **高风险误报率**：被人工否决的高优先级 findings 占比。
- **未修改项目检出率**：真实相关、但未参与本次 MR 的下游项目被发现的比例。
- **证据定位有效率**：报告链接是否对应实际代码与相符的结论。
- **扫描完整性**：纳管项目、依赖来源、配置覆盖率以及失败数量。
- **评审节省时间**：相同规模变更的人工评审时间对照。

系统只能证明**当前可见代码、配置和采集证据支持了什么结论**。若存在未知 D、未纳管代码或未执行的分支，不允许自动声明“无影响”。应输出未覆盖边界，并建议兼容措施或人工核验。

## 13. 第一版开发原则

1. **先可用，后智能**：先实现可靠 Git Diff、静态依赖、证据链接和确定性规则，再增加 AI。
2. **先多 MR 联合分析，后自动测试**：优先解决人工看不到的跨仓库遗漏。
3. **代码扫描结果作为事实，AI 作为解释与线索发现器**；不允许 AI 编造依赖。
4. **版本化、可复现**：每份报告绑定 Git commit 和扫描器版本，后续代码变化必须重新验证。
5. **保留旧新图谱并集**：被删除的调用、字段或配置关系同样可能构成风险。
6. **支持存量系统**：不强制替换现有 SPI、升级 Dubbo 或升级生产 ES。
7. **逐步补全依赖证据**：采集到新的依赖或人工确认遗漏后，更新基线，服务下一次评审。

---

**最终交付标准**：只需提交多个自建 GitLab MR 地址，就能得到一份面向资深开发人员的、可追溯的**“本次代码主要变化 + 跨项目影响路径 + 疑似遗漏改动 + 人工重点评审清单”**，而不是冗长的逐行代码解释。
