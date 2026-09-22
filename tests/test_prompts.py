"""MCP Prompt 注册、证据门禁与工具引用契约。"""

import ast
import re
from pathlib import Path

from vivado_mcp.prompts import PROMPT_FUNCTIONS, register_prompts
from vivado_mcp.server import mcp

EXPECTED_NAMES = [
    "fpga_workflow",
    "debug_timing",
    "debug_gt_mapping",
    "debug_ip_config",
    "debug_pcie",
    "simulation_bringup",
    "cdc_audit",
    "ila_hardware_debug",
    "project_bringup",
    "waveform_debug",
    "constraints_authoring",
]

SHARED_SKILLS = {
    "project_bringup": "vivado-project-bringup",
    "debug_timing": "vivado-timing-closure",
    "waveform_debug": "vivado-waveform-debug",
    "cdc_audit": "vivado-cdc-audit",
    "constraints_authoring": "vivado-constraints-authoring",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeMcp:
    """只实现 FastMCP Prompt 注册所需的装饰器接口。"""

    def __init__(self) -> None:
        self.registered = []

    def prompt(self):
        def decorator(function):
            self.registered.append(function)
            return function

        return decorator


def _registered_tool_names() -> set[str]:
    """从真实工具模块 AST 提取 ``@mcp.tool()`` 注册名称。"""
    names: set[str] = set()
    for path in (PROJECT_ROOT / "src" / "vivado_mcp" / "tools").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                function = decorator.func
                if (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "mcp"
                    and function.attr == "tool"
                ):
                    names.add(node.name)
    return names


def _declared_tool_names(prompt_body: str) -> set[str]:
    """提取 Prompt 的“可用入口”行，Tcl 子命令不在该声明中。"""
    line = next(line for line in prompt_body.splitlines() if line.startswith("**可用入口**"))
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", line))


def test_registers_prompts_in_compatible_order():
    """保留原八项顺序，三项窄范围工作流追加。"""
    fake_mcp = _FakeMcp()

    register_prompts(fake_mcp)

    assert [function.__name__ for function in PROMPT_FUNCTIONS] == EXPECTED_NAMES
    assert fake_mcp.registered == list(PROMPT_FUNCTIONS)


async def test_fastmcp_lists_and_renders_all_prompts():
    """穿透真实 FastMCP manager 验证 prompts/list 与 prompts/get。"""
    listed = await mcp.list_prompts()

    assert [prompt.name for prompt in listed] == EXPECTED_NAMES
    for function in PROMPT_FUNCTIONS:
        result = await mcp.get_prompt(function.__name__)
        assert len(result.messages) == 1
        assert result.messages[0].content.text == function()


def test_prompts_are_compact_and_include_common_safety_contract():
    """每个 Prompt 可独立执行，同时共享同一证据闭环。"""
    required = (
        "## 前置条件",
        "## 证据闭环",
        "Fresh evidence",
        "禁止假绿",
        "连续两轮改动没有净改善",
        "## 固定输出",
        "最终状态(PASS/FAIL/BLOCKED)",
        "不得推测为 PASS",
    )

    for function in PROMPT_FUNCTIONS:
        body = function()
        if function.__name__ in SHARED_SKILLS:
            continue
        assert 1500 <= len(body) <= 3600, (function.__name__, len(body))
        for marker in required:
            assert marker in body, (function.__name__, marker)


def test_shared_prompts_equal_distributed_skill_bodies():
    """直接从分发文件提取正文，MCP 不能出现另一个漂移版本。"""
    functions = {function.__name__: function for function in PROMPT_FUNCTIONS}
    for prompt_name, skill_name in SHARED_SKILLS.items():
        raw = (PROJECT_ROOT / "skills" / skill_name / "SKILL.md").read_bytes()
        text = raw.decode("utf-8-sig")
        lines = text.splitlines(keepends=True)
        closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        body = "".join(lines[closing + 1:])
        assert functions[prompt_name]() == body
        assert 800 <= len(body) <= 3600


def test_prompt_tool_references_are_registered_tools():
    """Prompt 只声明真实 MCP 工具；领域 Tcl 一律经 run_tcl/safe_tcl。"""
    registered = _registered_tool_names()

    for function in PROMPT_FUNCTIONS:
        declared = _declared_tool_names(function())
        assert declared <= registered, (function.__name__, declared - registered)
        assert "run_tcl" in declared


def test_high_risk_prompts_have_domain_specific_gates():
    """关键领域不能仅靠公共模板获得表面安全。"""
    bodies = {function.__name__: function() for function in PROMPT_FUNCTIONS}

    assert "post-route" in bodies["fpga_workflow"]
    assert "set_false_path` 和 multicycle 不是性能优化手段" in bodies["debug_timing"]
    assert "没有板级真值来源即 BLOCKED" in bodies["debug_gt_mapping"]
    assert "golden XCI" in bodies["debug_ip_config"]
    assert "上一层没有证据通过时，不跳到" in bodies["debug_pcie"]
    assert "compile success 属于 FAIL" in bodies["simulation_bringup"]
    assert "空报告" in bodies["cdc_audit"]
    assert "bitstream、LTX、器件和 commit" in bodies["ila_hardware_debug"]
    assert "check_timing -verbose" in bodies["constraints_authoring"]
    assert "-min" in bodies["constraints_authoring"]
    assert "-max" in bodies["constraints_authoring"]
    assert "功能未验证" in bodies["project_bringup"]
    assert "simulation_verdict" in bodies["waveform_debug"]
    for name in ("cdc_audit", "debug_timing", "constraints_authoring"):
        assert "report_bus_skew" in bodies[name]
