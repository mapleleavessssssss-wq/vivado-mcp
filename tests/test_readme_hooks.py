"""README hook 配置示例的可执行性测试。

背景(2026-06 审计 docs 分区):
  - P1: bitstream-guard 旧示例无条件 exit(2),会永久阻断 generate_bitstream
    (README 自己的烧板示例流程都跑不通)→ 改为输出 permissionDecision "ask"
    的 JSON,由用户确认放行。
  - P2: 5 条 hook 的多行 python -c 在 Windows cmd 下 SyntaxError 静默失效
    (非 2 的非零退出码 = 非阻断错误,hook 等于没装)→ 全部改为单行分号串联。

本文件直接从 README.md 提取 settings.json 片段,锁住三件事:
  1. 片段是合法 JSON;
  2. 每条 hook command 是单行(跨 cmd/bash 安全的前提);
  3. 每条命令喂 stdin JSON 实跑(shell=True,Windows 下即 cmd 语境),
     返回码与输出符合预期 —— 含 bitstream-guard 的 "ask" JSON、
     xdc-lint 的 rc=2 阻断、session-guard 的痕迹检测。
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"


def _extract_settings_snippet() -> dict:
    """从 README 的「可复制的 settings.json 片段」提取并解析 JSON。"""
    text = README.read_text(encoding="utf-8")
    m = re.search(
        r"可复制的 settings\.json 片段.*?```json\n(.*?)\n```", text, re.DOTALL
    )
    assert m, "README 中找不到 settings.json 片段代码块"
    return json.loads(m.group(1))  # 不合法 JSON 直接抛错让测试红


def _all_hook_commands(snippet: dict) -> dict[str, str]:
    """按 statusMessage 收集全部 hook command。"""
    commands: dict[str, str] = {}
    for entries in snippet["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                commands[hook["statusMessage"]] = hook["command"]
    return commands


def _run_hook(command: str, stdin_payload: dict, cwd: Path | None = None):
    """用 shell=True 模拟 hook 实际执行环境(Windows 下即 cmd /c <command>,
    正是审计 P2 多行 python -c 失效的那个语境)。"""
    return subprocess.run(
        command,
        shell=True,
        input=json.dumps(stdin_payload).encode("utf-8"),
        capture_output=True,
        cwd=cwd,
        timeout=60,
    )


@pytest.fixture(scope="module")
def hook_commands() -> dict[str, str]:
    return _all_hook_commands(_extract_settings_snippet())


class TestSnippetShape:
    """片段本身的静态约束。"""

    def test_snippet_is_valid_json(self):
        snippet = _extract_settings_snippet()
        assert "hooks" in snippet

    def test_expected_five_hooks_present(self, hook_commands):
        assert set(hook_commands) == {
            "bitstream-guard",
            "xdc-lint",
            "verilog-lint",
            "iverilog-check",
            "session-guard",
        }

    def test_all_commands_single_line(self, hook_commands):
        # 多行 python -c 在 Windows cmd 下 SyntaxError 静默失效(审计 P2)
        for name, cmd in hook_commands.items():
            assert "\n" not in cmd, f"hook {name} 的 command 含换行,cmd 下会失效"


class TestBitstreamGuard:
    """P1:不再 exit(2) 硬阻断,改为输出 ask JSON 由用户放行。"""

    def test_outputs_ask_decision(self, hook_commands):
        r = _run_hook(
            hook_commands["bitstream-guard"],
            {"tool_name": "mcp__vivado__generate_bitstream", "tool_input": {}},
        )
        assert r.returncode == 0, f"应 rc=0(非阻断),实际 {r.returncode}: {r.stderr!r}"
        out = json.loads(r.stdout.decode("utf-8"))
        spec = out["hookSpecificOutput"]
        assert spec["hookEventName"] == "PreToolUse"
        assert spec["permissionDecision"] == "ask"
        assert "check_bitstream_readiness" in spec["permissionDecisionReason"]


class TestPostToolUseHooks:
    """xdc-lint / verilog-lint / iverilog-check:早退路径 + xdc 正例。"""

    @pytest.mark.parametrize("name", ["xdc-lint", "verilog-lint", "iverilog-check"])
    def test_non_target_file_exits_zero(self, hook_commands, name):
        # 非目标后缀早退 rc=0;python -c 整段先编译,单行语法错在这里就会暴露
        r = _run_hook(
            hook_commands[name],
            {"tool_input": {"file_path": "notes.txt"}, "tool_response": {}},
        )
        assert r.returncode == 0, f"{name} 对非目标文件应 rc=0: {r.stderr!r}"

    def test_xdc_lint_blocks_on_issue(self, hook_commands, tmp_path):
        # 缺 IOSTANDARD 的 XDC → lint 出 issue → rc=2 阻断并回喂 stderr
        bad = tmp_path / "pins.xdc"
        bad.write_text(
            "set_property PACKAGE_PIN W5 [get_ports clk]\n", encoding="utf-8"
        )
        r = _run_hook(
            hook_commands["xdc-lint"],
            {"tool_input": {"file_path": str(bad)}, "tool_response": {}},
        )
        assert r.returncode == 2, f"有 issue 的 XDC 应 rc=2: {r.stderr!r}"
        assert "[xdc-lint hook]" in r.stderr.decode("utf-8")


class TestSessionGuard:
    """session-guard:扫 vivado_pid*.str 痕迹。"""

    def test_clean_dir_exits_zero(self, hook_commands, tmp_path):
        r = _run_hook(hook_commands["session-guard"], {}, cwd=tmp_path)
        assert r.returncode == 0, f"无痕迹应 rc=0: {r.stderr!r}"

    def test_leftover_str_file_blocks(self, hook_commands, tmp_path):
        (tmp_path / "vivado_pid12345.str").write_text("12345", encoding="utf-8")
        r = _run_hook(hook_commands["session-guard"], {}, cwd=tmp_path)
        assert r.returncode == 2, f"有痕迹应 rc=2: {r.stderr!r}"
        assert "vivado_pid12345.str" in r.stderr.decode("utf-8")
