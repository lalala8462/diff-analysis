# Diff Analysis

面向多 Java 项目的版本差异审核。以固定 BASE/HEAD Git SHA 的最终净差异为底账，结合提交归因、Java 调用关系、跨项目资源线索和人工审核，生成离线 HTML 报告。

**运行要求：Git、Python 3.9+、完整 JDK 8（含 java、javac）。** 不调用模型 API，不执行被分析项目的构建、测试或业务脚本。

## 快速开始

Windows PowerShell 示例：

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
java -version
javac -version

# 交互输入多个本地项目或 GitLab MR
.\.venv\Scripts\python.exe -m diff_analysis wizard

# 或直接指定本地项目与两个完整 SHA
.\.venv\Scripts\python.exe -m diff_analysis review-local --repo "D:\repos\recon" --project "reconciliation" --base "<旧CommitSHA>" --head "<新CommitSHA>"

# 查看命令及参数
.\.venv\Scripts\python.exe -m diff_analysis --help
~~~

打开运行目录中的 **report.html**，查看优先确认项、触发条件、可能问题和代码对照。支持场景仅在本 README 说明；通用解析缺口收在 HTML 页尾“比较范围与覆盖”，只在妨碍具体判断时附到对应确认项。

安装、GitLab Token、多 MR、JAR classpath 清单和 JSON 请求见 [Windows 操作指南](docs/WINDOWS_SETUP.md)。Linux 使用等效命令，Java 分析以 JDK 8 为主要验证目标。

## 比较与取证范围

- **比较项目**：使用输入的 BASE/HEAD SHA，最终净差异是原始底账；提交中的工单号仅作为归因线索。
- **EVIDENCE 项目**：将未修改项目的固定版本纳入 Java 关系分析。在向导选择 EVIDENCE，或追加 `--evidence "rhf/consumer=D:\repos\consumer@<BASE-SHA>@<HEAD-SHA>"`。两侧可为同一 SHA，证据项目不自动计为本次变更需求。
- **登记项目**：通过 `--project-registry projects.json` 补充 Maven/SDK、数据库表、Redis/反射等扫描范围。单一固定版本不能证明消费者发生过升级。
- **架构项目**：若存在 `G:/Code/rhf-all/AGENTS.md`，从模块表识别其他服务；每次检查从远端确认并刷新其默认分支，再固定 SHA。默认分支不代表实际部署版本；远端失败、仓库缺失或默认分支不明会记录覆盖缺口。

登记文件示例（UTF-8，相对仓库路径以该文件目录为基准，修订号在运行时固定为 SHA）：

~~~json
{
  "projects": [
    {"project": "local/consumer", "repo": "D:/repos/consumer", "revision": "main"}
  ]
}
~~~

登记项目和架构项目参与受支持的依赖、资源扫描，不代表已完成跨项目 Java 调用绑定。架构 Git 副本保存在 `project_code/<服务名>/`，MR 副本在 `project_code/_mr/` 的独立目录；不会切换或重置原工作区。副本不检出工作树、不自动清理，远端失败时不以过期缓存冒充最新结果。

## 通用场景分析能力

分析以“变化事实 → 依赖关系 → 使用方假设 → 触发条件 → 待确认影响”为主线。具体框架语法用于提取事实，不根据业务方法名、状态名猜测正确含义。

| 通用能力 | 核心分析问题 | 落地范围与状态 |
| --- | --- | --- |
| 数据与控制依赖追踪 | 哪些输入或条件能够影响当前改动 | 已支持：方法参数、简单赋值和包围条件；HTTP、Dubbo、MQ、WebSocket 直接或一跳入口作为补充证据。 |
| 生产者与消费者契约对照 | 提供方改变后，使用方是否仍依赖旧约定 | 已支持：SDK 字段、简单方法及枚举成员使用，查询输出和部分缓存结构关联。SDK 优先 javac 归属，失败保留词法候选；仅 import 不触发重点。 |
| 调用影响反向传播 | 哪些未修改的调用方可能受影响 | 已支持：分别建立 BASE/HEAD Java 调用关系；跨项目名称匹配是弱线索，协议绑定需要固定版本及补充证据。 |
| 共享资源间接关联 | 哪些项目通过共同资源形成依赖 | 已支持：数据库表、Redis 键/前缀、缓存值类型、反射类/方法及 Bean 名称。资源身份与实例仍需核对。 |
| 输入能力与允许值匹配 | 调用方是否有能力传入不再支持的值 | 已支持：MyBatis 查询输出与 PageHelper/orderBy 动态排序，即使没有前端旧字段字面量也能产生候选；尚非任意参数取值范围分析。 |
| 配套演进一致性检查 | 相关部分是否完成必要的同步调整 | 已支持：本次 SDK 源码/POM 变化但版本未变、SDK 版本变化而消费者声明未配套升级；单版本无法证明升级过程。 |
| 多改动汇合与联合审核 | 多项变化是否作用于同一符号、链路或消费者 | 已支持：同类多工单、文件演进、同符号与调用关系汇合；不自动推导组合后的业务结果。 |
| 前置条件与校验覆盖 | 新要求是否在相关路径上得到保证 | 首版支持：非空、数组下标、整数除数非零要求，方法内校验及直接调用方实参；复杂表达式、循环和未知调用保留缺口。 |
| 返回结果与副作用假设 | 返回或异常改变后，调用方是否仍正确处理 | 首版支持：新增可空返回被使用、已确定的失败结果未处理后继续执行、显式写库/缓存/消息操作。自定义结果语义来自源码与声明式映射，不凭名称判断。 |
| 读写闭环与历史兼容 | 新旧写入形态与读取逻辑是否匹配 | 首版支持：Redis 显式键及字段值流、简单 SQL 显式字段/取值，当前、旧版可能写入与滚动升级组合。序列化、动态 SQL 与真实历史数据仍需核实。 |

SDK 只核对本次产出的依赖，不检查任意第三方升级；源码归属不证明实际 JAR。查询输出缺失与 SQL ORDER BY 合法性分别确认，同一查询的多个字段合并展示、原始候选保留。数据库同名表不证明同一数据源/Schema，Redis 前缀和反射名称也不证明实际绑定。

所有关系均带固定版本；身份消歧、配置端点及 profile 只是证据，不能代替实际部署确认。行为路径证据强弱、输入影响程度和缺陷严重性是不同概念。覆盖不足记为未知，不能把“未发现”当成安全结论。三类行为分析的语义映射、预算及具体边界见 [行为分析设计](docs/BEHAVIOR_ANALYSIS.md)。

资源候选保留独立 `R-*` 编号及原始 `D-*` 差异引用；HTML 按变化项目与消费者项目汇总 SDK 契约、数据库、Redis、反射资源，明细可展开。同文件、同符号、调用路径等其他线索保留在 Markdown/JSON 中，不一定成为首页优先项。

### 外部输入影响等级

以**当前改动点**为边界，而非服务边界：

| 等级 | 判断依据 |
| --- | --- |
| 高（HIGH） | 请求或上层调用传入的参数影响改动表达式或包围分支，包括可识别的简单局部变量传播、方法参数动态排序。 |
| 中（MEDIUM） | 参数来源或传播不明、复杂表达式无法解析，或只有外部入口线索而未绑定到改动点。 |
| 低（LOW） | 仅受字面量或可确定的内部值影响；不会仅因入口邻近而提升等级。 |

等级表示受外部输入影响的程度，不等于缺陷严重性或缺少校验。内部固定值也可能引发兼容问题。查询候选的 `evidence_level` 另表示静态证据强弱，与输入影响等级分开。

## 报告与证据

默认每次生成到本项目 `.diff-analysis/runs/<UTC时间-随机标识>/`；`--run-root` 只能指向该根目录或其子目录。

| 产物 | 用途 |
| --- | --- |
| **report.html** | 优先确认项、分组明细、代码对照；比较范围与覆盖放在页尾。纯 import/空白差异经可靠识别后默认折叠，原始证据保留，折叠不表示已确认无影响。 |
| report.md、questions.md/json、group_reports/、evidence/ | 详细问题、工单归组、固定版本差异和源码窗口。 |
| stage_manifest.json、dependency_scope.json | 阶段状态、比较及取证范围、失败与覆盖缺口。 |
| coverage_latest.json | Git、Java、关系图、Maven/SDK、数据库、查询契约、输入影响、共享资源、实例证据和行为分析的统一覆盖状态；报告生成单独记录。 |
| query_contract_latest.json、external_influence_latest.json | 查询字段/排序契约、改动处输入影响和入口证据。 |
| behavior_impact_latest.json、behavior_review_latest.json | `B-*` 行为候选、触发路径、固定版本及缺口；按审核单元重新归属的问题与指纹。 |
| database_impact_latest.json、shared_resource_latest.json、resource_impact_latest.json | 数据库、共享资源及统一资源候选。 |
| instance_scope_latest.json | HEAD 配置中的数据源/Redis profile 与端点摘要。配置匹配不证明实际部署同实例，配置不同也不证明环境隔离；不导出凭据或完整连接 URL。 |
| ledger_checkpoint.json、analysis.json | 原始 Git 底账、版本与分析事实；人工修订只更新派生资料。 |
| unified_*、contract_context_latest.json、supplements/、review_*、revisions/ | 关系图、补证和人工修订资料；部分需执行辅助命令后生成。 |
| ai_review.md | 可交给工具外 AI 的离线资料；导出成功不表示已被完整阅读。 |

`.diff-analysis/` 与 `project_code/` 均不纳入 Git。固定 SHA 源索引缓存只复用受扫描规则、文件范围和预算约束的结果，不代表完整业务关系。

## 辅助命令

以下步骤按需执行；自动报告不代表已完成协议绑定或人工审核。参数见 `<命令> --help` 和 [操作指南](docs/WINDOWS_SETUP.md)。

| 命令 | 用途 |
| --- | --- |
| catalog-add / catalog-list | 管理本地登记的项目范围。 |
| baseline-local / baseline-mr / baseline-status / baseline-verify | 建立固定版本基线、查看状态并人工确认源码与扫描范围；确认不代表业务依赖完整。 |
| rebuild-checkpoint | 从已有 Git 底账恢复基础报告。 |
| discover-connections / bind-connections / rebuild-unified | 搜索协议线索、校验两端固定版本与人工部署声明、更新统一关系图。人工声明与代码吻合仍不等于运行时链路。 |
| contract-context / auto-evidence / semantic-evidence / supplement | 为问题补充固定 SHA 的方法、契约和消费者上下文。 |
| revise-groups / recompute-review | 拆分、移动或合并审核单元并重算派生资料，保留原始差异。 |
| import-review / import-basis-check / review-status | 导入人工或外部 AI 判断、复核依据、查看分歧和待重审项。 |

`review-status` 刷新当前运行已保存产物的覆盖与判断适用性；`recompute-review` 在尚未人工分组时也可刷新。两者不自动拉取仓库、修改固定 SHA 或重新扫描源码。原始 `D-*` 底账、证据文件和人工判断日志保留，HTML 为生成时快照，最新状态以 `coverage_latest.json`、`review_status.json` 为准。

覆盖状态使用 `COMPLETE / PARTIAL / FAILED / NOT_EXECUTED / UNKNOWN`：完整执行且没有适用对象可为 `COMPLETE`；有缺口或截断为 `PARTIAL`；明确执行失败为 `FAILED`；明确跳过为 `NOT_EXECUTED`；产物尚未生成或不能确认执行状态为 `UNKNOWN`。必需阶段不完整且 Git 底账仍可用时，批次为 `PARTIAL`，`analysis.json.status` 为 `PARTIAL_EVIDENCE`；底账整体不可用为失败。未启用的可选增强工具不计入必需阶段，HTML 生成成功不改变分析覆盖。

普通及语义补证使用固定项目、SHA、侧别、位置和内容哈希绑定问题，相关 QUESTION 以及所属 GROUP、DIFF 的 IMPACT/CONFORMANCE 判断转为 `NEEDS_REVIEW`。无关组保持原适用性；重复取证、相同内容重算和状态查询不使判断反复失效。已失效历史判断持续保留，重新审核会追加绑定当前依赖指纹的新记录。

当前版本作为初版，仅接受当前数据格式。方法事实须包含源码范围、当前 AST 和扫描规则及分侧诊断；审核判断须绑定依赖指纹；Maven/SDK 缺口直接使用结构化对象。格式不符时拒绝读取或将对应阶段标为失败，不转换旧数据、不重建缺失的问题文件，也不提供旧字段和旧函数接口。

## 分析边界与开发验证

静态结果是待确认线索。缺失依赖、父 POM、动态 SQL、复杂重载、反射/代理、异步调用和扫描预算均可能造成误报或漏报；“未发现消费者”不证明没有影响。输入传播不是完整数据流分析，SDK 源码归属不证明 Maven 实际选中的 JAR。

行为分析以方法内结构化路径和直接调用一跳为界，每方法最多保留 256 个抽象状态；未知调用撤销字段保证。仅处理可识别的显式异常，不模拟任意库异常、事务回滚、重试或并发。旧版可能写入的字段和值来自固定源码，不证明历史数据实际存在；未知序列化不推断为已确认格式破坏。

实例证据仅来自固定 HEAD 的受支持配置，不验证配置中心、运行环境、动态路由或生产连接。程序不连接数据库或 Redis，不构造预计合并树，不分析所选范围以前的共同修改，不自动合并或发布。真实 GitLab、跨服务协议与业务兼容性仍需结合实际案例验收。

开发者可在完整 JDK 8 环境运行：

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q diff_analysis tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

更多资料：[Windows 操作指南](docs/WINDOWS_SETUP.md) · [管线与验收设计](docs/PIPELINE_V4.md)（设计目标不等于已实现能力） · [功能规格](specs/001-change-impact-review/spec.md) · [历史记录](CHANGELOG.md) · [四阶段评审记录](docs/FOUR_STAGE_REVIEW.md)。
