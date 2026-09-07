"""Shared hardware-manager address and exact-selection helpers."""

from urllib.parse import urlsplit


def is_valid_hw_server_url(url: str) -> bool:
    """Validate a bare host:port endpoint, including bracketed IPv6."""
    value = url.strip()
    if not value:
        return False
    try:
        parsed = urlsplit(value if "://" in value else f"tcp://{value}")
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.hostname
        and port is not None
        and 1 <= port <= 65535
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def is_loopback_hw_server(url: str) -> bool:
    """Return True only for localhost loopback host names/addresses."""
    value = url.strip()
    if not is_valid_hw_server_url(value):
        return False
    parsed = urlsplit(value if "://" in value else f"tcp://{value}")
    return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


def parse_hw_server_url(url: str) -> tuple[str, int]:
    """Return normalized host/port after the public endpoint validation."""
    value = url.strip()
    if not is_valid_hw_server_url(value):
        raise ValueError(f"invalid hw_server URL: {url!r}")
    parsed = urlsplit(value if "://" in value else f"tcp://{value}")
    assert parsed.hostname is not None and parsed.port is not None
    return parsed.hostname.lower(), parsed.port


def select_exact_tcl_proc() -> str:
    """Tcl helper selecting one object by object name or NAME property."""
    return """proc __vmcp_select_exact {objects requested kind} {
    if {$requested eq ""} {
        if {[llength $objects] != 1} {
            error "Expected exactly one $kind, found [llength $objects]: $objects"
        }
        return [lindex $objects 0]
    }
    set matches {}
    foreach obj $objects {
        set name ""
        catch {set name [get_property NAME $obj]}
        if {[string equal $obj $requested] || [string equal $name $requested]} {
            if {[lsearch -exact $matches $obj] < 0} {lappend matches $obj}
        }
    }
    if {[llength $matches] != 1} {
        error "Expected exactly one $kind named '$requested', found [llength $matches]"
    }
    return [lindex $matches 0]
}"""


def select_hw_server_tcl_proc() -> str:
    """Tcl helper selecting/connecting the exact requested hw_server endpoint."""
    return r"""proc __vmcp_hw_host_matches {expected actual} {
    set expected [string tolower $expected]
    set actual [string tolower $actual]
    set loopbacks {localhost 127.0.0.1 ::1}
    if {[lsearch -exact $loopbacks $expected] >= 0
            && [lsearch -exact $loopbacks $actual] >= 0} {
        return 1
    }
    return [string equal $expected $actual]
}
proc __vmcp_find_hw_servers {expected_host expected_port} {
    set matches {}
    foreach server [get_hw_servers -quiet] {
        set host [get_property HOST $server]
        set sid [get_property SID $server]
        set sid_port ""
        regexp {^[^:]+:(.*):([0-9]+)$} $sid -> sid_host sid_port
        if {[__vmcp_hw_host_matches $expected_host $host]
                && [string equal $expected_port $sid_port]} {
            lappend matches $server
        }
    }
    return $matches
}
proc __vmcp_select_hw_server {url expected_host expected_port} {
    set matches [__vmcp_find_hw_servers $expected_host $expected_port]
    if {[llength $matches] == 0} {
        connect_hw_server -url $url
        set matches [__vmcp_find_hw_servers $expected_host $expected_port]
    }
    if {[llength $matches] != 1} {
        error "Expected exactly one hw_server at $url, found [llength $matches]: $matches"
    }
    return [lindex $matches 0]
}"""
