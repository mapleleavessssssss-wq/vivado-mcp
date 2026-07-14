# 贡献指南

感谢你对 vivado-mcp 的关注！以下是参与贡献的说明。

## 开发环境搭建

```bash
# 克隆仓库
git clone https://github.com/mapleleavessssssss-wq/vivado-mcp.git
cd vivado-mcp

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# 或 .venv\Scripts\activate  # Windows

# 安装开发依赖
pip install -e ".[dev]"
```

## 代码风格

- 使用 [ruff](https://docs.astral.sh/ruff/) 进行代码检查和格式化
- 行宽限制 100 字符
- 源码注释和 docstring 使用中文
- 遵循 PEP 8 命名规范

```bash
# 检查
ruff check src/ tests/

# 自动修复
ruff check --fix src/ tests/
```

## Tcl 协议规则（改 `src/vivado_mcp/vivado/` 或 `tcl_scripts.py` 前必读）

本项目通过 sentinel 前缀协议解析 Vivado 输出，这一层的 bug 最难追（曾有命名空间冲突 bug 潜伏了十个版本）。硬规则：

1. **`VMCP_OK:` / `VMCP_ERR:` / `VMCP_END:` 是 session 层 sentinel，应用层 Tcl 脚本禁止输出这三个前缀**。新加输出前缀一律 `VMCP_<语义>:`（如 `VMCP_PROJ:`、`VMCP_IP_INFO:`），加之前先 `grep -rn "VMCP_" src/` 确认不撞名
2. Tcl 临时变量一律 `__` 双下划线前缀，防止与用户会话里的 Tcl 变量重名
3. Tcl error 用 `catch` 接住并以 `VMCP_<语义>_ERR:` 前缀输出，不要让裸异常穿透到 session 协议层
4. `puts` 输出必须带前缀——不带前缀的行会被原样转发给 AI，产生噪音
5. 长 Tcl 片段集中放 `tcl_scripts.py`：走 `.format()` 传参的模板里 Tcl 花括号写 `{{ }}`，不走 `.format()` 的用单 `{ }`（文件内有注释标注哪个是哪个）
6. stdio（`session.py`）与 TCP/GUI（`gui_session.py`）是两条独立传输路径，改一边必须确认另一边不受影响

## 测试

```bash
# 运行所有测试（不需要 Vivado 安装）
pytest

# 运行特定测试
pytest tests/test_tcl_utils.py -v
```

## PR 流程

1. Fork 本仓库
2. 创建功能分支 (`git checkout -b feature/my-feature`)
3. 编写代码和测试
4. 确保 `ruff check` 和 `pytest` 通过
5. 提交 PR，描述你的更改

## 安全相关

如果发现安全漏洞，请通过 Issue 私密报告，不要在公开 Issue 中公布细节。
