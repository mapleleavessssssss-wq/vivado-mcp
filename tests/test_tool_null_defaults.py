"""工具参数 schema 回归：默认值为 null 的参数必须接受 null。

参数写成 ``wcfg: str = None`` 时，tools/list 公布 ``"default": null``，但类型不含 null；
客户端把默认值显式传回(``wcfg=null``)会在工具执行前被参数校验拒绝。不需要 Vivado。
"""

from __future__ import annotations

import pytest

from vivado_mcp.server import mcp


def _accepts_null(schema: dict) -> bool:
    if "anyOf" in schema:
        return any(_accepts_null(s) for s in schema["anyOf"])
    t = schema.get("type")
    return t is None or t == "null" or (isinstance(t, list) and "null" in t)


@pytest.mark.asyncio
async def test_null_default_params_accept_null():
    bad = [
        f"{tool.name}.{name}"
        for tool in await mcp.list_tools()
        for name, prop in tool.input_schema.get("properties", {}).items()
        if "default" in prop and prop["default"] is None and not _accepts_null(prop)
    ]
    assert bad == [], f"默认值为 null 但拒绝 null: {bad}"


def _validate(tool_name: str, arguments: dict):
    meta = mcp._tool_manager.get_tool(tool_name).fn_metadata
    return meta.arg_model.model_validate(meta.pre_parse_json(arguments))


def test_explicit_null_passes_validation():
    assert _validate("set_wave_zoom", {"start_ns": 0, "end_ns": 100, "wcfg": None}).wcfg is None
    args = _validate("set_wave_analog", {"signals": ["/tb/y0"], "min": None, "max": None})
    assert args.min is None and args.max is None
    assert _validate("safe_tcl", {"template": "puts hi", "args": None}).args is None


def test_string_and_list_values_unchanged():
    """改成 ``X | None`` 后，普通字符串、JSON 文本列表照旧解析。"""
    zoom = _validate("set_wave_zoom", {"start_ns": 0, "end_ns": 1, "wcfg": "C:/sim/tb.wcfg"})
    assert zoom.wcfg == "C:/sim/tb.wcfg"
    assert _validate("safe_tcl", {"template": "read_verilog {0}", "args": '["a b.v"]'}).args == [
        "a b.v"
    ]
