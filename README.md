# Diff Analysis

面向**多 Java 项目的版本级变更影响审核**。以每个项目固定的 BASE/HEAD Git commit 的最终净差异为底账，结合提交来源、Java 调用关系、跨项目协议线索和人工审核记录，生成可追溯的离线报告。

**运行要求：Git、Python 3.9+、完整 JDK 8。** 支持 Windows PowerShell/CMD 与 Linux；CI 仅配置 Windows/Ubuntu + JDK 8。不调用模型接口，不执行被分析项目的构建、测试或业务脚本，不自动发布或合并。

> **main 已集成现有分析链路，真实业务集成待验收。** 真实 GitLab、Dubbo、MQ 集成仍待验证；“未发现消费者”不证明没有影响。

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

# 或分析一个本地项目的固定版本，替换路径与 SHA
.\.venv\Scripts\python.exe -m diff_analysis review-local --repo "D:\repos\spms-back" --project "rhf/spms-back" --base "<完整旧CommitSHA>" --head "<完整新CommitSHA>"
~~~

向导会在独立运行目录生成离线 `report.html`、Markdown、JSON 和 AI 证据包；HTML 支持重点场景、工单筛选及左右代码对照。

HTML 人工视图默认折叠可可靠识别的 Java 纯 import 和空白格式差异，并保留折叠列表与原始证据；字符串、注释、其他语言、混合变更或无法可靠判断的片段仍展示。import 变化可能影响类型绑定，折叠不代表已经证明无影响。

SDK 消费者检查可以通过 `--project-registry projects.json` 补充登记项目：

~~~json
{
  "projects": [
    {"project": "local/consumer", "repo": "D:/repos/consumer", "revision": "main"}
  ]
}
~~~

登记文件使用 UTF-8，相对仓库路径相对于登记文件所在目录；运行时将修订号固定为 SHA。本次输入的项目使用向导指定的旧版和新版，其他登记项目仅检查单一固定版本，不能判断是否升级。仅当本次变更项目产出的 API/SDK 在所选两个修订号之间版本发生变化时，对照登记项目的 Maven 引用；不检查第三方依赖版本，也不查询生产或最新发布版本。API/SDK 自动识别和 Maven 依赖只基于固定版本源码中的声明；间接依赖、外部父 POM、未登记项目及无法解析的值可能遗漏，报告会提示覆盖限制，不运行 Maven。

每次生成外部依赖检查时，若本机存在 `G:/Code/rhf-all/AGENTS.md`，工具从其中的模块表识别服务：本次比较项目仅使用指定 SHA；其余服务每次从远端实时确认默认分支并刷新到独立副本，记录分支与固定 SHA，供 Maven 引用检查。架构项目 Git 数据放在本项目 `project_code/<服务名>/`，MR 克隆放在 `project_code/_mr/` 的独立目录；只读取原仓库的已提交 Git 对象，不切换、重置或覆盖原工作区，副本不检出工作树。远端获取失败、缺失仓库或无法识别默认分支时，报告标明覆盖缺口，不使用过期缓存冒充最新结果。默认分支不是实际部署版本。`project_code/` 是 Git 缓存，不纳入本项目 Git，MR 独立目录不会自动删除。

需要将**未修改项目的固定版本纳入现有 Java 关系分析**时，在向导选择 EVIDENCE 角色，或为 review-local / review-mrs 追加以下参数：

~~~text
--evidence "rhf/consumer=D:\repos\consumer@<固定BASE-SHA>@<固定HEAD-SHA>"
~~~

EVIDENCE 两侧允许同一 SHA，不把证据项目的差异自动当作本次需求。登记项目和架构默认分支当前主要用于 Maven/SDK 引用检查，不等于对这些项目的数据库、Redis 和所有调用关系完成扫描。多个自建 GitLab MR、只读 Token、JAR classpath 清单等操作见 [Windows 操作指南](docs/WINDOWS_SETUP.md)。

## 当前会作为重点展示的场景

下表描述**现有 HTML 首页的实现**，不是已确认缺陷；各项展示固定版本、原因及可追溯证据。

| 场景 | 当前触发条件与限制 |
| --- | --- |
| 同一类涉及多个工单候选 | 同一 Java 类的最终差异关联多个工单；按项目合并，展开各类和差异。文件级和行级归因仍需人工核实。 |
| 同次比较中的文件演进 | 上述类可展示范围内的新增、修改和对应提交差异；文件新增后被另一工单修改时，区分中间演进与最终净差异。历史缺失、合并提交和不能确认先后关系要单独说明。 |
| SDK 自身版本 | 识别为 API/SDK 模块的源码或 POM 变化，且两侧模块版本未变化或无法解析时，提示核对版本。模块命名与静态识别存在覆盖限制。 |
| SDK 消费者版本 | 本次比较项目产出的 SDK 版本发生变化，消费者声明仍为旧版、与新版不一致或无法判断时，提示确认是否配套升级。只提供单一固定版本时无法判断升级过程。 |
| SDK 契约使用候选 | 比较直接 Maven SDK 的 Java 字段、简单方法和枚举声明，查找消费者固定版本中有类型作用域线索的实际成员表达式；按消费者项目归组。仅 import 不触发重点，静态候选不证明不兼容。 |
| 跨项目数据库表候选 | 本次变化 SQL/DDL/显式表映射关联其他固定 SHA 的表访问位置；同名不等于同实例，实际部署须人工确认。 |
| Redis、缓存 DTO 与反射候选 | 固定 SHA 下通过 Key/前缀、值类型线索及 Bean/反射名称关联消费者，均需人工核实绑定。 |
| 统一资源候选及修订 | 为资源关联建立独立 R-* 编号并挂接 ReviewUnit，拆组仅重算归属和问题指纹，原始事实保留。 |
| 覆盖缺口 | 扫描失败、源码或历史缺失、POM/父 POM 无法解析、版本或范围不完整等需查看阶段状态；未发现线索不表示没有影响。 |

程序还会生成同文件、同符号、Java 调用路径和潜在消费者等候选，供 `report.md`、`questions.md` 与 JSON 进一步核查；它们目前不一定被整理为 HTML 首页重点。`discover-connections`、`bind-connections` 和 `contract-context` 是需要单独执行的辅助步骤，不代表默认扫描已完成跨项目数据库或接口绑定。

## 阶段 0：固定 SHA 源文件索引（已实现基础能力）

运行 `discover-connections` 时，会按“项目 + 固定 Git SHA + 索引规则版本 + 文件类型范围 + 文件数量上限”读取或创建源文件元数据索引，正常运行缓存位于 `.diff-analysis/cache/source_snapshots/`，跨运行目录复用；独立测试目录的缓存则位于自身目录内。缓存损坏时重新扫描，文件路径不可安全处理或超出数量上限会保留 omissions；BASE/HEAD 相同 SHA 可共享同一索引，不读取未提交的工作区修改。

**边界：目前缓存的是 Java/XML/SQL/配置文件的路径和 Git blob SHA，并非已解析的 SDK 契约、数据库表、Redis Key 或反射调用使用关系。** 四类资源的结构化提取、消费者关联与增量重算仍按下文阶段继续实施。现有协议发现结果仍为候选，不推断运行时连接。

## 阶段 1：SDK 契约和未修改消费者（首期静态候选）

现支持直接 Maven 依赖声明匹配的 SDK（`api`/`*-api`/`sdk`/`*-sdk` 模块），根据最终 Git 净差异对比前后 Java 字段类型及序列化名称、简单方法签名和枚举常量。消费者从固定 SHA 的整个 Java 源文件清单查找显式类型 import、局部类型声明及成员表达式，**即使消费者无代码改动**也能产生候选。报告按 SDK 与消费者项目合并，展示生产方旧/新契约、两侧 SHA、消费者固定 SHA、代码行及声明的 Maven 版本；只有 import 不会生成重点项。保留 SDK 自身与消费者的版本检查。

**限制**：首期属于词法与类型作用域候选，不等于 JDK 8 javac 精确字段绑定；不自动证明不兼容。复杂泛型、同名重载、多层继承、跨行声明、动态代理、字段语义和间接 Maven 依赖可能无法解析，扫描预算及异常会形成缺口。消费者仅覆盖登记或显式输入的固定 SHA，不推定实际部署版本；生产环境与 Java 运行行为需人工验证。后续会结合既有 javac AST 增强解析精度。

## 阶段 2：跨项目数据库表索引（已实现首期静态候选）

HTML 首页和 `database_impact_latest.json` 已纳入表级影响候选：针对 CHANGE 项目固定 BASE/HEAD 的最终净差异，提取 MyBatis XML（select/insert/update/delete）、SQL 脚本 DDL/读写语句、Java 字面量 SQL 及显式 `@TableName` / `@Table(name=...)` 映射。只从**实际重叠本次变化行的表访问**出发，反向查找其他已登记或已输入项目在固定 SHA 的访问位置（即使消费者没有改动）。结果按变化项目、表名和消费者项目合并，保留双方 SHA、表读写角色、行号、D-* 差异 ID、数据源名称线索和缺口。

分析使用源 Git blob，不检出、不连接数据库，不执行被分析项目代码。按项目 SHA、表解析规则和预算缓存扫描结果；源文件超限、动态 SQL、文件解析失败等写入 omissions。**同名表仅表示 SOURCE_TABLE_TOKEN_ONLY 候选，绝不代表已经确认相同实例、Schema、路由或业务影响。** 同名异数据源 / 异 Schema 需人工确认。暂不支持字段血缘、运行态连接、多层 Java 字符串拼接和完整动态 SQL，真实 ADPTS/WebAPI 案例仍待验收。

## 阶段 3：Redis / 反射 / Spring Bean（首期实现）

`shared_resources.py` 在每个项目固定 Git SHA 下建立 Java/XML 名称和源码位置的只读索引，记录：Redis 操作（读取、写入、删除、TTL）、Spring `@Cacheable` / `@CachePut` / `@CacheEvict`、简单 key 常量与动态前缀、Bean 注解及 XML Bean ID、`Class.forName`、`getMethod` / `getDeclaredMethod` 和 `getBean`。当生产侧最终代码变化触及这些名称时，反查其他项目的固定版本调用。对缓存 DTO，只对可由变量声明推出写入值类型、且字段契约发生变化的场景提供候选；并非完整类型和序列化流分析。

索引基于项目、SHA 和规则版本缓存，输出 `shared_resource_latest.json`；动态名称、未知 key、扫描上限和失败都产生覆盖缺口。**同名 Redis Key、同名 Bean、反射方法名不能证明同一容器、实例、类或运行时绑定。** 动态 SpEL、跨方法常量传播、完整序列化契约、复杂反射路径与 Redis 实例/环境精确身份仍待增强。

## 阶段 4：统一资源报告与修订驱动重算（首期实现）

SDK、数据库表、Redis 和反射/Spring 关联统一规范为 `resource_impact_latest.json` 的固定版本资源候选：`R-*` 身份独立于人工分组，保留生产方/消费者项目、源码位置、双方 SHA、原始 `D-*`、名称匹配状态和缺口。HTML 继续显示各场景的重点证据，并新增“统一资源关联索引”；`shared_resource_latest.json` 和 `database_impact_latest.json` 分别提供原始场景结果。

`resource_review_latest.json` 将资源候选按当前 ReviewUnit 的 **主归属组** 挂接，产生 `RESOURCE_CONSUMER` 问题，进入现有 `review_package_latest.json` 的问题/证据计划及指纹。执行 `revise-groups` / `recompute-review` 时，仅从缓存的固定 SHA 资源事实重算受影响归属，未变组可复用；当人工拆分新行且原资源只在旧行出现，保留 `SOURCE_NOT_IN_EXACT_UNIT_RANGE` 缺口，不推定旧行位置。**底层资源事实、原始 D-* 底账和旧审核记录均不改写。**

目前是**精确归属约束与问题指纹**，不是跨全语法树的字段级数据流或运行时精确绑定。SDK / DB / Redis / 反射之间跨资源的实际依赖关系仍需人工核实；未命中不等于不存在调用者。所有分组与关联是待确认候选。

## 已提供的分析链路

| 环节 | 代码和产物 | 证据边界 |
| --- | --- | --- |
| 固定输入 | 按项目、角色、BASE/HEAD SHA 保存 Git 文件事件、最终净差异、来源候选，Git 底账优先落盘 | 提交任务号不等于需求；版本组合不等于部署组合 |
| Java 关系 | JDK 8 javac AST 与可归属方法标识；两侧调用边、未解析调用、多来源反向传播 | 缺依赖、动态代理、反射和不确定重载不能自动证明调用 |
| 跨项目依赖 | discover-connections 搜索 Dubbo/HTTP/MQ/数据库源码线索；bind-connections 校验两端固定版本、协议身份及人工部署声明，进入统一关系图 | USER_ATTESTED_MATCH 只表示人工声明和代码吻合，**不是运行时链路** |
| 问题及取证 | 生成 ReviewQuestion；contract-context 定位受影响消费者的分支、DTO/枚举、MyBatis/SQL 候选位置；读取完整方法或契约文件 | 标识符共现不是类型绑定或业务数据流证明 |
| 审核记录 | import-review 保存人工确认的 ExpectedGoal、规则点以及人工/外部 AI 独立判断；import-basis-check 单独复核依据 | 结构化引用合法不证明结论成立；AI 实际阅读范围未知 |
| 人工修订 | revise-groups 拆分、移动、合并 ReviewUnit；局部更新跨项目传播、问题和必要证据计划，保留修订历史 | 不从新版拆分行推算旧版行；不改写原始 D-* 底账 |

统一图中的方法和协议端点均附项目、侧别和 Git SHA，沿 caller → callee 的反方向追踪潜在消费者。端点锚定不唯一、协议身份或版本未知时保留候选及 omissions，不建立虚假的确定关系。

~~~text
review-local / review-mrs / wizard   选择项目与固定版本
rebuild-checkpoint                  由 Git 底账恢复基础报告
discover-connections                检索协议线索
bind-connections                    核验两端代码和部署声明
rebuild-unified                     更新版本统一图与影响传播
contract-context                    变化字段、方法及消费者分支候选
auto-evidence / semantic-evidence   按问题取完整方法或契约文件
supplement                          固定 SHA 的受限代码补证
revise-groups / recompute-review    归属修订与局部重算
import-review / import-basis-check  判断、目标与依据复核
review-status                       查看分歧和待重审结论
~~~

不内置 AI 客户端。用户可将离线报告交给本地 AI，再通过 import-review 导入其结构化判断。所有自动归组、提交归因和跨项目关系仍需审核者核实。

## 运行目录与阅读顺序

默认每次在项目的 .diff-analysis/runs/ 下创建独立的 UTC 时间＋随机标识目录；`--run-root` 只能指定该目录内的子目录。建议依次查看：

1. **report.html、stage_manifest.json、dependency_scope.json**：人工重点、比较及取证范围、阶段状态和覆盖缺口。
2. **questions.md / questions.json、group_reports/、evidence/D-*.md**：问题、固定版本差异及代码窗口。
3. **unified_graph_latest.json / unified_impact_latest.json**：版本关系边、反向影响与遗漏；导入协议绑定前仍保留 Java 静态图。
4. **contract_context_latest.json、supplements/**：执行相应命令后生成的完整方法、契约及字段、分支候选；读取受固定 Git SHA、轮次和字节预算约束。
5. **review_package_latest.json、review_state.json、revisions/**：当前问题/证据计划、人工归属、预期目标、判断与版本化快照。
6. **ai_review.md**：用于工具外 AI 的离线资料；导出成功**不表示 AI 已完整阅读**。

ledger_checkpoint.json 保存原始 Git 底账；analysis.json 保存版本和分析事实。人工修订只更新派生资料，**不覆盖**原净差异。向导和比较命令只在本次独立运行目录生成报告与证据，不再生成旧版兼容报告。

## 后续重点场景与代码改动方案

**阶段 1 至 4 的首期静态候选与资源修订链路已实现；精确类型/实例绑定、复杂表达式及真实业务验收仍为后续工作。** 目标是在静态扫描时记录跨项目使用关系，比较时从本次真正变化的契约或资源反向查找消费者，并将同一变化原因按消费者项目合并，支持逐项展开证据。依赖版本、部署环境、数据库或 Redis 实例无法确认时保留待确认候选，不推定缺陷或无影响。

### 1. SDK 契约变化与旧用法

- 已调整 [Maven/SDK 检查](diff_analysis/maven_inventory.py) 和 [HTML 重点项](diff_analysis/human_report.py)：仅因变更类 import SDK 不再生成重点项；保留 SDK 自身及消费者版本检查。
- 比较固定版本内对外 API/SDK 的方法参数、返回类型、DTO 字段与序列化名称、枚举值/映射、约束或注释等契约变化；字段含义改变需结合前后代码和业务证据，不能仅凭名字或注释宣称语义变化。
- 查找消费者对**实际变化的类型、方法、字段、枚举或条件**的使用，包括本次没有修改的消费者；重点展示旧调用、旧参数、旧字段或旧枚举处理仍存在且可能与新契约不一致的候选。消费者类有其他无关改动，不能因此视作已完成配套修改。
- 区分“确有静态使用证据”“仅名称/类型线索”“版本或绑定未知”；列出生产方变化、消费者使用位置、两侧 SHA、依赖版本和待确认问题。证据不足时不输出确定不兼容结论。

### 2. 反射和 Spring Bean 名称调用

- 已在独立固定 SHA 资源索引中记录 `Class.forName`、方法名查找、Spring Bean 按名获取及 Bean 注解/XML 名称，支持部分简单字面量/常量。后续仍需结合 [Java 扫描器](diff_analysis/scanner.py) 的 javac 精确绑定与复杂表达式。
- 比较时将类名、方法名、签名或 Bean 名称的变化与记录的调用点匹配；展示调用点、可能目标、固定版本及推断依据。对动态拼接、配置外置、多候选或无法解析的名称记录覆盖缺口，不能按相同字符串直接认定调用关系。

### 3. 跨项目数据库表

- 已建立 MyBatis XML、简单 Java SQL/注解、DDL 和显式表映射的固定 SHA 表级使用索引；复杂动态 SQL 及数据源身份解析仍待扩展。首期以**表**为关联单位，不要求字段级血缘。
- 已从实际变化行重叠的 DDL、Mapper/SQL 和显式 PO 表映射线索发现可能涉及的表，再列出其他项目相应固定版本的访问位置；ADPTS 与 WebAPI 的 `trans_record` 真实案例仍待核验。无显式表映射、动态 SQL 或表名不能确定时保留缺口。
- 同名表但无法确认同一个数据源/实例时，仍以**待确认候选**列出；数据源、库名、路由和环境证据应随结果展示，不能把表名相同当作已确认共库。只读源码与配置，不直接访问或修改数据库。

### 4. Redis 缓存结构与使用方

- 已静态记录部分 Redis key/前缀、简单常量和读写/删除/过期位置，能根据显式值变量类型与 DTO 字段差异生成候选；完整序列化结构及 SPMS、WebAPI、CPS、对账的实际业务覆盖仍待验证。
- 本次缓存 key、结构或读写约定变化时，列出其他项目的候选使用方、固定 SHA 和相关代码；同一个变化按消费者项目合并，并保留不同 key 和使用点。
- Redis 实例、库、环境或动态 key 无法确认时仍列为**待确认候选**；名称相似不证明共享同一缓存，也不能据此断言序列化兼容或运行时连接。

### 5. 扫描、报告与验收顺序

1. **建立版本索引**：已具备固定 SHA 源文件元数据索引及缓存；后续仍需扩展现有扫描与关系图，为上述契约、反射、表及缓存资源保存项目、固定 SHA、位置、解析方式和缺口；按项目 SHA 与扫描规则版本复用结果，避免每次重复全量扫描。比较项目保留 BASE/HEAD 两侧，默认分支取证项目仅有单侧 SHA，不能推断其未修改或实际部署版本。
2. **识别变化并关联消费者**：已从最终净差异出发，为 SDK、数据库表、缓存、Bean/反射名称生成弱候选与未解析缺口；后续完善确定的 javac 字段绑定、实例身份和更精准的跨资源图谱，不跨 BASE/HEAD 混接为确定调用链。
3. **收敛首页重点项**：已建立按“变化源／资源 + 消费者项目”的统一 R-* 资源索引与 HTML 汇总，保留既有场景明细；进一步去重和折叠冗余卡片仍待优化。数据库/Redis/Bean 名称未绑定时只显示待确认候选。
4. **验收**：以构造仓库覆盖 SDK 字段/枚举/参数变化但消费者沿用旧逻辑、常量与动态反射、Bean 名称变化、同表不同数据源、Redis key/DTO 变化和无关 import。再以固定 SHA 验证 ADPTS/WebAPI 表访问及 SPMS 缓存案例，记录漏项、误报、解析缺口和人工核查结果；静态线索不等于真实部署或运行验证。

预计改动位置：在 `diff_analysis/java/JavaAstScanner.java` 和 `diff_analysis/scanner.py` 扩展源码提取，在 `diff_analysis/protocol_discovery.py` 与关系图模块补充资源和调用线索，在 `diff_analysis/maven_inventory.py` 关联 SDK 契约及消费者，并在 `diff_analysis/human_report.py` 调整重点项归组和证据展示；相应补充 `tests/` 中的构造仓库测试。具体复用范围以实施时的代码结构为准。

## 四阶段代码评审（2026-10-27）

已检查四阶段的缓存、契约、SQL、Redis/反射、资源归属及报告链路，并对主要风险做定点修复。详见 [四阶段代码评审与遗留项](docs/FOUR_STAGE_REVIEW.md)。

- **阶段 0**：源索引缓存校验补充实际 `file_count` 一致性与截断声明；符合扩展名的符号链接未扫描时增加显式遗漏记录。索引仍是源码 Git 对象事实，不是完整业务关系。
- **阶段 1**：SDK 方法/字段/枚举的证据 ID 按**具体旧/新声明行与 D-* 区间**筛选，防止单个成员错误继承整个 SDK 的全部差异；移除无用的 SDK 全 Java 目录扫描；泛型参数分隔支持 `Map<String, Integer>`；注释和字符串中的伪用法不产生消费者证据。
- **阶段 2**：已复核 `DELETE FROM` 等基本读写分类；表级关联继续采用变化行重叠与同名候选，**同名不是同实例**，跨 Schema/数据源证据不足时保持待确认。复杂动态 SQL 和字段级关系未实现。
- **阶段 3**：读取字符串/字符常量前先区分注释，Redis Key 中 `http://` 等文本不会因 `//` 被截断；Java 日志或字符串中的伪 Redis/Bean/反射调用被排除。仍不证明序列化格式、Bean 作用域或 Redis 连接实例相同。
- **阶段 4**：统一资源 `R-*` 按固定版本与资源身份去重合并 D-* 及消费者位置，避免人工修订索引因重复 ID 覆盖证据。人工拆组仍只更新派生问题/归属，保留旧侧未知行和原始差异。

**验证边界**：已对部分隔离解析场景做本地检查；新增回归用例和整个仓库的 Windows/Ubuntu + JDK 8 自动测试还未完成可靠执行。GitHub Actions 出现 Job 在零执行步骤即失败的情况，需先排查 Runner、账户/配额及工作流配置；不应推断为测试代码失败或通过。当前功能为**首期静态候选**，不能标注“生产可用、精确绑定、无遗漏”。

## 四阶段代码评审与修复记录（2026-10-27）

本次检查固定 SHA 索引、SDK 契约提取、数据库 SQL/MyBatis、Redis/反射候选、HTML 汇总以及 ReviewUnit 修订流水线。已修复数据库 `DELETE FROM` 可能错标为 READ 的解析顺序，并补充对应回归用例；SQL 词法扫描同时排除注释和字符串字面量中的伪表引用。数据库解析规则版本升级，避免复用旧缓存结果。

**必须区分已实现与已验收：**本轮仅确认静态实现路径及回归用例已提交，没有在此处执行完整 Python unittest 与 JDK 8 Java AST 集成测试。GitHub Actions 的之前几次运行在执行步骤前失败，并不能证明代码正常或异常。

**仍有风险**：Java SQL 字符串拼接、MyBatis 动态 SQL、SDK 重载及跨项目实际 Maven 依赖、Redis 操作对象的值类型推断、反射名称拼接和容器绑定，都存在静态误报/漏报。生产部署的数据库实例和缓存实例未经验证；同名仅可作为候选。对实际 ADPTS/WebAPI 表访问、SPMS/WebAPI Redis 使用、消费者固定 SHA 的端到端验收仍须以真实仓库运行结果判定。

## 四阶段审查补充：扫描结果完整性

审查发现并修复 Spring Cache 没有显式 Key 时被错误标记为精确键的风险。现在这类注解只输出 Key 命名空间前缀，并记录 `CACHE_KEY_GENERATED_UNRESOLVED`；增加对应回归用例。数据库与共享资源候选达到单关联证据上限时，会显式记录 `TABLE_EVIDENCE_LOCATIONS_TRUNCATED` / `RESOURCE_EVIDENCE_LOCATIONS_TRUNCATED`，防止用户把截断的使用位置误认为完整清单。

这些修复不提升静态规则到运行时精确绑定。真实项目测试、完整 unittest/JDK 8 CI 仍未成功验证，不能声称四阶段已经通过全面验收。

## 验证与限制

- 当前支持 JDK 8 的静态分析；反射、动态代理、外部父 POM、真实数据源/缓存实例和跨服务部署关系仍可能无法解析。报告和索引均须披露未分析范围。
- 真实 GitLab、Dubbo、MQ、数据库、Redis 关系及大规模扫描效果仍需按实际案例验收；不根据候选数量宣称准确率或提效比例。
- 首期不构造预计合并树，不分析更早历史共同修改，不运行待分析项目构建或业务脚本，也不自动合并或发布。

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q diff_analysis tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

当前以 **JDK 8** 为主要验证目标，不以 JDK 17/21 作为主工具运行要求。

## 文档导航

- [Windows 操作指南](docs/WINDOWS_SETUP.md)：安装、GitLab、JSON 请求和命令示例。
- [当前管线与验收设计](docs/PIPELINE_V4.md)：阶段契约、覆盖预算、人工修订、后续目标；设计目标不能当作已完成能力。
- [功能规格](specs/001-change-impact-review/spec.md)：行为约束与验收口径。
- [CHANGELOG](CHANGELOG.md)：历史开发记录和决策，README 不再按日期堆叠状态。
