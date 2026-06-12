"""xdc_auto_fixer 单元测试。

覆盖:MISSING_IOSTANDARD / CLOCK_NO_PERIOD / 不可修问题跳过 / 未知板 /
dry_run vs apply / 多文件。
"""

import logging
from pathlib import Path

from vivado_mcp.analysis.xdc_auto_fixer import (
    apply_fixes,
    format_fix_report,
    plan_fixes,
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# -- MISSING_IOSTANDARD ------------------------------------------------------- #

def test_missing_iostd_generates_insert_patch(tmp_path):
    xdc = _write(tmp_path, "basys3.xdc", """\
# clock
set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]

# led without IOSTANDARD
set_property PACKAGE_PIN U16 [get_ports {led[0]}]
""")
    rep = plan_fixes([xdc], board="basys3")
    # 应该只为 led[0] 生成一条补丁,clk 已经有 IOSTANDARD 不补
    assert len(rep.patches) == 1
    p = rep.patches[0]
    assert p.rule == "MISSING_IOSTANDARD"
    assert p.action == "insert_after"
    assert "LVCMOS33" in p.after_text
    assert "led[0]" in p.after_text


def test_apply_writes_new_line_after_trigger(tmp_path):
    xdc = _write(tmp_path, "missing.xdc", """\
set_property PACKAGE_PIN U16 [get_ports {led[0]}]
""")
    rep = plan_fixes([xdc], board="basys3")
    applied = apply_fixes(rep)
    assert applied.dry_run is False
    assert str(xdc) in applied.files_modified
    new_lines = xdc.read_text(encoding="utf-8").splitlines()
    assert len(new_lines) == 2
    assert "PACKAGE_PIN U16" in new_lines[0]
    assert "IOSTANDARD LVCMOS33" in new_lines[1]
    assert "led[0]" in new_lines[1]


def test_default_iostd_is_lvcmos33_without_board(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="")
    assert len(rep.patches) == 1
    assert "LVCMOS33" in rep.patches[0].after_text


def test_kc705_uses_lvcmos25(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN AE5 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="kc705")
    assert "LVCMOS25" in rep.patches[0].after_text


# -- CLOCK_NO_PERIOD ---------------------------------------------------------- #

def test_clock_no_period_known_board(tmp_path):
    xdc = _write(tmp_path, "clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
    rep = plan_fixes([xdc], board="basys3")
    # lint 检出 CLOCK_NO_PERIOD,board=basys3 => period=10.0
    clock_patches = [p for p in rep.patches if p.rule == "CLOCK_NO_PERIOD"]
    assert len(clock_patches) == 1
    assert "-period 10.0" in clock_patches[0].after_text


def test_clock_no_period_unknown_board_skipped(tmp_path):
    xdc = _write(tmp_path, "clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
    rep = plan_fixes([xdc], board="")   # 未指定板卡
    clock_patches = [p for p in rep.patches if p.rule == "CLOCK_NO_PERIOD"]
    assert len(clock_patches) == 0  # 不猜 period
    clock_skipped = [i for i in rep.skipped if i.rule == "CLOCK_NO_PERIOD"]
    assert len(clock_skipped) == 1


def test_clock_apply_modifies_line(tmp_path):
    xdc = _write(tmp_path, "clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
    rep = plan_fixes([xdc], board="basys3")
    apply_fixes(rep)
    new = xdc.read_text(encoding="utf-8")
    assert "-period 10.0" in new
    assert "create_clock" in new


# -- 不可修问题:全部进 skipped,不进 patches ---------------------------------- #

def test_pin_conflict_not_auto_fixed(tmp_path):
    xdc = _write(tmp_path, "x.xdc", """\
set_property PACKAGE_PIN W5 [get_ports a]
set_property IOSTANDARD LVCMOS33 [get_ports a]
set_property PACKAGE_PIN W5 [get_ports b]
set_property IOSTANDARD LVCMOS33 [get_ports b]
""")
    rep = plan_fixes([xdc], board="basys3")
    assert all(p.rule != "PIN_CONFLICT" for p in rep.patches)
    assert any(i.rule == "PIN_CONFLICT" for i in rep.skipped)


def test_duplicate_port_not_auto_fixed(tmp_path):
    xdc = _write(tmp_path, "x.xdc", """\
set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
set_property PACKAGE_PIN W7 [get_ports clk]
""")
    rep = plan_fixes([xdc], board="basys3")
    assert all(p.rule != "DUPLICATE_PORT" for p in rep.patches)


# -- dry_run 语义 ------------------------------------------------------------- #

def test_plan_does_not_modify_file(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    original = xdc.read_text(encoding="utf-8")
    rep = plan_fixes([xdc], board="basys3")
    assert rep.dry_run is True
    assert xdc.read_text(encoding="utf-8") == original
    assert rep.files_modified == []


# -- 多问题混合 -------------------------------------------------------------- #

def test_multi_issues_mixed(tmp_path):
    xdc = _write(tmp_path, "mixed.xdc", """\
create_clock -name sys_clk [get_ports clk]
set_property PACKAGE_PIN W5 [get_ports clk]
set_property PACKAGE_PIN U16 [get_ports foo]
set_property PACKAGE_PIN V16 [get_ports bar]
set_property PACKAGE_PIN W5 [get_ports baz]
""")
    # 有:CLOCK_NO_PERIOD(1) + MISSING_IOSTANDARD(4) + PIN_CONFLICT(W5 给 clk+baz)
    rep = plan_fixes([xdc], board="basys3")
    iostd_patches = [p for p in rep.patches if p.rule == "MISSING_IOSTANDARD"]
    clock_patches = [p for p in rep.patches if p.rule == "CLOCK_NO_PERIOD"]
    pin_skipped = [i for i in rep.skipped if i.rule == "PIN_CONFLICT"]
    assert len(iostd_patches) == 4
    assert len(clock_patches) == 1
    assert len(pin_skipped) >= 1


def test_apply_multi_insert_preserves_line_order(tmp_path):
    """多个 insert_after 按 line 降序插,行号不偏移。"""
    xdc = _write(tmp_path, "x.xdc", """\
set_property PACKAGE_PIN A1 [get_ports a]
set_property PACKAGE_PIN A2 [get_ports b]
set_property PACKAGE_PIN A3 [get_ports c]
""")
    rep = plan_fixes([xdc], board="basys3")
    apply_fixes(rep)
    lines = xdc.read_text(encoding="utf-8").splitlines()
    # 3 原行 + 3 插入行 = 6 行,每个 PACKAGE_PIN 后紧跟 IOSTANDARD
    assert len(lines) == 6
    assert "PACKAGE_PIN A1" in lines[0]
    assert "IOSTANDARD" in lines[1] and "[get_ports {a}]" in lines[1]
    assert "PACKAGE_PIN A2" in lines[2]
    assert "IOSTANDARD" in lines[3] and "[get_ports {b}]" in lines[3]
    assert "PACKAGE_PIN A3" in lines[4]
    assert "IOSTANDARD" in lines[5] and "[get_ports {c}]" in lines[5]


# -- P0:已满足的约束(通配符 / 纯 -dict)不得生成覆盖性补丁 ------------------- #

def test_wildcard_iostd_no_patch(tmp_path):
    """通配符 IOSTANDARD 已覆盖端口时,不得生成覆盖性补丁、不得写盘。"""
    xdc = _write(tmp_path, "wild.xdc", (
        "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n"
        "set_property PACKAGE_PIN W5 [get_ports {led[0]}]\n"
    ))
    original = xdc.read_text(encoding="utf-8")
    rep = plan_fixes([xdc], board="basys3")
    assert rep.patches == []
    applied = apply_fixes(rep)
    assert applied.files_modified == []
    assert xdc.read_text(encoding="utf-8") == original


def test_dict_only_iostd_no_patch(tmp_path):
    """纯 -dict IOSTANDARD + 独立 PACKAGE_PIN:约束已满足,不得生成补丁。"""
    xdc = _write(tmp_path, "d.xdc", (
        "set_property -dict { IOSTANDARD LVCMOS18 SLEW FAST } [get_ports btn]\n"
        "set_property PACKAGE_PIN T17 [get_ports btn]\n"
    ))
    rep = plan_fixes([xdc], board="basys3")
    assert rep.patches == []


def test_cross_file_iostd_no_overwrite_patch(tmp_path):
    """审计 P1:IOSTANDARD 与 PACKAGE_PIN 分属不同 XDC 时,不得生成覆盖性补丁。

    评审复现场景:iostd.xdc 写真实值 LVCMOS18,pins.xdc 只放 PACKAGE_PIN;
    修复前 fixer 会向 pins.xdc 插入 board 默认 LVCMOS33,后加载覆盖真实值。
    """
    f_iostd = _write(tmp_path, "iostd.xdc",
        "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n")
    f_pins = _write(tmp_path, "pins.xdc",
        "set_property PACKAGE_PIN U16 [get_ports {led[0]}]\n")
    original = f_pins.read_text(encoding="utf-8")
    rep = plan_fixes([f_iostd, f_pins], board="basys3")
    assert rep.patches == []
    apply_fixes(rep)
    assert f_pins.read_text(encoding="utf-8") == original


# -- P1:反斜杠续行 ------------------------------------------------------------ #

def test_continued_create_clock_with_period_no_patch(tmp_path):
    """-period 在续行上的合法 create_clock:不误报、不写盘。"""
    xdc = _write(tmp_path, "cont.xdc", (
        "create_clock -name sys_clk \\\n"
        "    -period 10.000 [get_ports clk]\n"
    ))
    original = xdc.read_text(encoding="utf-8")
    rep = plan_fixes([xdc], board="basys3")
    assert rep.patches == []
    apply_fixes(rep)
    assert xdc.read_text(encoding="utf-8") == original


def test_continued_create_clock_missing_period_skipped(tmp_path):
    """真缺 -period 的续行 create_clock:就地改会断续行,必须归 skipped。"""
    xdc = _write(tmp_path, "cont2.xdc", (
        "create_clock -name sys_clk \\\n"
        "    [get_ports clk]\n"
    ))
    original = xdc.read_text(encoding="utf-8")
    rep = plan_fixes([xdc], board="basys3")
    assert all(p.rule != "CLOCK_NO_PERIOD" for p in rep.patches)
    assert any(i.rule == "CLOCK_NO_PERIOD" for i in rep.skipped)
    apply_fixes(rep)
    assert xdc.read_text(encoding="utf-8") == original


def test_insert_after_continued_dict_keeps_continuation(tmp_path):
    """跨行 -dict 缺 IOSTANDARD:新行插在续行链之后,不得插进续行中间。"""
    xdc = _write(tmp_path, "contdict.xdc", (
        "set_property -dict { PACKAGE_PIN U16 \\\n"
        "    SLEW FAST } [get_ports {led[0]}]\n"
    ))
    rep = plan_fixes([xdc], board="basys3")
    assert len(rep.patches) == 1
    assert rep.patches[0].line == 2  # 插入点 = 续行链最后一行
    apply_fixes(rep)
    lines = xdc.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("\\")  # 续行结构未被破坏
    assert "SLEW FAST" in lines[1]
    assert "IOSTANDARD LVCMOS33" in lines[2]


def test_trailing_continuation_at_eof_skipped(tmp_path):
    """审计 P2:续行链悬挂到 EOF(最后一行以反斜杠结尾)时拒绝插入,归 skipped。

    悬挂反斜杠后插新行,Tcl 会把 backslash-newline 折叠成空格,
    两条 set_property 拼成一条非法命令 —— 原文件合法,修完反而坏。
    """
    xdc = _write(tmp_path, "dangle.xdc",
        "set_property PACKAGE_PIN U16 [get_ports foo] \\\n")
    original = xdc.read_bytes()
    rep = plan_fixes([xdc], board="basys3")
    assert all(p.rule != "MISSING_IOSTANDARD" for p in rep.patches)
    assert any(i.rule == "MISSING_IOSTANDARD" for i in rep.skipped)
    applied = apply_fixes(rep)
    assert applied.files_modified == []
    assert xdc.read_bytes() == original  # 文件一字未动


# -- P1:编码 / 备份 / 原子写 / 行尾保留 --------------------------------------- #

def test_gbk_file_roundtrip_preserves_chinese(tmp_path):
    """GBK 编码 XDC:写回仍是 GBK,中文注释原样保留,不得变 U+FFFD。"""
    xdc = tmp_path / "gbk.xdc"
    content = (
        "# 时钟引脚约束\n"
        "set_property PACKAGE_PIN U16 [get_ports {led[0]}]\n"
    )
    xdc.write_bytes(content.encode("gbk"))
    rep = plan_fixes([xdc], board="basys3")
    assert len(rep.patches) == 1
    apply_fixes(rep)
    new_text = xdc.read_bytes().decode("gbk")  # 写回仍是 GBK
    assert "时钟引脚约束" in new_text
    assert "�" not in new_text
    assert "IOSTANDARD LVCMOS33" in new_text


def test_undecodable_file_refused(tmp_path, caplog):
    """既非 UTF-8 也非 GBK 的文件:不生成补丁、不写回、原字节不变。"""
    xdc = tmp_path / "weird.xdc"
    raw = b"set_property PACKAGE_PIN U16 [get_ports foo]\n# \xff\xff\n"
    xdc.write_bytes(raw)
    with caplog.at_level(logging.WARNING):
        rep = plan_fixes([xdc], board="basys3")
    assert rep.patches == []
    assert any(i.rule == "MISSING_IOSTANDARD" for i in rep.skipped)
    # 降级有日志痕迹(error-handling spec 要求)
    assert any("DEGRADED" in r.getMessage() for r in caplog.records)
    # 审计 P2:降级还必须在用户可见报告里露出(不只 caplog)——
    # lint 的 ENCODING_DEGRADED 自然流入 fixer 的 skipped 展示
    assert any(i.rule == "ENCODING_DEGRADED" for i in rep.skipped)
    text = format_fix_report(rep)
    assert "ENCODING_DEGRADED" in text
    applied = apply_fixes(rep)
    assert applied.files_modified == []
    assert xdc.read_bytes() == raw


def test_apply_creates_bak_with_original_content(tmp_path):
    """写回前留 .bak 备份,内容 = 修改前的原始字节。"""
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    original_bytes = xdc.read_bytes()
    rep = plan_fixes([xdc], board="basys3")
    apply_fixes(rep)
    bak = tmp_path / "x.xdc.bak"
    assert bak.exists()
    assert bak.read_bytes() == original_bytes


def test_apply_write_failure_visible_and_bak_cleaned(tmp_path, monkeypatch):
    """审计 P2:写回失败要进 failed_files、报告里露出 [DEGRADED],并清理刚建的 .bak。

    模拟 Windows 文件被其它进程占住时 os.replace 抛 PermissionError 的场景。
    """
    import vivado_mcp.analysis.xdc_auto_fixer as fixer_mod

    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    original = xdc.read_bytes()
    rep = plan_fixes([xdc], board="basys3")

    def _boom(src, dst):
        raise PermissionError("[WinError 5] 拒绝访问(模拟文件被其它进程占用)")

    monkeypatch.setattr(fixer_mod.os, "replace", _boom)
    applied = apply_fixes(rep)

    assert applied.files_modified == []
    assert len(applied.failed_files) == 1
    fpath, reason = applied.failed_files[0]
    assert fpath == str(xdc)
    assert "拒绝访问" in reason  # 失败带具体原因,不只说"失败了"
    # 报告可见 [DEGRADED] 段,调用方不会把 0 个修改误判成成功
    text = format_fix_report(applied)
    assert "[DEGRADED]" in text
    assert "写回失败" in text
    # 失败时清理现场:.bak 与 .tmp 都不残留,原文件未损
    assert not (tmp_path / "x.xdc.bak").exists()
    assert not (tmp_path / ".x.xdc.tmp").exists()
    assert xdc.read_bytes() == original


def test_crlf_file_keeps_crlf(tmp_path):
    """CRLF 文件写回后所有换行仍是 CRLF。"""
    xdc = tmp_path / "crlf.xdc"
    xdc.write_bytes(b"set_property PACKAGE_PIN U16 [get_ports foo]\r\n")
    rep = plan_fixes([xdc], board="basys3")
    apply_fixes(rep)
    data = xdc.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")  # 没有混入裸 LF
    assert data.count(b"\r\n") == 2  # 原行 + 插入行


def test_lf_file_keeps_lf(tmp_path):
    """LF 文件写回后不得混入 CR。"""
    xdc = tmp_path / "lf.xdc"
    xdc.write_bytes(b"set_property PACKAGE_PIN U16 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="basys3")
    apply_fixes(rep)
    assert b"\r" not in xdc.read_bytes()


# -- format ------------------------------------------------------------------- #

def test_format_dry_run_mentions_confirm(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="basys3")
    text = format_fix_report(rep)
    assert "DRY-RUN" in text
    assert "dry_run=False" in text


def test_format_applied_lists_files(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="basys3")
    applied = apply_fixes(rep)
    text = format_fix_report(applied)
    assert "APPLIED" in text
    assert "x.xdc" in text
