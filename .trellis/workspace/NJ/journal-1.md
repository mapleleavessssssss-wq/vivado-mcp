# Journal - NJ (Part 1)

> AI development session journal
> Started: 2026-04-25

---



## Session 1: Trellis 接入 + 项目专属 spec 落地

**Date**: 2026-04-25
**Task**: Trellis 接入 + 项目专属 spec 落地
**Branch**: `main`

### Summary

(Add summary)

### Main Changes

## 本次工作

| 阶段 | 产出 |
|---|---|
| 接入 trellis | 完成 onboard,理清三层架构(tools/vivado/analysis)在 trellis spec 里的归属 |
| 改造 spec 模板 | 把"web 应用"形态的默认模板(database/API/前端)改造成 vivado-mcp 真实形态(MCP server + Tcl 协议 + EDA 包装) |
| 写 6 份 spec | 全部基于实机踩过的坑(B14/B15/B16、0.2.0 砍工具、0.3.8 七处 except: pass、0.3.9 diagnose_all 拒收、0.3.12 跨平台测试) |
| 改名 | `database-guidelines.md` → `tcl-protocol-guidelines.md`(原文件名语义错,本项目无数据库) |
| 写 memory | 三条非显然事实:trellis-spec / release-flow(PyPI 验证坑)/ less-is-more 工具哲学 |

## Spec 文件清单(`.trellis/spec/backend/`)

- `index.md` — 项目形态、三层架构、读 spec 优先级
- `directory-structure.md` — 三层职责边界 + 加新模块决策树 + 文件大小红线
- `tcl-protocol-guidelines.md`(新名) — sentinel 协议、`VMCP_*` 命名空间(B16 教训核心)
- `error-handling.md` — `[READY]/[WARN]/[BLOCK]/[DEGRADED]` 四档判定 + 降级路径模式
- `quality-guidelines.md` — "少即是多"工具哲学 + 加新工具准入条件 + 9 条 anti-pattern 禁令
- `logging-guidelines.md` — `logger = logging.getLogger(__name__)`,stdio MCP 下 print 禁止

## Memory(`~/.claude/projects/.../memory/`)

- `MEMORY.md`(索引)+ `trellis-spec.md` + `release-flow.md` + `less-is-more.md`

## 关键决策

1. **不重写全局 CLAUDE.md 规则**(中文输出、1.4 错误处理铁律)—— spec 只写项目特化补充,避免重复
2. **`tcl-protocol-guidelines.md` 设为必读** —— 协议层 bug 是本项目最难追的(B16 潜伏从 0.3.0 到 0.3.10),加任何 `VMCP_*` 前缀前必须查
3. **spec 内容全部基于真实事件**,不是泛泛"最佳实践";拒绝模式都有版本号溯源(便于未来 AI 理解 why)

## 未提交内容(下一步要 commit)

- `.trellis/spec/` 全部 6 份(spec 内容)
- `.trellis/workflow.md` / `.trellis/scripts/` / `.trellis/.template-hashes.json` 等 trellis 自带文件
- `.trellis/.gitignore`
- `.claude/settings.json` 改动 + `.claude/agents,commands,hooks` 三个目录
- `AGENTS.md`(根目录)

trellis 脚本只 auto-commit `.trellis/workspace` 和 `.trellis/tasks`,其余需手动 commit。

## Updated Files

- `.trellis/spec/backend/index.md`(重写)
- `.trellis/spec/backend/directory-structure.md`(重写)
- `.trellis/spec/backend/tcl-protocol-guidelines.md`(改名 + 重写)
- `.trellis/spec/backend/error-handling.md`(重写)
- `.trellis/spec/backend/quality-guidelines.md`(重写)
- `.trellis/spec/backend/logging-guidelines.md`(重写)
- `~/.claude/projects/.../memory/MEMORY.md`(新建)
- `~/.claude/projects/.../memory/{trellis-spec,release-flow,less-is-more}.md`(新建)


### Git Commits

| Hash | Message |
|------|---------|
| `03bd7b2` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
