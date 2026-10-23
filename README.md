# Diff Analysis

**多项目版本变更分析工具**：以最终 Git 差异为底账，利用提交历史解释修改来源，生成需求候选组与组间交叉检查项，输出可以直接交给本地 AI 的离线报告。

> 当前实现：Python CLI + Java AST + SQLite 基线 + 离线 Markdown/JSON 报告。  
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
| Git 演进 | 收集提交拓扑、提交说明和文件级提交候选；不保证精确逐 Hunk 归因 |
| 分组 | 有唯一明确任务号时尝试合成一个候选需求组；其他变化独立保留 |
| 交叉检查 | 共享文件/符号、旧或新版静态调用路径以及可能受影响的未修改消费者 |
| 离线报告 | 汇总和分组 Markdown、旧新代码证据、完整 JSON；**不调用 AI** |
| 版本基线 | SQLite 保存人工纳管项目清单和固定 SHA 快照；真实 MR 合并晋升仍待开发 |

**目前不能证明所有业务依赖已被识别。** 特别是 Spring/Dubbo 动态代理、MyBatis 动态 SQL、数据库/MQ/Redis 等跨程序关系需要更多证据。没有交叉结果不意味着没有影响。

## 5. 后续计划

- 提高混杂提交的 Hunk→提交归因与 Java 重构识别准确度，再评估 CodeTracker/RefactoringMiner 的可选适配。
- 丰富分组依据，在不强制合组的前提下借鉴 SmartCommitCore；后续增加 Code Maat 历史共同修改线索。
- 提高跨服务资源关系解析，建设 A 主链 / 共享资源 / B 主链的证据展示。
- 后续根据需要增加 Windows 本地 Web 界面；保持分析与模型推理彻底解耦。

[离线分析管线设计](docs/PIPELINE_V4.md) · [Windows 配置](docs/WINDOWS_SETUP.md) · [原有 CLI 用法](docs/P0_USAGE.md)

旧设计仅作历史参考，首页不再重复追加过时的模型客户端说明。
