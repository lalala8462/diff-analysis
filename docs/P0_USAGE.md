# P0 命令行使用说明

当前版本为**可运行的多 MR 静态分析器**，不是 AI 自动评审服务或 Web UI。依赖 Python 3.9+、Git、JDK 8+（必须包含 javac）。

- **Windows PowerShell 原生运行**：请阅读 [Windows 完整接入指南](WINDOWS_SETUP.md)。
- Linux/macOS：安装后可使用以下命令。

```bash
git clone https://github.com/lalala8462/diff-analysis.git
cd diff-analysis
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

## 本地固定 SHA 评审

```bash
python3 -m diff_analysis review-local \
  --repo /path/to/java-project \
  --base <完整基线CommitSHA> \
  --head <完整目标CommitSHA> \
  --output report.md --json-output review.json
```

可追加 `--catalog /path/to/another-project@<完整CommitSHA>`，只读扫描一个未修改项目以发现跨项目**候选**方法调用。

## 自建 GitLab 多 MR

```bash
read -rsp "GitLab Token: " GITLAB_TOKEN; echo
export GITLAB_TOKEN
python3 -m diff_analysis review-mrs \
  --allowed-host gitlab.example.cn \
  --mr 'https://gitlab.example.cn/team/a/-/merge_requests/123' \
  --mr 'https://gitlab.example.cn/team/b/-/merge_requests/456' \
  --output report.md --json-output review.json
unset GITLAB_TOKEN
```

权限需要 `read_api` 与 `read_repository`。源码按 commit SHA 锁定版本读取，不执行待分析仓库的构建或测试脚本。GitLab HTTPS 自签证书需正确安装公司 CA，不应关闭验证；仅隔离内网 HTTP 可明确加 `--allow-http`（不推荐）。

**跨平台 Git 私有认证**：Windows 使用临时 `.cmd` askpass，POSIX 使用临时 `.sh` askpass，均交给 Python 读取进程环境中的 Token，不在脚本、URL 或命令参数中明文传 Token。

## 当前已实现与未实现

已实现：多 MR 元数据和源码拉取、完整 Git Diff、Java 方法/调用语法树、旧新调用边合并、反向调用方、API 版本基础检查、固定 SHA catalog、Markdown/JSON 报告。Windows/Linux 路径及 UTF-8 基础适配和跨平台测试。

未实现：真实 MR 多合并树冲突验证、全量 Spring/Dubbo 解析、数据库 SQL 字段级分析、ZooKeeper/RabbitMQ/Redis/ES 动态依赖图谱、长期持久化图谱、AI 业务语义评审、GitLab 评审批注、Web UI。

静态调用不可证明兼容性；未纳管项目和历史未知依赖不会被自动判定为无影响。
