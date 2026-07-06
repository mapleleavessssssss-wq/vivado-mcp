"""Tests for Tcl snippets that query IP metadata."""

from vivado_mcp.tcl_scripts import INSPECT_IP_PARAMS, QUERY_PROJECT_INFO


def test_inspect_ip_params_falls_back_to_ipdef():
    """Vivado 2018.x IP objects may expose IPDEF instead of VLNV."""
    tcl = INSPECT_IP_PARAMS.format(ip_name="gtwizard_0")

    assert "set __props [list_property $__ip]" in tcl
    assert "lsearch -exact $__props VLNV" in tcl
    assert "lsearch -exact $__props IPDEF" in tcl
    assert "set __vlnv [get_property IPDEF $__ip]" in tcl
    assert tcl.index("set __props [list_property $__ip]") < tcl.index(
        "get_property VLNV $__ip"
    )


def test_query_project_info_falls_back_to_ipdef():
    """Project IP listing should use IPDEF when VLNV is unavailable."""
    assert "set __ip_props [list_property $__ip]" in QUERY_PROJECT_INFO
    assert "lsearch -exact $__ip_props VLNV" in QUERY_PROJECT_INFO
    assert "lsearch -exact $__ip_props IPDEF" in QUERY_PROJECT_INFO
    assert "set __vlnv [get_property IPDEF $__ip]" in QUERY_PROJECT_INFO
