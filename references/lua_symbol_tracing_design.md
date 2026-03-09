# Lua Nil Review 跨函数符号追踪设计文档

## 1. 文档目标

本文面向 `lua-nil-review` 项目，回答 `REQUIREMENTS_LUA_SYMBOL_TRACING.md` 提出的需求，给出一个与当前代码结构兼容、可渐进落地、并且满足大规模 Lua 仓库审计约束的最优设计方案。

本文的核心结论只有一句话:

**最优方案不是把 `lua-nil-review` 重写成全程序解释器，而是在现有 persisted review workflow 上新增一层“仓库级符号智能层”，由它提供冲突感知的模块索引、精准函数切片、可持久化的 trace bundle，以及有预算约束的 3-5 层主动溯源能力。**

---

## 2. 对现有 `lua-nil-review` 的理解

在设计之前，必须先明确当前项目真实的能力边界。

### 2.1 当前项目已经做得很好的部分

从 `references/architecture.md`、`references/workflow.md`、`workflow.py`、`state.py`、`tests/test_pipeline.py` 可以确认，`lua-nil-review` 已经具备一套非常适合扩展的审计骨架:

1. `run_review_cycle.py` 提供稳定的高层入口，工作流是持久化的。
2. `workflow.py` 已经具备增量复用、分片、锁、心跳、恢复、merge 等能力。
3. `analyzer.py` 已经能对单文件执行 AST 级 nil 流分析，并生成短 snippet，而不是强迫审计者读整个大文件。
4. 当前 artifact 目录已经天然适合继续挂载更多“可恢复、可缓存、可增量”的中间产物。

### 2.2 当前项目的关键限制

当前实现仍然是一个**单文件、单规则、局部数据流**分析器，典型限制如下:

1. 只关注 `string.find(arg1, ...)`。
2. `_eval_expression()` 对函数返回值一律近似为 `maybe_nil`，没有跨函数返回值追踪。
3. 没有仓库级符号表，也没有“Jump to Definition”。
4. 没有模块冲突感知能力，无法应对扁平化 `require("config")` 的多路径候选。
5. snippet 以“事件行邻域”为中心，不具备“函数逻辑切片”能力。

### 2.3 为什么最优方案必须复用现有 workflow

需求里的真正难点，不是“能否找到函数定义”，而是:

1. 如何在 3000+ 文件场景下长期复用结果。
2. 如何在多轮 agent 调用中保持链路上下文不丢。
3. 如何只给 agent 喂最小必要代码，而不是重新打开整仓库。

这三点，`lua-nil-review` 现有的 persisted workflow 恰好已经解决了 70% 的基础设施问题。因此最佳设计应该是:

1. 保留现有 `analyze -> prepare -> claim -> complete -> merge` 主脉络。
2. 在 `analyze` 阶段旁路生成符号 facts。
3. 在 `prepare` 前后插入 trace enrichment。
4. 在 `claim` 阶段把可复用 trace 结果一并带给 agent。

而不是另起一套脱离当前项目的新系统。

---

## 3. 设计目标与非目标

### 3.1 设计目标

1. 在不修改 Lua 业务代码的前提下，建立**扁平命名空间感知**的符号索引。
2. 支持**多候选模块返回**的 `Jump to Definition`。
3. 支持**函数级逻辑切片**，避免整文件读取。
4. 支持**3-5 层有预算约束的跨函数追踪**。
5. 支持**冲突分叉验证**，输出“哪些路径安全，哪些路径风险”。
6. 支持**自动静默**与**needs_source_escalation**。
7. 与现有 artifact、分片、恢复、增量策略兼容。

### 3.2 非目标

1. 不做完整 Lua 解释器级别的精确运行时模拟。
2. 不试图在 v1 支持所有 metatable / dynamic dispatch / `loadstring` / 反射式 `require`。
3. 不要求一次性替换掉现有 `analyzer.py` 的全部实现。
4. 不默认读取整个大文件，也不把 LLM 当作主解析器。

---

## 4. 方案总览

### 4.1 方案摘要

新增一层 **Symbol Intelligence Layer**，由四个子系统组成:

1. **File Semantic Extractor**
   从每个 Lua 文件提取模块键、函数定义、导出关系、`require` 绑定、调用点、返回摘要。
2. **Collision-Aware Symbol Index**
   聚合全仓 file-level facts，形成“逻辑模块名 -> 多物理路径候选”的索引。
3. **Definition Navigator**
   提供面向 agent 的 Jump API，返回多候选定义和逻辑切片。
4. **Bounded Trace Engine**
   以 finding 或 call-origin 为起点，在 3-5 层内展开跨函数追踪，并持久化 trace bundle。

### 4.2 为什么这是最优方案

这是最优方案，因为它同时满足四个看似冲突的目标:

1. **精度**: 通过 AST 提取真实函数边界、导出关系、调用关系，而不是正则猜测。
2. **成本**: 不做全图 fixpoint，不做整仓文本灌给 agent，只做“按需、分叉、有预算”的追踪。
3. **兼容性**: 复用现有 artifact、resume、review shard、snippet 机制。
4. **可演进性**: 先把“索引 + 导航 + trace bundle”接起来，后续再扩充规则和静态证明能力。

---

## 5. 新架构与现有 pipeline 的关系

建议将主流程扩展为:

```text
discover files
  -> parse / local nil analysis
  -> emit file symbol facts
  -> aggregate symbol index
  -> trace-enrich active findings
  -> shard findings
  -> claim review payload
  -> complete / merge
```

### 5.1 新增模块

建议新增以下 Python 模块:

1. `lua_nil_review/symbol_models.py`
   定义模块、函数、导出、调用边、trace bundle 的结构。
2. `lua_nil_review/symbol_extractor.py`
   从单文件 AST 提取 symbol facts。
3. `lua_nil_review/symbol_index.py`
   聚合 file-level facts 为仓库级索引。
4. `lua_nil_review/symbol_slices.py`
   负责函数逻辑切片与 slice 缓存。
5. `lua_nil_review/symbol_query.py`
   提供 jump / lookup API。
6. `lua_nil_review/tracer.py`
   实现 3-5 层 bounded trace。

### 5.2 对现有模块的改动方向

1. `analyzer.py`
   从“只输出 finding”升级为“共享解析结果 + 输出 finding 和 function summary 原料”。
2. `workflow.py`
   增加 symbol build 和 trace enrich 两个阶段。
3. `state.py`
   增加 `symbol_index/`、`trace_bundles/`、`symbol_slices/` 等目录。
4. `config.py`
   增加 symbol tracing 相关配置。
5. `run_review_cycle.py`
   增加 `jump`、`trace` 等 agent 入口，或在 `claim` 返回中嵌入 trace 概览。

---

## 6. 核心数据模型

这一层是整个设计的关键。需求的核心不是“找函数”，而是**在扁平命名空间下稳定表达多候选关系**。

### 6.1 Logical Module Key

定义:

1. `logical_module_key`
   运行时逻辑模块名。默认采用文件 `basename` 去掉 `.lua` 后的名称。
2. `collision_group`
   同一个 `logical_module_key` 下的全部物理文件集合。
3. `declared_module_keys`
   文件内部显式声明的模块名，例如 `module("foo.bar")`。

设计原则:

1. **basename 优先**。这是需求文档定义的运行时主语义。
2. 显式声明名作为补充索引，不替代 basename。
3. 一个文件可以同时属于多个 logical key。

示例:

```json
{
  "file": "src/ui/config.lua",
  "logical_module_keys": ["config", "ui.config"],
  "collision_group": "config"
}
```

### 6.2 Function Symbol

每个函数定义都记录为一个独立 `FunctionSymbol`:

```json
{
  "function_id": "sha1(file|qualified_name|start_line)",
  "file": "src/ui/config.lua",
  "local_name": "get_impl",
  "qualified_name": "M.get",
  "visibility": "local",
  "exported_as": ["config.get"],
  "range": {
    "start_line": 12,
    "end_line": 37,
    "start_offset": 188,
    "end_offset": 744
  },
  "signature_line": 12,
  "logic_hash": "sha256(ast-normalized-body)",
  "slice_id": "..."
}
```

字段说明:

1. `local_name`
   函数在当前文件内部的定义名。
2. `qualified_name`
   语法层面的名字，例如 `M.get`、`Config:get` 标准化后为 `Config.get`。
3. `exported_as`
   逻辑对外名字，例如 `config.get`。这是 Jump API 最重要的字段。
4. `logic_hash`
   用于跨文件逻辑去重，不依赖物理路径。

### 6.3 Export Binding

仅有函数定义还不够，必须记录“这个函数如何成为模块对外 API”。

建议记录三类导出绑定:

1. `table_field_export`
   例如 `function M.get()`、`M.get = function() ... end`。
2. `return_table_export`
   例如 `return { get = get_impl }`。
3. `global_export`
   例如 `function get() ... end`，当文件以全局方式暴露能力时使用。

示例:

```json
{
  "module_key": "config",
  "member_name": "get",
  "binding_kind": "return_table_export",
  "function_id": "fn_123",
  "source_line": 40
}
```

### 6.4 Require Binding

要解析 `Config.get(...)`，必须知道 `Config` 来自哪里。

因此每个文件还要记录 `require` 绑定:

```json
{
  "alias_name": "Config",
  "module_keys": ["config"],
  "binding_kind": "local_require",
  "line": 3
}
```

需要覆盖的模式:

1. `local Config = require("config")`
2. `Config = require("config")`
3. `local mod = require "config"`
4. `require("config")` 裸调用，用于记录全局 receiver hint

### 6.5 Call Edge

追踪不是直接在文本上跳，而是在结构化调用边上展开。

每个函数内部记录 `CallEdge`:

```json
{
  "caller_function_id": "fn_123",
  "line": 28,
  "callee_expr": "Config.get",
  "callee_kind": "module_member",
  "receiver_name": "Config",
  "member_name": "get",
  "resolved_targets": [],
  "arg_exprs": ["name"],
  "returns_used": true
}
```

`callee_kind` 建议至少包含:

1. `local_function`
2. `same_file_table_member`
3. `module_member`
4. `global_function`
5. `dynamic_unknown`

### 6.6 Return Summary

真正支撑 3-5 层追踪的，不是全文阅读，而是**函数摘要**。

建议先聚焦**第一返回位**，因为当前规则的 sink 以单值传递为核心。

```json
{
  "function_id": "fn_123",
  "first_return": {
    "state": "call_dependent",
    "reason": "returns result of Helper.resolve(x)",
    "dependencies": [
      {
        "kind": "call",
        "callee_expr": "Helper.resolve",
        "line": 32
      }
    ]
  },
  "guards": ["x asserted non_nil"],
  "summary_confidence": "medium"
}
```

`state` 建议标准化为:

1. `always_non_nil`
2. `always_nil`
3. `maybe_nil`
4. `param_passthrough`
5. `call_dependent`
6. `field_dependent`
7. `unknown`

### 6.7 Trace Bundle

为解决“多轮工具调用会丢上下文”的问题，必须把追踪过程本身持久化。

建议引入 `TraceBundle`:

```json
{
  "finding_id": "finding_abc",
  "status": "partial",
  "root": {
    "kind": "sink_argument",
    "file": "foo.lua",
    "line": 88,
    "expression": "name"
  },
  "nodes": [],
  "edges": [],
  "branch_outcomes": [],
  "frontier_node_ids": [],
  "max_depth": 5
}
```

Trace bundle 价值:

1. agent 下一轮只需要给 `finding_id` / `node_id`，不需要重新描述整个链路。
2. 可以缓存已展开分支。
3. 可以记录“哪些候选已证明安全，哪些还没展开”。

---

## 7. 索引构建设计

### 7.1 Parse Once 原则

当前 `analyzer.py` 已经能利用 `luaparser` 拿到节点范围与 token offset。最佳方案不是再做一套独立文本扫描，而是引入共享的单文件语义提取过程:

1. 解析一次 AST。
2. 同一次遍历中提取:
   - nil finding 原料
   - function boundary
   - require 绑定
   - export 绑定
   - 调用边
   - 返回摘要原料

### 设计建议

重构方向不是立即大拆，而是先抽出一个共享对象:

```text
ParsedLuaFile
  - source_index
  - ast_root
  - text
  - file metadata
```

然后让 `LuaNilAnalyzer` 和 `SymbolExtractor` 共享它。

### 7.2 模块键发现规则

每个文件至少生成以下 module keys:

1. `basename(file)`
2. `module("...")` 声明出的模块名
3. 配置里显式声明的 override key

默认情况下:

1. `src/ui/config.lua` -> `config`
2. `src/net/config.lua` -> `config`

于是这两个文件天然进入同一个 `collision_group=config`。

### 7.3 函数提取规则

必须覆盖:

1. `local function foo()`
2. `function foo()`
3. `function M.foo()`
4. `M.foo = function() ... end`
5. `return { foo = function() ... end }`
6. `return { foo = local_impl }`

同时记录:

1. 起止行号与 offset
2. 所属 module key
3. 对外导出名
4. 是否匿名函数
5. `logic_hash`

### 7.4 逻辑去重

需求明确要求冲突文件中相同逻辑只给 agent 一份副本。

建议 `logic_hash` 采用两级策略:

1. 一级: 对函数体 AST 做去位置信息、去注释、去空白后的 canonical serialization。
2. 二级 fallback: 对函数切片做 whitespace-normalized hash。

Jump 和 trace 默认按 `logic_hash` 去重，返回结构中保留 `duplicate_files`:

```json
{
  "logic_hash": "hash_1",
  "representative_file": "src/ui/config.lua",
  "duplicate_files": [
    "src/ui/config.lua",
    "src/net/config.lua"
  ]
}
```

### 7.5 Artifact 布局

建议在当前 `artifacts/string-find-nil/` 下新增:

```text
symbol_index/
  manifest.json
  files/
    <file_id>.json
  modules/
    <module_key>.json
  collisions.json
  globals.json
symbol_slices/
  <slice_id>.txt
trace_bundles/
  <finding_id>.json
```

说明:

1. `symbol_index/files/<file_id>.json`
   单文件 symbol facts，增量复用的基本单位。
2. `symbol_index/modules/<module_key>.json`
   逻辑模块到物理候选的聚合结果。
3. `collisions.json`
   快速查询所有重名模块组。
4. `symbol_slices/`
   逻辑切片缓存，供 jump / claim / trace 复用。
5. `trace_bundles/`
   持久化链路上下文。

### 7.6 增量构建策略

推荐继续沿用现有 `content_hash + fingerprint` 思路。

每个文件新增:

1. `symbol_fingerprint`
2. `symbol_status`
3. `symbol_artifact_path`

增量规则:

1. 文件内容未变且 symbol fingerprint 未变 -> 复用 file symbol facts。
2. 任一文件的 symbol facts 变化 -> 仅重建受影响 module key 的聚合索引。
3. 与变更文件相关的 trace bundle 标记为 stale。

---

## 8. Jump to Definition API 设计

### 8.1 为什么必须是“上下文化跳转”

用户需求不是普通 IDE 的“跳定义”。真正需要的是:

1. 在 `foo.lua:88` 看到 `Config.get(name)`。
2. 知道 `Config` 其实来自 `require("config")`。
3. 发现 `config` 有多个同名物理文件。
4. 返回所有候选的 `get()` 函数逻辑切片。

因此 API 必须支持**上下文化解析**。

### 8.2 API 输入

建议支持两种入口:

### 入口 A: 直接查逻辑符号

```json
{
  "query_type": "logical_symbol",
  "symbol": "config.get"
}
```

### 入口 B: 在调用点上下文中解析

```json
{
  "query_type": "callsite_expr",
  "file": "foo.lua",
  "line": 88,
  "expression": "Config.get"
}
```

入口 B 的处理顺序:

1. 根据 `file + line` 找到所在函数。
2. 在该文件的 require bindings 中解析 `Config -> config`。
3. 在 `module_key=config` 下查全部候选。
4. 在每个候选模块的导出表中找 `member=get`。

### 8.3 API 输出

返回结果必须结构化，示例:

```json
{
  "resolution_kind": "collision_multi_candidate",
  "module_key": "config",
  "candidates": [
    {
      "file": "src/ui/config.lua",
      "function_id": "fn_ui_get",
      "exported_as": "config.get",
      "slice_path": "symbol_slices/slice_1.txt",
      "logic_hash": "hash_same",
      "duplicate_files": ["src/ui/config.lua", "src/net/config.lua"]
    }
  ],
  "suppressed_duplicates": 1
}
```

### 8.4 逻辑切片策略

与当前按事件取 snippet 不同，Jump API 必须返回**函数逻辑切片**。

默认策略建议为 `logic_slice`:

1. 始终包含函数签名行。
2. 包含与返回路径相关的 `if / assert / defaulting` 行。
3. 包含被追踪 call / assignment 行。
4. 包含 return 行。
5. 每个片段带 1-2 行上下文。
6. 合并重叠区间。
7. 删除纯注释长段和无关 filler。

切片模式建议支持:

1. `logic_slice` 默认，最省 token。
2. `contiguous_body` 当函数很短时返回完整函数体。
3. `return_focus` 只看签名、关键 guard、return 区域。

默认预算建议:

1. 单 slice 不超过 60 行。
2. 单次 jump 不返回超过 3 个去重后切片。
3. 超出部分只返回摘要和 `expand_token`。

---

## 9. Trace Engine 设计

### 9.1 追踪入口

Trace 可以从两种对象启动:

1. `finding_id`
   最常见。对现有 finding 做主动溯源。
2. `callsite`
   agent 在阅读代码时主动调用。

### 9.2 追踪节点类型

建议 `TraceNode.kind` 至少支持:

1. `sink_argument`
2. `local_assignment_origin`
3. `function_return`
4. `call_target`
5. `module_collision_branch`
6. `field_read`
7. `unknown_dynamic`

### 9.3 追踪展开规则

核心规则如下:

1. 若 origin 是 `nil` 字面量 -> 直接判定 risky。
2. 若 origin 是 `or` 默认值且右侧已知 non_nil -> 判定 safe。
3. 若 origin 是函数调用:
   - 先做 Jump resolve。
   - 对每个候选函数查看 return summary。
   - `always_non_nil` -> 分支 safe。
   - `always_nil` / `maybe_nil` -> 分支 risky。
   - `call_dependent` -> 继续展开下一层。
   - `unknown` -> 若深度未超限则返回 slice 并保留 uncertain。
4. 若 origin 是 table / field 读取:
   - 若有明确非 nil 默认值或 guard -> safe。
   - 否则通常视为 `field_dependent` 或 `unknown`。

### 9.4 分叉验证

对于 `require("config")` 的 collision group，Trace Engine 必须真正分叉，而不是选一个猜。

建议分支模型:

1. 每个候选模块生成一个 branch。
2. branch 之间共享 dedup 后的 slice，但各自保留独立 outcome。
3. 最终输出按 branch 聚合:
   - `all_safe`
   - `all_risky`
   - `mixed`
   - `all_uncertain`

输出示例:

```json
{
  "overall": "mixed",
  "branches": [
    {"file": "src/ui/config.lua", "status": "safe"},
    {"file": "src/net/config.lua", "status": "risky"}
  ]
}
```

这正好满足“哪些配置路径下安全，哪些配置路径下有风险”的需求。

### 9.5 自动静默与提权

自动化决策建议如下:

1. **auto_silence**
   若在 `depth <= 3` 内，所有 branch 都被证明 safe，则该 finding 自动静默。
2. **keep_active**
   若任一 branch 被证明 risky，则 finding 保持活跃，并带上 branch 证据。
3. **needs_source_escalation**
   若 `depth == 5` 仍无法定论，或 branch 判定依赖不可见外部装配配置，则标记提权。

注意:

1. auto_silence 只影响 review queue，不删除原始 finding。
2. 被自动静默的 finding 应写回 analysis artifact，标记 `trace_auto_silenced=true`。

### 9.6 Trace 预算

必须硬性限制 trace 展开，避免变成全图搜索。

建议默认预算:

1. `max_depth = 5`
2. `auto_silence_depth = 3`
3. `max_branch_count = 16`
4. `max_expanded_nodes = 64`
5. `max_unique_slices = 12`

超预算时:

1. 停止继续展开。
2. 输出 `budget_exhausted`。
3. 进入 `needs_source_escalation`。

### 9.7 Trace Bundle 持久化

trace bundle 设计目标不是“记录日志”，而是“保存 agent 的工作上下文”。

建议记录:

1. 已展开节点
2. 已解析的 symbol candidates
3. dedup 结果
4. branch outcome
5. 预算消耗
6. 尚未展开的 frontier

这样 agent 的下一次调用可以是:

```bash
python scripts/run_review_cycle.py trace --finding-id <id> --expand-node <node_id>
```

而不需要重复解释前情。

---

## 10. 与现有 review workflow 的集成方式

### 10.1 `run_analyze` 阶段

新增职责:

1. 对每个 Lua 文件产出 `analysis/<file_id>.json`
2. 同时产出 `symbol_index/files/<file_id>.json`
3. 如文件未变化则复用二者

实现建议:

1. 在单文件 parse 之后，先提取 function / module / require / export 原料。
2. 再执行当前 nil 分析。
3. 避免同一文件解析两次。

### 10.2 `run_prepare_shards` 前的 trace enrich

推荐在 `prepare` 中新增一个 enrichment 子阶段:

1. 读取未 suppressed 的 finding。
2. 对 `nil_state=maybe_nil` 或来源为 `call / unresolved name` 的 finding 做 trace。
3. 写出 `trace_bundles/<finding_id>.json`。
4. 更新 finding:
   - `trace_status`
   - `trace_summary`
   - `trace_bundle_path`
   - `trace_auto_silenced`
   - `needs_source_escalation`

然后再决定是否进入 shard。

### 10.3 `claim` 阶段

claim payload 建议新增:

1. `trace_summary`
2. `branch_outcomes`
3. 首跳 definition slice
4. `trace_bundle_path`

这样 agent 在多数 case 下只读 claim payload 就能判断，不必再二次打开源文件。

### 10.4 `merge` 阶段

merge 阶段不需要知道全部 trace 细节，但应保留关键 trace 结论:

1. safe by trace
2. risky branch count
3. mixed collision outcome
4. escalated because budget exhausted / external config dependency

---

## 11. 配置设计

建议扩展 `.lua-nil-review.json`:

```json
{
  "include": ["*.lua", "**/*.lua"],
  "exclude": ["vendor/**"],
  "nil_guards": ["assert"],
  "safe_wrappers": [],
  "symbol_tracing": {
    "enabled": true,
    "flatten_require_mode": "basename",
    "max_depth": 5,
    "auto_silence_depth": 3,
    "max_branch_count": 16,
    "max_expanded_nodes": 64,
    "slice_mode": "logic_slice",
    "max_slice_lines": 60,
    "module_resolution_overrides": {
      "config": [
        "src/ui/config.lua",
        "src/net/config.lua"
      ]
    }
  }
}
```

说明:

1. `flatten_require_mode=basename` 是默认运行时模型。
2. `module_resolution_overrides` 不是必须项，只在已知外部配置时使用。
3. 若没有 override，系统必须维持多候选返回，而不是伪造唯一答案。

---

## 12. CLI / API 设计

建议增加三个直接面向 agent 的命令:

1. `build-symbol-index`
   手动重建 symbol index，便于调试。
2. `jump`
   根据逻辑符号或调用点上下文返回候选定义切片。
3. `trace`
   对 finding 或 node 做 bounded trace。

示例:

```bash
python scripts/run_review_cycle.py jump --file foo.lua --line 88 --expr Config.get
python scripts/run_review_cycle.py trace --finding-id <finding_id>
python scripts/run_review_cycle.py trace --finding-id <finding_id> --expand-node <node_id>
```

如果不想扩展 wrapper，也至少应新增独立脚本:

1. `scripts/build_symbol_index.py`
2. `scripts/jump_to_definition.py`
3. `scripts/trace_finding.py`

输出格式必须是 JSON，便于 agent 直接消费。

---

## 13. 性能设计

### 13.1 为什么该方案可以满足规模要求

性能来自三个层面，而不是单点优化:

1. **per-file fact 持久化**
   让大仓库绝大多数文件在二次运行时直接复用。
2. **module-key 局部聚合**
   改一个文件不需要重建全图。
3. **bounded trace**
   只在 finding 需要时展开 3-5 层，而不是全仓提前建完整调用闭包。

### 13.2 推荐性能策略

1. 单文件 facts 输出使用 JSON，避免复杂二进制依赖。
2. 聚合索引按 module key 分桶存储，不做一个超大单文件。
3. slice 缓存使用文本文件，路径稳定，可直接复用到 claim payload。
4. trace 只针对 active finding 运行，不针对所有函数预跑全图。

### 13.3 解析后端策略

当前项目已依赖 `luaparser`，并且现有分析器已经依赖其 token range。就当前项目阶段而言，**v1 最优选择仍然是复用 `luaparser`**，理由如下:

1. 能直接复用现有 AST 与位置能力。
2. 接入成本最低。
3. 风险最低，不需要在需求落地第一阶段同时重构 parser backend。

但设计上建议预留 `ParserFacade` 抽象，方便未来如果 `luaparser` 在某些仓库性能或容错不足，再接入 Tree-sitter 作为替代实现。

---

## 14. 风险与降级策略

### 14.1 高动态代码

场景:

1. `require(dynamic_name)`
2. `obj[method_name](...)`
3. metatable 注入导出
4. 运行时 patch module table

策略:

1. 明确标记 `dynamic_unknown`
2. 不猜测唯一目标
3. 进入 `needs_source_escalation` 或保守保留 finding

### 14.2 外部打包优先级不可见

场景:

1. 多个 `config.lua` 实际哪个生效由外部打包脚本决定

策略:

1. 保持多 branch
2. 输出 branch-level 安全性
3. 若最终结论依赖外部不可见优先级，则标 `needs_source_escalation`

### 14.3 索引不完整

场景:

1. 个别文件 parse 失败
2. symbol facts 缺失

策略:

1. 仅让相关 branch 降级为 uncertain
2. 不影响其他 branch 的安全证明
3. 在 trace summary 中注明缺失原因

---

## 15. 测试设计

建议新增测试覆盖以下维度:

### 15.1 单文件 symbol extraction

1. 识别 local/global/exported function
2. 识别 `require` alias
3. 识别 `return { foo = foo_impl }`
4. 正确计算函数范围和 `logic_hash`

### 15.2 collision-aware jump

1. 两个 `config.lua` 时返回两个候选
2. 逻辑相同的候选被 dedup
3. `Config.get` 可通过 `require` alias 正确解析到 `config.get`

### 15.3 trace engine

1. 3 层内全 safe -> auto silence
2. 5 层内 mixed -> finding 保留并输出 branch 结果
3. 超预算 -> `needs_source_escalation`
4. 外部配置不可见 -> `needs_source_escalation`

### 15.4 workflow integration

1. 变更单文件时 symbol facts 增量复用正确
2. `claim` payload 包含 trace summary 和 slice
3. stale trace bundle 在文件变更后被正确失效

---

## 16. 推荐实施顺序

为了控制风险，建议分四步落地。

### Phase 1: 先把索引建起来

交付:

1. `FunctionSymbol`
2. `ExportBinding`
3. `RequireBinding`
4. `symbol_index/files/*.json`
5. `symbol_index/modules/*.json`
6. `jump` API

价值:

1. 立即拥有 collision-aware jump to definition。
2. agent 审计效率会立刻提升。

### Phase 2: 再做 return summary

交付:

1. `ReturnSummary`
2. 同文件 / 跨文件 call dependency 抽取

价值:

1. trace engine 不再每次都靠读 slice 才能前进。

### Phase 3: 接入 bounded trace 和 trace bundle

交付:

1. `trace_bundles/*.json`
2. `trace` API
3. branch-level outcome

价值:

1. 满足 3-5 层主动溯源需求。
2. 解决多轮调用上下文丢失问题。

### Phase 4: 把 trace enrichment 接进 prepare

交付:

1. auto silence
2. needs_source_escalation 自动提权
3. claim payload trace summary

价值:

1. 真正减少人工审计工作量，而不仅仅是提供新工具。

---

## 17. 为什么不选其他方案

### 17.1 不选“纯 LSP / ctags / path-based 索引”

原因:

1. 这类方案默认物理路径就是逻辑模块名。
2. 无法表达 basename flattening collision。
3. 无法理解 `return { get = get_impl }` 这类 Lua 导出模式。

### 17.2 不选“全仓全图 fixpoint 分析”

原因:

1. 实现成本过高。
2. 动态 Lua 下收益不成比例。
3. 会直接推高性能和维护风险。
4. 与当前项目“高精度审计工作流”定位不匹配。

### 17.3 不选“完全依赖 agent 临时读源码”

原因:

1. token 成本不可控。
2. 多轮对话容易丢上下文。
3. 不能复用历史结果。

---

## 18. 最终结论

基于 `lua-nil-review` 当前代码结构，满足需求文档的最优实现路径是:

1. **在 `analyze` 阶段旁路产出 file-level symbol facts**
2. **建立 collision-aware 的仓库级 module/function index**
3. **提供上下文化的 Jump API 和函数逻辑切片**
4. **引入持久化 trace bundle，做有预算的 3-5 层分叉追踪**
5. **在 `prepare` 阶段完成 auto silence / escalation enrichment**

这个方案既不脱离 `lua-nil-review` 现有 persisted workflow，又能把需求文档中最关键的几个难点一次性解决:

1. 扁平化命名空间
2. 多候选冲突
3. 精准函数切片
4. 主动溯源
5. 上下文持久化
6. 大仓库增量复用

如果后续进入实现阶段，我建议先按 Phase 1 和 Phase 2 落地，因为这两步已经能显著降低 agent 的误判和盲读成本，同时对现有代码入侵最小。
