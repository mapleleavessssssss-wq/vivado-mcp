"""``vivado-mcp install`` / ``uninstall`` 实现。

**幂等性**：反复运行 ``install`` 不会重复注入；``uninstall`` 找不到注入标记就是 no-op。
**安全性**：第一次运行会备份原 ``Vivado_init.tcl`` 到 ``Vivado_init.tcl.vmcp_backup``。
**守卫**：注入的代码只绑定 install 指定的单一端口（默认 9999），被占即退出、
不滑动到其他端口；``launch_runs`` 子进程抢不到端口会静默退出（不影响综合）。
详见 ``scripts/vivado_mcp_server.tcl``。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from vivado_mcp.config import find_vivado
from vivado_mcp.vivado.gui_session import _locate_server_script

logger = logging.getLogger(__name__)

# 注入标记（用于 install/uninstall 幂等性）
_BEGIN_MARK = "## ===== vivado-mcp injection begin ====="
_END_MARK = "## ===== vivado-mcp injection end ====="

_BACKUP_SUFFIX = ".vmcp_backup"


def _read_optional_bytes(path: Path) -> bytes | None:
    """读取文件原始字节；不存在时返回 ``None``。"""
    return path.read_bytes() if path.is_file() else None


def _atomic_write_text(
    path: Path,
    text: str,
    *,
    expected_bytes: bytes | None,
    backup_suffix: str = _BACKUP_SUFFIX,
) -> None:
    """校验原内容后，以同目录临时文件原子替换文本文件。

    ``expected_bytes`` 是调用方完成解析时看到的原始内容；``None`` 表示文件
    当时不存在。写入前再次比较，拒绝覆盖并发修改。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if _read_optional_bytes(path) != expected_bytes:
        raise OSError(f"{path} 在修复期间发生变化，拒绝覆盖")

    backup = path.with_suffix(path.suffix + backup_suffix)
    if expected_bytes is not None and not backup.exists():
        shutil.copy2(path, backup)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)

        if path.exists():
            shutil.copymode(path, temp_path)
        # 第二次比较紧邻 replace，缩小解析后并发更新的覆盖窗口。
        if _read_optional_bytes(path) != expected_bytes:
            raise OSError(f"{path} 在修复期间发生变化，拒绝覆盖")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _resolve_init_tcl(vivado_path: str | None) -> Path:
    """定位 Vivado_init.tcl 文件路径。

    Vivado 安装路径为 ``.../Vivado/<ver>/bin/vivado.bat``，
    ``init.tcl`` 在 ``.../Vivado/<ver>/scripts/Vivado_init.tcl``。
    """
    exe = Path(find_vivado(vivado_path)).resolve()
    # exe 形如 D:/Xilinx/Vivado/2019.1/bin/vivado.bat
    # 目标 init.tcl: D:/Xilinx/Vivado/2019.1/scripts/Vivado_init.tcl
    vivado_root = exe.parent.parent
    init_tcl = vivado_root / "scripts" / "Vivado_init.tcl"
    return init_tcl


def _build_injection_block(server_script: Path, port: int) -> str:
    """生成要写入 init.tcl 的注入代码块。

    关键点：
    - 用 ``-port`` 变量通过 ``set`` 传给 server 脚本（可选，脚本有默认值）
    - catch 保护：加载失败不阻断 Vivado 启动
    """
    # 路径转正斜杠（Tcl 兼容 + 跨平台）
    script_posix = server_script.as_posix()
    return (
        f"{_BEGIN_MARK}\n"
        f"## 由 `vivado-mcp install` 自动生成。如需删除请运行 `vivado-mcp uninstall`\n"
        f"## 或手动删除 begin/end 标记之间的所有行。\n"
        f"set ::VMCP_PORT_PREF {port}\n"
        f'if {{[catch {{source "{script_posix}"}} __vmcp_err]}} {{\n'
        f'    puts "vivado-mcp: init server load failed: $__vmcp_err"\n'
        f"}}\n"
        f"{_END_MARK}\n"
    )


def _has_other_vendor_injection(content: str) -> str | None:
    """检测是否有其他 MCP 产品（如 SynthPilot）的注入，返回名称以便提示用户。"""
    indicators = {
        "SynthPilot": ["SynthPilot", "synthpilot"],
        "未知 MCP 产品": ["mcp_stop_server"],
    }
    for name, keywords in indicators.items():
        for kw in keywords:
            if kw in content:
                return name
    return None


def install(vivado_path: str | None = None, port: int = 9999) -> None:
    """注入 Vivado_init.tcl，让 GUI 启动自动开启 TCP server。

    Args:
        vivado_path: 可选的 Vivado 可执行文件路径，留空自动检测。
        port: 监听端口，默认 9999（server 只绑定该端口，被占即退出不滑动）。

    Raises:
        FileNotFoundError: 无法定位 Vivado 安装或 server 脚本。
        PermissionError: 没有写 init.tcl 的权限（需管理员运行）。
    """
    init_tcl = _resolve_init_tcl(vivado_path)
    server_script = _locate_server_script()

    print(f"Vivado init.tcl: {init_tcl}")
    print(f"Server 脚本:     {server_script}")
    print(f"监听端口:        {port}")

    # 确保目标目录存在
    init_tcl.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有内容（不存在则空字符串）
    original_bytes = _read_optional_bytes(init_tcl)
    content = original_bytes.decode("utf-8", errors="replace") if original_bytes else ""

    # 检测是否已注入
    if _BEGIN_MARK in content:
        print("已检测到旧的 vivado-mcp 注入，将替换为最新版本。")
        content = _remove_injection(content)

    # 检测其他 vendor
    vendor = _has_other_vendor_injection(content)
    if vendor:
        print(
            f"⚠ 警告：检测到机器上可能装有 {vendor}（Vivado_init.tcl 中有其注入）。\n"
            f"  {vendor} 也会尝试占用 9999 端口，可能与 vivado-mcp 冲突。\n"
            f"  建议先卸载 {vendor}，或将 vivado-mcp 的端口改为其他值（目前 {port}）。\n"
            f"  继续安装..."
        )

    # 写入新内容
    injection = _build_injection_block(server_script, port)
    new_content = content.rstrip() + "\n\n" + injection
    backup_path = init_tcl.with_suffix(init_tcl.suffix + _BACKUP_SUFFIX)
    backup_will_be_created = original_bytes is not None and not backup_path.exists()

    try:
        _atomic_write_text(init_tcl, new_content, expected_bytes=original_bytes)
    except PermissionError:
        raise PermissionError(
            f"无权限写入 {init_tcl}。请用管理员权限运行 `vivado-mcp install`，\n"
            f"或手动将以下内容追加到该文件末尾：\n\n{injection}"
        )
    if backup_will_be_created:
        print(f"原文件已备份到: {backup_path}")

    print(f"✓ vivado-mcp 已成功注入 {init_tcl}")
    print("  下次启动 Vivado GUI 时会自动开启 TCP server。")
    print(f"  或在 MCP 里调用 start_session(mode='attach', port={port}) 连接。")


def _remove_injection(content: str) -> str:
    """从文本中移除 vivado-mcp 注入块（begin..end）。"""
    lines = content.splitlines(keepends=True)
    result = []
    in_block = False
    for line in lines:
        if _BEGIN_MARK in line:
            in_block = True
            continue
        if _END_MARK in line:
            in_block = False
            continue
        if not in_block:
            result.append(line)
    return "".join(result)


def uninstall(vivado_path: str | None = None) -> None:
    """从 Vivado_init.tcl 移除 vivado-mcp 注入。

    如果 init.tcl 从没被我们碰过（无注入标记），直接 no-op。
    """
    try:
        init_tcl = _resolve_init_tcl(vivado_path)
    except FileNotFoundError as e:
        print(f"未找到 Vivado 安装: {e}")
        return

    if not init_tcl.is_file():
        print(f"{init_tcl} 不存在，无需操作。")
        return

    original_bytes = _read_optional_bytes(init_tcl)
    assert original_bytes is not None
    content = original_bytes.decode("utf-8", errors="replace")
    if _BEGIN_MARK not in content:
        print(f"{init_tcl} 中未发现 vivado-mcp 注入，无需操作。")
        return

    cleaned = _remove_injection(content).rstrip() + "\n"
    try:
        _atomic_write_text(init_tcl, cleaned, expected_bytes=original_bytes)
    except PermissionError:
        raise PermissionError(
            f"无权限写入 {init_tcl}。请用管理员权限运行 `vivado-mcp uninstall`。"
        )

    print(f"✓ 已从 {init_tcl} 移除 vivado-mcp 注入。")

    # 如果有备份，提示用户可以手动恢复
    backup_path = init_tcl.with_suffix(init_tcl.suffix + _BACKUP_SUFFIX)
    if backup_path.is_file():
        print(f"  原文件备份位置: {backup_path}（如需完全恢复可手动替换）")
