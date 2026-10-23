# Diff Analysis

**帮助审核者看清多个 Java 项目的版本变化、理解可能需求，并检查对既有功能和其他项目的影响。**

系统以每个项目两个指定 Git 版本的最终净差异为底账，结合比较范围内的提交说明整理候选需求，再用两侧版本的代码证据分析关联影响。人工确认和 AI 复核应分别留存，明确分歧与未完成范围。

目前已有命令行初版。**本文将现有能力与下一步设计分开说明；出现候选或未发现影响，都不代表业务已验证安全。**

## 1. 设计目标与首期范围

| 项目 | 约定 |
| --- | --- |
| 待分析对象 | Java 项目；SQL、XML、配置等配套文件变化也进入底账，未支持的语义分析明确披露 |
| 比较语义 | 各项目独立指定旧版和新版，比较最终净差异；完全回退的中间变化不生成最终需求 |
| 提交说明 | 仅使用比较范围内的记录解释来源和归纳需求，不以提交说明证明代码依赖 |
| 依赖证据 | 旧版、新版分别取证，包含纳入范围但没有修改的消费者 |
| 评审目标 | 区分代码归纳的候选目标与用户提供或人工确认的预期目标和约束 |
| 结论 | 分组确认、需求理解、影响判断、需求符合性分别评审；人工与 AI 独立记录 |
| 首期之外 | 更早历史的共同修改、预计合并结果、自动发布门禁及业务执行验证 |

一组项目 SHA 表示本次评审组合。没有部署清单等依据时，不能将它表述为实际生产部署组合。项目集合由用户维护，未纳入项目属于分析边界。

需求范围见 [功能规格](specs/001-change-impact-review/spec.md)；详细方案见 [分析管线 v4.1](docs/PIPELINE_V4.md)。

## 2. 总体方案

下图为目标流程；底账优先、完整关系边、关联代码取证和独立评审闭环仍需按开发计划补齐。

~~~mermaid
graph TD
    A["输入变化项目和关联证据项目"] --> B["固定两侧 SHA 与评审范围"]
    B --> C["先保存 Git 最终差异底账"]
    C --> D["分阶段增强：AST、提交来源、两侧关系图"]
    D --> E["候选需求组与可解释的调整建议"]
    E --> F["从关系图检查组间影响及未变更消费者"]
    F --> G["按需获取相关方法、契约和两端代码"]
    G --> H["按组准备有预算和缺口清单的证据包"]
    H --> I["可选 AI 推理和复核"]
    H --> J["人审报告及原始代码证据"]
    I --> J
    K["用户提供或人工确认的预期目标"] --> I
    K --> L["需求、分组、影响和符合性评审"]
    J --> L
    L --> M["人工与 AI 独立记录，修订后按范围重审"]
~~~

核心原则：

- **关系边是影响分析的基础。** 展示路径是图查询的摘要，不能替代完整关系边。
- **需求归属与影响关系分开。** 两组有调用或资源关联，只说明可能需要联合检查。
- **阅读片段与分析证据分开。** 前后各 30 行用于快速阅读，业务判断还可能需要完整相关方法、契约及未修改消费者代码。
- **证据登记与影响分析分别计状态。** 增强步骤失败时，应保留已完成底账和具体缺口。
- **真实需求与代码推测分开。** 没有明确预期目标时，“是否超出需求”保持未确定。

## 3. 当前实现与已知差距

以下为代码层面的能力盘点，不是本轮实测结论；待修正项不能仅通过标注“候选”补偿。

| 能力 | 当前初版 | 下一步需要补齐 |
| --- | --- | --- |
| 输入与版本 | 本地 Git、GitLab MR、多项目向导、固定 SHA；非交互命令支持额外消费者仓库 | 统一项目角色和两侧版本输入；向导当前拒绝同 SHA 项目 |
| 最终差异 | 已有净差异记录、文件事件、前后 30 行代码及独立运行目录 | 主入口仍先扫描再产出新底账；JDK 等整体失败可能阻止底账报告 |
| 关系与影响 | 已有旧、新调用路径摘要和跨项目名称／参数数量候选 | 保存两侧关系边和两侧未解析调用；修正两个变更节点之间连接可能漏检的问题 |
| 候选分组 | 唯一明确任务号时尝试合组，其余保守保留 | 可解释的归组建议、人工拆合调整、Hunk 内细分评审单元 |
| 模型输入 | 已有按组输入及显式启用的模型适配 | 关联方法和消费者代码取证；整个输入统一预算、逐类披露遗漏 |
| 模型结果 | 已有引用 ID 检查及假设状态 | 引用存在不代表推理成立；需要证据充分性、需求目标及独立复核 |
| 评审记录 | SQLite 已有基线，运行索引另存摘要 | 尚无完整的分组／需求／影响／符合性判断、修订和重审结构 |
| 效果评估 | 已有部分回退、重命名、分组及降级测试 | 补影响漏检、噪声、分组质量、规模和真实审核用时案例 |

## 4. Windows 安装与启动

需要 Git for Windows、Python 3.9+、完整 JDK 8+（含 `javac`）。可使用 JDK 17 运行分析器，待分析业务项目仍可为 Java 8。

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

交互式多项目比较：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard
~~~

也可双击仓库根目录的 [start-wizard.cmd](start-wizard.cmd)。每个项目可输入不同的旧／新分支、Tag 或 SHA，执行前解析为固定 SHA。GitLab 模式使用未合并 MR 地址及环境变量 `GITLAB_TOKEN`；Token 配置方式见 [Windows 指南](docs/WINDOWS_SETUP.md)。`owner` 只是可选责任人备注。

当前向导不支持旧、新 SHA 相同的证据项目。以下非交互入口可追加固定 SHA 的消费者仓库，但它还不能替代设计中的统一两侧消费者输入：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-local --repo "G:\Code\spms-back" --base "<完整旧CommitSHA>" --head "<完整新CommitSHA>" --catalog "G:\Code\consumer-service@<消费者完整CommitSHA>" --run-root ".diff-analysis/runs"
~~~

本地单项目固定 SHA 比较也可不传 `--catalog`。多 MR 可使用 `review-mrs --allowed-host <GitLab主机> --mr <MR地址>`，重复 `--mr` 加入多个 MR。完整参数：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis --help
.\.venv\Scripts\python.exe -m diff_analysis review-local --help
~~~

基线和旧命令见 [P0 使用说明](docs/P0_USAGE.md)；这些文档描述已有入口，当前设计边界以功能规格及 v4.1 方案为准。

## 5. 当前输出与报告改进方向

每次运行创建 `.diff-analysis/runs/<UTC日期时间>-<随机标识>/` 独立目录，可用 `--run-root` 修改父目录。目录名使用 UTC；例如 `20261023T030000Z` 对应北京时间 2026-10-23 11:00。

~~~text
.diff-analysis/runs/20261023T030000Z-a1b2c3d4/
  report.md                         候选组、交叉项及缺口
  analysis.json                     结构化差异、来源和分析结果
  evidence/
    D-xxxxxxxxxxxxxxxx.md           两侧版本及前后 30 行代码
  model_inputs/
    G-xxxxxxxxxxxxxxxx.json         当前组的模型输入
    G-xxxxxxxxxxxxxxxx.result.json  实际调用模型后的结果
~~~

原 `--output` / `--json-output` 仍输出旧格式报告，兼容已有脚本；**优先阅读独立目录中的 `report.md`**。目前重点代码主要在配套 `evidence` 文件中，后续应将关键片段直接嵌入评审项，并保留完整证据入口。

目标阅读顺序为：可能需求 → 预期目标与约束（如有）→ 新旧行为 → 潜在影响 → 评审问题及两端代码 → 人工／AI 判断与缺口。业务说明不堆实际代码路径；路径、SHA 和技术关系用于证据定位。

计划增加的关系边、证据清单、评审记录和阶段状态尚未全部成为现有输出字段，具体见设计文档。

## 6. 可选模型分析

默认只生成模型输入，不调用模型。显式指定接口和模型后，才启用按组分析；无模型时仍可在当前扫描成功的前提下生成静态报告。

本地兼容服务示例：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard --model-endpoint "http://127.0.0.1:1234/v1/chat/completions" --model-name "your-local-model"
~~~

远程 HTTPS 模型需额外指定 `--allow-remote-model`；密钥默认从 `DIFF_ANALYSIS_MODEL_API_KEY` 读取，可用 `--model-key-env` 修改变量名。接口适配面向兼容的 Chat Completions JSON 输出，具体供应商兼容性仍需验证。

当前单组约 24,000 字符预算主要约束变化片段，默认最多调用 20 组（`--max-model-groups`）。**提交说明、关联项和消费者的截断披露尚不完整，不能据此认为模型已读完所有关联证据。** 下一步按最终实际发送内容计总预算，并逐类列出未提供对象；引用 ID 检查仅证明引用在输入内。

## 7. 开发顺序与参考工具

采用“先补齐首期评审流程”的方向，继续使用 Python、Java 分析器、SQLite 和本地命令行。

| 优先级 | 交付重点 |
| --- | --- |
| P1-A | 底账优先保存，项目／阶段失败隔离，可重建的部分报告 |
| P1-B | 两侧关系边、统一消费者版本输入，修正直接变更节点与旧版消费者漏检 |
| P1-C | 关联方法／契约／消费者取证，完整输入预算与逐类缺口 |
| P2-A | 预期目标、分组及独立评审记录，按修订范围重新核对 |
| P2-B | 可解释归组建议、Hunk 内细分、人工拆合及效果评估 |
| 后续 | 根据案例按需增强类型绑定、重构映射；Web、历史统计和行为执行另行规划 |

[SmartCommitCore](https://github.com/Symbolk/SmartCommitCore) 参考分组和关系机制；[RefactoringMiner](https://github.com/tsantalis/RefactoringMiner) 评估两侧结构映射；[CodeTracker](https://github.com/jodavimehran/code-tracker) 仅在本次范围内对来源歧义按需追踪；[Code Maat](https://github.com/adamtornhill/code-maat) 放在更早历史分析的后续扩展。四个项目目前均未直接接入，精确提交归因不再作为下一张最高优先级任务卡。

验收同时记录分组误合／误拆、已知消费者发现、无关候选、人工调整次数、审核用时及规模，不预先声称准确率或提效比例。方案、数据模型、七项缺口对应验收及样本规模见 [v4.1 详细设计](docs/PIPELINE_V4.md)。

## 8. 文档与操作边界

- [功能规格](specs/001-change-impact-review/spec.md)：首期产品范围与成功标准。
- [分析管线 v4.1](docs/PIPELINE_V4.md)：现状差距、流程、关系图、取证、评审反馈及实施顺序。
- [Windows 指南](docs/WINDOWS_SETUP.md)：安装、GitLab 凭据和环境问题。
- v2／v3 文档保留历史设计，不作为当前功能完成声明。

分析不执行待分析项目的构建或脚本，不修改其代码、分支和提交。基线验证状态、源码覆盖、影响识别和人工需求确认各自独立；不自动合并、发布或将候选版本晋升为已验证基线。
