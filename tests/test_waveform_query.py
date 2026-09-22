"""离线 VCD 工具 JSON 错误、日志及无会话查询测试。"""

import json
from pathlib import Path

import pytest

from vivado_mcp.tools.waveform_query import query_waveform


@pytest.mark.asyncio
async def test_query_without_session():
    path = Path(__file__).parent / "fixtures" / "waveform_handshake.vcd"
    result = json.loads(await query_waveform(str(path), ["top.valid", "top.ready"], condition={
        "op": "all_equals", "values": {"top.valid": "1", "top.ready": "1"},
    }))
    assert result["success"]
    assert result["matches"][0]["time"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "code"), [
    ("missing.vcd", "file_error"), ("trace.wdb", "unsupported_format"),
])
async def test_file_error_is_structured_and_logged(tmp_path, caplog, name, code):
    result = json.loads(await query_waveform(str(tmp_path / name)))
    assert not result["success"]
    assert result["error_code"] == code
    assert result["simulation_verdict"] == "not_evaluated"
    assert "离线 VCD 查询失败" in caplog.text
