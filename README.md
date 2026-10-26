# Diff Analysis

面向**多 Java 项目的版本级变更影响审核**。以每个项目固定的 BASE/HEAD Git commit 的最终净差异为底账，结合提交来源、Java 调用关系、跨项目协议线索和人工审核记录，生成可追溯的离线报告。

**运行要求：Git、Python 3.9+、完整 JDK 8。** 支持 Windows PowerShell/CMD 与 Linux；CI 仅配置 Windows/Ubuntu + JDK 8。不调用模型接口，不执行被分析项目的构建、测试或业务脚本，不自动发布或合并。

> **开发分支待集中验证。** 已提交代码不等于已通过测试或真实 GitLab、Dubbo、MQ 集成验收；“未发现消费者”不证明没有影响。

## 快速开始

Windows PowerShell 示例：

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
git switch feat/p1a-ledger-first-checkpoint

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
java -version
javac -version

# 交互输入多个本地项目或 GitLab MR
.\.venv\Scripts\python.exe -m diff_analysis wizard

# 或分析一个本地项目的固定版本，替换路径与 SHA
.\.venv\Scripts\python.exe -m diff_analysis review-local --repo "D:\repos\spms-back" --project "rhf/spms-back" --base "<完整旧CommitSHA>" --head "<完整新CommitSHA>"
~~~

需要检查**未修改消费者**时，在向导选择 EVIDENCE 角色，或为 review-local / review-mrs 追加以下参数：

~~~text
--evidence "rhf/consumer=D:\repos\consumer@<固定BASE-SHA>@<固定HEAD-SHA>"
~~~

EVIDENCE 两侧允许同一 SHA，不把证据项目的差异自动当作本次需求。多个自建 GitLab MR、只读 Token、JAR classpath 清单等操作见 [Windows 操作指南](docs/WINDOWS_SETUP.md)。

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

默认每次在 .diff-analysis/runs/ 下创建独立的 UTC 时间＋随机标识目录，可通过 --run-root 修改。建议依次查看：

1. **report.md、stage_manifest.json**：范围、候选组、失败阶段和覆盖缺口。
2. **questions.md / questions.json、group_reports/、evidence/D-*.md**：问题、固定版本差异及代码窗口。
3. **unified_graph_latest.json / unified_impact_latest.json**：版本关系边、反向影响与遗漏；导入协议绑定前仍保留 Java 静态图。
4. **contract_context_latest.json、supplements/**：完整方法、契约及字段、分支候选，读取受固定 Git SHA、轮次和字节预算约束。
5. **review_package_latest.json、review_state.json、revisions/**：当前问题/证据计划、人工归属、预期目标、判断与版本化快照。
6. **ai_review.md**：用于工具外 AI 的离线资料；导出成功**不表示 AI 已完整阅读**。

ledger_checkpoint.json 保存原始 Git 底账；analysis.json 保存版本和分析事实。人工修订只更新派生资料，**不覆盖**原净差异。旧版 --output/--json-output 兼容文件不代表完整运行目录。

## 尚需完成与验证

- **真实部署绑定**：Dubbo 2.6.x 注册/消费版本、HTTP 目标路由、MQ 交换机/路由、MyBatis 与数据源、字段级血缘需要实际项目证据；现有源码关联无法证明 Spring 动态代理和运行时行为。
- **工程测试**：当前开发分支新增代码没有成功完成 Python 单测和 JDK 8 编译验证；GitHub Actions 此前出现任务 0 步骤失败，不能据此认定代码通过或失败。
- **恢复和质量**：阶段重试、重构工具兼容、规模／预算截断及影响漏检、分组误报率需要预标注样本验收，不能用问题数量替代准确度。
- **首期之外**：Web、长期需求资产、历史共同修改、预测合并树与真实业务执行对比不是当前既有功能。

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q diff_analysis tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

当前只按 **JDK 8** 验证，不以 JDK 17/21 作为主工具运行要求；集中验收之后再决定是否合并开发 PR。

## 文档导航

- [Windows 操作指南](docs/WINDOWS_SETUP.md)：安装、GitLab、JSON 请求和命令示例。
- [当前管线与验收设计](docs/PIPELINE_V4.md)：阶段契约、覆盖预算、人工修订、后续目标；设计目标不能当作已完成能力。
- [功能规格](specs/001-change-impact-review/spec.md)：行为约束与验收口径。
- [CHANGELOG](CHANGELOG.md)：历史开发记录和决策，README 不再按日期堆叠状态。
