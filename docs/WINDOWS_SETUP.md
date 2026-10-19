# Windows 原生安装与 GitLab MR 接入（PowerShell 5.1 / 7）

> 适用：Windows 10/11，**不需要 WSL、Docker、MySQL**。当前版本为 P0 命令行分析工具，没有 Web 界面或 LLM 自动业务评审。按本文执行后生成 `report.md` 和 `review.json`。

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

## 4. 通过多个 GitLab MR 运行联合分析

把下方域名、项目路径和 MR 编号换成真实值：

```powershell
.\.venv\Scripts\python.exe -m diff_analysis review-mrs `
  --allowed-host gitlab.example.cn `
  --mr "https://gitlab.example.cn/team/spms-back/-/merge_requests/101" `
  --mr "https://gitlab.example.cn/team/adpts/-/merge_requests/52" `
  --output ".\report.md" `
  --json-output ".\review.json"
```

- `--allowed-host` 只写 GitLab **域名**（例如 `gitlab.example.cn`），不要写 `https://` 或仓库路径。
- `--mr` 可以重复多次，输入同一需求对应的多个项目 MR。
- GitLab MR URL 必须匹配 `https://主机/组/项目/-/merge_requests/编号`；不接受账号密码或 Token 写在 URL 中。
- 内网 GitLab 若只能使用 HTTP，必须显式附加 `--allow-http`。这会明文传输凭据，**仅在已确认可信的隔离网络中使用**，优先启用 HTTPS。
- 支持将未修改、但可能是调用方的**本地其他仓库**加入静态扫描：重复追加 `--catalog "D:\repos\other-service@<完整CommitSHA>"`，该仓库必须已经存在且含有对应 SHA。

分析成功后生成：

| 文件 | 内容 |
| --- | --- |
| `report.md` | 人工阅读的 Markdown 变更报告（受影响方法、调用链、API 版本规则、待确认跨项目调用候选） |
| `review.json` | 机器可读的结构化报告，便于后续对接 AI、Web 页面或 CI |

输出文件是当前工作目录中的路径。可用 VS Code、IDEA 或 Markdown 预览器打开：

```powershell
code .\report.md
```

运行完成后清理当前会话的 Token：

```powershell
Remove-Item Env:\GITLAB_TOKEN -ErrorAction SilentlyContinue
```

## 5. 不依赖 GitLab：先分析本地 Git 项目

需要一对**完整 Commit SHA**（不是分支名或短 SHA）：

```powershell
git -C "D:\repos\spms-back" rev-parse main
git -C "D:\repos\spms-back" rev-parse my-feature-branch

.\.venv\Scripts\python.exe -m diff_analysis review-local `
  --repo "D:\repos\spms-back" `
  --base "<完整40位CommitSHA>" `
  --head "<完整40位CommitSHA>" `
  --output ".\local-report.md" `
  --json-output ".\local-review.json"
```

该方式很适合先校验 Java 解析、反向调用图及报告格式，无需 GitLab Token。

## JDK 8 下 AST 扫描器编译失败的处理

如果测试报错类似：

    package com.sun.source.tree does not exist
    package com.sun.source.util does not exist
    cannot find symbol: TreePathScanner

说明当前 JDK 编译器 API 没有被正确找到。JDK 8 的相关 API 通常位于完整 JDK 的 lib/tools.jar 中；JRE 目录没有这个文件。

本次已修正：自动判断 javac 版本；使用 JDK 8 时自动把 tools.jar 加入编译与运行 classpath；找不到时输出可操作的错误信息。**推荐让分析工具独立使用 JDK 17，即使被分析项目仍用 Java 8。**

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

**注**：Windows 的 javac 报错原先还可能出现中文乱码；修正后的编译命令统一按 UTF-8 输出诊断。

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
| PowerShell 续行错误 | 反引号 `\` 必须是每个续行的**最后一个字符**，后面不要有空格 |

## 7. 当前限制与安全边界

- P0 能分析多 MR，但**尚不验证多个 MR 临时合并后的组合树和冲突**。
- 尚不自动接入 ZK、RabbitMQ、Redis、ES 或完整数据库字段级依赖。
- **不会自动发现所有未知程序 D**。可手动用 `--catalog` 纳入未修改仓库；跨项目按方法名匹配只算候选关系。
- 尚无 AI 业务语义审查 / Web UI。报告只代表当前可见代码和静态证据，不能宣称完整或安全。
- 目前 Windows 功能有自动化 CI 覆盖；**实际公司内网 GitLab 的 Token、TLS、代理仍需在你的环境中测试**。
