# Changelog

## [0.2.0] — 2026-04-17

### BREAKING CHANGES

- **删除 8 个 facade 工具**（`create_project` / `open_project` / `close_project` / `add_files` / `vivado_help` / `get_status` / `report`），这些都是一行 Tcl 就能做的包装。请使用 `run_tcl` 或新增的 `safe_tcl` 代替。迁移指南见 `docs/MIGRATION_0.1_to_0.2.md`。
- 工具总数 21 → 11。

### 新增

- **双模式会话**：`start_session(mode=...)` 现在支持三种模式：
  - `"gui"`（默认）—— MCP 自动 spawn `vivado -mode gui`，可视化 + TCP 9999 连接
  - `"tcl"` —— 原 subprocess 无头模式（CI 友好）
  - `"attach"` —— 连接到用户已开的 Vivado GUI（需先运行 `vivado-mcp install`）
- **`vivado-mcp install` CLI**：注入 `Vivado_init.tcl`，让 Vivado 启动自动起 TCP server（端口池 9999-10003）。
- **`vivado-mcp uninstall` CLI**：恢复 `Vivado_init.tcl`。
- **`safe_tcl` 工具**：带参数模板的 Tcl 执行器，自动对路径、标识符做 Tcl list 转义，支持 Windows 含空格/中文/$ 的路径。

### 修复

- **B1 [P0 CRITICAL]** 哨兵协议命令输出错位：`VMCP_ERR` 行打印在 sentinel 之后导致错误消息溢出到下一条命令的输出。修复后错误消息与对应命令严格对齐。
- **B2 [P0 CRITICAL]** `get_timing_report` / `get_io_report` 假阳性：报告命令失败时（如"No open design"）返回默认值 WNS=0 → 错误判定为 "PASS 时序满足"。现在失败时直接返回错误信息。
- **B3 [P0]** `verify_io_placement` 不支持 XDC `-dict` 语法：`set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 }` 无法被识别。放弃 Tcl 正则，改走纯 Python 读 XDC 文件，支持两种语法。
- **B4 [P1]** 综合/实现完成后未自动 `open_run`：导致紧随其后的 report 工具全失败。
- **B5 [P1]** Vivado stderr 流完全未读：失败命令的详细错误信息丢失。现在 stderr 被持续 drain 并在错误时附加到 output。
- **B6 [P1]** `get_io_report` 在 Vivado 2019.1 上解析不到端口：老版 Vivado 的 `report_io` 是按 Pin 而非按 Port 的表格（`Pin Number | Signal Name | Bank Type | ...`），与 parser 期望的 "Port Name" 表头不匹配。扩展 parser 自动识别两种表头格式。

### GUI 模式实机验证中新发现并修复

- **B8 握手验证缺失（TCP 会话）**：MCP spawn GUI Vivado 后按端口池顺序尝试连接，若恰好先连上其他产品（如残留的 SynthPilot）占用的端口，会误认为是自己的 server。新增握手步骤：连上后发送 `puts VMCP_HANDSHAKE_ACK`，若响应不是 vivado-mcp 的 length-prefix + JSON 协议则关闭连接跳到下一个端口。
- **B9 TCP 模式 `puts` 输出丢失 + 重复**：
  - **丢失原因**：`puts X` 在 GUI/TCP 模式下写到 Vivado 主 stdout（Tcl Console），客户端拿不到——导致所有依赖 `puts VMCP_XXX` 多行输出的内部命令（`run_synthesis` 的 Python 轮询、`COUNT_WARNINGS`、`EXTRACT_CRITICAL_WARNINGS`、`CHECK_PRE_BITSTREAM`、`INSPECT_IP_PARAMS` 等）在 GUI 模式下失效。
  - **重复原因**：Tcl 的 `append` 返回新字符串值，拦截用的 `captured_puts` 把 append 结果当 return value 返回 → 命令返回值也是 buffer 内容 → 合并时出现两份。
  - **修复**：`vivado_mcp_server.tcl` 用 `rename` 拦截 `puts`，把 stdout 输出捕获到 `::vmcp::captured_buf`（**用绝对路径，避免 rename 后 namespace resolve 陷阱**），`return ""` 模仿原生 puts 语义。subprocess 模式走原来的 sentinel 协议不受影响。

### 架构改进

- 抽象 `BaseSession`，`SubprocessSession` 和 `GuiSession` 各自实现，工具层无感切换。
- 长任务（综合/实现）改为 Python 侧轮询 `get_property STATUS/PROGRESS`，不再依赖 Tcl `wait_on_run` 阻塞事件循环。GUI 模式下 Vivado 界面保持响应。
- TCP 协议使用 length-prefix framing（4 字节 big-endian + UTF-8 payload），比 stdio 时代的 sentinel 协议简洁可靠。
- `init.tcl` 注入守卫使用端口占用判断，避免 `launch_runs` 子进程抢占端口。

## [0.1.0] — 2026 年早期

首个公开版本。21 个工具，subprocess `-mode tcl` 通信。
