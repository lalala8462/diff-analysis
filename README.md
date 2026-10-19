# Diff Analysis

**多 GitLab MR 版本影响分析**：从人工维护的项目范围出发，按相邻版本建立源码基线，识别改动和关联程序的待确认影响，逐步发展为业务主链/交叉点可视化与版本回归验证。

> 当前实现：**P0 静态分析 CLI + P1-B 首批 SQLite 项目目录与固定 SHA 基线**。  
> **尚未实现**：Web UI、同仓库多个 MR 的真实合并候选、基线自动晋升、数据库/MQ/Redis 字段级跨服务影响、双版本执行与 AI 业务评审。请勿把候选关系当成已经证明的业务缺陷。

## 1. 已实现功能与边界

| 模块 | 已实现 | 当前限制 |
| --- | --- | --- |
| 多 MR 评审 | `review-mrs` 获取 GitLab MR、固定 base/head，Java AST 与基础调用方分析 | 各 MR 独立分析，未模拟最终合并树 |
| 项目目录 | `catalog-add` / `catalog-list`，SQLite 保存人工纳管项目与目录修订号 | 尚无 Web 管理界面；无法自动推断项目清单之外的系统 |
| 首次旧版本基线 | `baseline-local` 或 `baseline-mr`：从固定旧版本 Commit 提取 Git 文件清单、AST 方法与调用点，记录扫描失败 | 静态方法图不是完整业务依赖图 |
| MR 候选版本 | `baseline-mr` 记录 MR 的旧目标 SHA、merge-base、源 HEAD SHA/树哈希 | 候选状态为 `SOURCE_HEAD_ONLY`，**不是最终合并版** |
| 基线复用/核验 | 同项目/版本/SHA/扫描器版本可复用；不同 Git 对象触发 `BASELINE_DIVERGED`；可人工确认无警告的基线 | `VERIFIED` 只代表指定源版本与扫描范围已核验，非完整业务正确性 |
| 报告 | `review-local` / `review-mrs` 生成 Markdown/JSON | 尚无 Diff/Impact 双台账及主链交叉点 UI |
| Windows | Python 3.9+、Git for Windows、完整 JDK 8+，可在 PowerShell 运行 | 企业 GitLab 证书、代理、鉴权需在实际网络验证 |

版本规则：MR 从 **V1.1 合并至 V1.0** 时，首次扫描目标旧版本 V1.0 并锁定 Git SHA；源 HEAD 只作为 V1.1 候选。**MR 真正合并后必须读取最终 Git 树并审核，再将其晋升为 V1.1；该晋升逻辑尚未编码。** 后续 V1.2 → V1.1 不得仅凭版本名字跳过 SHA 校验。

## 2. Windows 安装与启动

Windows PowerShell 5.1/7：

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m diff_analysis --help
~~~

需要 Git for Windows、Python 3.9+、完整 JDK 8+（含 `javac`，建议独立用 JDK 17 运行分析器）。当前无需安装 MySQL、Redis、Docker；SQLite 随 Python 标准库提供。

## 3. 使用 P1-B 新增基线功能

### 3.1 建立人工项目清单

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis catalog-add --project rhf/spms-back --owner payment
.\.venv\Scripts\python.exe -m diff_analysis catalog-add --project rhf/refund-service --owner payment
.\.venv\Scripts\python.exe -m diff_analysis catalog-add --project rhf/reconciliation-job --owner finance

.\.venv\Scripts\python.exe -m diff_analysis catalog-list
~~~

项目必须先纳入清单才能建基线。命令默认使用当前目录的 `.diff-analysis/state.sqlite3`，已被 `.gitignore` 排除。`catalog-list` 会显示登记但没有快照的项目，不把它们从扫描范围统计中隐藏。

### 3.2 不连接 GitLab：用本地仓库验证固定版本

~~~powershell
$baseSha = git -C "G:\Code\spms-back" rev-parse V1.0

.\.venv\Scripts\python.exe -m diff_analysis baseline-local `
  --project rhf/spms-back `
  --version V1.0 `
  --repo "G:\Code\spms-back" `
  --sha $baseSha

.\.venv\Scripts\python.exe -m diff_analysis baseline-status
~~~

如果本地没有名为 `V1.0` 的分支/标签，可使用从 GitLab 获取的完整旧版本 Commit SHA。注意本阶段**不会自动以 main/master 作为旧版本**。

### 3.3 使用未合并 GitLab MR 建首次基线

Token 需有 `read_api` 和 `read_repository` 权限。不要写入脚本、Git URL 或 SQLite。PowerShell 安全输入方式见 [Windows 接入指南](docs/WINDOWS_SETUP.md)。

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis baseline-mr `
  --allowed-host gitlab.example.cn `
  --mr "https://gitlab.example.cn/rhf/spms-back/-/merge_requests/101" `
  --old-version V1.0 `
  --new-version V1.1

.\.venv\Scripts\python.exe -m diff_analysis baseline-status
~~~

执行逻辑：读取 MR 的 `diff_refs.start_sha`（旧目标版本）和 `head_sha`；再次请求目标分支当前 SHA，**不一致就报 `TARGET_MOVED`**，不使用悄悄漂移的分支基线；克隆真实 Git 对象并对 V1.0 扫描；最后记录 V1.1 的 `SOURCE_HEAD_ONLY` 候选。已合并 MR 不支持用当前目标分支倒推原来的 V1.0，必须用精确旧 Commit 处理。

基线没有扫描警告且人工核对旧版本 SHA/项目范围后，可显式确认：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis baseline-verify `
  --project rhf/spms-back `
  --version V1.0 `
  --acknowledge
~~~

**该确认不代表已经识别全部业务依赖**；无法解析的动态调用和未纳管资源仍需后续阶段处理。

如果要使用另一个数据库文件，可以给每条新命令添加 `--db "G:\diff-analysis-data\state.sqlite3"`。数据库文件不要提交到 Git 仓库。

## 4. 原有 P0 代码评审保持可用

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-mrs `
  --allowed-host gitlab.example.cn `
  --mr "https://gitlab.example.cn/rhf/spms-back/-/merge_requests/101" `
  --mr "https://gitlab.example.cn/rhf/refund-service/-/merge_requests/52" `
  --output report.md `
  --json-output review.json
~~~

此命令与新增的基线命令**目前仍然是独立路径**：P0 生成方法级评审报告；P1-B 建立带版本的源码/分析快照。后续的 Diff Coverage Ledger、跨程序业务主链关联会复用这两类数据。

## 5. 下一阶段路线

| 阶段 | 任务 | 状态 |
| --- | --- | --- |
| P0-S | Windows 本地测试、GitLab MR 读取与基本 Java 静态分析 | 已有基础实现，企业实测仍需确认 |
| **P1-B01~B03** | 人工项目目录、SQLite、首次固定旧版本基线、MR source HEAD 候选 | **已实现首批** |
| **P1-B04~B10** | 全纳管项目版本快照、最终合并结果验证与晋升、跨项目版本批次、扫描缓存与 API | 待开发 |
| P2-L | 每个 Git Diff Hunk 处理记录 + A 项目主逻辑 | 待开发 |
| P3-X | 跨项目交叉媒介和证据断点：Java → Dubbo/SQL/MQ/Redis | 待开发 |
| P4-U | Python + SQLite + 本地 Web，按 A 主链/交叉点/B 主链分层展示 | 待开发 |
| P5-C | 长期业务场景库、NEW/CHANGED/UNCHANGED/UNKNOWN 契约 | 待开发 |
| P6-E | 一条隔离资金业务的 V1/V2 可观察行为差异验证 | 待开发 |

**质量原则**：项目全集由用户维护，扫描失败和未知项必须显示；代码差异处理完整不等于业务影响已全部发现；所有依赖/业务结论都应有可核验的证据，不可让 AI 按猜测生成确定调用边。

## 6. 文档

- [Windows 安装、GitLab Token、JDK 8/17 排障](docs/WINDOWS_SETUP.md)
- [现有 P0 命令行使用及能力边界](docs/P0_USAGE.md)
- [v3 完整架构：版本基线、双台账、主链交叉点](docs/ARCHITECTURE_V3.md)
- [v3 分阶段开发与验收标准](docs/DEVELOPMENT_PLAN_V3.md)

旧 v2 文件仅存档，不作为当前实施依据。
