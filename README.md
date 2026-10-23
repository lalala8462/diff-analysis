# Diff Analysis

**多项目版本变更分析工具**：以最终 Git 差异为底账，利用提交历史解释修改来源，生成需求候选组与组间交叉检查项，输出可以直接交给本地 AI 的离线报告。

> 当前实现：Python CLI + Java AST + SQLite 基线 + 离线 Markdown/JSON 报告 + 可选重构映射。  
> **程序不会连接、调用或上传代码到任何 AI 模型。** AI 的使用与推理结果由用户在工具外自行管理。需求候选分组、代码关联和提交归因尚未经过人工确认。

## 1. Windows 安装

需要 Git for Windows、Python 3.9+、完整 JDK 8+（含 `javac`）。业务项目可保持原有 Java 版本。

~~~powershell
cd G:\Code\diff-analysis
git pull origin main

.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

首次安装参考 [Windows 环境说明](docs/WINDOWS_SETUP.md)。

## 2. 逐项输入本地 Git 项目与版本

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard
~~~

也可双击 `start-wizard.cmd`。向导逐项提示：

1. 添加本地 Git 项目或未合并 GitLab MR；
2. 输入项目标识、旧版与新版 Git Tag/分支/SHA；
3. 可选输入 `owner`（仅为责任团队备注，不参与认证或分组）；
4. 重复添加项目，输入 `0` 结束；
5. 查看固定 SHA 清单，确认执行比较。

例如：

~~~text
添加项目：1=本地 Git，2=GitLab MR，0=完成输入: 1
本地 Git 项目路径: G:\Code\rhf-all\cps
项目标识: local/cps
旧版 Git 修订号: 345c6142e285618b6a64f1a356bb5da8ebb3ff94
新版 Git 修订号: 657514bc68df3c0704a6cf9c96946de62320dda8
旧版本标签: 1.140.0
新版本标签: 1.141.0
项目责任人: [回车跳过]

添加项目：1=本地 Git，2=GitLab MR，0=完成输入: 1
本地 Git 项目路径: G:\Code\rhf-all\webapi
项目标识: local/webapi
...输入各自的旧新 Git 版本...
添加项目：1=本地 Git，2=GitLab MR，0=完成输入: 0
确认开始扫描并生成报告？y/N: y
~~~

每个项目可使用不同的 Git 修订号，实际分析前转换为固定 SHA。

不想使用向导时仍可调用 `review-local` / `review-mrs`：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-local `
  --repo "G:\Code\rhf-all\cps" `
  --base "<完整旧 Commit SHA>" `
  --head "<完整新 Commit SHA>"
~~~

## 3. 报告生成在哪里？

每次运行在 `.diff-analysis/runs/<UTC时间>-<随机号>/` 中独立保存：

~~~text
.diff-analysis/runs/20261023T050000Z-1234abcd/
  report.md                  # 人工阅读：项目、差异候选组、跨组检查
  ai_review.md               # 可直接交给本地 AI 的完整上下文报告
  analysis.json              # 所有净差异、Git 历史、候选组和关系数据
  evidence/
    D-xxxxxxxxxxxxxxxx.md    # 单条差异：旧新版各前后 30 行
  group_reports/
    G-xxxxxxxxxxxxxxxx.md    # 单组离线 AI 输入：提交、差异、跨组、代码
~~~

### 可选：提高 Java 重命名、移动与提取方法识别准确率

默认运行只使用 Git、Java AST 和基于变更行的 `git blame` 候选归因，不需要额外安装。对于复杂 Java 重构，可指定本机已安装的 RefactoringMiner 可执行文件：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard `
  --refactoringminer-bin "D:\Tools\RefactoringMiner\bin\RefactoringMiner.bat"
~~~

RefactoringMiner 3.x 运行需要 Java 17+，与当前 Java 8 项目的编译运行环境可分开配置。程序使用 `-bc <repo> <baseSHA> <headSHA> -json <output>` 获取提交间的重构证据；未安装、超时或执行失败时**保留完整净差异报告**，并标注 `PARTIAL`。重构检测不等于行为等价证明。具体安装请参考 [RefactoringMiner 官方说明](https://github.com/tsantalis/RefactoringMiner/blob/master/documentation/how-to.md)。

CodeTracker 是 Java 代码元素历史追踪库，目前**未直接嵌入其 Java API**。如已使用 CodeTracker 提取了旧新元素对应关系，可转换为项目自定义的 `codetracker-bridge-v1` JSON 再导入：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard `
  --codetracker-evidence "G:\Code\refactoring-evidence"
~~~

文件夹中每个项目使用 `local_cps.json`、`local_webapi.json` 等名称（将项目标识中的 `/` 转为 `_`），也可分析单项目时直接传 JSON 文件。必须与项目及固定旧新 SHA 一致；不匹配只记录警告，不污染版本基线。

~~~json
{
  "schema_version": "codetracker-bridge-v1",
  "project": "local/cps",
  "base_sha": "<完整旧 SHA>",
  "head_sha": "<完整新 SHA>",
  "mappings": [
    {
      "kind": "Move Method",
      "old": {"path": "src/main/java/A.java", "start": 12, "end": 22},
      "new": {"path": "src/main/java/B.java", "start": 35, "end": 46}
    }
  ]
}
~~~

重构证据写入 `analysis.json` 的 `evolution_relations`，并显示在分组报告里。多提交重命名/移动事件额外生成 `element_chains`，只有第一父提交线性、元素签名能连续匹配、间隔提交没有改动相关文件时，才标记 `CONTIGUOUS_CANDIDATE`；不满足条件时明确记录 `BOUNDARY_UNVERIFIED` 或其他证据缺口。身份链不代表业务行为等价。

**Extract Method 混杂行为判断**：从每次提取发生时的父提交和当前提交读取 Java 方法代码，检查新提取的方法体是否文本上来自原方法；若提取方法出现原方法没有的条件、运算符或常量，标记 `POSSIBLE_MIXED_BEHAVIOR`，否则仅标记文本候选或证据不足。不会自动认定纯重构，也不替代 RefactoringMiner 的 Purity Checker。

**同一 Commit 包含多个需求**：`git blame` 只能定位最后修改提交，无法从没有任务线索的混杂提交中自动推断真实需求。可使用经过人工核实的 `--attribution-rules`，按**固定项目、旧新 SHA 和最终 D-* 差异 ID**补充任务归属。例如在上次 `analysis.json` 中找到待分组的差异 ID，写入：

~~~json
{
  "schema_version": "attribution-rules-v1",
  "project": "local/cps",
  "base_sha": "<旧版本完整 SHA>",
  "head_sha": "<新版本完整 SHA>",
  "decisions": [
    {
      "diff_id": "D-xxxxxxxxxxxxxxxx",
      "issue_keys": ["PAY-123"],
      "reason": "已核实对应支付改动"
    }
  ]
}
~~~

再执行：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard `
  --attribution-rules "G:\Code\attribution\local_cps.json"
~~~

多项目建议传一个目录，规则文件名使用 `local_cps.json`、`local_webapi.json`。如果一个 **Hunk 内部**确实混入两个需求，可列出多个 `issue_keys` 和不重叠的 `segments`（新版本的起始行和行数），但该 Hunk 仍保留单独的 `MIXED_HUNK_REVIEW`，不假装能够自动拆成两条确定需求。规则 SHA、差异 ID 或行范围不匹配时拒绝归属，保留原始 Diff。

**需求分组改进：** 在最终差异 Hunk 的新版本行使用 `git blame` 取得最后修改提交，并关联其任务编号；只有存在唯一任务编号时才允许候选合组。当同文件由多个任务提交修改时，能够区分部分混杂变化。遇到删除行、多任务提交、追踪不明时仍保守处理，不因发现重构而自动合并两个需求。

### 如何交给本地 AI

首先打开 `report.md` 了解变更范围和候选组数目。然后：

- **版本较小**：将 `ai_review.md` 直接粘贴或作为文件交给本地 AI，要求按组分析需求目的、旧新行为及业务交叉影响。
- **版本较大**：将 `group_reports/G-*.md` 分组输入本地 AI，避免超出上下文窗口；最后结合 `report.md` 和 `analysis.json` 汇总组间交叉问题。
- 需要核对来源时，可查看 `evidence/D-*.md` 和 `analysis.json`。每个最终变化有固定 `D-*` ID，不依赖模型生成。

报告自带分析提示，要求模型引用 D-* 证据，并区分源码事实、Git 提交候选来源和业务推断。**报告不包含模型生成的结论，模型也不会由本项目自动运行。**

## 4. 当前实现能做到什么？

| 处理环节 | 能力与边界 |
| --- | --- |
| 最终差异 | 从旧 SHA → 新 SHA 计算 Git 净变化，保存文件/Hunk 记录；不支持的类型保留待核验状态 |
| Git 演进 | 收集提交拓扑、文件级候选和变更行 blame 任务号；不能证明业务需求身份 |
| 分组 | 优先按变更行的唯一任务号，退化到文件历史单一任务号；不能据此自动认定需求 |
| 交叉检查 | 共享文件/符号、版本内调用、重构演进证据及未修改消费者 |
| 离线报告 | 汇总和分组 Markdown、旧新代码证据、完整 JSON；**不调用 AI** |
| 版本基线 | SQLite 保存人工纳管项目清单和固定 SHA 快照；真实 MR 合并晋升仍待开发 |

**目前不能证明所有业务依赖已被识别。** 特别是 Spring/Dubbo 动态代理、MyBatis 动态 SQL、数据库/MQ/Redis 等跨程序关系需要更多证据。没有交叉结果不意味着没有影响。

## 5. 后续计划

- 已增加严格边界的多提交身份链、Extract Method 行为混杂启发式检查以及按 SHA/Diff ID 的人工任务归属。下一步继续支持更复杂的非线性 Git 历史、重构纯度与同 Hunk 精细拆分。
- 丰富分组依据，在不强制合组的前提下借鉴 SmartCommitCore；后续增加 Code Maat 历史共同修改线索。
- 提高跨服务资源关系解析，建设 A 主链 / 共享资源 / B 主链的证据展示。
- 后续根据需要增加 Windows 本地 Web 界面；保持分析与模型推理彻底解耦。

[离线分析管线设计](docs/PIPELINE_V4.md) · [Windows 配置](docs/WINDOWS_SETUP.md) · [原有 CLI 用法](docs/P0_USAGE.md)

旧设计仅作历史参考，首页不再重复追加过时的模型客户端说明。
