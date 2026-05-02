"""Vivado MCP Server — 精简开源替代方案。"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vivado-mcp")
except PackageNotFoundError:
    # 包没装(直接跑源码)的兜底
    __version__ = "unknown"
