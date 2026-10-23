# Diff Analysis

**帮助审核者看清多个 Java 项目的版本变化、理解可能需求，并检查对既有功能和其他项目的影响。**

系统以各项目两个指定 Git 版本的最终净差异为底账，结合范围内的提交说明、逐行来源和可选重构证据整理候选需求，导出人工报告及供本地 AI 阅读的离线资料。

**当前版本不连接任何模型接口，不配置模型 API 或密钥，不自动上传源码。** AI 阅读和推理由用户在工具外完成；工具尚未提供完整的人工／AI 结论导入、修订和重审闭环。候选分组、结构映射和交叉线索都不代表需求或业务影响已经确认。

## 1. 当前能力与首期边界

| 能力 | 当前实现 | 限制与待补项 |
| --- | --- | --- |
| 输入与版本 | 本地 Git、未合并 GitLab MR、多项目向导；每项目独立固定旧、新 SHA | 向导仍拒绝同 SHA；额外消费者需用非交互 `--catalog`，统一角色／两侧版本待补 |
| 最终差异 | 文件／Hunk 底账、删除／改名事件、两侧前后 30 行代码、独立运行目录 | 主入口先扫描再生成新底账；JDK／扫描器整体失败的隔离仍待修正 |
| 提交来源 | 范围内提交 DAG、说明、文件候选及新变更行 git blame 候选 | blame 只追踪最后修改；删除行、大 Hunk、混杂提交不能据此确认真实需求 |
| 候选分组 | 人工固定 SHA 归属优先；否则按新变更行的唯一任务号，必要时回退文件级唯一任务号 | 无唯一任务号仍独立保留；完整人工拆合和细分评审单元待补 |
| 结构映射 | 可选 RefactoringMiner CLI、CodeTracker 标准化桥接导入、受限的多提交身份链 | 不是完整类型解析或任意 DAG 追踪，不证明行为等价 |
| 行为混杂提示 | Extract Method 的文本／条件／运算符／常量启发式检查 | `POSSIBLE_MIXED_BEHAVIOR` 是待核实线索，不等于纯度证明或缺陷 |
| 影响分析 | 共享文件／符号、两侧路径摘要、跨项目名称／参数数量候选及未变更消费者线索 | 完整关系边、旧版消费者检查和跨项目绑定仍待补；单路径摘要可能漏项 |
| 输出与存储 | `report.md`、`ai_review.md`、分组离线报告、JSON、代码证据；SQLite 基线及运行索引 | 关联方法／契约取证、逐类阅读缺口和对象级评审记录尚不完整 |

待分析主体是 Java；SQL、XML、配置及前端等配套文件的变化也进入底账，但登记不等于已完成语义分析。依赖判断以指定旧、新版本的代码为依据；比较范围内的提交用于解释来源，不以提交说明证明依赖。

完全回退的中间变化不生成最终需求。一组 SHA 表示本次评审组合，部署对应关系未知时不能认定为生产组合。更早历史的共同修改、预计合并结果和业务执行验证不属于首期范围。

需求规格见 [功能规格](specs/001-change-impact-review/spec.md)，补齐七项缺口的方案见 [v4.1 设计](docs/PIPELINE_V4.md)。

## 2. 总体流程与开发目标

当前按证据组织离线资料；下图中底账优先隔离、完整关系边、按需补证和独立评审记录仍需按计划补齐。

~~~mermaid
graph TD
    A["选择项目并固定旧版、新版 SHA"] --> B["保存最终差异与代码证据"]
    B --> C["提交来源、Java AST、可选结构映射"]
    C --> D["候选需求组及独立交叉检查"]
    D --> E["按需补关联方法、契约和消费者代码"]
    E --> F["导出人审报告与分组离线资料"]
    F --> G["用户在工具外交给本地 AI"]
    H["用户提供或人工确认的预期目标"] --> G
    F --> I["人工阅读与确认"]
    G --> J["后续：导入双方判断及阅读范围"]
    I --> J
    J --> K["后续：按目标、分组和证据修订重审"]
~~~

- 关系边是影响分析的基础；代表路径只用于展示，不能替代关系图。
- 需求归属与影响关系分开，两个需求需要联合检查时不自动合组。
- 前后 30 行是快速阅读窗口，完整影响判断还可能需要远处方法、类型、契约和未修改消费者代码。
- 代码归纳的候选目标与权威预期目标分开。没有后者时，“是否超出需求”保持未确定。
- 现有离线报告不包含模型结论。后续保存人工与外部 AI 判断时，应分别记录对象、理由、证据和修订。

## 3. Windows 安装与启动

需要 Git for Windows、Python 3.9+、完整 JDK 8+（含 `javac`）。可选 RefactoringMiner 3.x 需要单独具备 Java 17+ 运行环境，业务项目可以保持原有 Java 版本。

首次安装：

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

已有仓库更新前先处理自身未提交改动，再更新代码并执行测试：

~~~powershell
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

交互式多项目比较：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard
~~~

也可双击 [start-wizard.cmd](start-wizard.cmd)。按提示逐项添加本地 Git 项目或未合并 GitLab MR，输入各自的旧／新分支、Tag 或 SHA，输入 0 完成清单，再确认固定 SHA 后执行。`owner` 仅为可选责任人备注。GitLab 使用环境变量 `GITLAB_TOKEN`，安全输入方法见 [Windows 指南](docs/WINDOWS_SETUP.md)。

非交互本地比较可追加固定 SHA 消费者：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-local --repo "G:\Code\rhf-all\cps" --project "local/cps" --base "<完整旧CommitSHA>" --head "<完整新CommitSHA>" --catalog "G:\Code\consumer-service@<消费者完整CommitSHA>" --run-root ".diff-analysis/runs"
~~~

不需要额外消费者时省略 `--catalog`。多 MR 可使用 `review-mrs --allowed-host <GitLab主机> --mr <MR地址>`，重复 `--mr` 加入多个 MR。上述消费者入口还不等同于目标中的统一两侧版本输入。

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis --help
.\.venv\Scripts\python.exe -m diff_analysis wizard --help
.\.venv\Scripts\python.exe -m diff_analysis review-local --help
~~~

基线命令和旧入口见 [P0 使用说明](docs/P0_USAGE.md)。旧设计和用法文档不作为当前功能完整性声明。

## 4. 报告在哪里，怎样阅读？

默认父目录为 `.diff-analysis/runs`，可用 `--run-root` 修改。每次创建独立的 UTC 日期时间＋随机标识目录；例如 `20261023T050000Z` 对应北京时间 2026-10-23 13:00。

~~~text
.diff-analysis/runs/20261023T050000Z-a1b2c3d4/
  report.md                  人审摘要：项目、候选组、交叉项与缺口
  ai_review.md               全部分组与代码上下文，供本地 AI 离线阅读
  analysis.json              净差异、提交、归属及结构映射等结构化资料
  evidence/
    D-xxxxxxxxxxxxxxxx.md    单条差异、固定版本及两侧前后约 30 行
  group_reports/
    G-xxxxxxxxxxxxxxxx.md    单组提交、变化、代码和交叉线索
~~~

优先打开独立目录的 `report.md`，而不是原 `--output` / `--json-output` 生成的旧格式兼容输出。终端会打印离线 AI 报告 `ai_review.md` 的完整路径。

版本较小时可将 `ai_review.md` 作为资料交给本地 AI；版本较大时逐组提供 `group_reports/G-*.md`，再结合全局交叉项汇总。报告附带分析提示，要求引用 D-* 并区分源码事实、来源候选和业务推测。**工具无法自动确认外部 AI 实际读到了哪些文件，也不管理模型上下文预算或结果。**

当前代码证据主要保留变化窗口；关联方法、契约和未修改消费者代码不足时，不能仅凭名称完成业务影响判断。源码／历史有大小或数量限制，长代码行也可能被截短，后续需完善逐类遗漏及阅读范围清单；“按组导出”不等于全部必要上下文已提供。

## 5. 可选 Java 重构与人工归属

默认不需要安装下面的工具。增强失败保留基础净差异及限制说明；但主入口的整体扫描失败隔离仍待完善。

### RefactoringMiner：本地 CLI 适配

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard --refactoringminer-bin "D:\Tools\RefactoringMiner\bin\RefactoringMiner.bat"
~~~

程序调用 `-bc <repo> <baseSHA> <headSHA> -json <output>` 收集重构事件，并关联最终 D-*。记录 `evolution_relations`；对符合严格条件的线性多提交事件形成 `element_chains`。第一父链、元素身份或事件间稳定性不满足时降级，不声称支持所有分支合并历史。

Extract Method 检查是文本启发式：`TEXTUAL_EXTRACTION_CANDIDATE` 不证明纯重构；新增条件、运算符或常量可能产生 `POSSIBLE_MIXED_BEHAVIOR`，也可能需要人工排除误报。具体安装见 [RefactoringMiner 官方说明](https://github.com/tsantalis/RefactoringMiner/blob/master/documentation/how-to.md)。

Git 将跨类移动识别为文件重命名时，方法体可能没有行差异。两侧路径吻合同一条改名记录时，工具保留文件级演进候选 RENAMED_FILE_CANDIDATE，并以 mapping_basis 区分 Hunk 范围和文件关联；该候选不表示方法修改行已被精确定位。

### CodeTracker：标准化桥接导入

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard --codetracker-evidence "G:\Code\refactoring-evidence"
~~~

目前不直接调用 CodeTracker Java API。需由外部工具将映射转为项目自定义的 `codetracker-bridge-v1` JSON；这不是其原生输出。校验 project、完整 base_sha／head_sha，以及 mappings 中 old／new 的 Git 相对路径和起止行。可传单文件或目录；目录按项目名将 / 替换为 _ 查找，例如 `local_cps.json`。格式示例见 [设计中的现有增强接口](docs/PIPELINE_V4.md#当前可用增强接口)。

### 人工 Hunk 任务归属

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard --attribution-rules "G:\Code\attribution"
~~~

先从 `analysis.json` 查找 D-*，再用 `attribution-rules-v1` JSON 对固定项目、旧新 SHA 和 `diff_id` 提供 `issue_keys` 与明确 `reason`。目录同样按项目标识匹配文件；范围不匹配时拒绝规则并保留原始差异。

一个 Hunk 有多个任务时可提供不重叠的 `segments`，字段为 `new_start`、`new_count`、`issue_key`。它仍标记为 `MIXED_HUNK_REVIEW`，不自动拆成两个已确认需求。这项归属规则也不等同于完整的需求确认或人工／AI 评审记录。

## 6. 下一步重点

继续使用 Python、Java 分析器、SQLite 和本地命令行，优先完成首期评审流程：

| 优先级 | 工作重点 |
| --- | --- |
| P1-A | Git 底账先保存，项目／阶段失败隔离，部分报告可重建 |
| P1-B | 两侧完整关系边和未解析调用、统一消费者版本输入；修正多变更节点及旧版消费者漏检 |
| P1-C | 关联方法／契约／消费者取证，离线包完整度和逐类遗漏清单 |
| P2-A | 导入预期目标及外部 AI 结论；按分组、需求、影响、符合性独立记录和重审 |
| P2-B | 可解释归组建议、Hunk 内评审单元、人工拆合与受影响结果重算 |
| 持续评估 | 分组误合／误拆、消费者发现、无关候选、人工调整次数、审核用时及规模 |

已有 blame、结构映射和归属规则可继续复用，但精确提交归因不替代上述影响基础。SmartCommitCore 可借鉴关系分组；CodeTracker 的桥接不等于其完整追踪能力；Code Maat 的更早历史共同修改仍放在首期之后。

## 7. 文档和操作边界

- [功能规格](specs/001-change-impact-review/spec.md)：产品范围与验收目标；独立评审记录尚待实现。
- [分析管线 v4.1](docs/PIPELINE_V4.md)：保留七项缺口设计，并与当前离线输出及增强接口对齐。
- [Windows 指南](docs/WINDOWS_SETUP.md)：环境、GitLab 凭据和排错。
- v2／v3 保留历史设计，不代表当前实现；原模型客户端及其参数已从新版移除。

不执行待分析项目的构建或脚本，不修改其代码、分支和提交；可选 RefactoringMiner 只按显式路径运行分析工具。基线验证、源码登记、影响判断和需求确认分别表达，不自动合并、发布或晋升版本基线。

## 8. 最近一次验证

2026-10-23，在 Windows、Python 3.14 环境重新运行完整单元测试：**52 项全部通过**。包含此前 3 项失败的修复，以及新增的纯改名和错误路径匹配边界测试。

此前失败的原因和处理：

- test_same_body_is_only_textual_candidate：测试仍匹配旧文案；现在核对“不证明行为等价、纯度或调用点正确性”的限制含义。
- test_move_method_across_classes_maps_both_files：Git 改名中的方法体没有行差异，被原 Hunk 相交规则漏掉；增加双端路径校验后的文件级候选，保留精确范围匹配优先和原始底账。
- test_mixed_issue_commit_not_unconditionally_grouped：测试仍期待旧 UNASSIGNED 标签；现在检查 AMBIGUOUS_OR_UNASSIGNED、独立保留变化、无确定任务号且底账不丢失。

这些测试不包含真实 RefactoringMiner 可执行文件、公司 GitLab 集成或业务影响效果验收，也不代表第 6 节的设计缺口已全部补齐。
