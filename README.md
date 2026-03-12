# vivado-mcp

[![PyPI version](https://img.shields.io/pypi/v/vivado-mcp)](https://pypi.org/project/vivado-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/vivado-mcp)](https://pypi.org/project/vivado-mcp/)
[![License](https://img.shields.io/github/license/mapleleavessssssss-wq/vivado-mcp)](LICENSE)
[![CI](https://github.com/mapleleavessssssss-wq/vivado-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mapleleavessssssss-wq/vivado-mcp/actions/workflows/ci.yml)

精简的 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) Server，通过 **21 个工具** 控制 Xilinx Vivado EDA。

让 AI 助手（如 Claude）能够直接启动 Vivado、执行 Tcl 命令、运行综合/实现/比特流生成等完整 FPGA 开发流程，并**自动诊断 CRITICAL WARNING、验证引脚布局、结构化分析时序报告、诊断 IP 配置问题**。

## 特性

- **21 个精简工具** — 覆盖完整 FPGA 开发流程 + 智能诊断 + IP 调试，替代 200+ 专用工具的方案
- **智能诊断** — 综合/实现后自动提取 CRITICAL WARNING，按类别聚合并给出中文修复建议（覆盖 12 种已知 warning）
- **IO 验证** — 自动对比 XDC 约束与实际引脚分配，GT 端口不匹配标记为 CRITICAL
- **IP 调试** — 查询 IP 所有 CONFIG.* 参数（含 GUI 隐藏参数）、对比两个 XCI 文件配置差异
- **Bitstream 安全检查** — 生成比特流前自动检测 CRITICAL WARNING 并阻止（可 force 跳过）
- **结构化报告** — IO 和时序报告解析为结构化数据，便于 AI 精确分析
- **任意 Tcl 命令执行** — `run_tcl` 工具可执行任何 Vivado Tcl 命令
- **多会话支持** — 同时管理多个独立 Vivado 进程
- **安全设计** — 十六进制编码防 Tcl 注入、参数白名单验证
- **跨平台** — 支持 Windows 和 Linux
- **零额外依赖** — 仅依赖 `mcp` SDK

## 快速开始

### 1. 安装

```bash
pip install vivado-mcp
```

### 2. 配置 Claude Code

将以下内容复制到 `~/.claude.json` 的 `mcpServers` 字段中（[什么是 .claude.json？](https://docs.anthropic.com/en/docs/claude-code/settings)）：

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
> - 也可以不设置 `VIVADO_PATH`，将 Vivado `bin` 目录加入系统 `PATH` 或使用默认安装路径（自动检测）。

### 3. 重启 Claude Code

配置完成后重启 Claude Code，即可使用 21 个 Vivado 工具。

<details>
<summary>从源码安装（适合开发/贡献）</summary>

```bash
git clone https://github.com/mapleleavessssssss-wq/vivado-mcp.git
cd vivado-mcp
pip install -e ".[dev]"
```
</details>

## 工具列表

### 会话管理
| 工具 | 说明 |
|------|------|
| `start_session` | 启动 Vivado Tcl 交互会话 |
| `stop_session` | 关闭指定会话 |
| `list_sessions` | 列出所有活跃会话 |

### Tcl 执行
| 工具 | 说明 |
|------|------|
| `run_tcl` | 执行任意 Vivado Tcl 命令（最核心的工具） |
| `vivado_help` | 查询 Tcl 命令帮助 |

### 项目管理
| 工具 | 说明 |
|------|------|
| `create_project` | 创建新项目 |
| `open_project` | 打开已有项目（.xpr） |
| `close_project` | 关闭当前项目 |
| `add_files` | 添加 HDL / 约束文件 |

### 设计流程
| 工具 | 说明 |
|------|------|
| `run_synthesis` | 运行综合（完成后自动诊断警告） |
| `run_implementation` | 运行实现（完成后自动诊断警告） |
| `generate_bitstream` | 生成比特流（默认前置 CRITICAL WARNING 安全检查） |
| `get_status` | 查询运行状态 |
| `program_device` | 编程 FPGA 设备 |

### 诊断
| 工具 | 说明 |
|------|------|
| `get_critical_warnings` | 提取并分类 CRITICAL WARNING，按 ID 聚合 + 中文修复建议 |
| `verify_io_placement` | 对比 XDC 约束与实际 IO 布局，检测 GT 引脚交叉等严重错误 |

### IP 调试
| 工具 | 说明 |
|------|------|
| `inspect_ip_params` | 查询 IP 实例的所有 CONFIG.* 参数（含 GUI 隐藏参数），支持关键词过滤 |
| `compare_xci` | 对比两个 XCI 文件的 IP 配置差异（纯 Python，无需 Vivado 会话） |

### 报告
| 工具 | 说明 |
|------|------|
| `report` | 统一报告接口（utilization / timing / power / drc 等 11 种） |
| `get_io_report` | 结构化 IO 引脚报告（JSON），自动判定 GT/GPIO 类型 |
| `get_timing_report` | 结构化时序报告，含 PASS/FAIL 判定和关键路径详情 |

## 使用示例

### 基本流程

```
用户: 启动 Vivado 会话
AI: [调用 start_session] → 会话 'default' 已就绪

用户: 创建一个 Basys3 项目
AI: [调用 create_project(name="my_proj", directory="./my_proj", part="xc7a35tcpg236-1")]

用户: 添加这些源文件：top.v, counter.v
AI: [调用 add_files(files="./src/top.v ./src/counter.v")]

用户: 运行综合
AI: [调用 run_synthesis] → 综合完成，查看资源使用情况...
    [调用 report(report_type="utilization")] → LUT: 15%, FF: 8% ...

用户: 运行实现并生成比特流
AI: [调用 run_implementation]
    → 实现完成
    → !! 发现 16 条 CRITICAL WARNING !! 建议立即运行 get_critical_warnings 查看分类详情和修复建议。

用户: 查看警告详情
AI: [调用 get_critical_warnings]
    → [Vivado 12-1411] GT_PIN_CONFLICT x16
      GT端口PACKAGE_PIN约束与IP内部LOC冲突
      受影响端口: rxp[0]~rxp[7], txp[0]~txp[7]
      建议: 检查IP目录下GT LOC约束映射是否匹配PCB走线

用户: 验证引脚分配
AI: [调用 verify_io_placement]
    → !!! CRITICAL 不匹配（GT 高速收发器端口）!!!
      rxp[0]: XDC 约束 AA4 → 实际 M6
      ...

用户: 修复后生成比特流
AI: [调用 generate_bitstream] → 安全检查通过，比特流已生成
```

### IP 调试

```
用户: 查看 xdma_0 的 GT 相关参数
AI: [调用 inspect_ip_params(ip_name="xdma_0", filter_keyword="gt")]
    → PCIE_GT_DEVICE    GTX
      GT_LOC_NUM        4
      ...（含 GUI 中隐藏的参数）

用户: 对比两个 XCI 配置有什么区别
AI: [调用 compare_xci(file_a="golden.xci", file_b="suspect.xci")]
    → PF0_DEVICE_ID:     A=9024 | B=9038
      LINK_SPEED:        A=5.0_GT/s | B=8.0_GT/s
      LANE_REVERSAL:     A=false | B=true
```

### 任意 Tcl 命令

```
用户: 查看所有端口的属性
AI: [调用 run_tcl(command="foreach p [get_ports] { puts \"$p: [get_property PACKAGE_PIN $p]\" }")]
```

## 架构

```
Claude Code ──(stdio)──▶ FastMCP Server
                              │
                        SessionManager
                        ├─ "default" ──▶ vivado -mode tcl (subprocess)
                        └─ "session2" ──▶ vivado -mode tcl (subprocess)
```

核心通信协议：使用 `catch` + UUID sentinel 模式可靠地收发命令和输出。
用户命令通过十六进制编码后传输，确保任何内容都不会突破 Tcl 解析边界。

## 开发

```bash
# 安装开发依赖
git clone https://github.com/mapleleavessssssss-wq/vivado-mcp.git
cd vivado-mcp
pip install -e ".[dev]"

# 运行测试（不需要 Vivado）
pytest

# 代码检查
ruff check src/ tests/
```

## 文档

- [工具速查手册](docs/QUICK_REFERENCE.md) — 21 个工具 + 5 个 Prompt 的完整参数速查
- [IP 调试实践手册](docs/IP_DEBUG_GUIDE.md) — PCIe GT 映射调试、XCI 配置对比等实战案例

## 许可证

[Apache License 2.0](LICENSE)
