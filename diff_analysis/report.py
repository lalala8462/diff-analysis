"""Evidence-first Markdown; never invent business conclusions."""
from pathlib import Path
from urllib.parse import quote

def code(value):
    tick = chr(96)
    return tick + str(value) + tick

def loc(meta, file, line, sha=None):
    if not file:
        return 'unknown'
    shown = '%s:%s' % (file, line or '?')
    if not meta.get('url'):
        return code(shown)
    base = meta['url'].split('/-/merge_requests/')[0]
    url = base + '/-/blob/' + (sha or meta['head']) + '/' + quote(file, safe='/')
    if line:
        url += '#L' + str(line)
    return '[' + shown + '](' + url + ')'

def markdown(results, cross_candidates=None, meta=None):
    meta = meta or {}
    cross_candidates = cross_candidates or []
    lines = ['# 多 MR 联合代码影响评审报告', '',
             '> 结果来自指定 Git SHA 的静态分析；调用候选不等于实际运行依赖。', '',
             '## 一、项目与版本', '',
             '| 仓库 | 基线 SHA | 目标 SHA | 改动文件 | 改动方法 | 解析/采集状态 |',
             '| --- | --- | --- | ---: | ---: | --- |']
    for r in results:
        state = '不完整（需人工处理）' if r['incomplete'] else '扫描完成（静态范围）'
        lines.append('| %s | %s | %s | %s | %s | %s |' %
                     (code(r['project']), code(r['base'][:12]), code(r['head'][:12]),
                      len(r['changed_files']), len(r['changed_methods']), state))
    lines += ['', '## 二、本次主要代码变化', '']
    for r in results:
        lines.append('### ' + r['project'])
        meta_r = meta.get(r['project'], {})
        if not r['changed_files']:
            lines.append('- 没有检测到文件变更。')
        for f in r['changed_files'][:150]:
            lines.append('- %s %s' % (code(f['status']), loc(meta_r, f['path'], None)))
        if len(r['changed_files']) > 150:
            lines.append('- 超出展示限制，另有 %s 个文件；完整列表应通过 JSON 输出检查。' % (len(r['changed_files'])-150))
        lines.append('')
    lines += ['## 三、基于静态调用链的影响路径', '',
              '每条路径由变更方法向其上游调用者展开；并不证明业务行为真的改变。', '']
    for r in results:
        lines.append('### ' + r['project'])
        paths = [(sym, path) for sym, path in r['impact_paths'].items() if len(path) > 1]
        for sym, path in sorted(paths, key=lambda x: (len(x[1]), x[0]))[:80]:
            caller = r['methods'].get(sym) or r['old_methods'].get(sym)
            lines.append('- **%s**：%s' % (loc(meta.get(r['project'], {}), caller.get('path'), caller.get('start')) if caller else sym, code(' ← '.join(path))))
        if not paths:
            lines.append('- 没有解析到上游调用方法；这**不代表没有影响**。')
        if len(paths) > 80:
            lines.append('- 其余 %s 条路径省略展示。' % (len(paths)-80))
        lines.append('')
    lines += ['## 四、重点人工评审项', '']
    any_finding = False
    for r in results:
        for issue in r['api_issues']:
            if issue['status'] == 'BUMPED':
                continue
            any_finding = True
            label = '高优先级：API 版本未变化' if issue['status'] == 'NOT_BUMPED' else '待核实：无法识别 API 模块版本'
            lines.append('- **%s**（%s）：检测到 API 相关源码变化，当前版本 %s → %s。需要按公司规则核对 API Jar/父 POM/版本管理及消费者兼容性。关联文件：%s' %
                         (label, code(r['project']), code(issue['old_version']), code(issue['new_version']),
                          ', '.join(loc(meta.get(r['project'], {}), x, None) for x in issue['paths'][:10])))
    for c in cross_candidates[:60]:
        any_finding = True
        lines.append('- **跨项目候选（待确认）**：%s 中 %s 调用了与 %s 修改方法名称/参数数量一致的调用。候选目标：%s。仅有静态候选信息，必须检查具体类型与实现。' %
                     (code(c['caller_project']), loc(meta.get(c['caller_project'], {}), c['path'], c['line']),
                      code(c['callee_project']), code(c['callee'])))
    for r in results:
        for f in r['changed_files']:
            suffix = Path(f['path']).suffix.lower()
            if suffix in ('.sql', '.xml', '.yml', '.yaml', '.properties', '.vue', '.css', '.scss', '.js', '.ts', '.tsx'):
                any_finding = True
                lines.append('- **资源/配置变更需专项分析（待确认）**：%s：%s。当前 P0 尚未建立此类型的精确影响图。' %
                             (code(r['project']), loc(meta.get(r['project'], {}), f['path'], None)))
    if not any_finding:
        lines.append('- 未触发当前已实现的专项规则；这不是安全或无遗漏证明。')
    lines += ['', '## 五、未确认范围与分析局限', '']
    for r in results:
        lines.append('- %s：新版本未解析的调用点 %s 个，旧版本 %s 个；不可据此断言没有调用关系。' %
                     (code(r['project']), len(r['unresolved']), r['old_unresolved_count']))
        for w in r['warnings'][:25]:
            lines.append('  - 扫描警告：%s' % code(w))
    lines += ['- 本阶段仅有 JDK AST 静态语法分析，未完成类路径解析、动态代理、反射、Dubbo/ZK、RabbitMQ、Redis 和数据库字段级血缘采集。',
              '- 仅分析输入 MR/显式附加的仓库；**未输入且未纳管的项目不会自动被发现**。',
              '- 未模拟多个 MR 的最终合并树、未校验跨仓库 API 二进制兼容性；需要人工确认候选版本的组合。',
              '- 所有结果仅用于人工评审，不可替代回归测试或自动发布决策。', '']
    return '\n'.join(lines)
