# Windows 安装与操作指南

适用 Windows 10/11、PowerShell 5.1/7、Git、Python 3.9+、**完整 JDK 8**。本程序仅生成离线影响报告，不运行被分析业务项目的构建、测试或脚本，不调用模型 API。Linux 可使用等效的 Python/Git/JDK 8 命令。

> 本指南描述已写入开发分支的接口，**并非已通过集成测试的承诺**。功能边界以 [README](../README.md) 为准；历史变更见 [CHANGELOG](../CHANGELOG.md)。

## 1. 安装、更新和自测

在 PowerShell 检查：

~~~powershell
git --version
py -3 --version
java -version
javac -version
$env:JAVA_HOME
Test-Path "$env:JAVA_HOME\lib\tools.jar"
~~~

要求使用**JDK 8，不是 JRE**；最后一个检查应为 True。Windows 上多个 Java 安装共存时，应确保 JAVA_HOME 与 PATH 的 javac/java 指向同一 JDK 8。示例路径需换成实际安装目录：

~~~powershell
$env:JAVA_HOME = "C:\Program Files\Java\jdk1.8.0_202"
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
java -version
javac -version
~~~

安装开发分支：

~~~powershell
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
git fetch origin
git switch feat/p1a-ledger-first-checkpoint
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m diff_analysis --help
~~~

首次安装后和每次更新代码后都执行：

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q diff_analysis tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

更新此分支可使用 git pull --ff-only origin feat/p1a-ledger-first-checkpoint；有本地未提交修改时先处理自己的修改，不要直接覆盖。本项目的主要运行逻辑使用 Python 标准库，不要求启动 MySQL、Docker 或 WSL。

## 2. 分析本地项目或多个 GitLab MR

### 本地单项目

替换项目路径和**完整 Git SHA**：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-local --repo "D:\repos\spms-back" --project "rhf/spms-back" --base "<完整旧SHA>" --head "<完整新SHA>"
~~~

### 多项目向导

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis wizard
~~~

或使用仓库根目录的 start-wizard.cmd。向导支持 CHANGE（本次审核变化）及 EVIDENCE（提供关联证据、可以没有变化）项目，逐项选择本地代码／GitLab MR、版本并确认固定 SHA。EVIDENCE 的两侧可使用相同 SHA；未修改消费者**不必**被当作新需求。

非交互入口可以附加：

~~~text
--evidence "rhf/consumer=D:\repos\consumer@<完整旧SHA>@<完整新SHA>"
--catalog "D:\repos\another-consumer@<完整SHA>"
~~~

--catalog 是仅有单侧 SHA 的兼容用法，建议新项目优先使用 --evidence。

### 自建 GitLab MR

请给 Token 授权目标项目的 read_api / read_repository，不要放入脚本、命令行参数、Git URL 或仓库。PowerShell 当前进程安全输入：

~~~powershell
$secret = Read-Host "GitLab Token" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $env:GITLAB_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
Remove-Variable secret, ptr -ErrorAction SilentlyContinue
~~~

示例：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis review-mrs --allowed-host gitlab.example.cn --mr "https://gitlab.example.cn/team/spms-back/-/merge_requests/101" --mr "https://gitlab.example.cn/team/consumer/-/merge_requests/52"
~~~

--allowed-host 仅写主机名。默认只允许 HTTPS；内网 HTTP 必须显式 --allow-http，但会明文传输凭据，应尽量避免。使用公司可信 CA，不要关闭证书验证。

完成后清除：

~~~powershell
Remove-Item Env:\GITLAB_TOKEN -ErrorAction SilentlyContinue
~~~

## 3. 运行目录及失败恢复

默认每次在项目的 .diff-analysis/runs/ 创建 UTC 时间＋随机标识的独立目录；`--run-root` 只能指定该目录内的子目录。重点文件：

向导和比较命令只在本次独立运行目录生成报告与证据；使用 `report.html`、`report.md` 和 `analysis.json` 查看结果。

| 路径 | 内容 |
| --- | --- |
| ledger_checkpoint.json | 首先保存的 Git 净差异与逐项目状态 |
| analysis.json / stage_manifest.json | 代码事实、阶段完成度与缺口 |
| report.md / ai_review.md | 人审入口及离线 AI 输入 |
| questions.json / questions.md | 按组和消费者生成的审核问题 |
| evidence/D-*.md / group_reports/ | 固定版本窗口与各组材料 |
| unified_graph_latest.json / unified_impact_latest.json | Java + 协议关系、反向跨项目影响 |
| bindings_latest.json / protocol_discovery_latest.json | 协议证据与人工绑定（执行对应命令后生成） |
| contract_context_latest.json / supplements/ | 定向字段／分支线索和代码补证 |
| review_package_latest.json / review_state.json / revisions/ | 最新问题、审核状态和不可覆盖历史快照 |

先看 stage_manifest.json 中失败的阶段。Git 底账有效但 Java 静态扫描失败时可仅凭底账重建新目录下的基础报告（**不会恢复静态关系图**）：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis rebuild-checkpoint --checkpoint ".\.diff-analysis\runs\<运行目录>\ledger_checkpoint.json"
~~~

存在 report.md 不意味着分析完整；存在关系图也不意味着所有项目和方法都解析成功。

## 4. 固定版本 Java 类路径（可选）

无外部 classpath 时可分析本项目和 JDK 可解析的源码；使用框架或多模块项目时，可按项目及版本指定不同的本地 JAR 以增加符号归属成功率。每个 JAR 必须指定真实 SHA-256，工具不下载依赖。

java-classpath.json 例子：

~~~json
{
  "schema_version": "java-classpath-v1",
  "projects": [{
    "project": "rhf/spms-back",
    "base_sha": "<完整旧SHA>",
    "head_sha": "<完整新SHA>",
    "base": [{"path": "D:\\libs\\api-old.jar", "sha256": "<该JAR的64位SHA256>"}],
    "head": [{"path": "D:\\libs\\api-new.jar", "sha256": "<该JAR的64位SHA256>"}]
  }]
}
~~~

在 review-local、review-mrs 或 wizard 后附加：

~~~text
--java-classpath-manifest ".\java-classpath.json"
~~~

JAR 与历史版本的实际匹配需要人工核对。javac 无法解决动态代理、反射或未经提供的依赖；有编译器缺失类型诊断时静态关系仍不完整。RefactoringMiner、CodeTracker 与人工 attribution-rules 属可选增强，详情参见 [管线设计](PIPELINE_V4.md#当前可用增强接口)；部分 RefactoringMiner 发行版要求高于 JDK 8，不在当前 JDK 8 支持验证范围内。

## 5. 协议线索、人工绑定与统一关系图

先枚举提交版本内可见的 Dubbo／HTTP／MQ／数据库线索：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis discover-connections --run-dir ".\.diff-analysis\runs\<运行目录>" --repo "rhf/spms-back=D:\repos\spms-back" --repo "rhf/consumer=D:\repos\consumer"
~~~

发现的协议文本只是候选。生成 connection-bindings-v1 文件，提供双方真实协议身份、Git SHA、源码范围和环境说明。以一个 Dubbo 提供方为例：

~~~json
{
  "schema_version": "connection-bindings-v1",
  "observations": [{
    "project": "rhf/spms-back",
    "side": "HEAD",
    "sha": "<该项目完整HEAD-SHA>",
    "kind": "DUBBO",
    "role": "PROVIDER",
    "identity": {
      "interface": "com.example.PayService",
      "group": "payment",
      "version": "1.0",
      "registry": "zk-uat",
      "service_id": "pay-service"
    },
    "evidence": {
      "path": "src/main/java/com/example/PayService.java",
      "start": 10,
      "end": 20,
      "contains": ["PayService"]
    },
    "deployment_binding": {
      "author": "reviewer",
      "environment": "uat",
      "service_id": "pay-service",
      "reason": "已比对双方具体服务版本与测试环境部署配置"
    }
  }]
}
~~~

还需消费者方的独立 CONSUMER 记录，同一侧完整协议身份完全对应时才生成连接。其他协议身份必需键：

| 协议 | 身份字段 | 角色 |
| --- | --- | --- |
| Dubbo | interface、group、version、registry、service_id | PROVIDER / CONSUMER |
| HTTP | method、path、target_service、environment、service_id | SERVER / CLIENT |
| MQ | broker、destination、route、environment、service_id | PRODUCER / CONSUMER |
| 数据库 | datasource、schema、table、environment、service_id | WRITER / READER |

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis bind-connections --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\bindings.json" --repo "rhf/spms-back=D:\repos\spms-back" --repo "rhf/consumer=D:\repos\consumer"
.\.venv\Scripts\python.exe -m diff_analysis rebuild-unified --run-dir ".\.diff-analysis\runs\<运行目录>"
~~~

bind-connections 会自动生成新的统一图和影响候选；rebuild-unified 主要用于重新生成。该连接最高只能标为 USER_ATTESTED_MATCH，**不会自动连接 ZooKeeper、MQ、HTTP 服务或生产数据库核实运行链路**。不完整、跨 SHA 或方法锚点不唯一的证据保留遗漏。

## 6. 问题式自动补证、人工审核与增量修订

从 questions.json、review_package_latest.json 查找实际 Q-* 问题；从 analysis.json 获取 D-* 变化和 G-* 组。自动提取关联方法与明确契约文件：

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis contract-context --run-dir ".\.diff-analysis\runs\<运行目录>" --repo "rhf/spms-back=D:\repos\spms-back" --repo "rhf/consumer=D:\repos\consumer"
.\.venv\Scripts\python.exe -m diff_analysis auto-evidence --run-dir ".\.diff-analysis\runs\<运行目录>" --repo "rhf/spms-back=D:\repos\spms-back" --repo "rhf/consumer=D:\repos\consumer"
~~~

contract-context 定位字段、枚举、SQL/MyBatis、消费者 if/switch 判断等**词法候选**；auto-evidence 在已索引的方法和固定版本文件上受预算取证，缺失信息不推断。手动请求精确完整方法／契约时使用 semantic-evidence-v1，或用如下 evidence-requests-v1 按行补证（问题 ID、路径、SHA 均换成真实值）：

~~~json
{
  "schema_version": "evidence-requests-v1",
  "selectors": [{
    "question_id": "Q-本次问题ID",
    "project": "rhf/spms-back",
    "sha": "<完整HEAD-SHA>",
    "side": "HEAD",
    "path": "src/main/java/com/example/PayService.java",
    "start": 10,
    "end": 70,
    "reason": "核查返回状态条件和关联调用"
  }]
}
~~~

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis supplement --run-dir ".\.diff-analysis\runs\<运行目录>" --request ".\request.json" --repo "rhf/spms-back=D:\repos\spms-back"
~~~

补证默认最多 3 轮，累计 1 MB；单次行号读取最多 500 行，路径和 SHA 严格绑定。旧证据不会因新的问题或归属修改被覆盖，已读取也不等于已证明足够。

人工／外部 AI 独立判断以 review-import-v1 文件导入：

~~~json
{
  "schema_version": "review-import-v1",
  "expected_goals": [{
    "id": "GOAL-payment-1",
    "status": "HUMAN_CONFIRMED",
    "author": "reviewer",
    "text": "旧支付成功状态仍需被消费者兼容处理"
  }],
  "decisions": [{
    "object_type": "QUESTION",
    "object_id": "Q-本次问题ID",
    "dimension": "IMPACT",
    "actor_kind": "HUMAN",
    "actor_id": "reviewer",
    "verdict": "INSUFFICIENT_EVIDENCE",
    "reason": "消费者对应的部署版本尚未验证",
    "evidence_ids": []
  }]
}
~~~

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis import-review --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\review-import.json"
~~~

人工拆分一个 Hunk、移动单元、拆分或合并候选组，使用 group-feedback-v1。下例只能在同一个未拆分的 D-* 新版行范围内使用：

~~~json
{
  "schema_version": "group-feedback-v1",
  "actions": [{
    "action": "SPLIT_HUNK",
    "diff_id": "D-本次差异ID",
    "actor": "reviewer",
    "reason": "混合两项独立需求，须分别复核",
    "segments": [{"start": 15, "count": 2, "group_id": "G-现有组ID"}]
  }]
}
~~~

~~~powershell
.\.venv\Scripts\python.exe -m diff_analysis revise-groups --run-dir ".\.diff-analysis\runs\<运行目录>" --file ".\group-feedback.json"
.\.venv\Scripts\python.exe -m diff_analysis review-status --run-dir ".\.diff-analysis\runs\<运行目录>"
~~~

人工组修订、ExpectedGoal 被新目标取代或两端绑定更新后，程序刷新关联的统一图、问题与证据计划；读取 review_package_latest.json 和 revisions/ 的状态及历史。旧版子行归属未知时保留覆盖缺口。证据依据的独立人工核对可通过 import-basis-check --file <basis-checks-v1.json> 导入，具体数据结构参见 [管线设计](PIPELINE_V4.md)。

## 7. 常见问题与验收边界

| 问题 | 检查 |
| --- | --- |
| javac、com.sun.source API 不可用 | JAVA_HOME 是否指向完整 JDK 8、tools.jar 是否存在；检查 java/javac 实际路径 |
| GitLab 401/403 | Token 对所有 MR 来源仓库的 read_api/read_repository 权限 |
| TLS 或代理失败 | 检查公司代理／内网 CA，不能为绕过问题关闭证书验证 |
| 只有 Git 底账没有 Java 结果 | 检查 stage_manifest.json 的 static_scan 和失败原因；保留部分报告并非“没有影响” |
| 找不到跨项目消费者 | 是否纳入 EVIDENCE 项目、两侧是否固定 SHA、协议／服务环境是否有完整绑定证据 |
| 外部 AI 没读完所有组 | 导出不等于实际阅读；按问题补证并保留 UNKNOWN 阅读范围 |
| 测试不通过 | 先执行 compileall/unittest；CI 任务 0 步骤失败通常还不能定位到代码 |

**未完成的验证**包括真实 Dubbo/HTTP/MQ/MyBatis 配置、字段数据流、Spring 动态代理、规模与性能、分组和消费者召回率。此工具不声称线上交易行为一致或受影响消费者完整。
