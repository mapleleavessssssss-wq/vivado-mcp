"""用真实 stdio MCP 握手验证离线诊断入口；不启动 Vivado。"""

import json
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_offline_diagnostics_over_stdio(tmp_path):
    """校验注册、参数传输和 JSON 返回，并确认没有创建 Vivado 会话。"""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vivado_mcp"],
        cwd=str(tmp_path),
        env={"PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"},
    )
    async with stdio_client(params) as streams:
        async with ClientSession(
            *streams, read_timeout_seconds=30,
        ) as client:
            await client.initialize()
            registered = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert "query_waveform" in registered
            assert "baseline_file" in registered["get_timing_report"].input_schema["properties"]
            assert len((await client.list_prompts()).prompts) == 11
            assert "get_cdc_report" in registered
            for name in ("project_bringup", "debug_timing", "waveform_debug",
                         "cdc_audit", "constraints_authoring"):
                prompt = await client.get_prompt(name)
                assert len(prompt.messages) == 1
                assert "**可用入口**" in prompt.messages[0].content.text

            async def call(name, arguments):
                response = await client.call_tool(name, arguments)
                assert not response.is_error
                return json.loads("".join(
                    block.text for block in response.content if block.type == "text"
                ))

            timing = await call("get_timing_report", {
                "report_file": str(ROOT / "tests/fixtures/sample_report_timing.txt"),
                "output_format": "json",
            })
            assert timing["kind"] == "vivado_timing"
            assert timing["metrics"]["setup"]["worst_slack_ns"] == 0.234
            assert timing["verdict"]["signoff"] is False

            baseline = tmp_path / "baseline.json"
            baseline.write_text(json.dumps(timing), encoding="utf-8")
            comparison = await call("get_timing_report", {
                "report_file": str(ROOT / "tests/fixtures/sample_report_timing.txt"),
                "output_format": "json", "baseline_file": str(baseline),
            })
            # 此 fixture 无设计/阶段页头，跨调用也不能伪造可比上下文。
            assert comparison["comparison"]["status"] == "incomparable"

            cdc = await call("get_cdc_report", {
                "report_file": str(ROOT / "tests/fixtures/cdc_details_2019.txt"),
                "max_details": 1,
            })
            assert cdc["kind"] == "vivado_cdc"
            assert cdc["summary"]["reported_checks"] == 3
            assert cdc["summary"]["by_severity"]["Critical"] == 2
            assert len(cdc["details"]) == 1 and cdc["details_truncated"]
            assert cdc["verdict"]["signoff"] is False
            no_clocks = await call("get_cdc_report", {
                "report_file": str(ROOT / "tests/fixtures/cdc_no_clocks_2019.txt"),
            })
            assert no_clocks["summary"]["reported_checks"] is None
            assert no_clocks["verdict"]["status"] == "unverified"
            missing_cdc = await call("get_cdc_report", {"session_id": "never-started"})
            assert missing_cdc["parse_status"] == "error"

            wavefile = tmp_path / "handshake.vcd"
            wavefile.write_text(
                "$timescale 1ns $end\n$scope module tb $end\n"
                "$var wire 1 ! valid $end\n$var wire 1 # ready $end\n"
                "$upscope $end\n$enddefinitions $end\n"
                "#0\n0!\n0#\n#5\n1!\n#10\n1#\n#20\n0!\n",
                encoding="ascii",
            )
            wave = await call("query_waveform", {
                "file_path": str(wavefile), "signals": ["tb.valid", "tb.ready"],
                "condition": {"op": "all_equals", "values": {
                    "tb.valid": "1", "tb.ready": "1",
                }},
            })
            assert wave["success"] is True
            assert [item["time"] for item in wave["matches"]] == [10]
            assert wave["simulation_verdict"] == "not_evaluated"
            assert not wave["scan_truncated"]

            missing = await call("get_timing_report", {
                "session_id": "never-started", "output_format": "json",
            })
            assert missing["parse_status"] == "error"
            assert missing["verdict"]["signoff"] is False
            sessions = await client.read_resource("vivado://sessions")
            assert json.loads(sessions.contents[0].text)["sessions"] == []
