# P0 快速运行与实际边界

本阶段已实现可运行的命令行分析器，不是完整的 AI 自动评审服务。环境依赖：Python 3.9+、Git、JDK 8+（需要 javac）。

## 1. 本地代码差异验证

在仓库根目录执行：

    python -m unittest discover -s tests -v
    python -m diff_analysis review-local \
      --repo /path/to/java-project \
      --base <完整基线CommitSHA> \
      --head <完整目标CommitSHA> \
      --output report.md --json-output review.json

产生 Markdown 和 JSON；可以对某个未修改的本地项目指定固定 SHA 添加只读静态检查：

    --catalog /path/to/another-project@<完整CommitSHA>

## 2. 内网自建 GitLab MR

为避免把私有 GitLab 的账号密码放进 URL 或配置文件：

    export GITLAB_TOKEN='<具有read_api/read_repository权限的令牌>'
    python -m diff_analysis review-mrs \
      --allowed-host gitlab.example.cn \
      --mr 'https://gitlab.example.cn/team/a/-/merge_requests/123' \
      --mr 'https://gitlab.example.cn/team/b/-/merge_requests/456' \
      --output report.md --json-output review.json

源码获取按 commit SHA 固定版本，不执行仓库构建或测试脚本；无法读取 MR 的 base/head 提交时直接报错，不静默分析当前默认分支。
内网 GitLab 如果仅支持 HTTP，必须主动设置 --allow-http；不要在不安全网络使用明文 Token。

## 3. 当前实现的能力

- 多 MR 输入、GitLab MR 元信息解析、固定 base/head SHA 的源码读取及完整 Git Diff。
- Java 语法 AST 提取类、方法、调用点，分析方法级改动，反向定位上游调用者。
- 将旧版和新版调用关系合并，避免漏掉已删除的调用边。
- 检查 API 类变更是否伴随可识别的 Maven 主模块版本变化。
- 可选输入固定 SHA 的额外仓库，识别跨仓库的潜在调用方。
- 生成带代码位置的 Markdown/JSON 报告，标记未解析的调用及静态分析局限。
- 不依赖外部大模型；所有高风险结论保留事实证据和不确定性。

## 4. 还没有实现（后续开发）

- [ ] 多 MR 真正临时合并后的候选树冲突/组合验证。
- [ ] Spring/Dubbo 完整依赖解析、MyBatis SQL 字段和条件级分析。
- [ ] ZooKeeper、RabbitMQ、Redis、ES 的动态依赖基线采集与反查。
- [ ] 长期持久化版本图谱、MySQL / Neo4j 管理服务与 Web UI。
- [ ] AI 按需读取关联代码、总结主要业务行为、验证潜在遗漏。
- [ ] GitLab 行级评论、自动化测试关联和发布门禁。

特别说明：未知 D 或未输入的项目暂时不会自动发现；跨仓库方法名匹配只能标记为候选关系，不能当作确定调用。部分分析失败必须保留为未知风险。
