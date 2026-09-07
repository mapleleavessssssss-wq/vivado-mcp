"""Vivado 路径检测与全局配置。

检测优先级：
1. VIVADO_PATH 环境变量
2. 系统 PATH 中的 vivado / vivado.bat
3. 平台相关的默认安装路径（取最新版本）
"""

import glob
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"^20\d{2}\.\d+$")
_UNWRAPPED_LAUNCHER_SEGMENT = "/bin/unwrapped/"


@dataclass(frozen=True)
class VivadoInstallation:
    """One discovered Vivado launcher with an explicit release identity."""

    version: str
    path: str


@dataclass(frozen=True)
class VivadoCompatibility:
    """Known interpreter boundary for a deliberately targeted release."""

    version: str
    tcl_runtime: str
    support_level: str
    notes: tuple[str, ...]


_KNOWN_COMPATIBILITY: dict[str, VivadoCompatibility] = {
    "2018.3": VivadoCompatibility(
        version="2018.3",
        tcl_runtime="8.5",
        support_level="targeted",
        notes=(
            "TCP bootstrap is restricted to Tcl 8.5-compatible constructs.",
            "Offline LTX parser does not claim support for legacy XML LTX.",
        ),
    ),
    "2020.2": VivadoCompatibility(
        version="2020.2",
        tcl_runtime="8.5",
        support_level="targeted",
        notes=("Project/run/report APIs use the common Tcl surface.",),
    ),
    "2024.2": VivadoCompatibility(
        version="2024.2",
        tcl_runtime="8.6",
        support_level="targeted",
        notes=(
            "Handshake records the actual Tcl patchlevel before reuse.",
            "Offline vendor-file parsers remain format-gated, not version-guessed.",
        ),
    ),
}


def get_vivado_compatibility(version: str) -> VivadoCompatibility | None:
    """Return a known profile without rejecting other Vivado releases."""
    return _KNOWN_COMPATIBILITY.get(version)


def vivado_versions_match(expected: str, actual: str) -> bool:
    """Match a release while allowing an official Answer Record patch suffix.

    Patched legacy installations can report values such as
    ``2018.3_AR71898`` from ``version -short``. This remains release 2018.3,
    while ``2018.3.1`` or ``2019.1`` must not be accepted implicitly.
    """
    return actual == expected or actual.startswith(expected + "_AR")


def _default_install_globs() -> list[str]:
    """根据当前平台返回 Vivado 默认安装搜索路径。"""
    if sys.platform == "win32":
        return [
            "C:/Xilinx/Vivado/*/bin/vivado.bat",
            "C:/AMD/Vivado/*/bin/vivado.bat",
            "D:/Xilinx/Vivado/*/bin/vivado.bat",
            "D:/AMD/Vivado/*/bin/vivado.bat",
            "E:/Xilinx/Vivado/*/bin/vivado.bat",
            "E:/AMD/Vivado/*/bin/vivado.bat",
        ]
    else:
        # Linux / macOS
        return [
            "/tools/Xilinx/Vivado/*/bin/vivado",
            "/opt/Xilinx/Vivado/*/bin/vivado",
            "/opt/xilinx/Vivado/*/bin/vivado",
            os.path.expanduser("~/Xilinx/Vivado/*/bin/vivado"),
        ]


def normalize_path(path: str) -> str:
    """将 Windows 反斜杠路径转换为正斜杠（Tcl 兼容）。"""
    return path.replace("\\", "/")


def validate_vivado_launcher(path: str) -> str:
    """Reject vendor-internal executables that require loader-owned state.

    AMD's public ``bin/vivado.bat`` (Windows) / ``bin/vivado`` (Linux)
    establishes PATH/LD_LIBRARY_PATH, Java, Tcl and patch-area state before it
    invokes ``bin/unwrapped/<platform>/vivado[.exe]``.  Launching the internal
    binary directly can fail before Tcl with missing libraries such as
    ``xv_common.dll`` (Windows status ``0xC0000135``).
    """
    normalized = normalize_path(path)
    if _UNWRAPPED_LAUNCHER_SEGMENT in normalized.casefold():
        raise ValueError(
            "拒绝直接启动 Vivado 内部 unwrapped executable: "
            f"{normalized}。该路径绕过 AMD loader 环境，可能报 "
            "xv_common.dll 缺失/0xC0000135。Windows 请使用 "
            "<Vivado>/<version>/bin/vivado.bat；Linux 请使用 bin/vivado。"
        )
    return normalized


def _version_key(version: str) -> tuple[int, ...]:
    """Return a numeric release key; never compare Vivado versions as strings."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (-1,)


def discover_vivado_installations() -> list[VivadoInstallation]:
    """Discover all versioned launchers without choosing one implicitly.

    Results are de-duplicated case-insensitively and sorted newest first by a
    numeric version key.  Discovery never launches a vendor executable.
    """
    found: dict[str, VivadoInstallation] = {}
    for pattern in _default_install_globs():
        for match in glob.glob(pattern):
            normalized = normalize_path(str(Path(match).resolve()))
            version = get_vivado_version(normalized)
            if not _VERSION_RE.fullmatch(version):
                continue
            found.setdefault(
                normalized.casefold(),
                VivadoInstallation(version=version, path=normalized),
            )
    return sorted(
        found.values(),
        key=lambda item: (_version_key(item.version), item.path.casefold()),
        reverse=True,
    )


def resolve_vivado(
    *,
    vivado_version: str | None = None,
    vivado_path: str | None = None,
) -> str:
    """Resolve one launcher and reject ambiguous or mismatched selections."""
    if vivado_version and not _VERSION_RE.fullmatch(vivado_version):
        raise ValueError(
            f"Vivado 版本格式非法: {vivado_version!r}，应形如 '2024.2'。"
        )

    if vivado_path:
        if not os.path.isfile(vivado_path):
            raise FileNotFoundError(f"Vivado launcher 不存在: {vivado_path}")
        normalized = validate_vivado_launcher(str(Path(vivado_path).resolve()))
        actual_version = get_vivado_version(normalized)
        if vivado_version and actual_version != vivado_version:
            raise ValueError(
                "Vivado 版本与 launcher 路径不匹配: "
                f"requested={vivado_version}, path_version={actual_version}, "
                f"path={normalized}"
            )
        return normalized

    if vivado_version:
        matches = [
            item for item in discover_vivado_installations()
            if item.version == vivado_version
        ]
        if not matches:
            raise FileNotFoundError(
                f"未找到 Vivado {vivado_version}。请传入该版本 vivado.bat 的绝对路径。"
            )
        if len(matches) > 1:
            paths = "\n".join(f"  - {item.path}" for item in matches)
            raise RuntimeError(
                f"发现多个 Vivado {vivado_version}，拒绝自动选择:\n{paths}"
            )
        return matches[0].path

    return find_vivado()


def find_vivado(vivado_path: str | None = None) -> str:
    """查找 Vivado 可执行文件路径。

    Args:
        vivado_path: 显式指定的路径，优先级最高。

    Returns:
        Vivado 可执行文件的完整路径（正斜杠格式）。

    Raises:
        FileNotFoundError: 未找到任何 Vivado 安装。
    """
    # 1. 显式传入
    if vivado_path and os.path.isfile(vivado_path):
        return validate_vivado_launcher(vivado_path)

    # 2. 环境变量 VIVADO_PATH
    env_path = os.environ.get("VIVADO_PATH")
    if env_path and os.path.isfile(env_path):
        return validate_vivado_launcher(env_path)

    # 3. 系统 PATH
    which = shutil.which("vivado") or shutil.which("vivado.bat")
    if which:
        return validate_vivado_launcher(which)

    # 4. 默认安装目录。多版本并存时拒绝猜测；调用方应显式传 version/path。
    installations = discover_vivado_installations()
    if len(installations) == 1:
        return validate_vivado_launcher(installations[0].path)
    if len(installations) > 1:
        choices = "\n".join(
            f"  - {item.version}: {item.path}" for item in installations
        )
        raise RuntimeError(
            "发现多个 Vivado 版本，拒绝自动选择。请显式设置 VIVADO_PATH "
            f"或在 start_session 传 vivado_version/vivado_path:\n{choices}"
        )

    raise FileNotFoundError(
        "未找到 Vivado 安装。请设置 VIVADO_PATH 环境变量，"
        "或确保 vivado 可执行文件在系统 PATH 中。"
    )


def get_vivado_version(vivado_path: str) -> str:
    """从路径中提取 Vivado 版本号（如 '2019.1'）。"""
    parts = Path(vivado_path).parts
    for i, part in enumerate(parts):
        if part.lower() == "vivado" and i + 1 < len(parts):
            candidate = parts[i + 1]
            # 版本号格式: 20xx.x
            if candidate[:2] == "20" and "." in candidate:
                return candidate
    return "unknown"
