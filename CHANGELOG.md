# Changelog

## [0.3.22] — 未发布

> 本版 = b8e748b(wave 工具 + 端口 B 方案,已合入未发布)+ 一轮全方位体检
> (59-agent 审计,17 条 P0/P1 经三视角对抗验证确认,0 误报)+ S3 反馈 PRD
> 实施 + 收尾 review 修复。工具数 25 → 27(仅 b8e748b 的两个 wave 工具,
> 体检轮零新增,less-is-more 兑现)。测试 526 → 695。

### 新工具(b8e748b)

- **`set_wave_zoom`** — XSim 无 Tcl zoom 命令,正则就地改 .wcfg
  `<zoom_setting>` 后 close -force/open 重载;BOM/CRLF/中文逐字保留。
- **`set_wave_analog`** — `WaveformStyle=STYLE_ANALOG` 配方封装
  (STYLE_ 前缀 / DESIGN_OBJECT 寻址 / 空对象判空三静默坑)。
- **多开端口 B 方案** — spawn 不再扫端口池:`bind(("",0))` 拿确切空闲端口,
  tcl server 绑不上即退出;`session_id→(port,pid)` 映射,stop 按 pid 精杀。

### 安全(体检确认,P1)

- **TCP Tcl server 改绑 127.0.0.1**(原 0.0.0.0)— 局域网内任意机器原本可
  向 Vivado 发任意 Tcl(= 任意代码执行)。如确需远程 attach 场景请提 issue。
- **`_handshake` 补 magic token 校验** — 0.3.21 的 token 反射验证只加在了
  probe,attach 握手路径漏掉;现统一为单一实现两处复用(B16 反 fork 纪律)。

### 修复(体检确认 P0/P1,全部带回归测试)

- **[P0] xdc_lint/xdc_auto_fix 不认通配符与 -dict 形式 IOSTANDARD** — 误报
  MISSING_IOSTANDARD 并插入覆盖用户真实电平的约束(改坏 XDC)。lint 改 Tcl
  glob 语义匹配 + -dict 解析 + **跨文件聚合**(IOSTANDARD 与 PACKAGE_PIN 分
  属不同 XDC 的常见组织方式)。
- **XDC 写回安全** — 原 utf-8+errors=replace 读 GBK 文件再写回会把中文注释
  全打成 U+FFFD;现编码探测(utf-8→gbk)同编码写回 + `.bak` 备份 + 临时文件
  原子替换 + CRLF/LF 保留;不可解码文件拒写并在报告露出 ENCODING_DEGRADED。
- **create_clock 续行(行尾 `\`)误报 CLOCK_NO_PERIOD** — parser 层折叠续行;
  fixer 对续行场景拒绝就地改(归 skipped),插入点推进到续行链末尾。
- **GUI 路径 JSON 转义漏 0x00-0x1F 控制字符** — 输出含 ESC/ANSI 序列时
  Python 端 json.loads 抛异常吞掉整条命令结果;tcl 端补 `\u00XX` 映射。
- **spawn 失败/超时不清理已启动的 Vivado** — 真孤儿进程(不进 _sessions,
  stop 无从触达);现失败分支按记录的 pid 清理 + session_manager 兜底。
- **timing_parser 摘要 'NA' 直接 float() 崩溃;表头缺失静默判 PASS** — 新增
  parse_status 三态,NA 报"无时序约束"、格式不识别报 [DEGRADED],绝不默认
  PASS;check_bitstream_readiness / get_pre_commit_summary 同步消费。
- **util_parser 在 2020.1+ 含 Prohibited 列的报告上整列错位**(误报 CRITICAL)
  — 改表头动态定位列;格式不识别显式 [DEGRADED] 而非静默 0 条。
- **verilog_compile_check 对 .sv 缺 -g2012** — 合法 SystemVerilog 误报 FAIL;
  另:iverilog `sorry:` 诊断归为 error(原会产出空 WARN 报告)。
- **generate_bitstream 安全门检查失败静默放行** — 现显式 [DEGRADED] 标记
  (含具体原因),不拦截但 AI/用户必然看见。
- **inspect_ip_params 不查 is_error** — 错误目标时假报"该 IP 没有 CONFIG.*
  参数";get_critical_warnings / flow 诊断概览同类问题一并修。
- **set_wave_zoom 带空格路径必炸** — wcfg 路径过 to_tcl_path 转义;同修
  unsaved 文案 FILE_NAME→FILE_PATH(原指引会撞 [Common 17-54])。
- **VMCP_POLL Tcl 片段与轮询循环两处复制** — 收口 tcl_scripts.py 单一定义
  + 共享 helper(B16 反 fork 纪律)。

### S3 反馈 PRD(0.3.22 计划项全部落地)

- **A1:list_sessions 主动探活 known sessions** — 原 is_alive 纯本地状态,
  Vivado 内部 server 挂了仍报活。现 fresh-connection 并发探活(asyncio
  gather,不阻塞 event loop);探活失败给中性 note(「可能正忙或挂死,勿立即
  stop_session」),**不会**把正在跑长命令的健康 Vivado 引导成 kill 目标
  (execute 超时后 _pending_response 窗口内跳过探活)。
- **A2:start_session banner 显示当前 project** — project 为空/"New Project"
  时提示先 open_project;查询走一次性独立短连接,绝不污染主连接。
- **B1/B2/B3 quirk hint** — `-scripts_only` 重生擦除提醒、wcfg BOM 损坏处置、
  [Coretcl 2-27] 路径排查指引(多层 glob,Tcl 无 ** 递归)。
- **B4:run_synthesis/run_implementation 显示 applied_generic /
  applied_verilog_define**(空值明示「(无)」,附 quirks §3 生效核对提醒)。
- **C2:get_utilization_report 加 detail 参数** — Block RAM 子表
  (RAMB36/FIFO* / RAMB36E1 only / RAMB18 / RAMB18E1 only);默认输出逐字节
  向后兼容。
- **hint 框架升级** — `_QUIRK_HINTS` 改三元组带 error_only 标记:内容型
  hint 在 rc=0 也能触发(A1 误报场景 rc=0,原框架永不触发);A1 误报命中时
  抑制矛盾的 A2 清理指引。
- **超时 hint** — run_tcl 超时追加「超时≠失败,Vivado 仍在执行,勿重发」
  指引(非工程模式长命令最易踩)。

### 场景覆盖(FPGA 全工作流盘点,零新工具)

- **prompts 修死引用** — debug_timing/debug_pcie 引用 0.2.0 已删的
  `report()` 工具;debug_timing 重写为 2019.1 验证过的时序收敛升级链
  (clock_interaction → cdc → methodology → qor_assessment → high_fanout →
  design_analysis);fpga_workflow 补 write_project_tcl 工程入库步骤。
- **第三方仿真器诊断 guard** — target_simulator 非 XSim 时不再用 XSim 日志
  布局得出误导结论(含陈旧 XSim 日志场景)。
- **get_next_suggestion 补仿真档** — 有 testbench 且没跑过行为仿真 → 先
  launch_simulation(原决策表从可综合直接跳综合)。
- **verilog_compile_check 挡 .vhd/.vhdl** — 返回 SKIP + check_syntax 替代
  方案(原会喂给 iverilog 报一堆无关错误)。
- **get_project_info 展示 testbench 列表**(sim_1 独有文件)。
- **docstring 配方** — program_device 补烧 flash 六步配方(write_cfgmem /
  PROGRAM.* 四件套);run_tcl 补 timeout 语义与 §8.6/§8.7 两坑。
- **quirks 新增 §12 多策略并行与增量编译、§13 ILA/VIO 硬件调试配方**
  (wait_on_hw_ila 必带 -timeout / VIO 写后必 commit_hw_vio)。

### 文档同步

- README:工具数 25→27、端口 B 方案语义(删全部「端口池 9999-10003」)、
  hook 节改"可选配置示例"(.claude/ 不随仓库分发,原宣称不成立;示例改
  单行命令 + bitstream-guard 改 ask 不死锁)、verify_io_placement_tool /
  nexys-a7 等名称订正、补 PITFALLS 链接。
- PITFALLS C1 配方订正为真机验证版(glob 寻址 + llength 判空),并指向
  set_wave_analog 工具。
- `::vmcp::start` 加重入守卫 — 修复 init.tcl + spawn -source 双加载导致
  一个 Vivado 进程同时监听 9999 和 auto-alloc 端口。
- 日志默认级别改 WARNING,支持 LOG_LEVEL 环境变量(对齐 logging spec)。

## [0.3.21] — 2026-05-25

### 修复(0.3.19 probe 假阳性 — 现场实测发现)

- **`probe_vmcp_server` 加 magic token 反射验证** — 0.3.19 实测中
  `list_sessions()` 间歇报 `<external@10000>`,但本机**无任何 Vivado GUI 在
  跑**。排查发现 port 10000 被 VMware/Hyper-V vNIC 上的 PID 6408 监听(绑
  192.168.159.1,不是 Vivado);Windows 多接口 weak host model + firewall race
  下被 127.0.0.1 偶发连通,旧 probe 只验响应是 `dict + 含 rc/output 字段`,
  挡不住巧合性回类 JSON 的服务,假阳性。修法:probe payload 改
  `puts VMCP_PROBE_<uuid16>`,vmcp 服务端 captured_puts 会反射 token 到响应
  output,probe 验响应 output 含该 uuid 才判 True。uuid 撞不上,假阳性归零。
- 测试新增 2 项(`tests/test_probe_then_attach.py`):
  `test_returns_false_when_output_lacks_magic_token`(合法 JSON 但 output 不含
  token 时挡掉)+ `test_returns_false_when_output_missing`(缺 output 字段挡
  掉)。468/468 pytest 全过(原 466 + 新 2),ruff clean。
- **协议兼容**:服务端 `vivado_mcp_server.tcl` **零改动**(captured_puts 早就
  把 puts 输出反射进 output);旧 MCP client 升级到 0.3.21 即可,旧 server 实例
  无需重启 / 重 source。

## [0.3.20] — 2026-05-25

### 修复 + 知识库沉淀(XSim 实战 10 项坑收纳 — W+ 路线)

> **设计哲学**:less-is-more 反对加 facade 工具,但**不**反对"把固定信息硬塞给
> AI"。本版本走 W+ 模式:能在 err 触发的坑走 `_safe_execute` 末尾追加 hint(AI
> 必看);写脚本前要知道的进 `run_tcl` tool description(每次调用都加载);用户
> 操作约束进项目根 PITFALLS.md;完整背景留 vivado-quirks.md 作设计文档。

#### A 类(MCP 主动 W hint)

- **A1:`open_wave_database` 在裸 Vivado GUI 里报 `'open_wave_config' failed`
  误报** —— rc=0 但 stderr 输出 err,wdb 实际加载成功。`_safe_execute` 检测到这
  条 err 末尾追加 verify 三连(`current_sim` / `current_wave_config` /
  `get_scopes /*`),AI 不需主动查 quirks。**不改写** 原始 Vivado 输出(透传契约
  保住),只追加。
- **A2:wave 操作失败留下孤儿 sim handle + GUI tab** —— 多次重试 → 多个 simulation_N
  + 多个 tab + 800MB 内存浪费。任何 wave 类命令(`open_wave_database` / `add_wave` /
  `log_wave` / `open_wave_config` / `current_wave_config`)出 err 时自动追加复制粘
  贴的清理片段(while close_sim + close_wave_config) + stop_session 重启兜底。
- **A4:中文 cwd 警告对只读 op 是狼来了** —— `_check_ascii_paths` 警告文本末尾追
  加"受影响命令"段:`create_project` / `synth_design` / `launch_runs` /
  `launch_simulation` 等会写 .runs/.sim/.cache 的命令踩;`open_wave_database` /
  `get_*` / `report_*` / `list_*` 只读 op 不受影响。**零代码逻辑改动,纯文案**。

#### B 类(进 `run_tcl` tool description,AI 每次调用必看)

- **B1:`-filter "name =~ {...[$var]...}"` 污染后续 `set_property` 静默失败** ——
  `[$var]` 触发 Tcl 命令替换,get_scopes 返回对了但 set_property RADIX 静默无效。
  必须改 foreach + regexp 自己过滤。
- **B2:`set_property RADIX` value 大小写敏感** —— `DEC` 静默退回 default,只
  `dec` 生效。Vivado 大部分 property 不敏感,这条反直觉。
- **B3:`wave_design_object` 无 Analog 显示样式属性** —— Tcl 物理上没接口,只
  GUI 右键手点。
- **B4:`add_wave -radix` vs `set_property RADIX` 值集对照表** —— signed
  decimal 叫 `dec` 不叫 `signed`,从 ModelSim 迁过来的脚本会踩。

#### C 类(进项目根 PITFALLS.md,给用户看)

- **C1:Analog 波形显示样式只能 GUI 手点**(B3 的用户侧操作约束)
- **C2:wave window 截图必须 `Win+Shift+S` 自截**

#### 现有 quirks 标注 W hint 自动覆盖

- §6(runme.log tail) → 顶上加"已在 `get_critical_warnings` 自动应用"指针
- §7(sim 多次失败重启) → 顶上加"已在 `_safe_execute` W hint 自动附 cleanup"指针

### 内部架构变化

- `server.py::_DIAG_HINT` 单条模式扩成 `_QUIRK_HINTS` 关键词字典 + `_append_quirk_hints()`
  通用追加函数。每条 hint = `(触发函数, hint 文本)`,触发函数签名
  `(output, command) -> bool`。新 hint 加一行就行。
- `_looks_like_run_failure` 改两参数签名 `(output, command)`,haystack 含命令文本
  让 `launch_runs` 等命令名也能触发(原只看 output)。
- `_safe_execute` 失败路径从"单一 hint 拼接"改成"按 quirk 字典追加 N 段"。

### Updated Files

- `src/vivado_mcp/server.py`(_QUIRK_HINTS 字典 + _append_quirk_hints + A1/A2 触发)
- `src/vivado_mcp/tools/session_tools.py`(_check_ascii_paths 文案加范围段)
- `src/vivado_mcp/tools/tcl_tools.py`(run_tcl docstring 加 B1-B4 精简提示)
- `.trellis/spec/backend/vivado-quirks.md`(§5.4 / §6 顶 W 指针 / §7 顶 W 指针 /
  §8.8 / §8.9 / §8.10 / §10 / §11)
- `PITFALLS.md`(新文件,C1/C2 用户操作约束)
- `tests/test_session.py`(扩 TestQuirkHintsA1A2 + TestAsciiPathScopeAnnotation 共 7 项)

### Acceptance

- 466 测试全过(本版新增 7 项)
- ruff 0 警告
- W 模式契约:**不改写** 原始 Vivado 输出,只追加(回归测试覆盖)

---

## [0.3.19] — 2026-05-25

### 修复(session 注册表与 attach 语义实战 bug)

- **Bug:`list_sessions` 漏报手动启动的 Vivado GUI + `start_session(mode="gui")`
  错误地把命令路由到原 GUI ⭐⭐** —— 用户手动开了 Vivado GUI(`vivado-mcp install`
  已注入 init.tcl,开机自动跑 TCP server),不 stop,从 MCP 调:
  1. `list_sessions` 报"无活跃会话"(只看 `_sessions` dict)
  2. `start_session(mode="gui")` 返回 "attach=False,端口 9999",看似 spawn 成功
  3. 后续 `run_tcl` 命令**实际生效在用户原 GUI**,新 spawn 的 vivado.exe 监听
     在 fallback 端口 10000 上,变成 ~800MB 内存的孤儿进程

  **根因(Python 端 + Tcl 端都是"先成功的赢"+ 无关联机制)**:
  - `vivado_mcp_server.tcl::start` 端口池 fallback(原 GUI 占 9999,新进程 bind
    到 10000)
  - `gui_session.py::start` 也从 9999 起试连,**第一个握手通过的就赢** → 永远
    连到原 GUI
  - `mode` property 直接返回 `"attach" if self._attach_only else "gui"`,字段语义
    是"用户请求的模式",不反映"实际连到外部还是 spawn 的进程"
  - `SessionManager.list_sessions` 只列 `self._sessions.values()`,完全看不见
    用户手动启动的 GUI

  **修复(`vivado/gui_session.py` + `vivado/session_manager.py`)**:
  1. **probe-then-attach**:`start(mode="gui")` 先 probe `port_preference`,握手
     通过 → 直接 attach 不 spawn,标 `_attached_external=True`。从根上消除
     "spawn 出孤儿" 副作用
  2. **`mode` property 改为反映实际行为**:`attach_only OR attached_external`
     都报 `"attach"`,banner 明示 "attach 到现有 GUI(端口 N)... stop_session
     不会关闭这个 GUI"
  3. **`stop()` 跳过外部 attach 进程**:`not self._attach_only and not
     self._attached_external` 时才走优雅 exit + taskkill
  4. **`list_sessions(probe_external=True)` 主动 probe `9999..10003`**:命中的
     端口列为 `owner="external"` + `mode="external"` + `session_id="<external@N>"`,
     note 注明"未由 MCP 启动 / stop_session 无权关闭"
  5. **`status_dict` 暴露 `port` + `attached_external`**:让 list_sessions /
     调试时一眼看出命令落到哪
  6. 新增 `probe_vmcp_server(host, port, timeout)` 同步 helper(用于
     `list_sessions`,绕开 event loop);async 路径仍走 `_handshake()`

  **验证**:`tests/test_probe_then_attach.py` 12 项隔离测试(全部用 OS 分配的
  ephemeral port,绝不撞 9999),覆盖 probe 正常 / 端口空 / 协议错 / 超时 /
  oversize length / GuiSession attach 路径 / stop 不杀外部进程 / spawn fallback /
  list_sessions external entry / probe_external=False / 已管理端口去重。

### Updated Files

- `src/vivado_mcp/vivado/gui_session.py`(probe_vmcp_server + _try_attach_existing +
  _attached_external + mode 语义修复 + stop 守卫)
- `src/vivado_mcp/vivado/session_manager.py`(list_sessions probe_external 参数
  + _EXTERNAL_PROBE_PORTS 配置)
- `src/vivado_mcp/vivado/base_session.py`(status_dict 暴露 port + attached_external)
- `tests/test_probe_then_attach.py`(新文件,12 项隔离回归测试)
- `tests/test_session.py`(`test_list_empty` 改用 `probe_external=False`)
- `.trellis/spec/backend/tcl-protocol-guidelines.md`(增 §7 端口池 + attach 语义)

---

## [0.3.18] — 2026-05-23

### 修复(0.3.17 实战暴露的 sim 工具链可用性问题)

- **Bug-1:中文 Windows stdio mode 输出乱码 ⭐** —— `run_tcl` / `report_*`
  返回的路径含中文时全是 `���` 或 `锟斤拷锟斤拷锟斤拷`。根因:Vivado 子进程 stdout 在
  中文 Win 默认 CP936(GBK)编码,但 `session.py` 4 处 decode 强制
  `raw.decode("utf-8", errors="replace")` 把 GBK 字节全 replace 成 U+FFFD。
  修:新增 `vivado/tcl_utils.py::decode_vivado_output(raw)` —— UTF-8 严格 decode +
  含 U+FFFD 时 fallback `mbcs`(Windows 系统 code page);`session.py` 4 处全部
  改走新 helper。**关键认知**:fallback 不是兜底,是和 Vivado stdout 实际编码
  对齐的完整解;hex 双向 transport 在没具体未解 case 前属预防性重构,违反
  less-is-more,本版本不做。
- **Bug-2:GUI session 中文路径范围扩展(quirks 文档化) ⭐** —— 0.3.13 quirks
  §1 只覆盖"工程根目录中文 → 综合 elaborate 崩 TclStackFree"。0.3.17 实战
  补充:**GUI session 内 `cd D:/项目/...` / `open_project D:/项目/xxx.xpr`
  同样触发 TclStackFree**,session 直接挂掉,范围不限于综合阶段。Vivado 2019.1
  C++ 层 bug 无法从 MCP 修,但 `_check_ascii_paths` 警告文本扩了 GUI 范围
  + quirks §1 扩"影响范围"段。

### 知识库沉淀(用户 0.3.17 会话踩过的 9 项 sim 坑)

`.trellis/spec/backend/vivado-quirks.md` 新增/扩段:

- **§5 重命名为"Tcl 命令替换 + filter 表达式陷阱"**,新增子段:
  - §5.2 `puts "[X]"` 命令替换(Note-6) → 改 `puts {[X]}`
  - §5.3 `=~ filter` 中 `[N]` 单字符是字面非 glob 字符类(Note-5) → 不要转义
- **新增 §8 XSim 仿真 Tcl 写法陷阱**,7 个 sub-section:
  - §8.1 `add_wave` / `get_objects` 拒 escaped id → 必须 `current_scope` + short name
  - §8.2 `if-generate` 命名块不是 scope → 内部 reg 无法 add_wave(0.3.17 实战
    中 `decim_cnt` / `strobe_pipe` / `align_reg` / `valid_d` 4 个 reg 全因此弃)
  - §8.3 `add_wave_group` ⭐⭐⭐ 必须 `-into $g`(最高频踩坑)
  - §8.4 `remove_wave` 只认 `[get_waves *]`,`-all` / `*` / `-of_objects` 都不工作
  - §8.5 `xsim -tclbatch` 在 EOF 不自动 quit,**必须显式 `quit`**
  - §8.6 `get_scopes` 不支持多 path 参数
  - §8.7 `size > 1` filter 对 escaped id 总线对象无效
- **新增 §9 stdio mode 中文输出乱码(0.3.18 修复)** 说明 + 为什么不上 hex transport

### 自动注入(让 AI 调 `run_tcl` 时主动看见 sim 坑)

`tools/tcl_tools.py::run_tcl` docstring 末尾增 "XSim 仿真常见坑摘要" 段,精选
4 个最高频 (§8.3 add_wave_group / §8.1 escaped id / §8.4 remove_wave / §8.5
xsim quit) + 1 个高频混淆点(§5.1 `[N]` 命令替换)。MCP 客户端在工具签名注入
时会读 docstring,AI 每次拿到 `run_tcl` 描述都能看到。

### Less-is-more 拒绝声明

`spec/backend/quality-guidelines.md §1.4 拒绝案例表` 新增一行:wave 类 helper
(`find_scope` / `add_signals_to_group` / `clear_wave_config` / `source_tcl_file`
4 个提议)**全部拒**。等价用法一行 `run_tcl` 即可,已落进 docstring + quirks。

### 测试

- 测试总数:437 → 446(+9 `test_session_encoding.py` decode fallback 覆盖
  UTF-8 / GBK / mbcs / 非法字节 / robustness;+1 `test_session.py` 锁住 GUI
  范围扩展的警告文本断言)
- ruff `All checks passed!`

## [0.3.17] — 2026-05-22

### 修复(0.3.16 实测发现的 3 个 sim 诊断漏洞)

- **Bug D:`LAUNCH_SCRIPTS_AND_GLOB` glob 漏 `/xsim` 三层路径 ⭐** ——
  Vivado 2019.1 实际 layout 是 `<sim_fs>/<phase>/xsim/<step>.{bat,sh}`(三层),
  跟 `TAIL_SIM_LOGS` 的 `*/xsim/*.log` 一致。但 0.3.16 fallback 模板写的是
  两层 `$__sim_dir/*/compile.bat` —— glob **永远空**,触发 `-scripts_only` 但
  拿不到 .bat,fallback 输出"未跑任何 .bat / .sh"完全失效。修:glob 模式补
  `/xsim/` 中间层。

- **Bug E:`RUN_BAT_STEP` 没 cd 到 `xsim_dir`,.bat 内相对路径全错位** ——
  Vivado 生成的 compile.bat 内部用 `-prj tb_xxx_vlog.prj` 等**相对路径**引
  依赖文件(假设 cwd = xsim_dir,这是 Vivado launch_simulation 自己的约定)。
  0.3.16 fallback `exec cmd /c <bat>` 没切 cwd → 抓到 "Unable to open project
  file"假错(实际是 cwd 错位诱发),把真错盖了。修:`cd [file dirname $__bat]`
  后再 exec,跑完 `cd $__orig_pwd` 复原。实测:compile + elaborate 全 rc=0,
  xvlog 完整分析所有 RTL,xelab 出 snapshot。

- **Bug C(0.3.16 实现 bug 补完):24H2 默认无注册表键时漏报** ——
  0.3.16 `_check_win_curdir_policy` 把"注册表无值"硬编码为"不警告",但 Win 11
  24H2 起微软改默认值 = 1(无键即等价于开启)。导致 24H2 用户**没改注册表**
  时 banner 不警告,正中我们的目标用户群体。修:加 `_is_win11_24h2_or_newer()`
  读 `sys.getwindowsversion().build >= 26100`,无键 + 24H2+ → 警告;HKCU 显式
  = 0 → 不警告(opt-out)。

### 关键认知校准(可让后人少走弯路)

- 0.3.16 推测过"是否 spawn vivado 时注入 env `NoDefaultCurrentDirectoryInExePath=0`
  绕开策略"。实测**不行**:Win 24H2 起 cmd.exe **只读注册表,忽略同名 env var**。
  Tcl `::env(NDCD)=0` 子进程能读到,但 cmd 自身仍按注册表行为(`compile.bat`
  无路径前缀照样报"不是命令")。**唯一根治路径 = 改注册表 + 注销重登**。
  fallback 用绝对路径 `cmd /c <full-path>/compile.bat` 是合法绕道。

### 测试

- 测试总数:436 → 437(+1 `_is_win11_24h2_or_newer` 分支,改原"无值=不警告"
  → 拆成老 Win / 24H2+ / 显式 = 0 三个 case)
- ruff `All checks passed!`

## [0.3.16] — 2026-05-22

### 修复(0.3.15 实战定位的 3 个核心问题)

- **Bug A:`get_property DIRECTORY` 在 sim fileset 上返回空 ⭐ 最严重** ——
  0.3.14 和 0.3.15 的 `TAIL_SIM_LOGS` / `LAUNCH_SCRIPTS_AND_GLOB` 都依赖这个属性
  取仿真目录,但 Vivado 2019.1 simulation fileset 的 DIRECTORY 属性**是空的**。
  导致两版 sim 诊断**全部走错分支**(直接报 "找不到 fileset"),用户实战 6 小时
  才定位。0.3.16 改用 `<proj_dir>/<proj_name>.sim/<sim_fs>` 命名约定推导。

- **Bug B:0.3.15 fallback 方向走错,Python subprocess → Tcl exec 返工** ——
  0.3.15 用 Python subprocess(MCP 进程 spawn .bat),实战发现 **MCP 进程 PATH
  不带 vivado/bin** → 跑 compile.bat 时内部 `call xvlog` 报 "xvlog not_recognized"。
  这是假阳性,真错是 Vivado wrapper 失败。0.3.16 改用 **Vivado session 内
  `exec cmd /c <完整路径>`**:Vivado session PATH 自带 vivado/bin + 完整路径绕开
  cwd-in-PATH 安全策略,两个根因都被绕开。新增 `RUN_BAT_STEP` Tcl 模板 +
  `parse_bat_run_output` 解析(处理 `errorcode=CHILDSTATUS pid rc` /
  `errorcode=NONE`(stderr+rc=0)等 Tcl exec 边界)。

- **Bug C:Win 11 24H2+ `NoDefaultCurrentDirectoryInExePath` 真根因检测** ——
  这是 launch_simulation 失败的**真正根因**:Win 11 24H2 默认开 = 1,Vivado 2019.1
  内部 spawn `compile.bat`(无路径)被该策略 block,12 个 USF-XSim ERROR 全是
  连锁反应。MCP 0.3.16 起 `start_session` 自动读 HKCU + HKLM 注册表,检测到 = 1
  就在 banner 警告 + 给用户根治命令:
  ```cmd
  reg add "HKCU\Environment" /v NoDefaultCurrentDirectoryInExePath /d 0 /f
  ```
  注销/重登生效。**MCP 不自动改用户注册表**(less-is-more + 用户系统主权)。

### Spec 文档

- `vivado-quirks.md` case 2 增补:
  - Win 11 24H2 真根因(顶在最显眼位置)
  - 注册表根治命令
  - 0.3.16 fallback 用 Tcl exec 而非 Python subprocess 的设计理由

### 测试

- 测试总数:428 → 436(+4 个 `parse_bat_run_output` + 3 个
  `TestWinCurdirPolicyCheck` + 改 3 个 `TestSimBatFallback` 适配新协议)
- ruff `All checks passed!`

## [0.3.15] — 2026-05-22

### 修复

- **`launch_simulation` 失败但 `xsim/*.log` 全空时的盲区** —— 0.3.14 已会
  glob 这些日志,但 0.3.13 实战又发现一个 case:Vivado wrapper 在生成
  `compile.bat / elaborate.bat / simulate.bat` 之后、spawn 它们之前就崩,
  xsim 工作目录里有 `.bat` 但**一个 `.log` 都没有**,0.3.14 sim 诊断 glob
  拍空,只能输出"未找到任何 xsim 日志文件"。同时 `get_msg_config -count`
  显示 12 个 ERROR 但 `-rules`/`-id` 拿不到具体内容(Vivado 2019.1 quirk)。

  0.3.15 起 sim 诊断在 `xsim/*.log` 缺失时自动走 **scripts-only fallback**,
  复刻用户已验证的绕过工作流:
  1. Vivado 内 `launch_simulation -scripts_only` 触发脚本生成
  2. MCP 进程自己 `cmd /c compile.bat` / `bash compile.sh` 等顺序跑,
     遇错即停,捕获每步 returncode + stdout/stderr 尾
  3. `simulate` 步骤跳过(会拉 xsim/GUI 阻塞),只回带脚本路径
  4. compile/elaborate 全过 → 关键结论:"Vivado wrapper 失败而非 xvlog/xelab
     本身失败,用户可用 -scripts_only + 外部 shell 工作流绕过"
  5. 任一步失败 → 把 stderr 摘要呈现,把 Vivado 吞掉的真错(找不到模块、
     语法错误等)暴露给 AI

  PRD locked: `.trellis/tasks/05-22-05-22-launch-sim-bat-fallback/prd.md`

### Spec 文档

- `vivado-quirks.md` case 2 增补 scripts-only fallback 描述 + 用户手动绕过
  工作流(`launch_simulation -scripts_only` + 外部 shell 跑 `.bat`)

### 测试

- 新增 8 个测试:`parse_launch_scripts_output` 4 个、`format_bat_steps_section`
  5 个、`TestSimBatFallback`(end-to-end mock subprocess)3 个。测试总数
  420 → 436(实际 428,小修了重复的 0.3.14 `test_no_logs_found`)。

## [0.3.14] — 2026-05-22

### 修复(0.3.13 实战发现的 5 个诊断盲区集中补)

- **`get_critical_warnings` 漏报非标 ERROR** —— Vivado 内部异常(中文路径触发的
  `TclStackFree`、`abort()`、`Segmentation fault`、xvlog 子进程 `'xxx' 不是内部或
  外部命令` 等)**不进 Vivado messageDb**,只在 runme.log 尾部 stderr 一行。0.3.13
  及之前 `get_critical_warnings` 只 grep `CRITICAL WARNING:` / `ERROR:` 前缀,这类
  非标错误全部漏报,AI 收到的反馈是 "未发现 ERROR 或 CRITICAL WARNING" —— 完全相反
  于实际情况。0.3.14 起:`errors=0` 且 `cw=0` 但 run STATUS 含 ERROR 时,自动 tail
  runme.log 最后 50 行,扫描非标关键词,命中就拼成"非标错误"段附在主报告下面,
  并按命中关键词给定向修复 hint(中文路径 → ASCII 迁移、xvlog 未找到 → 查 PATH 等)。

- **`launch_simulation` 失败时真错被吞** —— Vivado spawn `compile.bat` 调 xvlog/xelab/
  xsim,真错在 `<proj>.sim/sim_1/behav/xsim/*.log`(**不在** runme.log),0.3.13 之前
  MCP 完全没读这些路径,AI 只能看到 `failed due to earlier errors` 的二手错误。
  0.3.14 起:`get_critical_warnings(run_name='sim_1')` 自动 glob
  `<sim_fs>/*/xsim/*.log`,每个文件 tail 末尾 N 行 + 扫非标关键词,把真错暴露出来。

- **中文路径无预警** —— Vivado 2019.1 在 Windows 中文路径下已知会触发 TclStackFree
  内部 bug(0.3.13 实战遇到,工程目录搬到纯 ASCII 才好)。0.3.14 起 `start_session`
  自动检测 `vivado_path` / `cwd` 是否含非 ASCII 字符,触发醒目警告(只 warn 不 block,
  因为不是所有 2019.x 都必崩,且源文件中文路径仍可用)。

- **`run_tcl` / `safe_tcl` 失败缺诊断指引** —— AI 拿到 rc=1 不知道下一步该调什么。
  0.3.14 起 `_safe_execute` 在 Tcl 命令命中 run/sim 失败模式(`launch_simulation` /
  `launch_runs` / `synth_design` 等)且 rc!=0 时,自动追加一条 hint 指向
  `get_critical_warnings`。

- **launch_simulation 状态污染** —— `launch_simulation` 失败后 close_design + reset_run
  常常救不回,Vivado simulator runtime 内部状态残留。0.3.14 起仿真诊断输出末尾追加
  "考虑 close_sim / stop_session → start_session 重启" 提示。

### 新增 spec 文档

- **`.trellis/spec/backend/vivado-quirks.md`** —— 集中沉淀 7 条 Vivado 2019.1 / XSim
  实战 quirks:中文路径 TclStackFree、launch_simulation 真错位置、`set_property generic`
  类型反转、XSim add_wave 不认 `-label`/`-divider`、Tcl 命令替换吃 generate 层级路径
  `[0]` 等。把"踩了才知道"的坑变成可读的 case 库,新人 / AI 调试时能直接命中。

### 测试

- **391 → 415**(+24):
  - `tests/analysis/test_warning_parser.py`:`scan_nonstandard_errors` / `format_nonstandard_section` /
    `parse_tail_runme_output` / `parse_sim_logs_output` 4 个新函数共 17 条单测 + 2 个
    fixture log 文件(中文路径 TclStackFree、xvlog not in PATH)
  - `tests/test_diagnostic_tools.py`:`get_critical_warnings` 非标 fallback + sim 路径
    分支共 7 条集成测试
  - `tests/test_session.py`:`_check_ascii_paths` + `_safe_execute` hint 共 7 条单测

## [0.3.13] — 2026-05-02

### 修复

- **`__version__` 自 0.2.0 起一直停在 "0.2.0",`vivado-mcp version` CLI 长期打印错版本** —— `src/vivado_mcp/__init__.py` 硬编码 `__version__ = "0.2.0"` 从未跟随 pyproject.toml 更新。直接后果:用户照 README 「通过 Code Agent 提交 Bug」段跑 `vivado-mcp version` 上报版本号 → 全部报 "0.2.0",issue triage 看到老版本号会找错方向。
  - **修复**:`__init__.py` 改用 `importlib.metadata.version("vivado-mcp")` 单一来源,wheel 元数据改了 `__version__` 自动跟。包没装(直接跑源码)时兜底为 `"unknown"`。
  - **回归锁定**:新增 `tests/test_version.py`,断言 `__version__` 与 `pyproject.toml` 严格一致 + 格式必须是 `\d+\.\d+\.\d+`。再忘记同步会立刻 CI 红灯。

### 发布链路

- **publish.yml 加 `verify` job,在 PyPI 真出现新版本前不许 workflow 绿** —— 0.3.9/10/11 三个 tag 因 `test` job 跨平台问题静默失败到 0.3.12 才发现(`needs: [lint, test]` 链断了 publish 跳过,但顶层 workflow 状态被 GitHub Actions 报成 success)。新 verify job 在 publish 后回打 `https://pypi.org/pypi/vivado-mcp/json`,轮询 5 分钟拿不到新版本就红灯,彻底闭环。

### 测试

- **373 → 375**(+2):新增 `test_version.py` 两条断言。

## [0.3.12] — 2026-04-25

### 修复(CI 跨平台)

- **`test_readonly_project_dir_falls_back` 在 Linux runner 上挂掉,导致 0.3.9/0.3.10/0.3.11 三个 tag 的 publish.yml 全部失败,PyPI 自 0.3.8 起停滞** —— 子代理在 0.3.9 写测试时用 `Z:/nonexistent/...` 假盘符模拟"项目目录不可写",本地 Windows 因为没 Z 盘所以 fallback 触发、断言通过;但 GitHub runner 是 Linux,`Z:` 不是盘符只是合法目录名,Linux 把它当相对路径直接 mkdir 成功,fallback 没触发 → AssertionError → test job 红 → build/publish 跳过(`needs: [lint, test]`) → PyPI 没收到包。
  - **修复**:`monkeypatch Path.mkdir`,只对项目目录下 `.vmcp` 抛 OSError,fallback 目录走原行为。跨平台一致,不再依赖盘符假设。
  - **副作用**:这一改也补上 0.3.9/0.3.10/0.3.11 三个 tag 的 PyPI 发布 —— 0.3.12 一次性把 README 重组、违例路径建议、CW 差分、B16 修复都带上 PyPI。

### 经验教训

- 子代理写跨平台测试要主动想"在 Linux 上这条断言会怎样"。`Z:/` 看似"明显非法"其实只对 Windows 成立。
- WebFetch 看 GitHub Actions 页面拿不到 job 级别状态,顶层"success/failure"图标会误导(本案它把 failed 报成 success);要诊断 publish 失败必须人工进 run 详情页或用 gh CLI。

## [0.3.11] — 2026-04-21

### 文档(纯 docs 发布)

- **README 重组使用示例段** —— 删掉散的 5 个小段(基本流程 / 诊断 / 验证引脚 / XCI / 任意 Tcl),换成一条贯穿的实机调试闭环故事:`get_critical_warnings` 看 ERROR 详情 → `xdc_auto_fix` 修 XDC → `compare_with_last` 差分验证修复生效 → 时序违例时 `get_timing_report` 自动给 HIGH_FANOUT + MAX_FANOUT 建议 → 烧板。所有输出片段都来自 Vivado 2019.1 实机(basys3_uart + xdma_bd_test)。
- **头部「0.3 系列新增」置顶 0.3.9 两条**:时序违例自动定位(5 种模式 + Tcl 修复命令)、CW 修复效果差分可视化。0.3.6 之后的两次迭代在 README 头部终于可见。
- **工具表补全**:`get_timing_report` / `get_critical_warnings` 两行描述补上 0.3.9 关键字。
- 同步 PyPI README,让包详情页跟上 GitHub。

### 无代码改动

- 373 tests 不变,无 API 变化,安全升级。

## [0.3.10] — 2026-04-20

### 修复(field test 发现的老 bug)

- **B16 [P1] `get_critical_warnings` 在 tcl 模式下 ERROR 详情全部被吞** —— 实机用 Vivado 2019.1 + basys3_uart 项目测试 0.3.9 时发现:`place_design ERROR` 状态的项目调用工具只返回 "!! 发现 3 条 ERROR !!" 表头,无任何详情;`compare_with_last` 也没追加差分段。
  - **根因**:`EXTRACT_ERRORS` Tcl 脚本输出 `VMCP_ERR:行号|文本` 前缀,与 `SubprocessSession` 内部 sentinel 协议的 `VMCP_ERR: $__out`(见 `tcl_utils.py wrap_command`)**命名冲突**。session 层对所有 `VMCP_ERR:` 开头的行做前缀剥离,把我们应用层输出的 ERROR 详情吞成了 `行号|文本`(前 10 字符被砍掉),`parse_errors` regex 匹配失败 → 空列表 → 报告无内容。
  - **为什么长期没发现**:单元测试用 mock 直接注入 `VMCP_ERR:` 字符串,完全绕过 session 层,测试永远 pass。GUI 模式走 JSON 协议不走前缀剥离所以不受影响。这个 bug 从 0.3.0 引入 `EXTRACT_ERRORS` 至今一直潜伏。
  - **修复**:`EXTRACT_ERRORS` 输出改为 `VMCP_RUNLOG_ERR:行号|文本` 明确区分命名空间。同步更新 `parse_errors` regex + 4 处测试 mock 字符串。

### 经验教训

- 单元测试 mock 拍在解析器输入上,绕过了 session 传输层 —— 这类"协议穿透"问题靠 mock 测不出来,必须实机跑。考虑以后加一个 "session 原样透传"的集成测试(走 subprocess session 但跑 `puts "VMCP_RUNLOG_ERR:test"` 然后验证 Python 拿到的是原文)。
- 前缀命名要能看出是"谁的地盘"—— `VMCP_ERR:` 两边都在用,`VMCP_RUNLOG_ERR:` / `VMCP_CW:` / `VMCP_DIAG:` 这种语义化前缀不容易撞。

### 测试

- 373 → 373,全部依然绿。mock 字符串全量更新到新前缀。

## [0.3.9] — 2026-04-20

### 增强(现有工具扩能,不新建 MCP tool)

- **`get_timing_report` 违例路径详情 + 修复建议** —— 时序违例时自动跑二次 `report_timing -max_paths 10`(setup) + `-max_paths 5`(hold),解析 Top N 违例路径(起点/终点/slack/logic+route+skew 延迟分解/levels),嗅探 5 种模式自动给中文修复建议:
  - `CDC` —— 起止时钟不同 → 建议加 2 级同步器或 `set_false_path`
  - `IO_UNREGISTERED` —— 起止为顶层端口 → 建议加 IOB 寄存器 + `set_property IOB TRUE`
  - `HIGH_FANOUT` —— route_delay > 3× logic_delay → `report_high_fanout_nets` + `MAX_FANOUT` 约束
  - `LONG_COMBO` —— levels > 15 或 logic > 2× route → 切流水线寄存器
  - `UNKNOWN` —— 兜底引导手动 `report_timing -from -to`
  - 时序 PASS 时跳过二次查询省 10-30s;异常降级不阻断主报告。
- **`get_critical_warnings` 快照差分(compare_with_last)** —— 每次调用静默写快照到 `<project>/.vmcp/last_cw_{run}.json`(无项目 fallback 到 `~/.claude/vivado-mcp/`)。启用 `compare_with_last=True` 时读上次快照做 diff,按 warning_id + port + pin + source_file + normalized_message_hash 的指纹识别:
  - `[-]已消除` —— 修对了,结论用"修复生效"鼓励反馈
  - `[+]新出现` —— 改坏了,结论用"回滚检查"警告
  - `[=]仍存在` —— 没改到点子上
  - 指纹剥离行号,XDC 改动导致行号漂移不会被误判为新 CW。向后兼容:不加参数时行为不变,只是偷偷写快照。

### 测试

- **328 → 373**(+45):新增 `TestViolatingPath` / `TestAnalyzePath` / `TestFormatViolatingPaths` / `TestGetTimingReportWithPaths` / `TestWarningSnapshot` / `TestDiffWarnings` / `TestCompareWithLast` 等 7 个测试类。`test_diagnostic_tools.TestGetCriticalWarnings` 加 `autouse` 的 `_isolate_home` fixture 防止快照写污染真实 home。
- 新 fixture `tests/fixtures/sample_violating_paths.txt` 覆盖 5 种违例模式。

### 设计哲学同步

本轮遵守 `tcl_tools.py` 的原则 —— "工具的存在应该是因为它提供 Tcl 做不了或做不好的本地价值"。两个功能都选择**扩已有工具**而非新建 MCP tool:建议段集成到时序报告末尾,差分是 CW 工具的一个开关。避免工具集臃肿。

## [0.3.8] — 2026-04-20

### 修复(Bug 修复包,8 项漏洞)

- **B15 [P1] `iverilog-check` hook 重蹈 B14 覆辙** —— hook 脚本硬编码 `shutil.which('iverilog')` 判断工具存在,Windows+scoop 的 PATH snapshot 问题下永远返回 None,装了 iverilog 也永不触发。改为调 `compile_check(..., tool='auto').tool_available`,复用 0.3.5 的 scoop fallback。
- **[P1] 7 处 `except Exception: pass` 静默吞错(违反 CLAUDE.md 1.4)** —— 最严重:`get_pre_commit_summary` 项目没打开时 4 次 pass 后 verdict 仍 `[READY]`,误导用户贴假摘要进 commit。现在:全部 `logger.warning` 记录真实原因;`pre_commit` 增加 `[DEGRADED]` verdict + 采样失败列表显示;`check_bitstream_readiness` 时序查询失败时把具体异常展示给用户;`generate_bitstream` 前置安全检查失败时 logger 记录并在后续流程保留降级标记。
- **[P1] `generate_bitstream` 未同步 D5 Python 轮询架构** —— 仍用 Tcl `wait_on_run` 阻塞 Vivado event loop,GUI 模式下冻住界面且无进度反馈。重构为 `launch_runs` + Python 2s 轮询 STATUS/PROGRESS + `ctx.report_progress`,与 synthesis/implementation 对齐。
- **[P2] `open_run` catch 不看 `__open_err`** —— 旧写法 `catch { open_run } __open_err` 后只看 Tcl 外层 is_error(永远 false),错误被吞致后续 `report_*` 在旧 design 上跑。改为 `if {[catch { open_run } __open_err]} { puts VMCP_OPEN_ERR:$__open_err }`,Python 侧 grep 并在 "already open" 之外的错误上告警。
- **[P2] `program_device` 不校验 bitstream 路径** —— 用户传错路径要等到 `program_hw_devices` 才报 file not found,此时 hw_server/target 已连上留下脏状态。入口加 `os.path.isfile` + `.bit` 扩展名预检。
- **[P3] `list_sessions` 偷偷删死会话** —— 违反"查询无副作用"原则,AI 链式调 `list → stop` 会拿到误导的"会话不存在"。拆出 `prune_dead()` 显式清理,`list_sessions` 纯只读。
- **[P3] 临时 Tcl 脚本无 atexit 兜底** —— GUI 模式下 MCP server 被强杀时 `/tmp/tmp*.tcl` 堆积。新增全局 `_TMP_SCRIPTS` 集合 + `atexit.register` 清理钩子,正常 stop() 路径从集合移除避免重复 unlink。
- **[P3] DRY:XDC 文件列表 Tcl 串重复 3 处** —— `diagnostic_tools.py` 里 `verify_io_placement_tool` / `xdc_lint` / `xdc_auto_fix` 都硬编码同一段 `foreach __f [get_files ... FILE_TYPE == XDC] ...`。抽 `LIST_PROJECT_XDC_FILES` 常量到 `tcl_scripts.py` + `_fetch_project_xdc_paths(session)` 共享函数。

### 测试

- **328 pass → 328 pass**:同步 `test_allows_with_force` 的 mock side_effect 到新的 launch+poll+bit_dir 三步调用序列。

### 变更统计

8 文件 / +284 / -103 行。无 API 破坏。

## [0.3.7] — 2026-04-18

### 文档

- README 同步到 0.3.6 代码实况:25 工具 + 5 Hook 完整列表、"新手引导 & 工程摸底"新分类、`iverilog-check` hook、`get_critical_warnings` 19 种已知 ID 等。纯文档发布,让 PyPI 页面的 README 与 GitHub 一致。

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
