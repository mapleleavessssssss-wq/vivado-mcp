"""分发 Skill 的 MCP 示例必须与真实公开工具签名一致。"""

import ast
import inspect
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((PROJECT_ROOT / "skills").glob("*/SKILL.md"))


def _tool_signatures() -> dict[str, inspect.Signature]:
    """从工具注册源码读取签名，不需要 Vivado 或执行示例。"""
    signatures = {}
    for path in (PROJECT_ROOT / "src" / "vivado_mcp" / "tools").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(
                isinstance(decorator, ast.Call)
                and ast.unparse(decorator.func) == "mcp.tool"
                for decorator in node.decorator_list
            ):
                continue
            required_count = len(node.args.args) - len(node.args.defaults)
            parameters = [
                inspect.Parameter(
                    argument.arg,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=inspect.Parameter.empty if index < required_count else None,
                )
                for index, argument in enumerate(node.args.args)
            ]
            signatures[node.name] = inspect.Signature(parameters)
    return signatures


@pytest.mark.parametrize("skill_path", SKILLS, ids=lambda path: path.parent.name)
def test_skill_examples_bind_to_public_tools(skill_path):
    """接口变更不能让分发的示例引用未知工具、参数或缺少必需输入。"""
    signatures = _tool_signatures()
    examples = re.findall(r"```python\n(.*?)```", skill_path.read_text(encoding="utf-8"), re.S)

    assert examples, f"{skill_path.name}: no public tool examples"
    for example in examples:
        for statement in ast.parse(example).body:
            assert isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            call = statement.value
            assert isinstance(call.func, ast.Name) and call.func.id in signatures
            signatures[call.func.id].bind(
                *(ast.literal_eval(argument) for argument in call.args),
                **{keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords},
            )
