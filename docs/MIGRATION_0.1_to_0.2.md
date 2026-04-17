# 0.1.x → 0.2.0 迁移指南

0.2.0 是一次 **breaking change**。核心理念：**能 `run_tcl` 就不包装成工具**——减少 AI 上下文消耗，让 LLM 直接拼 Tcl（它完全胜任）。

## 被删除的 8 个工具 + 替代方案

### 1. `create_project`

```python
# 旧
create_project(name="my_proj", directory="./my_proj", part="xc7a35tcpg236-1")

# 新
safe_tcl("create_project {0} {1} -part {2}",
         args=["my_proj", "./my_proj", "xc7a35tcpg236-1"])
# 或（路径无特殊字符时）
run_tcl("create_project my_proj ./my_proj -part xc7a35tcpg236-1")
```

### 2. `open_project`

```python
# 旧
open_project(xpr_path="./my_proj/my_proj.xpr")

# 新
safe_tcl("open_project {0}", args=["./my_proj/my_proj.xpr"])
```

### 3. `close_project`

```python
# 旧
close_project()

# 新
run_tcl("close_project")
```

### 4. `add_files`

```python
# 旧
add_files(files="./src/top.v ./src/sub.v", fileset="sources_1")

# 新
safe_tcl("add_files -fileset [get_filesets sources_1] {0}",
         args=["./src/top.v ./src/sub.v"])
# 含中文/空格路径时务必用 safe_tcl：
safe_tcl("add_files -fileset [get_filesets sources_1] {0}",
         args=["C:/projects/中文 路径/top.v"])
```

### 5. `vivado_help`

```python
# 旧
vivado_help(tcl_command="create_clock")

# 新
run_tcl("help create_clock")
# 或让 AI 自己从记忆拼（大部分 Tcl 命令都是公开文档）
```

### 6. `get_status`

```python
# 旧
get_status(run_name="synth_1")

# 新
run_tcl("get_property STATUS [get_runs synth_1]")
# 查所有
run_tcl("foreach r [get_runs] { puts \"$r: [get_property STATUS $r]\" }")
```

### 7. `report`（通用报告接口）

```python
# 旧
report(report_type="utilization")
report(report_type="power", options="-hierarchical")

# 新
run_tcl("report_utilization -return_string")
run_tcl("report_power -hierarchical -return_string")
```

保留的结构化报告工具（继续可用）：
- `get_io_report` — JSON 格式 IO 报告
- `get_timing_report` — 中文格式时序摘要

## 新增工具：`safe_tcl`

用于含特殊字符（空格、中文、`$`、`[]`、`{}`、反斜杠）的参数。自动用 Tcl 引用规则转义：

```python
safe_tcl(
    template="set_property PACKAGE_PIN {0} [get_ports {1}]",
    args=["W5", "clk"],
)
```

## 会话模式变更

```python
# 旧：只有一种模式（subprocess headless）
start_session()

# 新：默认 GUI 可视化
start_session()                       # mode="gui"，自动弹 Vivado GUI
start_session(mode="tcl")             # 原无头模式（CI 用）
start_session(mode="attach", port=9999)  # 连到已开的 Vivado GUI
```

**GUI 模式需先运行一次**：

```bash
vivado-mcp install
```

该命令会修改 `Vivado_init.tcl`，让以后所有 Vivado GUI 启动时自动开 TCP server。

卸载：

```bash
vivado-mcp uninstall
```

## 兼容性建议

如果你有旧版本的 AI prompt 或自动化脚本引用已删除的工具名，建议：

1. 搜索关键词：`create_project` `add_files` `report(` `get_status` `vivado_help` `open_project` `close_project`
2. 替换为上面对应的 `run_tcl` / `safe_tcl` 写法
3. 如果用 `.mcp.json` / `.claude.json` 配置过 MCP server，更新后重启 AI 工具即可
