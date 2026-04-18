# vivado-mcp

[![PyPI version](https://img.shields.io/pypi/v/vivado-mcp)](https://pypi.org/project/vivado-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/vivado-mcp)](https://pypi.org/project/vivado-mcp/)
[![License](https://img.shields.io/github/license/mapleleavessssssss-wq/vivado-mcp)](LICENSE)
[![CI](https://github.com/mapleleavessssssss-wq/vivado-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mapleleavessssssss-wq/vivado-mcp/actions/workflows/ci.yml)

精简的 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) Server，通过 **15 个工具** 控制 Xilinx Vivado EDA——少即是多。

> **0.2.0 升级了什么**：新增 GUI 可视化模式（能看到 Vivado 图标）、修复 7 个关键 bug（含最致命的"假 PASS"时序报告）、删除 8 个 facade 工具（交给大模型拼 Tcl）。详见 [CHANGELOG](CHANGELOG.md) 和 [迁移指南](docs/MIGRATION_0.1_to_0.2.md)。

## 设计哲学 — 为什么是 15 个工具而不是 500 个？

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
- **15 个精简工具** — 覆盖完整 FPGA 开发流程 + 智能诊断 + IP 调试
- **智能诊断** — 综合/实现后自动提取 CRITICAL WARNING 分类 + 中文修复建议（含 12 种已知 warning）
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

配置完成后重启 Claude Code，即可使用 15 个 Vivado 工具。

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
| `stop_session` | 关闭指定会话 |
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
| `generate_bitstream` | 生成比特流（默认前置 CRITICAL WARNING 安全检查） |
| `program_device` | 编程 FPGA 设备（封装 open_hw_manager → connect → program） |

### 诊断（独家差异化）
| 工具 | 说明 |
|------|------|
| `get_critical_warnings` | 提取并按 ID 分类 CRITICAL WARNING，含 12 种已知类型的中文修复建议 |
| `verify_io_placement` | 对比 XDC 约束（-dict/传统两种语法）与实际 IO 布局，GT 不匹配标为 CRITICAL |

### IP 调试
| 工具 | 说明 |
|------|------|
| `inspect_ip_params` | 查询 IP 实例所有 CONFIG.* 参数（含 GUI 隐藏项），支持关键词过滤 |
| `compare_xci` | 纯 Python 对比两个 XCI 文件的参数差异（无需 Vivado 会话） |

### 结构化报告
| 工具 | 说明 |
|------|------|
| `get_io_report` | IO 引脚报告（JSON），自动判定 GT/GPIO 类型 |
| `get_timing_report` | 时序报告，含 PASS/FAIL 判定和关键路径详情 |

> 通用报告（utilization / power / drc / clock / methodology / cdc 等）请直接用
> `run_tcl("report_utilization -return_string")`，无需包装。

## 智能 Hook(Claude Code 独有)

仓库的 `.claude/settings.json` 预置了 **4 个 Claude Code hook**,让 AI 不只会"被动应答",还能**主动守门**:

| Hook | 触发事件 | 作用 |
|---|---|---|
| `bitstream-guard` | AI 调 `generate_bitstream` 前 | 拦截并提醒先跑 `check_bitstream_readiness`,避免时序违例时烧出废比特流 |
| `xdc-lint` | 保存任意 `.xdc` 文件后 | 纯 Python 静态检查:PIN_CONFLICT / 漏 IOSTANDARD / create_clock 缺 -period 等,无需等综合 |
| `verilog-lint` | 保存任意 `.v` / `.sv` 文件后 | 零依赖预检:module 名匹配文件名 / endmodule 存在 / 括号配对 |
| `session-guard` | Claude 停下时 | 扫 `vivado_pid*.str` 文件,提醒清理未关闭的 Vivado session |

首次打开本仓库时 Claude Code 会弹框:*"检测到项目配置了 hook,是否信任?"* — 选 **Yes** 即启用。

要禁用单个或全部:在 `.claude/settings.local.json`(个人本地文件,不进 git)写入 `{"hooks": {}}` 覆盖即可。

## 使用示例

### 基本流程

```
用户: 启动 Vivado GUI
AI: [start_session(mode="gui")] → 桌面弹出 Vivado 窗口，会话就绪

用户: 创建 Basys3 项目
AI: [safe_tcl("create_project {0} {1} -part {2}",
     args=["my_proj", "C:/proj", "xc7a35tcpg236-1"])]

用户: 添加源文件 top.v 和 counter.v
AI: [safe_tcl("add_files -fileset [get_filesets sources_1] {0}",
     args=["C:/src/top.v C:/src/counter.v"])]
    [run_tcl("set_property top top [current_fileset]")]

用户: 运行综合
AI: [run_synthesis] → 综合完成，自动 open_run 打开设计
    ✓ 诊断概览: errors=0, critical_warnings=0, warnings=3

用户: 查看资源
AI: [run_tcl("report_utilization -return_string")]

用户: 检查时序
AI: [get_timing_report] → PASS (WNS=+2.135 ns)

用户: 生成比特流
AI: [generate_bitstream] → 安全检查通过，比特流已生成
```

### 诊断 CRITICAL WARNING

```
用户: 综合完成但有警告，详细看看
AI: [get_critical_warnings]
    !! 发现 16 条 CRITICAL WARNING !!

    --- [Vivado 12-1411] GT_PIN_CONFLICT (16 条) ---
      受影响端口: rxp[0], rxp[1], ... 共 16 个
      约束文件: board_pins.xdc
      建议: GT端口PACKAGE_PIN约束与IP内部LOC冲突...
```

### 验证引脚（支持 -dict 语法）

```
用户: 验证 PCIe GT 引脚是否映射正确
AI: [verify_io_placement]
    !!! CRITICAL 不匹配（GT 高速收发器端口）!!!
      端口: pcie_7x_mgt_rtl_0_rxp[0]
        XDC 约束引脚:   AA4 (来源: board_pins.xdc)
        实际分配引脚:   M6
        FPGA 站点:      MGTXRXP3_116
```

### 对比 XCI 配置

```
用户: 对比 golden 和 suspect 两个 XCI 文件
AI: [compare_xci(file_a="golden.xci", file_b="suspect.xci")]
    PF0_DEVICE_ID:     A=9024       | B=9038
    LINK_SPEED:        A=5.0_GT/s   | B=8.0_GT/s
    LANE_REVERSAL:     A=false      | B=true
```

### 执行任意 Tcl（AI 的主力）

```
用户: 查看所有端口的 Bank 属性
AI: [run_tcl("foreach p [get_ports] { puts \"$p: [get_property PACKAGE_PIN $p]\" }")]

用户: 批量设置引脚
AI: [run_tcl("""
     set_property PACKAGE_PIN W5 [get_ports clk]
     set_property PACKAGE_PIN U18 [get_ports rst_n]
     """)]
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
