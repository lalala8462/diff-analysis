# 四阶段代码评审与遗留项

评审对象：`lalala8462/diff-analysis` 的四阶段首期静态分析代码（固定 SHA 索引、SDK 契约、数据库表、Redis/反射、统一资源报告与 ReviewUnit 修订）。本次评审修订依据为 2026-10-27 的 main；相关代码修复直接合并到 main，不拆出长期开发分支。

## 评审结论

当前实现可以作为**待人工核对的线索生成器**，不能宣称精确跨项目依赖或运行时兼容性验证。主要风险集中在候选的证据身份、源码词法误识别、缓存覆盖声明及人工归属失效。没有访问实际数据库、Redis、Spring 容器或执行业务代码。

| 严重度 | 模块 | 已确认问题 | 修复与影响 |
| --- | --- | --- | --- |
| 高 | SDK 契约 | 每个变化成员复用整个 SDK 的 `changes` 列表，可能把无关 D-* 归入同一契约成员和错误 ReviewUnit | 将成员声明旧/新行与 D-* Hunk 区间及路径逐一匹配；不重叠时记录 `SDK_MEMBER_DIFF_RANGE_UNMAPPED`，不生成伪关联 |
| 高 | Java 词法候选 | Java 字符串或日志文本中的 `dto.method()`、`redisTemplate.get()`、`getBean()` 可以被当成真实使用 | 字符串/字符常量和注释按顺序屏蔽，避免伪调用；保留原始源码的可审核位置 |
| 高 | 资源修订 | 同一固定资源可能由多条独立候选产出相同 `R-*`，后续 ID→候选映射可能覆盖前一项 | 在规范化阶段按完整资源身份合并来源 D-* 和消费者位置，并保留不一致状态为待确认 |
| 中 | Java 源码字符串 | Redis Key 字面量含 `//` 时，旧注释正则可能截断其内容 | 先识别 Java 字符串/字符常量，再剔除真正的注释，避免漏报合法 Key |
| 中 | SDK 参数解析 | 方法泛型内部逗号可能被当成方法参数分隔符 | 用尖括号平衡状态拆分顶层参数，失衡时标记不可解析 |
| 中 | SDK 扫描成本 | Maven 检查中扫描 SDK 全部 Java 类型构造的 `types` 集合未被使用 | 删除无用的整库读取，减轻首次运行开销 |
| 中 | 源码索引覆盖 | digest 校验通过但 `file_count` 不一致可被视为有效缓存；符号链接跳过无记录 | 校验元数据计数/截断状态，对未索引源码类型的符号链接写 omissions |

## 各阶段检查点

### 0. 固定 SHA 索引

- 校验 Git 输入 SHA、扩展名和 Windows 安全路径。
- 校验缓存索引身份、结构、文件计数与截断状态。
- 底层文件事实不因为 ReviewUnit 拆分而重新扫描；扫描缺口显式可见。
- **仍需**：大规模仓库耗时/空间基准、Git 对象读取失败的故障注入及缓存隔离测试。

### 1. SDK 契约

- Maven 声明版本检查不因单纯 `import` 自动认定契约受影响。
- 首期仅识别简单类方法、DTO 字段/序列化名称和枚举常量；同名且同参数数量的复杂重载不能确证。
- D-* 需要与实际变化成员位置对应；注释、字符串、无关成员变更不得被算作使用。
- **仍需**：以 JDK 8 javac 对声明与消费者进行精确绑定；继承/接口默认方法、泛型擦除、重载、注解参数、多类同文件与跨路径重命名等场景。词法结果只能写 `TYPE_SCOPED_LEXICAL_CANDIDATE`。

### 2. 数据库表

- MyBatis XML、SQL/DDL、有限的 Java 字符串 SQL 和显式表映射按固定 SHA 收集读写位置；相同表名只是关联候选。
- 修复确认的 `DELETE FROM` 误分类：原正则在 `DELETE FROM` 之前先匹配 `FROM`，可能将 DELETE 解析为 READ。已调整正则备选顺序、增加回归用例，并提升数据库解析规则版本使旧缓存失效。
- Hunk 与 SQL 片段重叠只能说明语句受到修改，不代表表结构必然有不兼容变更。
- **仍需**：SQL AST、跨行复杂 Java SQL、MyBatis include/choose/foreach、视图/别名、schema/数据源路由及字段级血缘。不同实例同名表必须保持待确认。

### 本轮追加修复：缓存 Key 精确度与证据截断

- `@Cacheable(cacheNames="...")` 未显式指定 `key` 时，实际 Key 由 Spring 默认生成器确定，不能把命名空间前缀误认成精确 Key。此处改为非精确前缀候选，记录 `CACHE_KEY_GENERATED_UNRESOLVED`。
- 同一跨项目数据库表或 Redis/反射资源候选的源码使用位置超过单候选展示上限时，增加 `TABLE_EVIDENCE_LOCATIONS_TRUNCATED` / `RESOURCE_EVIDENCE_LOCATIONS_TRUNCATED`，不能把前 50/60 条误当成全部命中。
- 新增 `DELETE FROM` 与 SQL 字符串伪表引用、缺省 Spring 缓存 Key 的回归用例；最新修改后**没有成功运行完整测试**。CI 仍在 Job 执行零步骤时失败，无法据此得出代码通过结论。

### 3. Redis / 反射 / Spring

- Key 字面量、简单常量/前缀、部分缓存注解、Bean XML/注解、反射名称记录为静态候选。
- DTO 字段变化与 Redis 写入值的关联只有局部变量类型线索，不等于编码器/序列化约定已解析。
- **仍需**：RedisTemplate 对象和实例/DB 身份，Spring 默认 Bean 名推断、Bean alias，复杂 SpEL，常量跨类传播、反射重载参数签名、缓存 TTL 单位与序列化 codec。
- 字符串与注释保护能减少明显误报，但不构成完整 Java 语义解析。

### 4. 统一报告与修订失效

- `resource_impact_latest.json` 是固定源码事实导出的派生候选，`resource_review_latest.json` 是当时 ReviewUnit 的归属叠加。
- 重复 `R-*` 合并后的 D-* 不丢失；问题 ID / 依赖指纹随组归属和事实内容而变。
- 拆分只给出新版行范围时，旧版资源位置不能凭空分配到新组。
- **仍需**：多侧字段关联精确失效、超预算的增量重算统计、旧审核判断在跨多轮修订及重命名时的完整验证、首页冗余展示收敛。

## 测试与验收状态

现有单元测试覆盖部分固定 SHA、SDK/SQL/缓存名称/修订示例，并增加缓存元数据、字符串伪使用、泛型参数、成员级 D-* 及重复 R-* 的回归用例。

**并未完成整个仓库的可靠测试验收。** 在本次评审中，本地仅运行了阶段 1、2 的既有隔离样例，共 8 个通过；这不等同于对最新修复后的 main 做了完整测试。GitHub Actions 的 Windows/Ubuntu Job 多次在零步骤执行情况下结束失败，暂不能把该结果归为代码错误或代码通过。应修复 Runner/工作流启动问题后重新跑全量测试。

Windows / Python 3.9+ / JDK 8 检查：

```powershell
java -version
javac -version
.\.venv\Scripts\python.exe -m compileall -q diff_analysis tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

建议之后以**实际固定 SHA 仓库**验收 SPMS 缓存 DTO→WebAPI/CPS 对应 Key、ADPTS `trans_record`→WebAPI 访问、SDK 方法/枚举旧用法，以及包含同名不同数据源、反射别名和无关 import 的负面样例。为每类保存输入 SHA、确认的正确答案、命中/漏报、标记等级和 omissions，不以候选数量计算准确率。

## 数据契约边界

- `D-*`：Git 最终净差异底账，永不回填推断。
- `R-*`：资源类型、生产方、消费者、资源名称和固定 SHA 确定的静态候选，可含多个 D-*，不等于已验证连接。
- `ReviewUnit`：人工归属叠加。拆组后重新生成派生问题，但不改变 Git 对象或已导出字节证据。
- `omissions`：扫描或身份解析不完整。零候选在任何情况下都**不**证明零影响。
