# CodeAgent 安装与使用新手指南

这份文档面向第一次接触 `lua-nil-review` 的用户。

你不需要先理解整个项目的内部实现。先把它当成一个“帮你审计 Lua nil 风险”的技能即可。它会：

- 扫描 Lua 仓库里的 `string.find(arg1, ...)` 风险。
- 自动做一部分跨函数追踪，尽量过滤噪音。
- 对第一轮还不确定的问题，再做一轮更激进的策略化 `jump/trace` 扩展。
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

### `trace_summary` 里先看什么

如果你是第一次跑，优先看下面这些字段：

- `auto_silenced`：已经被自动证明安全，所以不再进入人工队列的数量。
- `visible_after_trace`：自动追踪结束后，仍然需要人工确认的数量。
- `agentic_retraced`：有多少 `uncertain` finding 在 `claim` 前又被自动深挖了一轮。
- `agentic_improved`：二次深挖后，有多少 finding 的确定性变高了。
- `agentic_promoted_safe`：二次深挖后，又额外证明安全并静默掉了多少 finding。
- `agentic_frontier_jumps`：自动对不确定 frontier 节点补跑了多少次 `jump`。

### 如何理解结果

如果 `shards_total > 0`：

- 说明有需要人工确认的问题。

如果 `shards_total = 0`：

- 可能真的没有问题。
- 也可能是问题都被自动过滤了，比如 trace 或二次策略化 retrace 证明安全。

### 如果仓库很大，想看实时进度

你可以直接这样跑：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py refresh --progress
```

它会把阶段进度打印到 `stderr`，例如：

- 正在分析第几个文件
- 当前在做 `trace_enrichment` 还是 `building_shards`
- 已处理多少 finding
- 已补跑多少次 agentic retrace

如果你不想重新跑命令，也可以单独看状态快照：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py status
```

这时可以去看：

```text
artifacts/string-find-nil/manifest.json
```

重点关注里面的：

- `trace_summary`
- `prepare_progress`
- `candidate_overview`
- `finding_preview`

其中：

- `candidate_overview` 会告诉你当前还有多少可见 finding、其中多少已经有明确候选路径，以及最常见的候选文件路径。
- `finding_preview` 会直接给出前几个 finding 的 `candidate_summary`、`scenario_branches` 和 `why_still_uncertain`，不需要你先翻完整 shard。

### 为什么明明扫了很多文件，`shards_total` 还是 `0`

这在大仓库里很常见，不一定是异常。

原因是当前流程不是“边 trace 边切 shard”，而是：

1. 先做 `analyze`
2. 再做 `prepare -> trace_enrichment`
3. trace 都结束后，才开始真正 `building_shards`

所以在 `prepare_progress.phase = "trace_enrichment"` 时，就算已经扫了很多文件，`shards_total` 仍然可能是 `0`。

只有等 trace enrichment 完成后，系统才会开始把剩余 finding 切成 shard。

## 7. 安装后，客户应该如何和 CodeAgent 交互

这一步非常重要。

这个 skill 虽然提供了一组脚本，但它本质上是给 `CodeAgent` 使用的。

也就是说，正常情况下，客户不一定需要自己手工敲所有脚本命令。更常见的用法是：

1. 先把 skill 装到目标项目。
2. 在目标项目里打开 CodeAgent。
3. 直接用自然语言告诉 CodeAgent 你要它做什么。

### 最常见的交互方式

你可以直接对 CodeAgent 说：

```text
请使用 lua-nil-review skill 审计当前仓库，先 refresh，并告诉我自动过滤了多少噪音。
```

或者：

```text
请使用 lua-nil-review 领取下一个 shard，只基于 snippets、trace bundle 和 trace slices 审查，不要先全文搜索整个仓库。
```

或者：

```text
请用 lua-nil-review 对当前仓库做深度 nil 风险审计。先自动追踪跨函数返回值，再给我真正需要人工确认的结果。
```

### 你也可以给 CodeAgent 更明确的任务

例如：

```text
请使用 lua-nil-review skill：
1. refresh 当前仓库
2. claim 下一个 shard
3. 如果需要，使用 jump 和 trace 深挖
4. 最后生成 review.json，并准备 complete
```

### 当你想让 CodeAgent 深挖某个问题时

可以这样说：

```text
请对这个 finding 做进一步溯源，不要直接给 needs_source_escalation，先尝试 jump 和 trace。
```

或者：

```text
请检查 Config.get 这个调用到底会跳到哪些定义，并告诉我哪条路径安全、哪条路径危险。
```

### CodeAgent 收到这些话后会做什么

通常它会自己去做这些动作：

- 找到 `.codeagent/skills/lua-nil-review/scripts/run_review_cycle.py`
- 根据你的要求调用 `refresh / claim / trace / jump / complete`
- 读取 `artifacts/string-find-nil/` 下已经生成的持久化结果
- 尽量基于 shard、snippet、trace bundle 工作，而不是直接全文乱搜

### 什么时候客户需要自己手工敲命令

通常只有两种情况：

1. 你在调试环境，想确认 skill 是否安装成功。
2. 你不想通过对话，而是想自己显式控制每一步。

所以，本文后面的命令示例既可以由你手工执行，也可以作为你给 CodeAgent 的明确指令依据。

## 8. CodeAgent 内部会如何自动判定问题

很多新手第一次用这类技能时，最关心的不是“怎么执行命令”，而是：

### 这个工具到底有没有自己先判断，而不是把一堆原始报警都丢给我

答案是：有，而且默认就是这样工作的。

如果你让 CodeAgent “审计当前仓库”，它内部通常会按下面这个顺序工作。

### 先定位 skill 和目标仓库

CodeAgent 会先确认：

- 当前打开的是哪个目标仓库
- `.codeagent/skills/lua-nil-review/` 是否存在
- 当前仓库里是否有 `.lua-nil-review.json`

然后再决定后续命令怎么执行。

当前自动判定流程可以简单理解成下面 5 步。

### 第 1 步：先做本地静态分析

`refresh` 里的 `analyze` 会先扫描每个 Lua 文件，找出：

- `string.find(arg1, ...)` 这类目标调用
- 这些调用不只限于独立语句，也包括赋值右侧、`return`、`if` 条件和更深层的表达式上下文
- `arg1` 在本地作用域里的来源
- 是否有明显的 `nil`、索引访问、函数返回、guard 收窄等证据

这一步不会先把所有结果都交给人工，而是先做第一轮分类。

### 第 2 步：先做风险分级

当前默认分三档：

- `Level 1`：确定性风险
  例子：`local x = nil` 后传进 `string.find(x, ...)`
- `Level 2`：本地未保护索引
  例子：`info.user.email`
- `Level 3`：跨函数返回值或函数参数，暂时还没证明安全或危险
  例子：`local x = Utils.Get()`，`local function sink(name) string.find(name, ...) end`，或者 `string.find(mystery_name, ...)`

这一步的目的，是先把“明显危险”和“只是缺上下文”的问题分开。

### 第 3 步：自动做 trace，而不是直接甩给人工

`refresh` 里的 `prepare` 会继续做自动追踪。

它会对需要追踪的 finding：

- 自动跳到符号定义
- 自动追踪函数返回路径
- 如果 sink 参数来自函数形参，自动反向查 caller 传参链
- 如果本地分析只能得到 `unknown`，也不会直接丢弃，而是保留成 obligation，继续往 caller/callee/frontier lead 查
- 在冲突模块里分别看不同物理路径的结果
- 生成 `trace_bundle`

如果第一轮 trace 之后还是 `uncertain` 或 `budget_exhausted`，CodeAgent 在 `claim` 前还会再做一轮策略化扩展：

- 放宽 trace 深度和节点预算
- 对不确定 frontier callsite 再自动跑一次 `jump`
- 把这轮补充证据一起写回 `trace_bundle`

也就是说，像 `Utils.Get()` 这种情况，不会默认直接告诉你“请人工核实”，而是会先自己去查。

### 第 4 步：自动过滤一批你本来不该看到的噪音

自动追踪之后，工具会继续做自动判定：

- 如果 trace 证明安全：自动静默，不进人工队列
- 如果 trace 后仍然不能证明安全：保留给人工，并把已有 trace 证据一起带上
- 如果 trace 显示不同候选路径结果不同：保留分支证据，例如哪条路径安全、哪条路径危险

你可以把它理解成：

- 第一轮 trace：正常预算下的标准自动调查
- 第二轮策略化扩展：为了尽量减少人工介入，再做一次“更深一点、更宽一点”的自动确认

所以你最后看到的 shard，并不是“全量报警”，而是已经被自动筛过一轮的结果。

### 第 5 步：只把真正需要你判断的内容放进 shard

到了 `claim` 这一步，你看到的已经不是粗糙原始报警，而是：

- `message`
- `snippets`
- `trace_bundle`
- `trace_slices`
- `agentic_trace`
- `candidate_summary`
- `scenario_branches`
- `why_still_uncertain`
- `investigation_leads`

也就是说，CodeAgent 已经先完成了：

- 初步定位
- 风险分级
- 自动跨函数追踪
- 对不确定项做第二轮策略化 jump/trace 扩展
- 自动过滤明显安全项
- 多分支冲突展示

人工主要负责最后的审计结论，而不是从零开始全文搜索。

### 如果用户要求“深度审核”，CodeAgent 会怎么做

通常深度审核意味着它不会停留在“本地 maybe_nil”。

它会继续：

1. 用 `jump` 找调用目标
2. 用 `trace` 追返回路径
3. 检查是否存在多候选模块冲突
4. 比较不同候选路径的 `safe / risky / mixed`
5. 优先使用已有 `trace_bundle` 和 `trace_slices`
6. 只有证据仍不足时才扩大阅读范围

这就是为什么这个 skill 的目标不是“给你一堆报警”，而是“让 CodeAgent 先替你做一轮真正的调查”。

### 还有哪些事情仍然需要人工

虽然已经自动化了很多，但它不会替你拍板所有结论。

仍然建议人工判断的典型场景：

- 业务语义强依赖运行时环境
- 路由、配置、打包优先级只在外部系统里可见
- 现有 snippet 和 trace 证据仍不足以安全下结论

这时再使用：

- `confirm`
- `dismiss`
- `needs_source_escalation`

### 去哪里看自动判定结果

最常用的几个位置：

- `artifacts/string-find-nil/analysis/*.json`
  这里能看到 finding 的 `risk_level`、`risk_tier`、`human_review_visible`、`agentic_trace`，以及 `candidate_summary / scenario_branches / why_still_uncertain`
- `artifacts/string-find-nil/trace_bundles/*.json`
  这里能看到自动 trace 的分支结果、frontier leads，以及二次策略化扩展留下的 `agentic_strategy`
- `artifacts/string-find-nil/manifest.json`
  这里能看到 `trace_summary`，包括 `agentic_retraced` 这类统计，以及 `candidate_overview / finding_preview`
- `python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py status`
  这里能直接看到当前状态快照，不需要你手动翻完整个 `manifest.json`
- `artifacts/string-find-nil/final/summary.json`
  这里能看到最终汇总

如果你想验证“为什么这个问题没有进入人工队列”，优先看这几处。

## 9. 第四步：领取一个待处理分片

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
- `agentic_trace`
- `candidate_summary`
- `candidate_count`
- `top_candidate_paths`
- `scenario_branches`
- `why_still_uncertain`
- `investigation_leads`

这些就是技能已经帮你缩小后的证据范围。

### `agentic_trace` 是什么

这是为了让你快速看懂“CodeAgent 在 claim 前又做了什么”。

常见字段有：

- `triggered`：是否触发了第二轮策略化扩展
- `initial_overall`：第一轮 trace 的结论
- `retry_overall`：第二轮扩展后的结论
- `improved`：第二轮是否让结论更明确了
- `frontier_jump_count`：二次扩展里补跑了多少次 `jump`

如果你看到：

- `triggered = true`
- `initial_overall = "uncertain"`
- `retry_overall = "safe"`

通常就说明：这个问题本来第一轮没查清，但 CodeAgent 又继续查了一轮，并最终证明它安全，所以它可能不会再进入人工队列。

## 10. 第五步：填写 review JSON

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

## 11. 第六步：提交分片并生成最终报告

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

## 12. 最常用的完整流程

如果你只想记最核心的 3 个命令，就记这个：

```bash
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py refresh
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py claim
python .codeagent/skills/lua-nil-review/scripts/run_review_cycle.py complete --review-json review.json
```

## 13. 推荐先加一个最小配置文件

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

如果你后面想进一步调“claim 前自动深挖”的力度，可以再去看这些配置：

- `agentic_retrace_enabled`
- `agentic_retrace_depth_bonus`
- `agentic_retrace_max_branch_count`
- `agentic_retrace_max_expanded_nodes`
- `agentic_frontier_jump_limit`

如果你完全不确定怎么配，可以先不写配置，直接跑默认值。

## 14. 风险分级怎么理解

当前默认是三档：

- `Level 1`：确定性风险。比如直接传了 `nil`，或者本地 table 明确缺这个 key。
- `Level 2`：本地上下文里看起来没有保护的索引访问。
- `Level 3`：跨函数返回值或参数来源，先自动追踪，再决定能不能证明安全。

新手最重要的理解是：

### 不是所有“可能”都会直接留给你判断

工具会先自动做 trace。

如果第一次还不够，它还会自动再做一轮更激进的策略化扩展。

只有那些被 trace 证明安全的 finding 才会自动静默。

剩下没有被证明安全的 finding，即使只是低置信度，也会继续进入人工 review，并附带 trace 证据，避免因为上下文缺失而直接漏报。

这就是为什么有时你能在 `analysis/` 里看到 finding，但 `claim` 却拿不到 shard。

## 15. 什么时候用 `jump` 和 `trace`

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

## 16. 常见问题排查

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
- 第一轮 trace 不够，但第二轮策略化 retrace 已经把剩余问题证明安全了。
- 当前这轮里剩余 finding 都已经被确认安全，所以没有需要人工处理的 shard。

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

## 17. 推荐阅读顺序

如果你已经能跑通，再按这个顺序继续：

1. [configuration.md](configuration.md)
2. [workflow.md](workflow.md)
3. [architecture.md](architecture.md)

如果你要理解为什么工具能自动过滤跨函数噪音，再看：

4. [lua_symbol_tracing_design.md](lua_symbol_tracing_design.md)
