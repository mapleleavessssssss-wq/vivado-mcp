# vivado-mcp

[![PyPI version](https://img.shields.io/pypi/v/vivado-mcp)](https://pypi.org/project/vivado-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/vivado-mcp)](https://pypi.org/project/vivado-mcp/)
[![License](https://img.shields.io/github/license/mapleleavessssssss-wq/vivado-mcp)](LICENSE)
[![CI](https://github.com/mapleleavessssssss-wq/vivado-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mapleleavessssssss-wq/vivado-mcp/actions/workflows/ci.yml)

精简的 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) Server，通过 **25 个工具 + 5 个智能 Hook** 控制 Xilinx Vivado EDA——少即是多。

> **0.3 系列新增了什么**:
> - **时序违例自动定位(0.3.9)**:`get_timing_report` 违例时自动跑 `report_timing -max_paths 10`,嗅探 5 种模式(CDC / HIGH_FANOUT / LONG_COMBO / IO_UNREGISTERED / UNKNOWN)并给出具体 Tcl 修复命令,不再让你对着时序日志发呆
> - **CW 修复效果可视化(0.3.9)**:`get_critical_warnings(compare_with_last=True)` 对比上次快照,报告"已消除 N 条 / 新出现 N 条 / 仍存在 N 条",让"改 XDC 有没有改到点子上"直接有数
> - **长任务可视化**:`get_run_progress` 让 10-30 分钟的综合/实现不再是黑盒
> - **新手引导**:`get_next_suggestion` 根据项目状态告诉你下一步该干啥
> - **XDC 一键自修**:`xdc_auto_fix` 自动补 IOSTANDARD 和 create_clock period
> - **外部 Verilog 预检**:`verilog_compile_check` 用 iverilog/verilator,比 Vivado 综合快 50 倍
> - **IP 老化检测**:`get_ip_status` 扫出项目里哪些 IP 需要升级
> - **Commit 摘要**:`get_pre_commit_summary` 生成 WNS/资源/CW 的 markdown 片段直接贴 commit body
>
> 详见 [CHANGELOG](CHANGELOG.md)。

## 设计哲学 — 为什么是 25 个工具而不是 500 个？

主流 Vivado MCP（如 SynthPilot）动辄 500+ 工具，每个工具本质是一行 Tcl 包装。问题是：

- **每个工具都占用 AI 上下文**（工具签名注入到每次系统提示）→ 调不调都烧 token
- **大模型比我们更会拼 Tcl**（`create_bd_cell` 这种就是写一行 Tcl 的事）
- **绝大多数 facade 工具做的事 `run_tcl("...")` 能做**

本项目只保留**真正有本地价值**的工具——Tcl 做不了或做不好的事：

1. **结构化解析**：IO / 时序报告 → JSON / 中文摘要（比原始表格省 token）
2. **本地知识库**：CRITICAL WARNING 按 ID 分类 + 中文修复建议（Tcl 里写这个太难）
3. **跨命令协议**：sentinel、会话管理、超时、比特流前置安全检查
4. **跨会话工具**：`compare_xci` 纯 Python 对比两个 XCI 文件，不需要 Vivado

其他（BD / 仿真 / XSCT / 硬件调试 / IP 配置等）全部交给 `run_tcl`，让大模型自己拼 Tcl。

## 特性

- **双模式会话**：默认 GUI 可视化（能看到 Vivado 图标 + Tcl Console 实时输出），也支持无头 CI 模式和 attach 已开 GUI
- **25 个精简工具 + 5 个智能 Hook** — 覆盖完整 FPGA 开发流程 + 智能诊断 + 新手引导 + 外部工具链联动
- **智能诊断** — 综合/实现后自动提取 CRITICAL WARNING / ERROR 分类 + 中文修复建议（含 19 种已知 ID）
- **IO 验证** — XDC 约束（**支持 -dict 和传统两种语法**）对比实际引脚分配，GT 端口不匹配标记为 CRITICAL
- **IP 调试** — 查询 IP 所有 CONFIG.* 参数（含 GUI 隐藏参数）、纯 Python 对比两个 XCI 文件
- **Bitstream 安全检查** — 生成比特流前自动检测 CRITICAL WARNING 并阻止（可 force 跳过）
- **结构化报告** — IO 和时序报告解析为 JSON，便于 AI 精确提取数值（**不再有"假 PASS"陷阱**）
- **安全转义** — `safe_tcl` 自动对路径/标识符做 Tcl list 转义，Windows 含空格/中文/$ 的路径也能用
- **多会话支持** — 同时管理多个独立 Vivado 实例（端口池 9999-10003）
- **跨平台** — 支持 Windows 和 Linux
- **零额外依赖** — 仅依赖 `mcp` SDK

## 快速开始

### 1. 安装

```bash
pip install vivado-mcp
```

### 2. 注入 Vivado（一次性）

```bash
vivado-mcp install
```

这会修改你 Vivado 的 `Vivado_init.tcl`，让以后启动 GUI 时自动开启 TCP server（端口 9999-10003）。**原文件会备份**，`vivado-mcp uninstall` 可恢复。

如果 Vivado 装在受保护目录（如 `C:\Program Files\`），用管理员身份运行命令即可。

### 3. 配置 Claude Code

将以下内容复制到 `~/.claude.json` 的 `mcpServers` 字段中：

```json
"vivado": {
  "command": "python",
  "args": ["-m", "vivado_mcp"],
  "env": {
    "VIVADO_PATH": "D:/Xilinx/Vivado/2024.1/bin/vivado.bat"
  },
  "type": "stdio"
}
```

> 将 `VIVADO_PATH` 替换为你的 Vivado 实际路径：
> - **Windows**: `"D:/Xilinx/Vivado/2019.1/bin/vivado.bat"`
> - **Linux**: `"/opt/Xilinx/Vivado/2024.1/bin/vivado"`
> - 也可以不设置 `VIVADO_PATH`，将 Vivado `bin` 目录加入系统 `PATH`。

### 4. 重启 Claude Code

配置完成后重启 Claude Code，即可使用 25 个 Vivado 工具。

<details>
<summary>从源码安装（开发/贡献）</summary>

```bash
git clone https://github.com/mapleleavessssssss-wq/vivado-mcp.git
cd vivado-mcp
pip install -e ".[dev]"
```
</details>

## 会话模式

`start_session` 工具支持三种模式：

| mode | 效果 | 适合 |
|---|---|---|
| `"gui"` (默认) | MCP 自动 spawn `vivado -mode gui`，你能看到 Vivado 图标 | 交互开发、实时观察波形/原理图 |
| `"tcl"` | `vivado -mode tcl` 无头子进程 | CI、批处理、不需要 GUI |
| `"attach"` | 连接到你已手动打开的 Vivado GUI | 想在 GUI 里手动捣鼓 + AI 辅助 |

```
用户: 启动 GUI 会话
AI: [调用 start_session(mode="gui")] → 桌面弹出 Vivado 窗口

用户: 批处理跑 10 个项目
AI: [调用 start_session(mode="tcl")] → 无 GUI，跑得更快
```

## 工具列表

### 会话管理
| 工具 | 说明 |
|------|------|
| `start_session` | 启动 Vivado 会话（gui/tcl/attach 三种模式） |
| `stop_session` | 关闭指定会话(B13 修复:taskkill /T 递归杀进程树 + 清 vivado_pid*.str) |
| `list_sessions` | 列出所有活跃会话 |

### Tcl 执行（核心）
| 工具 | 说明 |
|------|------|
| `run_tcl` | 执行任意 Vivado Tcl 命令——**AI 拼命令的主力** |
| `safe_tcl` | 带参数模板，自动 Tcl 转义，路径含空格/中文/$ 时使用 |

### 设计流程
| 工具 | 说明 |
|------|------|
| `run_synthesis` | 运行综合，Python 轮询不阻塞，完成后自动 open_run + 诊断 |
| `run_implementation` | 运行实现（布局布线） |
| `get_run_progress` | **0.3.2** 查 run 实时进度:Phase 序列 + log 尾部 + mtime,log 超 2 分钟不更新自动提示可能卡住 |
| `generate_bitstream` | 生成比特流（默认前置 CRITICAL WARNING 安全检查） |
| `program_device` | 编程 FPGA 设备（封装 open_hw_manager → connect → program） |

### 新手引导 & 工程摸底
| 工具 | 说明 |
|------|------|
| `get_next_suggestion` | **0.3.2** 11 档决策表:没项目 → open/create,没顶层 → set_property TOP,综合完成 → run_implementation...每档附可执行命令 |
| `get_project_info` | **0.3.0** 一次拿齐项目摸底:名称/part/顶层/源文件/XDC/IP/runs 状态 |
| `get_pre_commit_summary` | **0.3.4** 生成 markdown 工程摘要直接贴 commit body:项目/时序 WNS+WHS/资源/CW/READY-WARN-BLOCK 门禁 |

### 诊断(独家差异化)
| 工具 | 说明 |
|------|------|
| `get_critical_warnings` | 提取并按 ID 分类 CRITICAL WARNING + ERROR,含 19 种已知 ID 的中文修复建议。**0.3.9** 加 `compare_with_last=True` 参数:对比上次快照输出"已消除 / 新出现 / 仍存在" 差分,验证修改是否真修到点 |
| `check_bitstream_readiness` | **0.3.0** 烧板前一键 READY/WARN/BLOCK 综合判定 |
| `verify_io_placement` | 对比 XDC 约束（-dict/传统两种语法）与实际 IO 布局，GT 不匹配标为 CRITICAL |
| `xdc_lint` | **0.3.0** 纯 Python 静态 XDC 检查(PIN_CONFLICT / 漏 IOSTANDARD / DUPLICATE_PORT / CLOCK_NO_PERIOD / 跨文件冲突),不需 Vivado |
| `xdc_auto_fix` | **0.3.3** 自动补 IOSTANDARD + create_clock -period,dry_run 预览 + 板卡 profile(basys3/nexys/arty/zybo/kc705),不碰 PIN_CONFLICT |
| `verilog_compile_check` | **0.3.4** 用 iverilog / verilator 做语法 + 连接性检查,比 Vivado 综合快 50 倍。未装返回 SKIP + 安装指引,支持 Windows+scoop 路径自动发现 |

### IP 调试
| 工具 | 说明 |
|------|------|
| `inspect_ip_params` | 查询 IP 实例所有 CONFIG.* 参数（含 GUI 隐藏项），支持关键词过滤 |
| `compare_xci` | 纯 Python 对比两个 XCI 文件的参数差异（无需 Vivado 会话） |
| `get_ip_status` | **0.3.4** 检查哪些 IP 需要升级 / 被锁定 / 已最新,附 upgrade_ip 批量建议 |

### 结构化报告
| 工具 | 说明 |
|------|------|
| `get_io_report` | IO 引脚报告（JSON），自动判定 GT/GPIO 类型 |
| `get_timing_report` | 时序报告,含 PASS/FAIL 判定、**数据来源标注**(post-synth 估算 vs post-route 最终)、关键路径详情。**0.3.9** 违例时自动附 Top N 违例路径 + 5 种模式分类(CDC/HIGH_FANOUT/LONG_COMBO/IO_UNREGISTERED/UNKNOWN)+ 具体 Tcl 修复命令 |
| `get_utilization_report` | **0.3.0** 结构化资源占用(LUT/FF/BRAM/DSP/IOB),> 90% 标 CRITICAL,70-90% 标 WARN |

> 通用报告（power / drc / clock / methodology / cdc 等）请直接用
> `run_tcl("report_power -return_string")`，无需包装。

## 智能 Hook(Claude Code 独有)

仓库的 `.claude/settings.json` 预置了 **5 个 Claude Code hook**,让 AI 不只会"被动应答",还能**主动守门**:

| Hook | 触发事件 | 作用 |
|---|---|---|
| `bitstream-guard` | AI 调 `generate_bitstream` 前 | 拦截并提醒先跑 `check_bitstream_readiness`,避免时序违例时烧出废比特流 |
| `xdc-lint` | 保存任意 `.xdc` 文件后 | 纯 Python 静态检查:PIN_CONFLICT / 漏 IOSTANDARD / create_clock 缺 -period 等,无需等综合 |
| `verilog-lint` | 保存任意 `.v` / `.sv` 文件后 | 零依赖预检:module 名匹配文件名 / endmodule 存在 / 括号配对 |
| `iverilog-check` | 保存任意 `.v` / `.sv` 文件后 | **0.3.4** iverilog 或 verilator 语法+连接性检查,未装静默跳过,有 error 时阻断 |
| `session-guard` | Claude 停下时 | 扫 `vivado_pid*.str` 文件,提醒清理未关闭的 Vivado session |

首次打开本仓库时 Claude Code 会弹框:*"检测到项目配置了 hook,是否信任?"* — 选 **Yes** 即启用。

要禁用单个或全部:在 `.claude/settings.local.json`(个人本地文件,不进 git)写入 `{"hooks": {}}` 覆盖即可。

## 使用示例 — 一轮完整的调试闭环

下面是实机(Vivado 2019.1 + basys3 + Kintex-7)跑出来的真实片段,串起 `get_critical_warnings` → XDC 修复 → `compare_with_last` 验证 → 时序自动诊断 → 烧板的完整闭环。

### 起点:打开项目,立刻看 ERROR / CW 详情

```
用户: 打开 basys3_uart 工程看看为什么 impl 挂了
AI:  [start_session(mode="gui")]
     [run_tcl("open_project .../basys3_uart.xpr")]
     [get_critical_warnings(run_name="impl_1")]

     !! 发现 3 条 ERROR !!
     诊断概览: errors=3, critical_warnings=0, warnings=0

     --- [ERROR][DRC BIVC-1] IO_STANDARD_MISMATCH (1 条) ---
       首次出现: 第 165 行
       示例消息: Bank IO standard Vcc: Conflicting Vcc voltages in bank 14...
       建议: Bank 内 IOSTANDARD 不一致(同一 Bank 的端口用了不同电压,如 LVCMOS18 和 LVCMOS33)。
       修复: 在 XDC 给所有端口显式指定 IOSTANDARD,同 Bank 保持电平一致。

     --- [ERROR][Vivado_Tcl 4-23] DRC_FAILED (1 条) ---
       建议: DRC 失败导致 Placer 未运行。
       修复: 查看同一日志里前面的 [DRC xxx-N] 定位根因,常见是 BIVC-1/NSTD-1/UCIO-1。

     --- [ERROR][Common 17-39] STAGE_ABORT (1 条) ---
       建议: 前置阶段失败导致后续阶段未能启动。
```

诊断第一步就锁定根因:`BIVC-1` 在 `Bank 14` 有电压冲突,后两条 ERROR 都是连锁反应。

### 修 XDC + 再综合

```
用户: 按建议在 XDC 里给所有端口显式补 IOSTANDARD 再综合
AI:  [xdc_auto_fix(board="basys3", dry_run=false)]   # 0.3.3 一键补 IOSTANDARD
     [run_synthesis]
     [run_implementation]
```

### 验证真的修到了点 — `compare_with_last` 差分

```
用户: 确认一下修好了
AI:  [get_critical_warnings(run_name="impl_1", compare_with_last=True)]

     诊断概览: errors=0, critical_warnings=0, warnings=3

     === CW 差分报告(对比上次快照)===
     修复效果: 已消除 3 条 / 新出现 0 条 / 仍存在 0 条

     [-] 已消除(3):
       • [DRC BIVC-1] IO_STANDARD_MISMATCH
       • [Vivado_Tcl 4-23] DRC_FAILED
       • [Common 17-39] STAGE_ABORT

     结论: 修复生效,所有上次存在的问题均已消除。
```

不需要人工对比两份日志 —— 差分直接给结论:3 条 ERROR 都消了,**修复生效**。

### 时序违例?自动给 Tcl 修复命令(0.3.9 亮点)

换到一个 WNS 违例的大工程(PCIe XDMA):

```
用户: 时序没过,告诉我怎么改
AI:  [get_timing_report]

     === 时序摘要 === 状态: FAIL (时序违例)
       Setup  WNS = -9.247 ns  TNS = -495735.094 ns
              失败端点: 247261 / 247354
       Hold   WHS = +0.015 ns  THS = 0.000 ns

     --- 违例路径 Top 10 ---
       [1] SETUP slack -9.247 ns  [HIGH_FANOUT]
           起点: .../xdma_0/.../tready_thrtl_reg/C
           终点: .../pcie_7x_i/pcie_block_i/CFGPMTURNOFFOKN
           延迟分解: logic 0.309 ns  route 2.094 ns  skew -6.845 ns  levels=2
           建议: 布线延迟(2.094 ns)远大于逻辑延迟(0.309 ns),通常是高扇出或跨芯片走线。
                 先跑 `report_high_fanout_nets -fanout_greater_than 1000` 定位,
                 然后对主干寄存器加 `set_property MAX_FANOUT 50 [get_cells <...>]`,
                 或在综合阶段加 `-fanout_limit 50` 让工具自动复制寄存器。

       [2] SETUP slack -9.219 ns  [HIGH_FANOUT]
           ...
```

不再有"时序挂了,咋办"的空转 —— 工具直接按模式(CDC / HIGH_FANOUT / LONG_COMBO / IO_UNREGISTERED / UNKNOWN)分类,给出具体 Tcl 命令。

### 烧板前安全门 + 编程设备

```
用户: 修好后准备烧板
AI:  [check_bitstream_readiness]
     结论: READY — route_design Complete / WNS +0.026 ns / CW=8 (GT_LOC 都是 IP 内部,可忽略)

     [generate_bitstream]          # 自动拦截:有 CW 会阻止,可 force=True 跳过
     [program_device(bitstream_path="impl_1/top.bit")]
```

### 常用旁路工具

其他单次查询/对比用例:

```
# 对比两个 XCI 找出配置漂移
compare_xci(file_a="golden.xci", file_b="suspect.xci")
# → PF0_DEVICE_ID: A=9024 | B=9038
# → LINK_SPEED:    A=5.0_GT/s | B=8.0_GT/s

# 验证 GT 引脚实际布局是否和 XDC 一致(支持 -dict 语法)
verify_io_placement
# → !!! CRITICAL 不匹配 !!!  端口: pcie_7x_mgt_rtl_0_rxp[0]
#    XDC: AA4 | 实际: M6

# 任意 Tcl — AI 拼命令的主力
run_tcl("foreach p [get_ports] { puts \"$p: [get_property PACKAGE_PIN $p]\" }")
safe_tcl("set_property PACKAGE_PIN {0} [get_ports {1}]", args=["W5", "clk"])
```

## 架构

```
AI Tool (Claude/Cursor/Codex) ──(stdio MCP)──▶  vivado-mcp
                                                    │
                              ┌─────────────────────┼──────────────────────┐
                              │                     │                      │
                              ▼                     ▼                      ▼
                      SubprocessSession        GuiSession             GuiSession
                      (mode="tcl")             (mode="gui")           (mode="attach")
                              │                     │                      │
                     vivado -mode tcl        Popen + TCP:9999+       TCP:9999+ 连到
                     (子进程 stdio)          auto-spawn GUI          已开的 Vivado GUI
```

**核心协议**：
- **subprocess 模式**：`catch + UUID sentinel`（stdio 分帧，修复了 0.1.0 的行顺序 bug）
- **GUI/attach 模式**：TCP length-prefix framing（4 字节 BE + UTF-8 payload）
- 命令通过十六进制编码传输，防 Tcl 注入、支持任何字符

## CLI 参考

| 命令 | 说明 |
|---|---|
| `python -m vivado_mcp` | 启动 MCP server（供 AI 工具调用） |
| `vivado-mcp serve` | 同上 |
| `vivado-mcp install [path] [--port 9999]` | 注入 Vivado_init.tcl |
| `vivado-mcp uninstall [path]` | 从 Vivado_init.tcl 移除 |
| `vivado-mcp version` | 显示版本 |

## 开发

```bash
git clone https://github.com/mapleleavessssssss-wq/vivado-mcp.git
cd vivado-mcp
pip install -e ".[dev]"

# 运行测试（不需要 Vivado）
pytest

# 代码检查
ruff check src/ tests/
```

## 文档

- [CHANGELOG](CHANGELOG.md) — 版本变更历史
- [迁移指南 0.1 → 0.2](docs/MIGRATION_0.1_to_0.2.md) — 每个被删工具的 run_tcl/safe_tcl 替代
- [审计报告](docs/AUDIT_REPORT.md) — 0.1.0 的 7 个 bug 根因分析
- [IP 调试实践手册](docs/IP_DEBUG_GUIDE.md) — PCIe GT 映射调试、XCI 配置对比等实战

## 许可证

[Apache License 2.0](LICENSE)
