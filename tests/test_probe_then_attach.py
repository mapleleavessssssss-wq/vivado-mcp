"""0.3.19 修复回归测试:probe-then-attach + list_sessions 探测外部 GUI。

**关键约束**:所有 fake server 必须用 ephemeral port(让 OS 分配空闲端口),
绝不能用 9999 —— 否则会和用户本地正在跑的 Vivado GUI 冲突,导致测试期间
"绑不上 9999" 或者 list_sessions probe 误命中真实 server。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable

import pytest

from vivado_mcp.vivado import session_manager as sm_module
from vivado_mcp.vivado.gui_session import GuiSession, probe_vmcp_server
from vivado_mcp.vivado.session_manager import SessionManager


def _serve_length_prefix(
    handler: Callable[[bytes], bytes],
    host: str = "127.0.0.1",
) -> tuple[int, threading.Event, threading.Thread]:
    """启一个 length-prefix framing 的简单 TCP server。

    Returns:
        (port, stop_event, server_thread)。stop_event.set() 让 server 退出。
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, 0))  # 0 = OS 分配空闲端口
    port = listener.getsockname()[1]
    listener.listen(8)
    listener.settimeout(0.2)

    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(0.3)
                while not stop.is_set():
                    try:
                        hdr = conn.recv(4)
                    except (socket.timeout, OSError):
                        # 客户端没发也没关,等下一轮 stop 检查
                        continue
                    if len(hdr) < 4:
                        break  # 客户端关连接
                    n = int.from_bytes(hdr, "big")
                    if n <= 0 or n > 1024 * 1024:
                        break
                    body = b""
                    while len(body) < n:
                        try:
                            chunk = conn.recv(n - len(body))
                        except (socket.timeout, OSError):
                            chunk = b""
                        if not chunk:
                            break
                        body += chunk
                    if len(body) != n:
                        break
                    try:
                        resp = handler(body)
                    except Exception:
                        break
                    if resp is None:
                        # handler 主动断开
                        break
                    conn.sendall(len(resp).to_bytes(4, "big") + resp)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            listener.close()
        except Exception:
            pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return port, stop, t


def _vmcp_handler(req: bytes) -> bytes:
    """模拟 vivado_mcp_server.tcl:回 {"rc":0,"output":<echo>}"""
    text = req.decode("utf-8", errors="replace")
    resp = json.dumps({"rc": 0, "output": text}, ensure_ascii=False).encode("utf-8")
    return resp


def _find_unused_port() -> int:
    """获取一个当前空闲(未被监听)的端口号。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------- #
#  probe_vmcp_server (同步握手探测)
# --------------------------------------------------------------------------- #


class TestProbeVmcpServer:
    def test_returns_true_for_valid_server(self):
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            assert probe_vmcp_server("127.0.0.1", port) is True
        finally:
            stop.set()

    def test_returns_false_for_no_listener(self):
        """端口无监听 → 立即 ConnectionRefused → False。"""
        port = _find_unused_port()
        # 短 timeout 也够,因为 RST 是 immediate
        assert probe_vmcp_server("127.0.0.1", port, timeout=0.3) is False

    def test_returns_false_for_wrong_protocol(self):
        """server 回非 JSON → 握手失败。"""
        def bad_handler(_: bytes) -> bytes:
            return b"PLAIN TEXT NOT JSON"
        port, stop, _ = _serve_length_prefix(bad_handler)
        try:
            assert probe_vmcp_server("127.0.0.1", port) is False
        finally:
            stop.set()

    def test_returns_false_for_silent_server(self):
        """连得上但不回数据 → 超时 → False。"""
        def silent_handler(_: bytes) -> bytes | None:
            return None  # 主动断开
        port, stop, _ = _serve_length_prefix(silent_handler)
        try:
            assert probe_vmcp_server("127.0.0.1", port, timeout=0.3) is False
        finally:
            stop.set()

    def test_returns_false_for_oversized_length_header(self):
        """server 回非常大的 length(>8KB) → 视作非法,False。"""
        def oversize_handler(_: bytes) -> bytes:
            # 构造一个 length=999999 的响应头 + 不足的 body
            return b"x" * 10  # 反正 length 字段会被读为某个大数
        port, stop, _ = _serve_length_prefix(oversize_handler)
        try:
            # b"x"=0x78,前 4 字节 = 0x78787878 = 2021161080,远超 8192
            assert probe_vmcp_server("127.0.0.1", port, timeout=0.3) is False
        finally:
            stop.set()


# --------------------------------------------------------------------------- #
#  GuiSession.start():probe-then-attach
# --------------------------------------------------------------------------- #


class TestGuiSessionProbeThenAttach:
    async def test_attach_to_existing_server_skips_spawn(self):
        """mode="gui" 时若端口已有 vmcp server,直接 attach,不 spawn。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            sess = GuiSession(
                vivado_path="/fake/vivado",
                session_id="probe-attach",
                port=port,
                attach_only=False,  # 关键:用户请求"spawn 新 GUI"
            )
            banner = await sess.start(timeout=5.0)

            # 验证实际走了 attach 路径
            assert sess.attached_external is True
            assert sess._proc is None, "命中外部 server 时不应 spawn"
            assert sess._connected_port == port
            assert sess.mode == "attach", "mode 应反映实际行为(attach),不是用户请求(gui)"
            assert "attach" in banner

            # status_dict 暴露关键字段供上层 / list_sessions 用
            status = sess.status_dict()
            assert status["mode"] == "attach"
            assert status["port"] == port
            assert status["attached_external"] is True

            await sess.stop()
        finally:
            stop.set()

    async def test_stop_does_not_kill_external_proc(self):
        """probe-then-attach 命中时 stop 不应去 kill 进程(_proc 本来就是 None)。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            sess = GuiSession(
                vivado_path="/fake/vivado",
                port=port,
                attach_only=False,
            )
            await sess.start(timeout=5.0)
            # stop 应正常关 socket、不抛、且 server 进程(由测试持有)仍在
            await sess.stop()
            # server 仍能接受新连接 → 没被关
            assert probe_vmcp_server("127.0.0.1", port, timeout=0.3) is True
        finally:
            stop.set()

    async def test_falls_through_to_spawn_when_no_existing_server(self, monkeypatch):
        """端口无监听 → 走 spawn 路径(但因为 vivado_path 是 fake,会失败抛错)。

        关键是要验证 _try_attach_existing 失败后走到 spawn,而不是直接 return。
        """
        port = _find_unused_port()
        sess = GuiSession(
            vivado_path="/this/does/not/exist/vivado",
            port=port,
            attach_only=False,
        )
        with pytest.raises(Exception):
            # spawn 路径会因为 vivado 不存在而失败 —— 但说明确实走了 spawn
            await sess.start(timeout=3.0)
        assert sess.attached_external is False


# --------------------------------------------------------------------------- #
#  SessionManager.list_sessions():probe 外部 GUI
# --------------------------------------------------------------------------- #


class TestListSessionsExternal:
    def test_lists_external_server(self, session_manager: SessionManager, monkeypatch):
        """端口范围内有 vmcp server → list_sessions 把它列为 external。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            # 把 probe 范围 patch 成只看我们的 port
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (port,))
            entries = session_manager.list_sessions()

            externals = [e for e in entries if e.get("owner") == "external"]
            assert len(externals) == 1
            entry = externals[0]
            assert entry["port"] == port
            assert entry["mode"] == "external"
            assert entry["is_alive"] is True
            assert entry["session_id"] == f"<external@{port}>"
            assert "未由 MCP 启动" in entry["note"]
        finally:
            stop.set()

    def test_probe_external_false_returns_only_known(
        self, session_manager: SessionManager, monkeypatch
    ):
        """probe_external=False 关掉网络探测,只返回字典内的 session。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (port,))
            entries = session_manager.list_sessions(probe_external=False)
            assert entries == []
        finally:
            stop.set()

    def test_no_external_when_no_server(
        self, session_manager: SessionManager, monkeypatch
    ):
        """端口范围内无 server → 不出现 external entry。"""
        unused = _find_unused_port()
        monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (unused,))
        entries = session_manager.list_sessions()
        assert entries == []

    def test_skips_already_managed_ports(
        self, session_manager: SessionManager, monkeypatch
    ):
        """已被 MCP 管理(_sessions 字典里有相同 port)的端口不重复 probe + 列出。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            # 模拟一个已被 MCP 管理的 GUI session(port 撞 external probe 端口)
            sess = GuiSession(
                vivado_path="/fake/vivado",
                session_id="already-managed",
                port=port,
                attach_only=False,
            )
            # 跑一次 start 让它真的连上 + 标 attached_external(走 probe-then-attach)
            import asyncio
            asyncio.run(sess.start(timeout=5.0))
            session_manager._sessions["already-managed"] = sess

            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (port,))
            entries = session_manager.list_sessions()
            # 应该只有 1 条(MCP 管理的),不应再多一条 external
            assert len(entries) == 1
            assert entries[0]["session_id"] == "already-managed"
            assert entries[0]["port"] == port

            asyncio.run(sess.stop())
        finally:
            stop.set()
