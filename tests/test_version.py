"""锁死 __version__ 与 pyproject.toml 一致,防止再次漂移。

历史:0.2.0 → 0.3.12 期间 __init__.py 一直停在 "0.2.0" 没同步,
导致 `vivado-mcp version` 和 README 引导用户上报的版本号都是错的。
"""

import re
from pathlib import Path

import vivado_mcp


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert m, "pyproject.toml 缺少 version 行"
    return m.group(1)


def test_version_matches_pyproject():
    """__init__.__version__ 必须与 pyproject.toml 一致。"""
    assert vivado_mcp.__version__ == _pyproject_version(), (
        f"__version__ ({vivado_mcp.__version__}) 与 pyproject "
        f"({_pyproject_version()}) 不一致 —— 改 pyproject 时 __init__ "
        "应通过 importlib.metadata 自动同步,不要再硬编码"
    )


def test_version_is_resolved():
    """importlib.metadata 拿不到时会落 'unknown',CI 必须红。"""
    assert re.match(r"^\d+\.\d+\.\d+", vivado_mcp.__version__), (
        f"__version__ 未正确解析: {vivado_mcp.__version__}"
    )
