# Diff Analysis

**Windows 上的多项目 Git 版本差异与关联影响分析工具。** 支持分步输入多个项目、各自旧/新 Git 修订号，一次生成 Markdown + JSON 评审报告，并用 SQLite 保存固定 SHA 的旧版扫描基线。

> 当前状态：**交互式命令行 + P0 Java AST 静态影响分析 + P1-B SQLite 基线**。尚无本地 Web UI、完整数据库/MQ/Redis 跨服务依赖图、MR 最终合并自动晋升或 V1/V2 业务执行器。跨项目调用只输出待确认候选，不能当作业务影响已得到证明。

## 1. 安装与启动（Windows CMD / PowerShell）

依赖：Git for Windows、Python 3.9+、完整 JDK 8+（必须有 `javac`）。首次安装：

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

**推荐交互式运行：**

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard
~~~

也可以在 Windows 资源管理器中**双击 [start-wizard.cmd](start-wizard.cmd)**；脚本会打开 CMD 控制台调用相同的交互程序，执行完不会立即关闭窗口。已有 CMD/PowerShell 窗口可直接输入 `start-wizard.cmd`。

### 交互示例：不同项目使用不同修订号

~~~text
=== diff-analysis 多项目版本比较向导 ===
添加项目：1=本地 Git，2=GitLab MR，0=完成输入: 1
本地 Git 项目路径: G:\Code\spms-back
项目标识（例如 rhf/spms-back）: rhf/spms-back
旧版 Git 修订号: V1.0
新版 Git 修订号: V1.1
旧版本标签: [回车沿用 V1.0]
新版本标签: [回车沿用 V1.1]
项目责任人/团队备注（可选，无权限含义）: [回车跳过]

添加项目：1=本地 Git，2=GitLab MR，0=完成输入: 1
本地 Git 项目路径: G:\Code\refund-service
项目标识: rhf/refund-service
旧版 Git 修订号: release/2.5
新版 Git 修订号: release/2.6
旧版本标签: V2.5
新版本标签: V2.6
项目责任人/团队备注: [回车跳过]

添加项目：1=本地 Git，2=GitLab MR，0=完成输入: 0
--- 比较清单 ---
1. rhf/spms-back: V1.0 (<旧版 SHA>) → V1.1 (<新版 SHA>)
2. rhf/refund-service: V2.5 (<旧版 SHA>) → V2.6 (<新版 SHA>)
确认开始扫描并生成报告？y/N: y
~~~

每个项目的**旧/新修订号可独立输入**：支持 Git 分支名、Tag、完整 Commit SHA、`HEAD~1` 等 Git 修订表达式。程序执行前会把它们分别解析为不可变的完整 SHA；旧/新 SHA 相同会提示重新输入。未完成输入或选择否时不会创建项目目录或报告。

扫描后生成：
- `report.md`：各项目版本、修改文件与方法、静态调用链、跨项目候选和未知范围。
- `review.json`：含每项目旧/新完整 SHA、版本标签、代码证据和候选关系。
- `.diff-analysis/state.sqlite3`：人工项目清单、旧版本固定 SHA 基线和扫描缺口。

默认将旧版本登记为 `PROVISIONAL`（待核验）；**不会因为比较完成就把新版自动晋升为已合并基线**。重新执行同旧版本/同 SHA 可复用基线；同一个版本名指向不同代码会被拒绝，提示 `BASELINE_DIVERGED`。

### GitLab MR 模式

若选择 `2=GitLab MR`，向导将逐项要求 MR 地址、GitLab 主机名、旧/新版本标签，自动解析未合并 MR 的目标旧版 SHA 和源分支候选 SHA，不必手工输入 Git SHA。允许与本地项目在同一批次比较。

运行前先在当前终端设置 `GITLAB_TOKEN`（权限：`read_api`、`read_repository`）。**推荐使用 [Windows 指南](docs/WINDOWS_SETUP.md)中的安全输入方式**，不把 Token 写入代码、命令参数、SQLite 或报告。MR 必须尚未合并，且目标分支未相对 MR 记录漂移；发生 `TARGET_MOVED` 时需要刷新后再分析。源分支 HEAD **不是**最终合并版本。

### CMD 使用与参数

~~~cmd
.venv\Scripts\python.exe -m diff_analysis wizard
.venv\Scripts\python.exe -m diff_analysis wizard --db "G:\diff-analysis-data\state.sqlite3" --output "G:\reports\report.md" --json-output "G:\reports\review.json"
~~~

**`owner` 是什么？** 项目所属责任人/团队备注，用于将来筛选和分派人工评审，**不是 GitLab 用户名、权限或必填项**；回车可跳过。已有项目保持原来的 owner，不会因为本次向导留空而覆盖。

## 2. 已有非交互命令（继续兼容）

不想使用向导时，仍可通过以下命令操作：

| 命令 | 用途 |
| --- | --- |
| `catalog-add --project rhf/spms-back` | 手工登记项目，`--owner` 可选 |
| `catalog-list` | 查看已登记项目与基线覆盖 |
| `baseline-local --project ... --version ... --repo ... --sha ...` | 指定固定旧版本 Commit 建立基线 |
| `baseline-mr --allowed-host ... --mr ... --old-version ... --new-version ...` | 从未合并的 GitLab MR 建立旧版基线和候选 |
| `baseline-status` | 查看 SQLite 基线、候选和扫描缺口 |
| `baseline-verify --project ... --version ... --acknowledge` | 人工确认已核对的版本/扫描范围 |
| `review-local --repo ... --base ... --head ...` | 比较单个本地项目固定 SHA |
| `review-mrs --allowed-host ... --mr ... [--mr ...]` | 非交互分析多个 GitLab MR |

完整参数：`.\.venv\Scripts\python.exe -m diff_analysis --help`；基线/旧命令详细规则参见 [P0 使用说明](docs/P0_USAGE.md) 与 [Windows 安装指南](docs/WINDOWS_SETUP.md)。

## 3. 当前能力与后续计划

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | GitLab MR、固定 Git Diff、Java AST、反向调用与 Markdown/JSON | 已有基础实现 |
| P1-B | SQLite 人工项目清单、首次旧版本固定 SHA 基线、MR head 候选 | 首批已实现 |
| 交互 CLI | 多本地项目/MR 逐步录入、独立 Git 修订号、统一报告 | 已编码，需本地 Windows 实测 |
| P1-B 后续 | 多 MR 合并候选、实际合并结果校验、新基线晋升、批次扫描、Web API | 待开发 |
| P2-L / P3-X | Diff Coverage Ledger、业务主链与跨项目资源交叉点 | 待开发 |
| P4-U | Python 后端 + 本地 Web 界面 + SQLite | 待开发 |
| P5-C / P6-E | 业务行为契约、单条资金业务 V1/V2 隔离差异执行 | 待开发 |

**重要边界**：默认静态扫描并不能保证发现所有依赖；项目全集由用户维护。方法同名匹配仅供候选，不能证明 B 必然受 A 影响。实际业务必须进一步核对 Dubbo、SQL、数据源身份、MQ/Redis 或执行结果。

## 4. 设计文档

- [v3 架构：相邻版本基线、双台账、业务主链交叉点](docs/ARCHITECTURE_V3.md)
- [v3 开发计划和待办](docs/DEVELOPMENT_PLAN_V3.md)
- [Windows GitLab 接入和故障处理](docs/WINDOWS_SETUP.md)

本 README 保留**当前可运行入口和准确能力边界**；旧版设计及历史讨论保存在 `docs/`，不再反复堆叠到首页。
