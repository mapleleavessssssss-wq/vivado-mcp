"""时序新增应用前缀穿过真实 stdio 响应解析，不与 sentinel 碰撞。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.session import SubprocessSession


@pytest.mark.asyncio
async def test_timing_prefixes_survive_subprocess_stdout_parser():
    session = SubprocessSession("unused-vivado", "timing-prefix-test")
    lines = [
        "VMCP_PRE_BIT:status=route_design Complete!,critical_warnings=-1",
        "VMCP_PRE_BIT_TOP:top",
        "VMCP_STAGE:stage=unknown|synth_status=Complete|impl_status=Complete",
        "VMCP_TIMING_ROUTE:   # of logical nets.......................... : 31 :",
        "VMCP_TIMING_ROUTE_ERROR:no design",
    ]
    process = MagicMock()
    process.stdin.drain = AsyncMock()
    process.stdout.readline = AsyncMock(side_effect=[
        (line + "\n").encode() for line in lines + ["<<<VMCP_timing_test_RC=0>>>"]
    ])
    session._process = process
    with patch("vivado_mcp.vivado.session.generate_sentinel", return_value="VMCP_timing_test"):
        result = await session._execute_impl("puts test")
    assert result.is_error is False
    assert result.output.splitlines() == lines
