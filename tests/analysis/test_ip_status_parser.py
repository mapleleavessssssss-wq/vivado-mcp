"""ip_status_parser 单元测试。"""

from vivado_mcp.analysis.ip_status_parser import (
    format_ip_status_report,
    parse_ip_status,
)

# Vivado 2019.1 典型输出(对齐列)
SAMPLE_MIXED = """\
INFO: [IP_Flow 19-5107] No upgrade source is specified, using latest version
Time (s): cpu = 00:00:01

IP STATUS
---------

IP                     Status                       Lock Status
-------------------------------------------------------------------
axi_gpio_0             IP upgrade is required       Unlocked
axi_bram_ctrl_0        Current                      Unlocked
fifo_generator_0       Major changes from prior     Locked
clk_wiz_0              Current                      Unlocked
"""

SAMPLE_ALL_CURRENT = """\
IP STATUS
---------
IP                 Status      Lock Status
-------------------------------------------
foo_0              Current     Unlocked
bar_1              Current     Unlocked
"""

SAMPLE_NO_IP = """\
ERROR: [Vivado 12-3645] There are no IP instances
"""


def test_parses_mixed_statuses():
    rep = parse_ip_status(SAMPLE_MIXED)
    assert len(rep.ips) == 4
    names = [ip.name for ip in rep.ips]
    assert "axi_gpio_0" in names
    assert "clk_wiz_0" in names


def test_detects_upgrade_required():
    rep = parse_ip_status(SAMPLE_MIXED)
    upgrade_names = {ip.name for ip in rep.need_upgrade}
    assert "axi_gpio_0" in upgrade_names
    assert "fifo_generator_0" in upgrade_names
    assert "axi_bram_ctrl_0" not in upgrade_names  # Current


def test_detects_locked():
    rep = parse_ip_status(SAMPLE_MIXED)
    locked_names = {ip.name for ip in rep.locked}
    assert "fifo_generator_0" in locked_names


def test_all_current_has_empty_upgrade_list():
    rep = parse_ip_status(SAMPLE_ALL_CURRENT)
    assert len(rep.need_upgrade) == 0
    assert len(rep.current) == 2


def test_no_header_returns_empty():
    rep = parse_ip_status(SAMPLE_NO_IP)
    assert len(rep.ips) == 0


def test_format_mentions_upgrade_count():
    rep = parse_ip_status(SAMPLE_MIXED)
    text = format_ip_status_report(rep)
    assert "升级" in text
    assert "axi_gpio_0" in text
    assert "upgrade_ip" in text  # 升级建议里


def test_format_all_current_says_up_to_date():
    rep = parse_ip_status(SAMPLE_ALL_CURRENT)
    text = format_ip_status_report(rep)
    assert "最新" in text


def test_empty_report_graceful():
    rep = parse_ip_status("")
    text = format_ip_status_report(rep)
    assert "无 IP" in text or "未能解析" in text
