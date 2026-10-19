# Diff Analysis

**多 GitLab MR 影响分析 · 版本级业务契约 · V1/V2 行为差异验证**

> 方案版本：v2.0（2026-10-19）｜当前代码：P0 命令行原型（Python 3.9+ + Java AST）  
> 目标：输入一个版本的全部 GitLab MR，识别主要业务变化、跨项目受影响场景及疑似遗漏，并逐步实现版本业务契约和受控环境下的新旧版本差异执行。  
> **注意：v2.0 是未来架构与开发计划，不表示全部功能已经实现。**

## 1. 为什么做这个项目？

AI 编程往往在单次需求上生成与当前实现紧耦合的测试。这样的测试可以发现部分局部错误，但不一定能证明整个支付业务正确；对内部 Service、Mapper、MQ 等全部 Mock 时，还容易掩盖真实状态变化和跨项目影响。

本项目采取三条原则：

1. **先确认影响，再决定测试什么**：从 Git Diff/AST、接口、中间件和共享数据资源反查受影响业务，而不是为每个改动方法机械生成测试。
2. **测试期望独立于实现**：新功能的预期结果来自需求/验收规则；已有功能明确标记“应保持不变 / 经批准有意改变 / 尚不确定”。
3. **比较业务可观察行为，不止比较返回值**：有条件地对比 V1/V2 的 HTTP/Dubbo 返回、数据库变更、Redis、MQ 和外部渠道调用意图。旧版本结果是回归基线，**不是天然正确答案**。

特别关注支付、退款、通知、对账、渠道签约、超时重试、幂等、资金状态等高风险链路。

## 2. 当前能运行什么？哪些还没做？

| 能力 | 现状 |
| --- | --- |
| 自建 GitLab 多 MR URL、认证、获取固定 base/head Commit | **已有基础实现** |
| 完整 Git Diff、Java AST 方法/调用点提取、反向静态调用分析 | **已有基础实现** |
| API 类改动的直接 Maven 版本检测 | **已有基础实现，父 POM/属性仍有限制** |
| 额外提供本地未修改仓库进行候选关联（`--catalog`） | **已有基础实现** |
| Markdown/JSON 报告、证据和未知项提示 | **已有基础实现** |
| Windows 原生 Python/JDK8 运行 | **已有；用户在 Windows 上确认基础测试通过** |
| 同仓库多 MR 合并候选、跨仓库完整版本清单 | **未实现** |
| Spring/Dubbo、MQ、DB 字段/条件、Redis/ES 资源级关系 | **未实现** |
| 版本需求集合、业务场景库、审批后的预期行为契约 | **未实现** |
| V1/V2 双环境执行、数据库/MQ/Redis/渠道副作用对比 | **未实现** |
| AI 按需读关联代码并给出可追溯业务评审 | **未实现** |
| Web UI、自动 GitLab MR 评论或发布门禁 | **未实现** |

**目前不支持直接提供一组 MR 就自动完成资金级业务回放。** P0 输出仅供人工评审，未发现依赖绝不代表没有影响。

### 当前代码目录

| 路径 | 当前职责 |
| --- | --- |
| `diff_analysis/cli.py` | `review-local`、`review-mrs` 入口 |
| `diff_analysis/gitlab.py` / `gitops.py` | GitLab MR 元数据、仓库克隆、Commit 与 Diff |
| `diff_analysis/scanner.py` / `java/JavaAstScanner.java` | JDK AST 扫描与调用关系 |
| `diff_analysis/review.py` | 旧新调用边合并、影响路径、初步 API 规则 |
| `diff_analysis/report.py` | Markdown 报告 |
| `tests/` | Windows / Python / Java AST 等基础测试 |

## 3. 快速启动：Windows PowerShell

依赖：**Git for Windows、Python 3.9+、完整 JDK 8+**（推荐独立使用 JDK 17 运行分析器，业务项目可以保持 Java 8）。

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

GitLab Token 需对目标私有项目有 `read_api` 和 `read_repository` 权限。请在当前 PowerShell 会话安全输入：

~~~powershell
$secret = Read-Host "GitLab Token" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $env:GITLAB_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
Remove-Variable secret, ptr -ErrorAction SilentlyContinue
~~~

输入真实 MR（域名/地址均为示例；PowerShell 反引号为续行符）：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-mrs `
  --allowed-host gitlab.example.cn `
  --mr "https://gitlab.example.cn/team/spms-back/-/merge_requests/101" `
  --mr "https://gitlab.example.cn/team/adpts/-/merge_requests/52" `
  --output ".\report.md" `
  --json-output ".\review.json"

Remove-Item Env:\GITLAB_TOKEN -ErrorAction SilentlyContinue
~~~

生成 `report.md`（供人工阅读）与 `review.json`（供后续系统消费）。本地比较可使用 `review-local --repo <路径> --base <完整SHA> --head <完整SHA>`。

详细步骤、JDK 8 `tools.jar`、证书、Token、代理和故障排查：

- [Windows 完整接入指南](docs/WINDOWS_SETUP.md)
- [P0 命令行使用方式（含 Linux）](docs/P0_USAGE.md)

## 4. v2.0 目标流程

~~~text
需求/验收规则 + 本次版本全部 GitLab MR
                   │
                   ▼
    Release Manifest（所有仓库 SHA、依赖版本、配置、DB 迁移）
                   │
                   ▼
      版本级语义 Diff + 旧新依赖图联合查询
                   │
                   ▼
      受影响业务入口/状态/消费者/历史场景
                   │
                   ▼
      业务行为契约分类与人工确认
       ├─ NEW：新功能，新增预期行为
       ├─ CHANGED：批准修改预期行为
       ├─ UNCHANGED：旧功能保持业务契约
       └─ UNKNOWN：证据不足，必须确认
                   │
                   ▼
      推荐版本回归场景 + 证据化评审清单
                   │
           （后续执行能力）
                   ▼
   V1/V2 独立环境 + 相同初始数据/外部渠道模拟
                   │
                   ▼
  比较响应 + DB 状态/字段 + Redis + MQ + 外部调用意图
                   │
                   ▼
  预期内变化 / 非预期回归 / 无法比较 / 尚未验证
~~~

### 两种 Diff，不可混淆

- **代码差异**：版本 A/B 的 Commit、API、配置、资源依赖变化，用于确定风险范围。
- **业务行为差异**：相同业务场景的不同版本执行结果及副作用，用于验证契约。只有在可控、等价的初始状态下才有可比性。

**关键限制**：Mock 主要放在银行/支付渠道等不可真实调用的外部边界；内部 Service、Mapper、DB 交互尽量真实运行。不能让 AI 从修改后的代码直接编造期望值，也不能拿旧版行为自动判定新版错误。

详见 [v2.0 架构与业务差异验证设计](docs/ARCHITECTURE_V2.md)。

## 5. 计划的核心模型

| 对象 | 作用 |
| --- | --- |
| `ReleaseManifest` | 一个版本内多仓库 Commit、API 依赖、环境配置、DB 迁移及部署范围 |
| `ChangeItem` | 每处代码/接口/数据资源的结构化变化 |
| `DependencyEvidence` | 带来源、版本与可信状态的调用/字段/MQ/Redis 依赖边 |
| `BusinessScenario` | 长期复用的业务场景，如支付成功后退款、通知超时重试、对账 |
| `BehaviorContract` | 对场景定义允许的输入、结果、持久化副作用及预期变化分类 |
| `ScenarioExecution` | V1/V2 在隔离环境执行的输入、初始数据和记录 |
| `EffectEvent` | 返回、DB、Redis、MQ、外部渠道调用意图等可观察事件 |
| `BehaviorDiff` / `ReviewFinding` | 差异及其是否符合契约、代码/执行证据和人工确认结果 |

业务场景应按 **`payment` / `refund` / `notification` / `reconciliation`** 等领域持续积累，**不按单个 MR 生成大量一次性测试**。每次版本只保存本次新增、修改、需回归和未确认的场景集合。

## 6. 重新安排的开发路线

| 阶段 | 主要交付 | 状态 |
| --- | --- | --- |
| **P0-S 稳定现有原型** | 真实内网 MR 验证、误报样例、错误/覆盖报告、CI 可诊断性 | 进行中 |
| **P1-V 版本联合分析** | Release Manifest、同仓库多 MR 候选合并、变更行为归类、跨仓库版本对齐 | 待开发 |
| **P2-D 跨项目依赖** | Java 类型/接口、Dubbo、DB 字段与 WHERE 条件、MQ、Redis 的证据化反查 | 待开发 |
| **P3-C 业务契约与场景选择** | 版本需求集合、长期业务场景库、预期行为确认、回归清单 | 待开发 |
| **P4-E V1/V2 差异执行** | 从一条隔离支付/退款场景开始，逐步比较 DB、MQ、Redis、渠道调用 | 待开发 |
| **P5-A AI 与产品化** | AI 语义评审、审计、可视化、GitLab 回写、评测及质量门禁 | 待开发 |

**调整重点**：不再优先为每个方法自动生成单元测试；先实现“版本影响 → 业务契约 → 场景选择”，再建设隔离执行能力。方法级测试仍用于金额算法、签名、状态机及边界条件等有价值的局部规则。

每个阶段的任务拆分、输入输出、验收标准、优先级和依赖关系列在 [详细开发计划](docs/DEVELOPMENT_PLAN_V2.md)。

### 下一步（优先执行）

- [ ] 用真实内网 GitLab 的 **2–3 个同一需求 MR** 验证当前分析链路和报告准确性。
- [ ] 准备 **1 个修改交易状态 + 1 个未提交 MR 的消费程序** 的已知影响案例，建立误报/漏报验收基准。
- [ ] 开发 `ReleaseManifest`，明确旧生产基线、各 MR 变更基线和最终候选版本不能混用。
- [ ] 优先产出 **“主要行为变化 + 需保持不变的旧场景 + 待确认遗漏”** 报告，之后才接双版本执行器。

## 7. 风险与安全边界

- 所有结论必须附实际 Git SHA、代码位置、依赖证据或场景执行记录；AI 只提出线索，不编造事实。
- 不执行未授权 MR 提交的脚本或构建操作；后续执行器必须在隔离环境运行、使用脱敏数据。
- **绝不连接生产支付渠道执行回放**；外部银行/支付接口只在经批准的模拟环境中调用。
- V1/V2 使用各自独立的 DB/Redis/MQ 状态；时间、随机值、异步延迟及环境配置需要控制或标记不可比较。
- 差异发现不等于缺陷；无差异不等于满足需求；缺少依赖证据不等于没有影响。
- 对高风险支付、退款、重复执行、金额及对账路径，`UNKNOWN` 不得自动标记通过。

## 8. 项目文档

- [架构 v2：代码影响、业务契约、差异执行与数据模型](docs/ARCHITECTURE_V2.md)
- [后续开发计划 v2：分阶段实施、工作项、验收用例](docs/DEVELOPMENT_PLAN_V2.md)
- [Windows 安装与 GitLab 接入](docs/WINDOWS_SETUP.md)
- [P0 实际使用及能力边界](docs/P0_USAGE.md)

参考方向：[jQAssistant](https://github.com/jqassistant/jqassistant)、[CodeQL](https://github.com/github/codeql)、[Testcontainers](https://testcontainers.com/)、[Playwright](https://github.com/microsoft/playwright)。是否引入由阶段性收益与环境约束决定，不预设必须替换现有 Java 8/Dubbo/ES 技术栈。
