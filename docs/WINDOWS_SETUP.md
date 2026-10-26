# 安装与启动指南（Windows PowerShell 5.1 / 7）

> 文档修订：2026-10-23 16:46:15 +08:00。适用 Windows 10/11，无需 WSL、Docker、MySQL。本文统一维护安装、更新、启动与命令用法，末尾补充其他平台入口。当前为本地命令行与离线报告工具，不连接模型接口；优先阅读日期时间运行目录中的报告。变化统一记录于 [CHANGELOG](../CHANGELOG.md)，能力与设计边界以 [README](../README.md) 为准。

## 1. 安装依赖

| 依赖 | 要求 | 检查命令 |
| --- | --- | --- |
| Git for Windows | 安装最新版稳定版，确保 PowerShell 可以找到 git.exe | `git --version` |
| Python | >= 3.9，建议 3.11；安装时启用 Python Launcher | `py -3 --version` |
| JDK | >= 8，建议 17；必须有 javac，只有 JRE 不够 | `java -version`、`javac -version` |
| 网络 | 能访问 GitHub 和你公司的 GitLab HTTP API / Git over HTTPS | 浏览器或公司网络检测 |
| GitLab Token | 有目标 MR 项目的 `read_api`、`read_repository` 权限 | 下文说明 |

建议在 Windows Terminal 中使用 PowerShell。Python、Git、Java 安装后**重新打开终端**使 PATH 生效。运行时不要求在本机安装被分析项目的 Maven 依赖，也不会执行目标项目的代码或构建脚本。

## 2. 克隆项目与创建虚拟环境

```powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .

# 查看命令
.\.venv\Scripts\python.exe -m diff_analysis --help

# 本地自测（包括 Windows Git 凭证接口和 Java 中文标识符）
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

使用 ` .venv\Scripts\python.exe ` 直接调用，不需要激活脚本，也不需要更改 PowerShell 执行策略。若环境不能访问 PyPI，可在符合 Python 版本要求的前提下直接使用 `py -3 -m diff_analysis` 从仓库根目录启动（此项目运行时主要依赖 Python 标准库）。

### 更新已有仓库

先处理自身未提交改动，再更新代码并执行测试：

```powershell
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 3. 创建 GitLab 只读 Token

在公司的自建 GitLab 中创建 **Personal Access Token** 或具有对应仓库权限的 **Project/Group Access Token**：

1. 给 Token 授予 `read_api` 和 `read_repository`（不同 GitLab 版本、Token 类型权限名称可能存在差异）。
2. 确认 Token 对**所有输入 MR 对应项目**有读权限，包括私有仓库。
3. 复制 Token，**不要将它写入 Git URL、PowerShell 脚本、README、Git 仓库或命令参数**。
4. 如果 GitLab 使用公司自签发 CA，请配置受信任的 CA；不要关闭 TLS/SSL 证书验证。

通过安全输入把 Token 放入**当前 PowerShell 进程**的环境变量（PowerShell 5.1/7 均适用）：

```powershell
$secret = Read-Host "GitLab Token" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $env:GITLAB_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
Remove-Variable secret, ptr -ErrorAction SilentlyContinue
```

程序会从 `GITLAB_TOKEN` 读取凭据，访问 GitLab API，并在内部建立临时 Git 认证包装器。**Windows 自动使用 `.cmd`，Linux/macOS 使用 `.sh`**；两者都通过 Python 获取环境中的 Token，不将 Token 写进临时脚本或 Git CLI 参数。临时文件用完自动清理，Git Credential Manager 不会弹框或保存该 Token。

## 4. 选择分析入口

多项目交互向导可输入本地分支、Tag 或 SHA，并在执行前确认固定 SHA；也可添加 GitLab MR：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard
```

也可双击仓库根目录的 [start-wizard.cmd](../start-wizard.cmd)。按提示逐项添加本地 Git 项目或未合并 GitLab MR，输入各自的旧／新分支、Tag 或 SHA，输入 0 完成清单，再确认固定 SHA 后执行。`owner` 仅为可选责任人备注。GitLab 凭据从当前会话的 `GITLAB_TOKEN` 读取，安全输入方法见第 3 节。

向导目前拒绝同 SHA 项目；未修改消费者需使用下述非交互 `--catalog` 入口。重构工具及人工归属参数见 [README](../README.md#5-可选-java-重构与人工归属)。

查看命令与参数：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis --help
.\.venv\Scripts\python.exe -m diff_analysis wizard --help
.\.venv\Scripts\python.exe -m diff_analysis review-local --help
```

### 通过多个 GitLab MR 运行联合分析

把下方域名、项目路径和 MR 编号换成真实值：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis review-mrs `
  --allowed-host gitlab.example.cn `
  --mr "https://gitlab.example.cn/team/spms-back/-/merge_requests/101" `
  --mr "https://gitlab.example.cn/team/adpts/-/merge_requests/52" `
  --run-root ".\.diff-analysis\runs"
```

- `--allowed-host` 只写 GitLab **域名**（例如 `gitlab.example.cn`），不要写 `https://` 或仓库路径。
- `--mr` 可以重复多次，输入同一需求对应的多个项目 MR。
- GitLab MR URL 必须匹配 `https://主机/组/项目/-/merge_requests/编号`；不接受账号密码或 Token 写在 URL 中。
- 内网 GitLab 若只能使用 HTTP，必须显式附加 `--allow-http`。这会明文传输凭据，**仅在已确认可信的隔离网络中使用**，优先启用 HTTPS。
- 支持将未修改、但可能是调用方的**本地其他仓库**加入静态扫描：重复追加 `--catalog "D:\repos\other-service@<完整CommitSHA>"`，该仓库必须已经存在且含有对应 SHA。

非交互 `review-local` / `review-mrs` 入口会先逐项目保存 Git 底账，然后尝试 Java 分析。JDK 缺失或部分项目扫描失败时仍生成部分报告，`analysis.json` 的 `status=PARTIAL_EVIDENCE`，并标出 `stage_failures`；保留的 Git 差异不表示依赖扫描完整。手动保留 `ledger_checkpoint.json` 便于诊断，可用以下命令在不调用 Git/JDK 的情况下从检查点重新生成基础报告；它不会恢复 Java 静态分析结果。向导当前仍可能在基线扫描阶段中止。

```powershell
.\.venv\Scripts\python.exe -m diff_analysis rebuild-checkpoint `
  --checkpoint ".\.diff-analysis\runs\<运行目录>\ledger_checkpoint.json" `
  --run-root ".\.diff-analysis\runs"
```

重建会创建新的运行目录，不覆盖原检查点；新报告的 `status=PARTIAL_EVIDENCE`，每个项目标记 `STATIC_SCAN/NOT_RERUN`。

执行后终端打印独立运行目录及 `ai_review.md` 的完整路径。默认每次创建 `.diff-analysis/runs/<UTC日期时间>-<随机标识>/`，例如 `20261023T050000Z-a1b2c3d4` 对应北京时间 2026-10-23 13:00：

| 文件 | 内容 |
| --- | --- |
| `report.md` | 候选需求组、静态交叉线索和覆盖缺口，优先作为人工阅读入口 |
| `ai_review.md` | 分组与代码上下文，供用户在工具外交给本地 AI |
| `analysis.json` | 最终差异、来源候选、归属和可选结构映射等资料；包含阶段状态和失败原因 |
| `ledger_checkpoint.json` | 非交互 CLI 在扫描前原子保存的逐项目 Git 底账与失败记录；向导暂未接入 |
| `evidence/D-*.md` | 单条差异、固定版本和两侧代码窗口 |
| `group_reports/G-*.md` | 单组提交、变化、代码与交叉线索 |

用 VS Code、IDEA 或 Markdown 预览器打开终端所打印的运行目录内文件。较大批次可逐组交给外部 AI；导出成功不代表外部 AI 已完整阅读，必要方法或消费者缺失时应保留判断限制。

原 `--output` 默认仍在当前工作目录生成旧格式 `report.md`，显式 `--json-output` 时另生成兼容 JSON；向导默认还生成 `review.json`。这些兼容输出不替代独立目录的证据报告，不要因当前目录存在同名文件就认定本次分析完整。

运行完成后清理当前会话的 Token：

```powershell
Remove-Item Env:\GITLAB_TOKEN -ErrorAction SilentlyContinue
```

## 4.1 新增：证据项目与离线审核闭环（待集成验证）

证据项目可以有独立的两个 SHA，也允许两侧是同一个 SHA；不自动把 EVIDENCE 项目的变化当作本次发布需求。交互向导选择 **3=本地证据项目**。非交互入口可追加：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis review-local `
  --repo "D:\repos\spms-back" --base "<完整旧SHA>" --head "<完整新SHA>" `
  --project "rhf/spms-back" `
  --evidence "rhf/reconciliation=D:\repos\reconciliation@<完整旧SHA>@<完整新SHA>"
```

成功或部分失败的运行目录将包含 `ledger_checkpoint.json`、`stage_manifest.json`、`questions.json`、`questions.md` 和 `evidence_manifest.json`。`analysis.json` 的 `relations.graphs` 分别记录 BASE／HEAD 边、未解析调用与影响传播；名称／参数数目匹配仍只是候选。

从 `questions.json` 查询真实 `Q-...` 编号，编写请求文件 `request.json`：

```json
{
  "schema_version": "evidence-requests-v1",
  "selectors": [{
    "question_id": "Q-替换为本次问题编号",
    "project": "rhf/spms-back",
    "sha": "替换为该项目对应侧的完整CommitSHA",
    "side": "HEAD",
    "path": "src/main/java/example/PayService.java",
    "start": 10,
    "end": 70,
    "reason": "检查改动涉及的完整方法及调用条件"
  }]
}
```

```powershell
.\.venv\Scripts\python.exe -m diff_analysis supplement `
  --run-dir ".\.diff-analysis\runs\<运行目录>" `
  --request ".\request.json" `
  --repo "rhf/spms-back=D:\repos\spms-back"
```

补证读取固定 Git 对象，拒绝越界路径和不匹配 SHA；单请求最大 500 行、原始文件 2 MB，默认累计 1 MB／最多 3 轮。新增证据写入 `supplements/`，无新增有效证据会标记停止。不会自行查找未纳入批次的项目，也不会执行仓库代码。

人工确认目标／规则及外部 AI 结论通过本地 `review-import-v1` JSON 导入，例如：

```json
{
  "schema_version": "review-import-v1",
  "expected_goals": [{
    "id": "GOAL-payment-1",
    "status": "HUMAN_CONFIRMED",
    "author": "reviewer",
    "text": "原有支付成功状态的处理逻辑必须继续兼容"
  }],
  "rule_points": [{
    "id": "RULE-payment-1",
    "expected_goal_id": "GOAL-payment-1",
    "status": "CANDIDATE",
    "author": "local-model",
    "condition": "收到支付成功状态时需要处理通知",
    "expected_behavior": "消费者必须正确识别状态并继续处理",
    "invariant": "相同交易不能重复结算"
  }],
  "decisions": [{
    "object_type": "QUESTION",
    "object_id": "Q-替换为本次问题编号",
    "dimension": "IMPACT",
    "actor_kind": "AI",
    "actor_id": "local-model",
    "verdict": "INSUFFICIENT_EVIDENCE",
    "reason": "目前没有核实消费者真实版本及绑定",
    "evidence_ids": []
  }]
}
```

```powershell
.\.venv\Scripts\python.exe -m diff_analysis import-review `
  --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\review-import.json"
```

`CONFORMANCE` 维度的正向判断必须绑定已人工确认的 ExpectedGoal，所有正向判断必须引用有效 D-* 或 EV-* 证据。引用结构合法仍不等于论断成立，外部模型身份与实际阅读范围也不会由工具自动认证。

人工拆分 Hunk、创建新的拆分组、移动评审单元、合并候选组，使用 `group-feedback-v1` 请求文件。示例（从 `analysis.json` 查询实际差异、组及新行范围）：

```json
{
  "schema_version": "group-feedback-v1",
  "actions": [{
    "action": "SPLIT_HUNK",
    "diff_id": "D-替换为本次差异ID",
    "actor": "reviewer",
    "reason": "这个Hunk包含不同需求的两个可独立核查片段",
    "segments": [
      {"start": 15, "count": 2, "group_id": "G-替换为候选组ID"}
    ]
  }]
}
```

```powershell
.\.venv\Scripts\python.exe -m diff_analysis revise-groups `
  --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\group-feedback.json"

.\.venv\Scripts\python.exe -m diff_analysis review-status `
  --run-dir ".\.diff-analysis\runs\<运行目录>"
```

另可使用 `SPLIT_GROUP` 操作，指定 `source_group_id`、`diff_ids`、`new_group_label`、`actor`、`reason` 形成新的 `G-MAN-*` 组；`MERGE_GROUPS` 指定 `source_group_ids` 与 `target_group_id`。不在人工分配段内的新行保留为未归属 ReviewUnit。旧版子范围不会从新版行机械推算。归属修订记录在 `revisions/`；原 `analysis.json`、D-* 底账不改写。

如需单独复核某个 AI／人工结论是否被引用依据支持，编写 `basis-checks-v1`，提交 `decision_id`、`actor_kind`、`actor_id`、`status`（SUPPORTED／NOT_SUPPORTED／UNCERTAIN）、`reason` 和 `evidence_ids`，再执行：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis import-basis-check `
  --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\basis-checks.json"
```

这只是独立的判断依据记录，不代表静态关系已转化为真实业务依赖，也不会自动发布或执行被分析项目。

## 4.2 Java 精确符号归属与四类协议绑定（新增，待验证）

Java 扫描现在优先采用 javac 本身的符号归属结果。当调用目标可以唯一映射到同版 Java AST 方法时标记 `JAVAC_ATTRIBUTED`；未能解析的第三方库、动态代理、反射等保留为 `STATIC_CANDIDATE` 或未解析调用。

没有 JAR 清单时仍按本地源码与 JDK 扫描，不会自动下载 Maven 依赖。需要更准确类型解析时，生成 `java-classpath.json`：

```json
{
  "schema_version": "java-classpath-v1",
  "projects": [{
    "project": "rhf/spms-back",
    "base_sha": "<完整旧SHA>",
    "head_sha": "<完整新SHA>",
    "base": [{"path": "D:\\libs\\old-api.jar", "sha256": "<64字符SHA256>"}],
    "head": [{"path": "D:\\libs\\new-api.jar", "sha256": "<64字符SHA256>"}]
  }]
}
```

每个 SHA 的 JAR 分别校验实际文件的 SHA-256；不允许默默使用当前最新版代替历史依赖。执行 `review-local` 或 `review-mrs` 时附加 `--java-classpath-manifest .\\java-classpath.json`；向导 `wizard` 同样接受此参数。若清单格式或哈希错误，Git 底账保留，静态扫描报告标记失败。

先获取跨系统协议候选的原始源码证据（提供本次运行涉及的所有需要扫描的仓库）：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis discover-connections `
  --run-dir ".\.diff-analysis\runs\<运行目录>" `
  --repo "rhf/spms-back=D:\\repos\\spms-back" `
  --repo "rhf/consumer=D:\\repos\\consumer"
```

读取 `protocol_discovery_latest.json`，根据实际代码、XML、SQL 完善两端身份，生成 `bindings.json`。例如 Dubbo 的两端均需要 interface、group、version、registry、service_id；HTTP 需要 method、path、target_service、environment、service_id；MQ 需要 broker、destination、route、environment、service_id；数据库需要 datasource、schema、table、environment、service_id。**字段完整不代表字段内容已经核实**。示例：

```json
{
  "schema_version": "connection-bindings-v1",
  "observations": [
    {
      "project": "rhf/spms-back", "side": "HEAD", "sha": "<spms固定HEAD-SHA>",
      "kind": "DUBBO", "role": "PROVIDER",
      "identity": {
        "interface": "com.example.PayService", "group": "default",
        "version": "1.0", "registry": "zk-uat", "service_id": "payment"
      },
      "evidence": {
        "path": "src/main/java/com/example/PayService.java",
        "start": 10, "end": 20, "contains": ["PayService"]
      },
      "deployment_binding": {
        "author": "reviewer", "environment": "uat", "service_id": "payment",
        "reason": "已核对对应环境部署配置与双方依赖版本"
      }
    }
  ]
}
```

再为消费者加入相同协议身份、但项目和 SHA 独立的 `CONSUMER` 观察记录，然后执行：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis bind-connections `
  --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\\bindings.json" `
  --repo "rhf/spms-back=D:\\repos\\spms-back" `
  --repo "rhf/consumer=D:\\repos\\consumer"
```

结果在 `bindings_latest.json` 和 `bindings/bindings-*.json`。无部署声明时为 `BINDING_UNCONFIRMED`；匹配且双方声明齐全时也只标 `USER_ATTESTED_MATCH`，**不表示已经联网核实 Dubbo/Zookeeper、MQ 或数据库的真实运行绑定**。本地工具不会自动建立生产连接。

从 `analysis.json` 的 `relations.graphs[].method_index` 查找固定版本完整方法的 symbol、path、start/end；使用 `semantic-evidence-v1` JSON 请求按需提取：

```json
{
  "schema_version": "semantic-evidence-v1",
  "selectors": [
    {
      "question_id": "Q-替换为本次编号",
      "project": "rhf/spms-back", "side": "HEAD",
      "sha": "<完整固定SHA>", "kind": "METHOD",
      "symbol": "com.example.PayService#pay(java.lang.String)"
    },
    {
      "question_id": "Q-替换为本次编号",
      "project": "rhf/spms-back", "side": "HEAD",
      "sha": "<完整固定SHA>", "kind": "CONTRACT",
      "path": "src/main/java/com/example/PayResponse.java"
    }
  ]
}
```

```powershell
.\.venv\Scripts\python.exe -m diff_analysis semantic-evidence `
  --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\\semantic.json" `
  --repo "rhf/spms-back=D:\\repos\\spms-back"
```

结果写入 `supplements/`，与原行区间补证共享轮次与累计字节预算；文件太大不静默截断。最新覆盖记录在 `evidence_manifest_latest.json`，原始 `evidence_manifest.json` 不覆盖。

也可以从问题自动生成完整方法与明确的 API/DTO 契约请求，不必逐个填写 symbol：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis auto-evidence `
  --run-dir ".\.diff-analysis\runs\<运行目录>" `
  --repo "rhf/spms-back=D:\\repos\\spms-back" `
  --repo "rhf/consumer=D:\\repos\\consumer"
```

`auto_evidence_plan.json` 保存不超过 50 个固定版本选择器与遗漏理由。只有已索引方法或明确命名的 API／DTO／Request／Response 等文件会被自动选择；不会无差别导出所有源码、敏感配置，也不会在 JDK 失败后猜测方法范围。新增证据与阅读范围仍不能证明实际 AI 阅读过该文件。

每次 `revise-groups` 会自动生成 `recomputed_latest.json` 和版本化 `revisions/recomputed-*.json`。如需按最近的人工修订重算：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis recompute-review `
  --run-dir ".\.diff-analysis\runs\<运行目录>"
```

派生报告含受影响组、复用传播数量、直接跨组候选、消费者与覆盖缺口。拆分 Hunk 只有新版子行范围时，BASE 侧不能推出旧版一一对应范围。原 Git 底账和主 `analysis.json` 不会覆盖。

## 4.3 统一影响追踪、定向字段证据与增量复核（JDK 8）

所有环境只要求 **JDK 8**（确保 `java -version`、`javac -version` 均为 1.8，JAVA_HOME 指向 JDK 而非 JRE）。静态分析不会运行目标业务工程。

先通过 `review-local`／`wizard` 取得固定 SHA 的 `analysis.json`，并通过 `bind-connections` 导入双端源码、协议身份和人工环境声明。新命令：

```powershell
# 显示当前固定版本的 Java + Dubbo/HTTP/MQ/数据库联合传播
.\.venv\Scripts\python.exe -m diff_analysis rebuild-unified `
  --run-dir ".\.diff-analysis\runs\<运行目录>"

# 用差异中的字段名称追踪相关消费者 if/switch 分支、DTO 字段、
# MyBatis XML 和 SQL 中的候选引用，仅在提供的固定版本仓库读取
.\.venv\Scripts\python.exe -m diff_analysis contract-context `
  --run-dir ".\.diff-analysis\runs\<运行目录>" `
  --repo "rhf/spms-back=D:\repos\spms-back" `
  --repo "rhf/consumer=D:\repos\consumer"

# 按最新问题，自动请求完整方法和候选契约文件
.\.venv\Scripts\python.exe -m diff_analysis auto-evidence `
  --run-dir ".\.diff-analysis\runs\<运行目录>" `
  --repo "rhf/spms-back=D:\repos\spms-back" `
  --repo "rhf/consumer=D:\repos\consumer"
```

产物：

- `unified_graph_latest.json`：版本化节点／边，方法与协议端点分别标识；证据、绑定可信度与未映射项清晰保留。
- `unified_impact_latest.json`：按组向上追踪的跨项目消费者路径与不确定性、增量重算和缓存命中情况。
- `contract_context_latest.json`：具体变化标识在消费者判断分支、字段声明、XML/SQL 中的候选命中。
- `review_package_latest.json`：当前审核问题、版本／修订指纹、必要证据计划及待重新验证的结论；历史保留于 `revisions/`。

人工 `revise-groups` 后会自动更新统一关系影响和审核问题包；导入替代旧 ExpectedGoal 的人工目标时同样触发关联问题与证据计划更新。再次运行 `auto-evidence` 会按照最新问题取证，原始 `analysis.json`、已经保存的历史判断不会被覆盖。若方法/协议端点无法唯一关联，只显示缺口，不凭接口同名推断为真实依赖。

以上关系追踪是静态源码证据与经人工声明的部署身份的组合，**仍非已部署链路验证或字段级数据流证明**。JDK 8 下的真实 Dubbo、MQ、MyBatis 工程样例仍需验收。

## 5. 不依赖 GitLab：先分析本地 Git 项目

非交互入口需要一对**完整 Commit SHA**（40／64 位，按仓库对象格式；不是分支名或短 SHA）。先用 `rev-parse` 解析：

```powershell
git -C "D:\repos\spms-back" rev-parse main
git -C "D:\repos\spms-back" rev-parse my-feature-branch

.\.venv\Scripts\python.exe -m diff_analysis review-local `
  --repo "D:\repos\spms-back" `
  --base "<完整旧CommitSHA>" `
  --head "<完整新CommitSHA>" `
  --run-root ".\.diff-analysis\runs"
```

该方式无需 GitLab Token，输出位置与上一节相同。可用 `--project` 指定报告中的项目标识，并追加固定 SHA 的未修改消费者：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis review-local `
  --repo "D:\repos\spms-back" `
  --project "local/spms-back" `
  --base "<完整旧CommitSHA>" `
  --head "<完整新CommitSHA>" `
  --catalog "D:\repos\consumer-service@<消费者完整CommitSHA>" `
  --run-root ".\.diff-analysis\runs"
```

不需要额外消费者时省略 `--catalog`。该入口仍不是目标设计中的统一两侧版本输入，不能据此认定消费者的实际部署版本。

### 项目目录与固定 SHA 基线

以下是现有辅助命令，不负责自动合并或晋升基线：

| 命令 | 用途 |
| --- | --- |
| `catalog-add` / `catalog-list` | 人工登记／查看项目范围 |
| `baseline-local` / `baseline-mr` | 创建本地固定版本快照，或记录 MR 目标基线与来源 HEAD 候选 |
| `baseline-status` | 查看快照、候选及扫描缺口 |
| `baseline-verify` | 人工确认固定版本身份和扫描范围；不代表业务正确或依赖完整 |

例如查看参数：`.\.venv\Scripts\python.exe -m diff_analysis baseline-local --help`。`baseline-verify` 需要显式 `--acknowledge`，有扫描警告的快照不能直接确认。普通版本比较不要求先执行这些命令。

## JDK 8 下 AST 扫描器编译失败的处理

如果测试报错类似：

    package com.sun.source.tree does not exist
    package com.sun.source.util does not exist
    cannot find symbol: TreePathScanner

说明当前 JDK 编译器 API 没有被正确找到。JDK 8 的相关 API 通常位于完整 JDK 的 lib/tools.jar 中；JRE 目录没有这个文件。

扫描器会判断 javac 版本；使用 JDK 8 时将 tools.jar 加入编译与运行 classpath，找不到时输出错误信息。分析工具可以独立使用完整 JDK 17，即使被分析项目仍用 Java 8；可选 RefactoringMiner 3.x 的运行环境另需 Java 17+。

请在 PowerShell 中检查：

    java -version
    javac -version
    where.exe java
    where.exe javac
    $env:JAVA_HOME

如果必须使用 JDK 8，检查：

    Test-Path "$env:JAVA_HOME\lib\tools.jar"

若返回 False，说明 JAVA_HOME 很可能指向 JRE、JDK 安装不完整或路径配置错误。将 JAVA_HOME 调整到**完整 JDK**目录，再重新打开终端测试。

如果已安装 JDK 17，可仅在当前终端切换（路径请替换成实际安装位置）：

    $env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17"
    $env:Path = "$env:JAVA_HOME\bin;$env:Path"
    java -version
    javac -version
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v

javac 编译诊断按 UTF-8 输出。当前入口先扫描后生成新底账，JDK／扫描器整体失败仍可能阻断运行；底账先落盘与阶段隔离是待实施设计，不能根据本文推定已有完整降级报告。

## 6. 故障排查

| 现象 | 检查方式 |
| --- | --- |
| `py` / `python` 无法识别 | 安装 Python / Launcher，确认 PATH；重开 PowerShell |
| `javac` 无法识别 | 安装 JDK（不是 JRE），设置 `JAVA_HOME` 与 PATH |
| `git` 无法识别 | Git for Windows 安装时选择可从命令行访问，重开终端 |
| `Missing GitLab token` | 当前 PowerShell 未配置 `$env:GITLAB_TOKEN` 或变量已清理 |
| `GitLab API request failed` | 检查域名、公司代理/VPN、Token 的 `read_api`、证书信任 |
| `Cannot clone GitLab project` | 检查 Git for Windows、`read_repository`、Git HTTPS 证书、私有项目权限 |
| `Pinned commit unavailable` | MR fork / 合并引用无法获取；需要能读取来源项目的 commit |
| Windows 文件名无法落盘 | 工具记录 `unsupported source path`，报告标记为不完整；不会悄悄忽略 |
| 修改 Java 后方法没有出现在影响报告 | 当前只做保守 AST 静态分析，不保证解析 Spring 代理、反射、所有方法调用 |
| PowerShell 续行错误 | 反引号必须是每个续行的最后一个字符，后面不要有空格；也可合并为单行 |

## 7. 当前限制与安全边界

- 可分析多个 MR，但尚不验证多个 MR 临时合并后的组合树和冲突。
- 尚不自动接入 ZK、RabbitMQ、Redis、ES 或完整数据库字段级依赖。
- **不会自动发现所有未知程序 D**。可手动用 `--catalog` 纳入未修改仓库；跨项目按方法名匹配只算候选关系。
- 程序不运行模型，没有 Web UI；只导出离线资料。已有受限补证、独立结论导入与修订重审的本地接口，但真实业务验证仍待进行，报告不能宣称业务完整或安全。
- 单元测试范围见 [CHANGELOG](../CHANGELOG.md)；公司内网 GitLab 的 Token、TLS、代理仍需实际验证。网络不通时同时检查本地代理配置，不关闭证书校验。

## 8. 其他平台入口

Linux/macOS 可用 `python3 -m pip install -e .` 安装，再用 `python3 -m diff_analysis review-local --repo /path/to/repo --base <完整旧SHA> --head <完整新SHA>` 运行；参数语义与上文一致，续行语法按所用 Shell 调整。
