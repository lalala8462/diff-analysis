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

## 当前支持的确认场景

| 场景 | 可能问题与确认重点 |
| --- | --- |
| 同类多工单与文件演进 | 同一 Java 类的最终差异关联多个工单，按项目汇总并展开类、提交和范围内演进。核对是否相互覆盖；文件、行级归因及工单关系仍需复核。 |
| SDK 自身版本 | API/SDK 模块源码或 POM 改变，而两侧已知模块版本相同时，核对契约与发布安排。模块及版本无法识别时记录覆盖缺口。 |
| SDK 消费者版本 | 本次产出的 SDK 在 BASE/HEAD 之间版本改变时，对照消费者声明，核对是否需要配套升级。只检查本次 SDK，不检查第三方依赖；单版本不能判断升级过程。 |
| SDK 契约旧用法 | 比较字段、简单方法和枚举，查找固定版本消费者中的成员使用；仅 import 不触发重点。优先用 javac 归属到选定 SDK 源码，失败时保留类型作用域词法候选与缺口；归属不证明实际使用的 JAR 版本或运行兼容性。 |
| 查询结果列与排序参数契约 | 比较 MyBatis SELECT 输出字段，关联 Mapper、调用处及动态排序。删除/改名字段遇到参数可控的 PageHelper/orderBy，即使没有前端字面量也列为候选。结果字段缺失与 SQL ORDER BY 合法性分别确认；同一查询的多个字段在 HTML 合并为一项，二级展开，原始候选独立保留。 |
| 改动处的外部输入影响 | 判断上层传入参数是否影响当前改动的表达式或分支，按下表分级。HTTP、Dubbo、MQ、WebSocket 的直接或一跳显式调用/Mapper 入口作为补充证据，可附到 SDK、数据库、Redis 等候选。 |
| 跨项目数据库表 | 从变化 SQL、DDL、显式映射关联其他固定 SHA 的同名表访问。核对数据源、Schema 和实例；同名不证明访问同一库。 |
| Redis 与缓存值 | 通过字面量、简单常量、Key/前缀和值类型线索关联消费者，核对键规则、结构与历史缓存兼容性；动态名称保留缺口。 |
| 反射与 Bean 名称 | 查找固定版本中的类名、方法名、Bean 名称使用候选，核对旧名称是否仍被调用；名称匹配不证明实际绑定。 |

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
| query_contract_latest.json、external_influence_latest.json | 查询字段/排序契约、改动处输入影响和入口证据。 |
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

## 分析边界与开发验证

静态结果是待确认线索。缺失依赖、父 POM、动态 SQL、复杂重载、反射/代理、异步调用和扫描预算均可能造成误报或漏报；“未发现消费者”不证明没有影响。输入传播不是完整数据流分析，SDK 源码归属不证明 Maven 实际选中的 JAR。

实例证据仅来自固定 HEAD 的受支持配置，不验证配置中心、运行环境、动态路由或生产连接。程序不连接数据库或 Redis，不构造预计合并树，不分析所选范围以前的共同修改，不自动合并或发布。真实 GitLab、跨服务协议与业务兼容性仍需结合实际案例验收。

开发者可在完整 JDK 8 环境运行：

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q diff_analysis tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

更多资料：[Windows 操作指南](docs/WINDOWS_SETUP.md) · [管线与验收设计](docs/PIPELINE_V4.md)（设计目标不等于已实现能力） · [功能规格](specs/001-change-impact-review/spec.md) · [历史记录](CHANGELOG.md) · [四阶段评审记录](docs/FOUR_STAGE_REVIEW.md)。
