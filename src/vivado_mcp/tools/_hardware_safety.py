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
