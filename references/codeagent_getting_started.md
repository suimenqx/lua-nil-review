# CodeAgent 安装与使用新手指南

这份文档面向第一次接触 `lua-nil-review` 的用户。

你不需要先理解整个项目的内部实现。先把它当成一个“帮你审计 Lua nil 风险”的技能即可。它会：

- 扫描 Lua 仓库里的 `string.find(arg1, ...)` 风险。
- 自动做一部分跨函数追踪，尽量过滤噪音。
- 把待处理问题切成小片段，而不是逼你一次读完整个大仓库。

如果你只想尽快跑通，按本文一步一步做就可以。

## 1. 你会得到什么

这个技能最终会在你的目标仓库里生成一套审计产物，默认位置是：

```text
artifacts/string-find-nil/
```

常见文件包括：

- `analysis/`：每个 Lua 文件的原始分析结果。
- `findings/`：当前待人工处理的问题分片。
- `reviews/`：你填写后的 review JSON。
- `trace_bundles/`：自动跨函数追踪的结果。
- `final/report.md`：最终 Markdown 报告。
- `final/summary.json`：最终 JSON 汇总。

## 2. 开始前准备

你需要准备两样东西：

1. `CodeAgent` 命令行本身可用。
2. `python` 可用，并且能安装 Python 依赖。

先确认：

```bash
codeagent --help
python --version
```

如果这两个命令都能正常输出，再继续。

## 3. 你需要区分两个目录

第一次使用最容易搞混的是“技能源码目录”和“目标项目目录”。

### 技能源码目录

就是这个 `lua-nil-review` 仓库本身，例如：

```text
/path/to/lua-nil-review
```

### 目标项目目录

就是你真正想审计的 Lua 仓库，例如：

```text
/path/to/your-lua-project
```

后面的安装动作，是把“技能源码目录”接入“目标项目目录”。

## 4. 第一步：安装 Python 依赖

先进入技能源码目录，安装依赖：

```bash
cd /path/to/lua-nil-review
python -m pip install -r requirements.txt
```

如果你之后是通过 `.codeagent/skills/lua-nil-review/` 里的脚本来运行，也要确保运行这些脚本时使用的是同一个 Python 环境。

如果你看到类似下面的报错：

```text
Missing dependency 'luaparser'
```

基本就是这一步没有做，或者 `python` 不是同一个环境。

## 5. 第二步：把技能接入目标项目

推荐使用 workspace 级别安装，这样最直观。

进入你的目标项目目录：

```bash
cd /path/to/your-lua-project
```

然后执行：

```bash
codeagent skills link /path/to/lua-nil-review --scope workspace
```

如果你不想用 link，也可以用 install：

```bash
codeagent skills install /path/to/lua-nil-review --scope workspace
```

### 安装成功后会发生什么

通常会在目标项目目录下出现：

```text
.codeagent/skills/lua-nil-review/
```

你可以手动确认：

```bash
ls .codeagent/skills/lua-nil-review
```

如果没看到这个目录，执行：

```bash
codeagent skills list --all
```

看实际安装路径，再去找它。

## 6. 第三步：先跑一次最简单的分析

如果你当前就在目标项目目录，直接执行：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py refresh
```

这是最适合新手的入口。

它会自动做两件事：

1. `analyze`：扫描 Lua 文件并生成分析结果。
2. `prepare`：做自动追踪、过滤噪音、准备 review shard。

### 你会看到什么

命令会输出一段 JSON，大致包含：

- `analyze`
- `prepare`

重点看这几个值：

- `prepare.shards_total`
- `prepare.suppressed_findings`
- `prepare.trace_summary`

### 如何理解结果

如果 `shards_total > 0`：

- 说明有需要人工确认的问题。

如果 `shards_total = 0`：

- 可能真的没有问题。
- 也可能是问题都被自动过滤了，比如 trace 证明安全。

这时可以去看：

```text
artifacts/string-find-nil/manifest.json
```

重点关注里面的 `trace_summary`。

## 7. 第四步：领取一个待处理分片

继续在目标项目目录执行：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py claim
```

如果有待处理问题，你会拿到一个 JSON，里面通常有：

- `claim.status = "claimed"`
- `claim.shard_id`
- `claim.review_template_path`
- `claim.findings`

### 你真正要看什么

新手最重要的一点：

不要一上来就自己打开整个仓库到处找。

先看 `claim.findings` 里已经带出来的内容：

- `message`
- `snippets`
- `trace_bundle`
- `trace_slices`

这些就是技能已经帮你缩小后的证据范围。

## 8. 第五步：填写 review JSON

`claim` 输出里会给你一个模板路径，类似：

```text
artifacts/string-find-nil/reviews/<shard_id>.template.json
```

打开它，补上结论。

最小示例：

```json
{
  "shard_id": "abc123",
  "reviewer": "codeagent",
  "summary": "This shard contains one real nil-risk finding.",
  "finding_reviews": [
    {
      "finding_id": "finding-id",
      "decision": "confirm",
      "rationale": "The value can be nil when it reaches string.find.",
      "severity": "medium"
    }
  ]
}
```

### `decision` 可以填什么

- `confirm`
- `dismiss`
- `needs_source_escalation`

### 什么时候用 `needs_source_escalation`

当你已经看完：

- snippet
- trace bundle
- trace slices

但仍然无法安全下结论时，再用它。

不要一开始就用，也不要跳过已有证据直接去翻整个大文件。

## 9. 第六步：提交分片并生成最终报告

假设你把 review JSON 存成：

```text
/path/to/your-lua-project/review.json
```

执行：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py complete --review-json review.json
```

这个命令会：

1. 完成当前 shard。
2. 自动执行 merge。
3. 生成最终结果。

最终重点看两个文件：

- `artifacts/string-find-nil/final/report.md`
- `artifacts/string-find-nil/final/summary.json`

## 10. 最常用的完整流程

如果你只想记最核心的 3 个命令，就记这个：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py refresh
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py claim
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py complete --review-json review.json
```

## 11. 推荐先加一个最小配置文件

在目标项目根目录创建：

```text
.lua-nil-review.json
```

新手最常见的最小配置例子：

```json
{
  "exclude": ["vendor/**", "third_party/**"],
  "symbol_tracing": {
    "module_resolution_priority": ["src/ui", "src/common"]
  }
}
```

### 这个配置是做什么的

- `exclude`：跳过第三方目录，减少噪音和耗时。
- `module_resolution_priority`：当 `require("config")` 这种名字在多个路径下都存在时，优先按你给的目录前缀解析。

如果你完全不确定怎么配，可以先不写配置，直接跑默认值。

## 12. 风险分级怎么理解

当前默认是三档：

- `Level 1`：确定性风险。比如直接传了 `nil`，或者本地 table 明确缺这个 key。
- `Level 2`：本地上下文里看起来没有保护的索引访问。
- `Level 3`：跨函数返回值，默认先不打扰人，先自动追踪。

新手最重要的理解是：

### 不是所有“可能”都会扔给你

工具会先自动做 trace。

只有那些 trace 之后仍然可疑的 `Level 3` 才会真正进入人工 review。

这就是为什么有时你能在 `analysis/` 里看到 finding，但 `claim` 却拿不到 shard。

## 13. 什么时候用 `jump` 和 `trace`

当你已经开始使用后，这两个命令非常有用。

### 跳到定义

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py jump --file foo.lua --line 88 --expr Config.get
```

适合场景：

- 你想知道某个调用到底可能落到哪个函数。
- 你怀疑有多个同名模块冲突。

### 继续追踪

按 finding 追踪：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py trace --finding-id <finding_id>
```

按调用点追踪：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py trace --file foo.lua --line 88 --expr Config.get
```

适合场景：

- 你想看返回值是怎么一路传到 `string.find` 的。
- 你想知道冲突路径里，哪条分支是安全的，哪条分支是危险的。

## 14. 常见问题排查

### 看不到 `.codeagent/skills/lua-nil-review`

处理方法：

```bash
codeagent skills list --all
```

先确认是否真的安装成功，再去找对应目录。

### 报错 `Missing dependency 'luaparser'`

处理方法：

```bash
python -m pip install -r requirements.txt
```

如果你是在目标项目里通过 `.codeagent/skills/...` 运行脚本，确保这个 `python` 和你安装依赖时用的是同一个环境。

### `claim` 返回 `empty`

这不一定是坏事。

可能原因：

- 没有 active finding。
- finding 都被 suppress 了。
- trace 已经自动证明安全并过滤了。
- Level 3 finding 没 trace 成 `risky/mixed`，所以没进入人工队列。

先看：

```text
artifacts/string-find-nil/manifest.json
```

以及：

```text
artifacts/string-find-nil/analysis/
```

### 为什么 `refresh` 后没有最终报告

因为 `refresh` 只做：

- 分析
- 追踪
- 分片

不会自动完成人工 review。

最终报告通常在你执行 `complete --review-json ...` 之后出现。

### 我应该先看哪个文件

按这个顺序：

1. `claim` 命令输出的 `findings`
2. `snippets`
3. `trace_bundle`
4. `trace_slices`
5. 只有证据还不够时，才去看原始 Lua 文件

## 15. 推荐阅读顺序

如果你已经能跑通，再按这个顺序继续：

1. [configuration.md](configuration.md)
2. [workflow.md](workflow.md)
3. [architecture.md](architecture.md)

如果你要理解为什么工具能自动过滤跨函数噪音，再看：

4. [lua_symbol_tracing_design.md](lua_symbol_tracing_design.md)
