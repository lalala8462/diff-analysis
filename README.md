# Diff Analysis

**版本级代码变更分析：以最终差异为底账、以提交演进解释来源、以代码关系形成需求候选组、检查组间交叉影响，再按组生成供人工评审的报告。**

> 现有技术栈：Python 3.9+、Java AST Scanner（完整 JDK 8+）、SQLite。  
> **当前实现**：固定 Git SHA 比较、最终差异与前后 30 行代码证据、提交拓扑/说明、保守的任务候选分组、弱交叉线索与未变更消费者候选、可选模型按组推理、日期时间报告目录。  
> **尚未实现**：SmartCommitCore/CodeTracker/RefactoringMiner/Code Maat 的直接适配、跨重构精确元素追踪、语义级 SQL/MQ/Redis 血缘、真正的合并候选树、自动基线晋升和 Web UI。候选组不等于已确认的需求，交叉线索不等于缺陷。

## 1. Windows 安装

需要 Git for Windows、Python 3.9+、完整 JDK 8+（含 `javac`；推荐分析器使用 JDK 17，业务项目可仍是 Java 8）。

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

## 2. 主入口：交互式版本比较

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard
~~~

或双击仓库根目录的 `start-wizard.cmd`。向导逐项输入**多个项目**及各自旧版/新版的分支名、Tag 或 SHA；GitLab 模式输入未合并 MR 地址并使用 `GITLAB_TOKEN`，流程结束前显示 SHA 摘要供确认。项目 `owner` 为可选的责任人备注，既不是认证参数也不影响分组。

非交互方式：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-local `
  --repo "G:\Code\spms-back" `
  --base "<完整旧CommitSHA>" `
  --head "<完整新CommitSHA>" `
  --output report.md `
  --json-output review.json
~~~

或 `review-mrs --allowed-host gitlab.example.cn --mr <MR URL> [--mr <MR URL>]`。完整旧命令与基线管理使用方式见 [Windows 接入说明](docs/WINDOWS_SETUP.md) 和 [P0 命令](docs/P0_USAGE.md)。

## 3. 新的分析输出

每次运行在 `.diff-analysis/runs/<UTC日期时间>-<随机标识>/` 创建**独立目录**（可用 `--run-root` 修改），和旧 `--output` / `--json-output` 并存以兼容原有脚本。

~~~text
.diff-analysis/runs/20261023T030000Z-a1b2c3d4/
  report.md                # 按候选需求组阅读、组间待检查事项
  analysis.json            # 完整最终差异、提交拓扑、候选组/交叉证据
  evidence/
    D-xxxxxxxxxxxxxxxx.md  # 前后各 30 行源码上下文、路径、固定 SHA
  model_inputs/
    G-xxxxxxxxxxxxxxxx.json        # 独立的按组分析输入
    G-xxxxxxxxxxxxxxxx.result.json # 仅实际启用模型时产生
~~~

`analysis.json` 和 `evidence/` 只采集并解释 **base → head 的最终净变化**。中间提交增加、修正、回退会保留在提交拓扑中；完全回退且最终没有差异的代码不生成需求项。

| 数据层 | 如何产生 | 可信边界 |
| --- | --- | --- |
| 最终差异 `D-*` | `git diff base head`，含重命名/删除/二进制的文件事件 | 是本次版本净变化，不代表已经解析行为 |
| 演进来源 | `git rev-list` 拓扑、commit message、按文件筛选的候选提交 | **文件级候选**，不宣称某个提交精准产生该 Hunk |
| 需求候选组 `G-*` | 仅当文件候选提交存在**唯一明确任务号**时尝试合组，否则独立保留 | 相同文件/方法/提交不能直接无条件合组 |
| 交叉检查 `X-*` | 不同组共享文件/符号、同一版本的静态调用路径 | `CHECK_TOGETHER`，不当作业务缺陷 |
| 模型推理 | 显式配置后逐组发送有限代码证据，引用已有 `D-*` | 模型结论是 `HYPOTHESIS`，不能自动批准 |
| SQLite | 保留项目/版本基线，另以 `analysis_run` 记录目录与摘要 | 不将需求猜测写入已核验基线 |

目前 SQL/XML/前端/配置等变化会被**登记**，但缺少对应语义解析器时记 `NEEDS_REVIEW`，不能因出现文件名就声称已经分析其业务关联。

## 4. 可选模型分析（默认关闭）

需要显式指定兼容 OpenAI Chat Completions JSON 输出的接口。**默认仅生成模型输入文件，不请求任何模型，也不上传源码。**

如在本地运行兼容服务：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard `
  --model-endpoint "http://127.0.0.1:1234/v1/chat/completions" `
  --model-name "your-local-model"
~~~

如需将源码发送到企业批准的 HTTPS 远程模型，必须额外添加 `--allow-remote-model`；接口密钥仅从环境变量 `DIFF_ANALYSIS_MODEL_API_KEY` 读取。**模型输入包含内部代码上下文，配置远程服务前需确认公司代码保密政策。**

模型单组输入预算默认约 24,000 字符，单次默认最多分析 20 组；超出部分明确列为 `unprovided_change_ids` / `NOT_ANALYZED_BUDGET`。模型接口不可用时仍输出静态报告，并写入失败状态，不以伪造的结论补全。

模型输出必须引用现有差异 ID，自动降级成待人工确认的假设。**本功能只完成了协议适配代码，尚未验证特定供应商或本地模型的实际兼容性。**

## 5. 下一阶段优先级

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 第一阶段 | 完整最终差异台账、提交证据、前后代码上下文、独立报告目录 | 首批已编码，需 Windows 实测 |
| 第二阶段 | 基于任务号的保守分组、分离组内与组间关联、保留版本内调用路径 | 首批已编码；精细元素身份映射待开发 |
| 第三阶段 | 组模型输入与可选模型调用、预算/错误/人工确认 | 首批已编码；模型质量评估待开发 |
| 第四阶段 | RefactoringMiner 重构检测/纯度、CodeTracker 精细演进追踪 | 待开发 |
| 第五阶段 | Code Maat 历史共同修改统计，SmartCommitCore 复杂关联分组 | 待开发 |
| 后续 | SQL/配置/MQ/Redis 血缘、跨服务主链交叉点、业务行为验证、Web | 待开发 |

完整数据模型、工具适配、回归样例、验收标准见 [分析管线 v4 设计](docs/PIPELINE_V4.md)。

## 6. 安全、质量与限制

- 不执行待分析仓库的构建/脚本。Git SHA 与历史演进分开保存；没有提交历史时最终 Diff 仍必须完整。
- **没有映射成 Java 方法的差异也必须进入底账。** 无法解析、二进制、文件类型不支持以及分析截断均要留下待审核状态。
- 当前旧/新反向调用图的并集只供旧 P0 报告参考；新跨组证据使用**分别属于 V1 或 V2** 的路径，不能拼成虚构完整调用链。
- 项目全集由用户维护；不在清单内的消费者和动态依赖不能保证找到。
- 批量格式化、文件级共同修改、提交说明与 AI 分组都不等于实际需求事实。
- 证据文件可能含公司源码，请勿公开上传分析目录、SQLite 或 Token。
- 若没有启用模型，新报告依然按候选组组织，但不包含模型生成的业务解释。

旧的 v3 文档仅作为架构演进历史保留；以本 README 与 `docs/PIPELINE_V4.md` 为当前实施基准。
