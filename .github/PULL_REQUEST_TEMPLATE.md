<!-- 感谢贡献!提交前请确认以下几条 -->

## 改动说明

<!-- 一句话讲清楚做了什么、为什么 -->

## 关联 issue

<!-- 例如 Closes #12 / Refs #34;无则填"无" -->

## 自检清单

- [ ] 跑过 `pytest -v`,所有测试 pass
- [ ] 跑过 `ruff check src/ tests/`,无新增警告
- [ ] 改了行为的话,**新增/更新了对应测试**
- [ ] 改了用户可见行为的话,**更新了 README / CHANGELOG**
- [ ] 如果触及 `vivado/` 协议层 或 `tcl_scripts.py`,我读过了 `.trellis/spec/backend/tcl-protocol-guidelines.md`
- [ ] 如果新增 MCP 工具,我确认过**不能**用 `run_tcl` 直接做(参考 README"少即是多"段)

## 实机验证(可选但强烈推荐)

<!--
如果改了 Vivado 交互逻辑,贴一段 Vivado 版本 + 实机调用日志,比如:
- Vivado 2019.1
- Project: basys3_uart
- 调用序列:run_synthesis → get_critical_warnings → ...
- 输出片段:...
-->
