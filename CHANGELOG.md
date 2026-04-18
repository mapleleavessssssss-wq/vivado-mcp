# Changelog

## [0.3.6] — 2026-04-18

### 修复(B14 第二层)

- **B14-2 [P1] `verilog_compile_check` iverilog 启动时 DLL 加载失败 0xC0000135** —— 0.3.5 的 `_scoop_fallback` 解决了 `shutil.which` 找不到的问题,但在 MCP server 的 subprocess 里调 iverilog.exe 仍返回 returncode=3221225781(0xC0000135 STATUS_DLL_NOT_FOUND),因为 iverilog.exe 启动要加载同目录里的 mingw/cygwin DLL,而父进程 PATH 里没有 scoop 的 apps bin 目录。修复:`compile_check` 里组装 `subprocess.run(env=...)` 时,把 exe 所在目录 + `~/scoop/apps/<name>/current/bin` 双保险注入 PATH 开头。
- **UI bug**:returncode 非 0 但没解析到 issue 时被错误显示为 "WARN (0 warnings)" —— 改判定为"运行异常"并输出 raw stderr + 0xC0000135 专项提示。

### 测试

- **327 → 328**(+1):新增 `test_subprocess_env_gets_scoop_bin_on_path`,验证 env 正确注入 scoop bin + 不覆盖原 PATH。

## [0.3.5] — 2026-04-18

### 修复

- **B14 [P1] `verilog_compile_check` 在 Windows+scoop 环境下 shutil.which 找不到 iverilog** —— 实机发现的典型坑:scoop 装完 iverilog 后 User PATH(注册表)已更新,但 Claude Code 父进程启动时 snapshot 的 PATH 仍是旧的,MCP server 子进程继承的 PATH 里没有 `%USERPROFILE%\scoop\shims`。用户要完全关闭 CC 应用重开才生效,体验很差。新增 `_scoop_fallback(name)` 辅助函数:`shutil.which` 失败时扫 `~/scoop/shims/{name}.exe` 默认路径,subprocess 拿到绝对路径能直接调。其他 Windows 包管理器(choco/winget)默认路径未来可以用同样模式扩展。

### 测试

- **326 → 327**(+1):新增 `test_scoop_fallback_when_path_missing`,mock USERPROFILE + 伪造 shim 文件,验证 which 返回 None 时 subprocess 拿到 shim 绝对路径。

## [0.3.4] — 2026-04-18

### 新增工具(批 3+4:生态联动,22 → 25)

- **`verilog_compile_check`** —— 用 iverilog 或 verilator 做语法 + 连接性检查,比 Vivado 综合快 50 倍(毫秒级 vs 30-60s)。自动探测工具链优先 iverilog,装了才跑,未装返回 SKIP + 安装指引。支持 Windows 路径。同时在 `.claude/settings.json` 追加可选 `iverilog-check` hook,保存 .v/.sv 时自动后台跑。
- **`get_ip_status`** —— 检查项目 IP 版本(`report_ip_status -return_string` 解析)。区分"需要升级" / "已锁定" / "已最新",附批量升级建议。老项目(Vivado 版本迁移后)的必备摸底工具。
- **`get_pre_commit_summary`** —— 生成可粘贴进 git commit body 的 markdown 摘要:项目元信息 / 时序 WNS+WHS / 关键资源占用 / CW+ERROR 计数 / READY/WARN/BLOCK 门禁标签。结束这种"改了 UART 模块"式的无信息量 commit。

### Hooks

- **`iverilog-check`** —— 新增 PostToolUse hook(.v/.sv 保存时),iverilog/verilator 装了才触发,有 error 时阻断并给 Claude 看结构化诊断。

### 测试

- **303 → 326**(+23):新增 verilog_compile_check(15,覆盖 parser+detect+timeout+Windows 路径)、ip_status_parser(8)。

## [0.3.3] — 2026-04-18

### 新增工具(批 2:XDC 自修,21 → 22)

- **`xdc_auto_fix`** —— 从 xdc_lint 的诊断升级为 quick-fix:自动往 XDC 文件里补 IOSTANDARD 语句(消除 NSTD-1/BIVC-1 隐患)和 create_clock -period 参数(仅已知板卡)。
  - **只修**:`MISSING_IOSTANDARD`(插入 IOSTANDARD 语句)、`CLOCK_NO_PERIOD`(板卡已知时补 period)
  - **坚决不碰**:`PIN_CONFLICT` / `DUPLICATE_PORT` / `PIN_CONFLICT_CROSS_FILE`(冲突必须人改)
  - **板卡 profile**:basys3 / nexys-a7 / arty-a7 / zybo / kc705 内置 IOSTANDARD + 时钟频率。未知板只修 IOSTANDARD,CLOCK 跳过。
  - **dry_run=True 默认**:只预览补丁,确认后再 dry_run=False 写回。修改行加 `# auto-fixed by xdc_auto_fix <date>` 注释,回溯容易。
  - **行号保护**:同一文件多条 insert 时按行号降序应用,避免行号偏移。

### 测试

- **303 → 317**,新增 14 个单元测试:xdc_auto_fixer(14)。覆盖 MISSING_IOSTANDARD / CLOCK_NO_PERIOD / 不可修问题跳过 / 未知板 / dry_run vs apply / 多文件 / 多 insert 不偏移。

## [0.3.2] — 2026-04-18

### 新增工具(批 1:长任务可视 + 新手引导,19 → 21)

- **`get_run_progress`** —— 查 run 的实时进度。综合/实现常跑 10-30 分钟,以前只能看到 `status=Running` 黑盒等待。现在返回:Vivado 原生 STATUS + PROGRESS 百分比、runme.log 里的 Phase 序列(最近 5 条 + 当前箭头)、日志尾部 30 行、log mtime 距现在多久(判断进程是否卡住)。log 超过 2 分钟没更新会自动提示"可能卡住"。
- **`get_next_suggestion`** —— 纯 Python 决策引擎,根据 QUERY_PROJECT_INFO 输出推断下一步。11 档决策:没项目 → 开/建项目 / 没源文件 → add_files / 没顶层 → set_property TOP / 没 XDC / 可综合 → xdc_lint + run_synthesis / 综合失败 → get_critical_warnings / 综合完成 → run_implementation / 实现失败 / 布线完成 → check_bitstream_readiness + generate_bitstream / bitstream 已生成 → program_device。每档附具体可执行的工具/Tcl 命令。

### 测试

- **289 → 315**,新增 26 个单元测试:run_progress_parser(11)、suggestion_engine(15)。

## [0.3.1] — 2026-04-18

### 修复

- **B13 [P0] `stop_session` 没真正杀 Vivado GUI 进程** —— 实机发现的严重 bug:原 `GuiSession.stop` 用 `asyncio.subprocess.Process.terminate()`,但 `vivado.bat` 在 Windows 上会起一条 `cmd.exe → vivado.exe` 的进程链;`terminate()` 只杀 cmd.exe 外壳,vivado.exe 成为孤儿进程,继续占 800MB+ 内存,Vivado 自己写的 `vivado_pid<PID>.str` 也不被清理(要等用户手动杀进程+删文件)。新策略:先发 Tcl `exit` 让 Vivado 优雅退出(自动清 pid),超时则 `taskkill /F /T /PID` 递归杀进程树(Windows)或 SIGKILL(Unix),最后兜底扫 `vivado_pid*.str` 强删。

## [0.3.0] — 2026-04-17

### 新增工具(4 个,15 → 19)

- **`check_bitstream_readiness`** —— 烧板前一键 READY/WARN/BLOCK 综合判定。一次性检查 impl 状态、CW 计数、时序收敛,避免烧板后才发现问题。
- **`get_utilization_report`** —— 结构化资源占用报告(LUT/FF/BRAM/DSP/IO)。> 90% 自动标 `[CRITICAL]`,70-90% 标 `[WARN]`,xc7a35t 这种小芯片做设计时最常需要看。
- **`get_project_info`** —— 一次拿齐项目摸底信息:项目名、part、顶层模块、源文件列表、XDC 约束、IP 实例、synth/impl 状态。AI 接手陌生项目的起点。
- **`xdc_lint`** —— 纯 Python 静态 XDC 检查,**不需要 Vivado 进程**。即时捕捉 PIN_CONFLICT、MISSING_IOSTANDARD(NSTD-1/BIVC-1 隐患)、DUPLICATE_PORT、CLOCK_NO_PERIOD、PIN_CONFLICT_CROSS_FILE 五类常见错误,省掉 30 秒以上的跑综合等待。

### 修复

- **B10 [P1] `get_critical_warnings` 严重级别盲区** —— 实现阶段出现 ERROR 时,工具只显示 `errors=3` 数字,不列出具体 ERROR 内容,用户拿到 `critical_warnings=0` 容易误判"没事"。现在 `errors>0` 自动触发 `EXTRACT_ERRORS` Tcl 脚本,报告顶部出现 `!! 发现 N 条 ERROR !!` 并展示分类 + 中文修复建议。`_KNOWN_CATEGORIES` 补充 `DRC BIVC-1`/`Vivado_Tcl 4-23`/`Common 17-39`/`Synth 8-27`/`Synth 8-439`/`Place 30-58`/`Route 35-162` 七类 ERROR/CW ID。
- **B11 [P1] `get_timing_report` 无状态感知** —— impl_1 place_design 失败时,current_design 回落到 synth_1,工具返回 `PASS WNS=+5.813 ns` 但其实是综合估算,用户误以为"时序 OK 可烧板"。新增 `QUERY_DESIGN_STAGE` Tcl 脚本查询 synth_1/impl_1 状态,报告头部明示 `数据来源: post-synth (综合后估算,非最终结果)` / `post-route`,impl 失败时额外插入 `[!] 注意: impl_1 失败...不要据此判断能否烧板` 醒目警告。
- **B12 [P2] `_RE_WARNING_ID` 正则匹配不到字母数字 ID** —— 老正则 `\w+[\s\-]\d+[\-\d]*` 只匹配纯数字 ID,`[DRC BIVC-1]`/`[DRC NSTD-1]`/`[DRC UCIO-1]` 等字母数字混合 ID 全部归类为 UNKNOWN。扩宽为 `\w+[\s\-][\w\-]+` 后可识别常见 DRC 系列。

### 测试

- **216 → 251**,新增 35 个单元测试:xdc_linter(9)、util_parser(9)、project_parser(5)、timing_parser Bug 2(10)、diagnostic_tools Bug 1(2)。

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
