"""入口模块：支持子命令 + MCP server 启动。

用法：
    python -m vivado_mcp              # 启动 MCP server（stdio，供 AI 工具调用）
    python -m vivado_mcp serve         # 同上，显式
    python -m vivado_mcp install       # 注入 Vivado_init.tcl
    python -m vivado_mcp uninstall     # 从 Vivado_init.tcl 移除
    python -m vivado_mcp doctor        # 只读环境诊断
    python -m vivado_mcp version       # 显示版本
    vivado-mcp install --port 9998     # 使用自定义端口
"""

from __future__ import annotations

import argparse
import sys

from vivado_mcp import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vivado-mcp",
        description="Vivado MCP Server — AI 驱动的 FPGA 开发助手。",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    # serve (默认)
    sub.add_parser(
        "serve",
        help="启动 MCP server（stdio 传输，供 Claude Code 等 AI 工具调用）。",
    )

    # install
    p_install = sub.add_parser(
        "install",
        help="高级兼容方式：注入安装级 Vivado_init.tcl，供手工 GUI attach。",
    )
    p_install.add_argument(
        "vivado_path",
        nargs="?",
        help="Vivado 可执行文件路径（可选，留空则自动检测）。",
    )
    p_install.add_argument(
        "--port",
        type=int,
        default=9999,
        help="监听端口（默认 9999；server 只绑定该端口，被占即退出不滑动）。",
    )

    # uninstall
    p_uninstall = sub.add_parser(
        "uninstall",
        help="从 Vivado_init.tcl 移除 vivado-mcp 注入。",
    )
    p_uninstall.add_argument(
        "vivado_path",
        nargs="?",
        help="Vivado 可执行文件路径（可选）。",
    )

    # version
    sub.add_parser("version", help="显示版本号并退出。")

    # doctor
    p_doctor = sub.add_parser(
        "doctor",
        help="诊断 Vivado、init Tcl、协议端口和 MCP 客户端配置。",
    )
    p_doctor.add_argument(
        "vivado_path",
        nargs="?",
        help="Vivado 可执行文件路径（可选，留空则自动检测）。",
    )
    p_doctor.add_argument("--port", type=int, default=9999, help="检查的 TCP 端口（默认 9999）。")
    p_doctor.add_argument("--json", action="store_true", help="输出稳定 JSON 结构。")
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="备份后原子写入缺失的客户端配置；默认不修改 Vivado_init.tcl。",
    )
    p_doctor.add_argument(
        "--install-init",
        action="store_true",
        help="与 --fix 一起显式安装高级兼容 init 注入；普通 GUI 启动不需要。",
    )
    p_doctor.add_argument(
        "--client",
        choices=("all", "claude-code", "codex"),
        default="all",
        help="--fix 要配置的 MCP 客户端（默认 all）。",
    )

    args = parser.parse_args()

    # 无参数 或 "serve" → 启动 MCP server
    if args.cmd in (None, "serve"):
        from vivado_mcp.server import mcp

        mcp.run(transport="stdio")
        return

    if args.cmd == "version":
        print(f"vivado-mcp {__version__}")
        return

    if args.cmd == "doctor":
        from vivado_mcp.doctor import format_report, run_doctor

        try:
            report = run_doctor(
                vivado_path=args.vivado_path,
                port=args.port,
                fix=args.fix,
                install_init=args.install_init,
                client=args.client,
            )
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(2)
        print(format_report(report, as_json=args.json))
        if report.exit_code:
            sys.exit(report.exit_code)
        return

    if args.cmd == "install":
        from vivado_mcp.install import install

        try:
            install(vivado_path=args.vivado_path, port=args.port)
        except (FileNotFoundError, PermissionError, OSError, RuntimeError, ValueError) as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.cmd == "uninstall":
        from vivado_mcp.install import uninstall

        try:
            uninstall(vivado_path=args.vivado_path)
        except (FileNotFoundError, PermissionError, OSError, RuntimeError, ValueError) as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        return


if __name__ == "__main__":
    main()
