"""0.3.19 修复回归测试:probe-then-attach + list_sessions 探测外部 GUI。

**关键约束**:所有 fake server 必须用 ephemeral port(让 OS 分配空闲端口),
绝不能用 9999 —— 否则会和用户本地正在跑的 Vivado GUI 冲突,导致测试期间
"绑不上 9999" 或者 list_sessions probe 误命中真实 server。
"""

from __future__ import annotations

import asyncio
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

    每个连接独立线程服务:真实 tcl server 是 fileevent 多连接并发,A2 的
    current_project 一次性短连接查询会在主连接存活期间到达,串行 accept
    会让它卡在 backlog 里永远等不到响应。

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

    def _serve_conn(conn: socket.socket) -> None:
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
                try:
                    conn.sendall(len(resp).to_bytes(4, "big") + resp)
                except OSError:
                    # 客户端已超时关连接(如 A2 查询放弃),静默退出本连接
                    break
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=_serve_conn, args=(conn,), daemon=True
            ).start()
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

    def test_returns_false_when_output_lacks_magic_token(self):
        """0.3.21 修复回归:server 回合法 JSON 但 output 不含本次探测 token →
        视为非 vmcp 服务(如 VMware vNIC 上某个偶发回 JSON 的服务),False。
        """
        def echo_unrelated_handler(_: bytes) -> bytes:
            # 不 echo 输入,固定回别的内容 —— 模拟非 vmcp 但格式相近的服务
            resp = json.dumps({"rc": 0, "output": "some unrelated payload"}).encode("utf-8")
            return resp
        port, stop, _ = _serve_length_prefix(echo_unrelated_handler)
        try:
            assert probe_vmcp_server("127.0.0.1", port, timeout=0.3) is False
        finally:
            stop.set()

    def test_returns_false_when_output_missing(self):
        """server 回 dict 但缺 output 字段 → False。"""
        def missing_output_handler(_: bytes) -> bytes:
            resp = json.dumps({"rc": 0}).encode("utf-8")
            return resp
        port, stop, _ = _serve_length_prefix(missing_output_handler)
        try:
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
        """显式端口但无监听 → 走 spawn 路径(因为 vivado_path 是 fake,会失败抛错)。

        关键是要验证 _try_attach_existing 失败后走到 spawn,而不是直接 return。
        """
        port = _find_unused_port()
        sess = GuiSession(
            vivado_path="/this/does/not/exist/vivado",
            port=port,  # 显式 port>0
            attach_only=False,
        )
        with pytest.raises(Exception):
            # spawn 路径会因为 vivado 不存在而失败 —— 但说明确实走了 spawn
            await sess.start(timeout=3.0)
        assert sess.attached_external is False

    async def test_unspecified_port_auto_allocs_and_skips_probe(self):
        """B 方案核心:port=0(未指定)= 要独立新实例,**不**去 attach 现有 server。

        哪怕某 ephemeral 端口上正跑着一个 vmcp server,port=0 路径也不该 probe 它
        attach 上去(证明"要独立新实例"意图不会被 probe 抢成 attach)。port=0 会
        auto-alloc 空闲端口 → spawn(fake vivado 会失败抛错),关键断言 attached_external
        始终 False。
        """
        # 起一个 fake vmcp server 占着某个 ephemeral 端口(模拟"已有第1个实例")
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            sess = GuiSession(
                vivado_path="/this/does/not/exist/vivado",
                session_id="auto-alloc",
                port=0,  # 哨兵:未指定 → 独立新实例,跳过 probe
                attach_only=False,
            )
            with pytest.raises(Exception):
                # 跳过 probe → auto-alloc → spawn(fake vivado 不存在 → 抛错)
                await sess.start(timeout=3.0)
            # 绝不能 attach 到那个现成的 server
            assert sess.attached_external is False, (
                "port=0 要独立新实例,绝不该被 probe 抢成 attach 到现有 server"
            )
            assert sess.mode == "gui"
            # 证明确实 auto-alloc 了一个端口(且不是那个已占用的 server 端口)
            assert sess._allocated_port is not None
            assert sess._allocated_port != port
        finally:
            stop.set()

    async def test_spawn_binds_exact_injected_port(self, monkeypatch):
        """防回归到池滑动:写进临时 tcl 的 VMCP_PORT_PREF 必须等于 Python 选定的确切端口。

        显式端口路径(port>0,无现存 server)→ spawn 应把**该确切端口**注入 tcl,
        不再注入"池起点"。这里 mock subprocess 拦下写好的临时脚本内容校验。
        """
        import subprocess as subprocess_mod

        explicit_port = _find_unused_port()
        captured: dict[str, str] = {}

        async def fake_exec(*args, **kwargs):
            # 此时临时 tcl 脚本已写好,读出内容校验注入端口
            sess_tmp = sess._tmp_script
            with open(sess_tmp, encoding="utf-8") as f:
                captured["tcl"] = f.read()

            # 返回一个 fake proc,returncode=None(假装活着),让连接循环超时退出
            class _FakeProc:
                pid = 4242
                returncode = None

                async def wait(self):
                    return 0

                def kill(self):
                    pass

            return _FakeProc()

        monkeypatch.setattr(
            "asyncio.create_subprocess_exec", fake_exec
        )
        # start 超时路径现在会清理 spawn 的进程(taskkill),拦下防真杀 pid 4242
        monkeypatch.setattr(subprocess_mod, "run", lambda *a, **kw: None)

        sess = GuiSession(
            vivado_path="/fake/vivado",
            session_id="exact-port",
            port=explicit_port,
            attach_only=False,
        )
        # 连不上(没真 server)→ 超时抛错,但 spawn 已发生、tcl 已写
        with pytest.raises(Exception):
            await sess.start(timeout=1.0)

        assert "tcl" in captured, "spawn 应已写临时 tcl 脚本"
        assert f"set ::VMCP_PORT_PREF {explicit_port}" in captured["tcl"], (
            "必须注入确切显式端口,不能是池起点或别的值"
        )
        assert sess.pid == 4242, "spawn 时应记下 vivado pid"

    async def test_spawn_auto_alloc_injects_allocated_port(self, monkeypatch):
        """port=0 路径:注入 tcl 的 VMCP_PORT_PREF 必须等于 auto-alloc 出的确切端口。"""
        import subprocess as subprocess_mod

        captured: dict[str, str] = {}

        async def fake_exec(*args, **kwargs):
            with open(sess._tmp_script, encoding="utf-8") as f:
                captured["tcl"] = f.read()

            class _FakeProc:
                pid = 777
                returncode = None

                async def wait(self):
                    return 0

                def kill(self):
                    pass

            return _FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        # 拦下超时清理路径的 taskkill,防真杀 pid 777
        monkeypatch.setattr(subprocess_mod, "run", lambda *a, **kw: None)

        sess = GuiSession(
            vivado_path="/fake/vivado",
            session_id="auto-alloc-inject",
            port=0,
            attach_only=False,
        )
        with pytest.raises(Exception):
            await sess.start(timeout=1.0)

        assert sess._allocated_port is not None
        assert f"set ::VMCP_PORT_PREF {sess._allocated_port}" in captured["tcl"]

    async def test_stop_kills_by_recorded_pid(self, monkeypatch):
        """spawn 路径记下 pid;stop 在双守卫通过时按该 pid 调 taskkill(Win)/proc.kill(Unix)。"""
        import subprocess as subprocess_mod
        import sys

        recorded_pid = 31337
        kill_calls: list[list[str]] = []
        killed = {"flag": False}

        def fake_run(cmd, *a, **kw):
            kill_calls.append(cmd)
            return None

        # start 超时清理路径 + stop 强杀路径都会走 taskkill,统一拦下记录
        monkeypatch.setattr(subprocess_mod, "run", fake_run)

        async def fake_exec(*args, **kwargs):
            class _FakeProc:
                pid = recorded_pid
                returncode = None

                async def wait(self):
                    # 永远不自己退,逼 stop 走强杀分支
                    raise asyncio.TimeoutError

                def kill(self):
                    pass

            return _FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        sess = GuiSession(
            vivado_path="/fake/vivado",
            session_id="kill-by-pid",
            port=_find_unused_port(),
            attach_only=False,
        )
        # 连不上 → start 超时抛错,但 self._proc / self._pid 已就绪
        with pytest.raises(Exception):
            await sess.start(timeout=1.0)
        assert sess.pid == recorded_pid

        # 让 stop 进入强杀分支:state 置成 READY 以触发双守卫内逻辑
        # (这里不需要真连接,只验 taskkill/kill 用对了 pid)
        # start 超时清理已记录过 taskkill,清掉只看 stop 的调用
        kill_calls.clear()

        # 重新构造一个 fake proc 给 stop 用(start 里的已被消费)
        class _StopProc:
            pid = recorded_pid
            returncode = None

            async def wait(self):
                # 第一次(等优雅退)超时,触发强杀;强杀后标记已死
                if killed["flag"]:
                    return 0
                raise asyncio.TimeoutError

            def kill(self):
                killed["flag"] = True

        stop_proc = _StopProc()
        sess._proc = stop_proc
        sess._writer = None  # 跳过优雅 exit 的 TCP 发送
        from vivado_mcp.vivado.base_session import SessionState
        sess._state = SessionState.READY

        await sess.stop(timeout=1.0)

        if sys.platform == "win32":
            assert kill_calls, "Windows 应调用 taskkill"
            assert str(recorded_pid) in kill_calls[0], "taskkill 必须用记录的 pid"
            assert "/T" in kill_calls[0], "必须保留 /T 递归杀进程树"
        else:
            assert killed["flag"] is True, "Unix 应调用 proc.kill"


# --------------------------------------------------------------------------- #
#  SessionManager.list_sessions():probe 外部 GUI
# --------------------------------------------------------------------------- #


class TestListSessionsExternal:
    async def test_lists_external_server(
        self, session_manager: SessionManager, monkeypatch
    ):
        """端口范围内有 vmcp server → list_sessions 把它列为 external。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            # 把 probe 范围 patch 成只看我们的 port
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (port,))
            entries = await session_manager.list_sessions()

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

    async def test_probe_external_false_returns_only_known(
        self, session_manager: SessionManager, monkeypatch
    ):
        """probe_external=False 关掉网络探测,只返回字典内的 session。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (port,))
            entries = await session_manager.list_sessions(probe_external=False)
            assert entries == []
        finally:
            stop.set()

    async def test_no_external_when_no_server(
        self, session_manager: SessionManager, monkeypatch
    ):
        """端口范围内无 server → 不出现 external entry。"""
        unused = _find_unused_port()
        monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (unused,))
        entries = await session_manager.list_sessions()
        assert entries == []

    async def test_skips_already_managed_ports(
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
            await sess.start(timeout=5.0)
            session_manager._sessions["already-managed"] = sess

            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (port,))
            entries = await session_manager.list_sessions()
            # 应该只有 1 条(MCP 管理的),不应再多一条 external
            assert len(entries) == 1
            assert entries[0]["session_id"] == "already-managed"
            assert entries[0]["port"] == port

            await sess.stop()
        finally:
            stop.set()


# --------------------------------------------------------------------------- #
#  握手 token 校验归一(0.3.21 修复补到 _handshake / attach 路径)
# --------------------------------------------------------------------------- #


class TestHandshakeTokenUnified:
    def test_verify_probe_resp_shared_logic(self):
        """probe 与 _handshake 共用的 token 校验:正反例。"""
        from vivado_mcp.vivado.gui_session import (
            _make_probe_payload,
            _verify_probe_resp,
        )

        token, payload = _make_probe_payload()
        assert token.encode("utf-8") in payload, "payload 必须携带本次 token"
        # 正例:output 反射了 token
        assert _verify_probe_resp({"rc": 0, "output": f"x {token}\n"}, token)
        # 反例:格式对但 token 未反射(VMware vNIC 类假阳性)
        assert not _verify_probe_resp({"rc": 0, "output": "unrelated"}, token)
        # 反例:缺字段 / 非 dict
        assert not _verify_probe_resp({"rc": 0}, token)
        assert not _verify_probe_resp(["rc", "output"], token)

    async def test_attach_path_rejects_unrelated_json_listener(self):
        """attach 路径(_handshake)不能再被"合法 JSON 但不反射 token"的
        listener 骗过 —— 0.3.21 只修了 probe 半边,本测试锁住 _handshake。
        """
        def echo_unrelated_handler(_: bytes) -> bytes:
            return json.dumps(
                {"rc": 0, "output": "some unrelated payload"}
            ).encode("utf-8")

        port, stop, _ = _serve_length_prefix(echo_unrelated_handler)
        try:
            sess = GuiSession(
                vivado_path="/this/does/not/exist/vivado",
                session_id="reject-fake",
                port=port,
                attach_only=False,
            )
            with pytest.raises(Exception):
                # 握手被拒 → 不 attach → 走 spawn(fake vivado 不存在 → 抛错)
                await sess.start(timeout=3.0)
            assert sess.attached_external is False, (
                "token 未反射的 listener 绝不能被 attach"
            )
        finally:
            stop.set()


# --------------------------------------------------------------------------- #
#  start() 超时清理 spawn 的进程(孤儿修复)
# --------------------------------------------------------------------------- #


class TestStartTimeoutCleansSpawnedProc:
    async def test_start_timeout_kills_spawned_proc(self, monkeypatch):
        """超时抛错前必须清理自己 spawn 的 Vivado(否则成无人管的孤儿)。"""
        import subprocess as subprocess_mod
        import sys

        recorded: dict[str, object] = {"taskkill": [], "killed": False}

        async def fake_exec(*args, **kwargs):
            class _FakeProc:
                pid = 24680
                returncode = None

                async def wait(self):
                    return 0

                def kill(self):
                    recorded["killed"] = True

            return _FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        def fake_run(cmd, *a, **kw):
            recorded["taskkill"].append(cmd)
            return None

        monkeypatch.setattr(subprocess_mod, "run", fake_run)

        sess = GuiSession(
            vivado_path="/fake/vivado",
            session_id="orphan-cleanup",
            port=_find_unused_port(),
            attach_only=False,
        )
        with pytest.raises(RuntimeError, match="超时"):
            await sess.start(timeout=1.0)

        if sys.platform == "win32":
            assert recorded["taskkill"], "Windows 超时清理应调用 taskkill"
            cmd = recorded["taskkill"][0]
            assert "24680" in cmd, "taskkill 必须用 spawn 时记录的 pid"
            assert "/T" in cmd, "必须 /T 递归杀进程树"
        else:
            assert recorded["killed"] is True, "Unix 超时清理应调用 proc.kill"

    async def test_cleanup_noop_without_spawned_proc(self, monkeypatch):
        """反例:没 spawn 进程(attach 路径)时清理是 no-op,绝不误杀。"""
        import subprocess as subprocess_mod

        def fail_run(*a, **kw):
            raise AssertionError("不该调用 taskkill")

        monkeypatch.setattr(subprocess_mod, "run", fail_run)

        sess = GuiSession(
            vivado_path="/fake/vivado",
            session_id="noop-cleanup",
            port=12345,
            attach_only=True,
        )
        # _proc is None → 直接返回,不抛不杀
        await sess._cleanup_failed_spawn()


# --------------------------------------------------------------------------- #
#  execute 超时:带 message 的异常 + _pending_response 标志
#  (原 liveness_probe 及其测试已删:生产零调用的孤儿,A1 实际由
#   probe_vmcp_server 的 fresh-connection 路径承担,见 session_manager)
# --------------------------------------------------------------------------- #


def _serve_header_then_stall() -> tuple[int, threading.Event]:
    """收到请求后只回 4 字节长度头、永不发 body:触发响应体读取超时。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(8)
    listener.settimeout(0.2)
    stop = threading.Event()

    def _serve() -> None:
        import time as time_mod

        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(0.3)
                hdr = b""
                while len(hdr) < 4 and not stop.is_set():
                    try:
                        hdr += conn.recv(4 - len(hdr))
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                if len(hdr) == 4:
                    n = int.from_bytes(hdr, "big")
                    got = 0
                    while got < n and not stop.is_set():
                        try:
                            got += len(conn.recv(n - got))
                        except (socket.timeout, OSError):
                            break
                    # 只发"声称 64 字节 body"的长度头,body 永不发
                    try:
                        conn.sendall((64).to_bytes(4, "big"))
                    except OSError:
                        pass
                    while not stop.is_set():
                        time_mod.sleep(0.05)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            listener.close()
        except Exception:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    return port, stop


async def _manually_connected_session(port: int) -> GuiSession:
    """绕开 start()(会触发 A2 横幅查询),直接手工建主连接。"""
    import time as time_mod

    from vivado_mcp.vivado.base_session import SessionState

    sess = GuiSession(
        vivado_path="/fake/vivado",
        session_id="manual-conn",
        port=port,
        attach_only=True,
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    sess._reader = reader
    sess._writer = writer
    sess._connected_port = port
    sess._state = SessionState.READY
    sess._start_time = time_mod.time()
    return sess


class TestExecuteTimeoutPendingResponse:
    async def test_body_read_timeout_has_message_and_sets_pending(self):
        """响应体读取超时:异常带超时秒数+命令文本(非裸 TimeoutError),
        并置 _pending_response=True(审计 P3 + P1)。
        """
        port, stop = _serve_header_then_stall()
        try:
            sess = await _manually_connected_session(port)
            assert sess._pending_response is False
            with pytest.raises(asyncio.TimeoutError) as ei:
                await sess.execute("puts BODY_STALL_CMD", timeout=0.3)
            msg = str(ei.value)
            assert "读取响应体超时" in msg, "裸 TimeoutError 缺具体原因"
            assert "0.3" in msg, "必须含超时秒数"
            assert "BODY_STALL_CMD" in msg, "必须含命令文本"
            assert sess._pending_response is True
        finally:
            stop.set()

    async def test_header_timeout_sets_pending_then_success_clears(self):
        """头部读超时置 _pending_response=True;下次成功收满一帧清 False。"""
        import time as time_mod

        state = {"stall_next": True}

        def handler(req: bytes) -> bytes:
            if state["stall_next"]:
                state["stall_next"] = False
                time_mod.sleep(0.8)  # 让第一条命令超时(响应迟到残留在流上)
            return _vmcp_handler(req)

        port, stop, _ = _serve_length_prefix(handler)
        try:
            sess = await _manually_connected_session(port)
            with pytest.raises(asyncio.TimeoutError, match="读取响应长度头超时"):
                await sess.execute("puts FIRST", timeout=0.2)
            assert sess._pending_response is True, "超时后必须标记 pending"

            # 第二条命令成功收到一帧(即便是迟到的上一帧)→ pending 清除
            await sess.execute("puts SECOND", timeout=5.0)
            assert sess._pending_response is False, "成功收帧后必须清除 pending"
            await sess.stop()
        finally:
            stop.set()


# --------------------------------------------------------------------------- #
#  list_sessions 对已管理会话探活(PRD A1)
# --------------------------------------------------------------------------- #


def _serve_silent_listener() -> tuple[int, socket.socket]:
    """监听但从不 accept/响应的端口:连接进 backlog,模拟"socket 通但挂死"。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    return listener.getsockname()[1], listener


class _FakeKnownSession:
    """伪装成已被 MCP 管理的 GUI 会话,只提供 list_sessions 用的 status_dict。"""

    def __init__(self, port: int, state: str = "ready"):
        self._port = port
        self._fake_state = state
        self._pending_response = False

    @property
    def is_alive(self):
        return True

    def status_dict(self):
        return {
            "session_id": "known",
            "mode": "gui",
            "state": self._fake_state,
            "vivado_path": "/fake/vivado",
            "is_alive": True,
            "uptime_seconds": 1.0,
            "port": self._port,
        }


class TestListSessionsLivenessProbe:
    async def test_marks_stalled_known_session_unresponsive(
        self, session_manager: SessionManager, monkeypatch
    ):
        """验收(PRD A1):socket 通但 server 不响应 → responsive=False +
        中性 note。is_alive 不翻转(未响应 ≠ 已死,审计 P1)。
        """
        port, listener = _serve_silent_listener()
        try:
            session_manager._sessions["known"] = _FakeKnownSession(port)
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", ())
            monkeypatch.setattr(sm_module, "_KNOWN_PROBE_TIMEOUT", 0.3)

            entries = await session_manager.list_sessions()
            assert len(entries) == 1
            entry = entries[0]
            assert entry["responsive"] is False
            assert entry["is_alive"] is True, "探活失败不得翻转 is_alive"
            assert "探活失败" in entry["note"]
            assert "未在" in entry["note"] and "内响应" in entry["note"]
        finally:
            listener.close()

    async def test_stalled_note_has_no_unconditional_stop_advice(
        self, session_manager: SessionManager, monkeypatch
    ):
        """探活失败的 note 必须中性:不得无条件建议 stop_session(审计 P1:
        「忙=死」误判会引导 AI 杀掉正在跑长命令的健康会话)。
        """
        port, listener = _serve_silent_listener()
        try:
            session_manager._sessions["known"] = _FakeKnownSession(port)
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", ())
            monkeypatch.setattr(sm_module, "_KNOWN_PROBE_TIMEOUT", 0.3)

            entries = await session_manager.list_sessions()
            note = entries[0]["note"]
            assert "建议 stop_session" not in note, "不得无条件建议 stop"
            assert "勿立即 stop_session" in note
            assert "已超时仍在跑" in note, "必须提示超时命令仍在执行的可能"
        finally:
            listener.close()

    async def test_keeps_alive_with_responsive_server(
        self, session_manager: SessionManager, monkeypatch
    ):
        """正例:server 正常反射 token → is_alive 保持 True,不加 note。"""
        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            session_manager._sessions["known"] = _FakeKnownSession(port)
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", ())
            monkeypatch.setattr(sm_module, "_KNOWN_PROBE_TIMEOUT", 0.5)

            entries = await session_manager.list_sessions()
            assert len(entries) == 1
            assert entries[0]["is_alive"] is True
            assert "note" not in entries[0]
            assert "responsive" not in entries[0]
        finally:
            stop.set()

    async def test_skips_probe_for_busy_session(
        self, session_manager: SessionManager, monkeypatch
    ):
        """BUSY(命令执行中,Tcl event loop 被占)不探活,避免误报死亡。"""
        port, listener = _serve_silent_listener()
        try:
            session_manager._sessions["known"] = _FakeKnownSession(
                port, state="busy"
            )
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", ())
            monkeypatch.setattr(sm_module, "_KNOWN_PROBE_TIMEOUT", 0.3)

            entries = await session_manager.list_sessions()
            assert entries[0]["is_alive"] is True
            assert "note" not in entries[0]
        finally:
            listener.close()

    async def test_skips_probe_for_pending_response_session(
        self, session_manager: SessionManager, monkeypatch
    ):
        """上一条命令超时但仍在跑(_pending_response=True)→ 跳过探活。

        run_tcl 超时后 _HINT_TIMEOUT 已告诉 AI「等它完成」;此窗口 Vivado
        event loop 被占,1s 探测必然落空,若再标 unresponsive 就和 hint
        自相矛盾、诱导 AI 杀健康长任务(审计 P1)。
        """
        port, listener = _serve_silent_listener()
        try:
            fake = _FakeKnownSession(port)
            fake._pending_response = True
            session_manager._sessions["known"] = fake
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", ())
            monkeypatch.setattr(sm_module, "_KNOWN_PROBE_TIMEOUT", 0.3)

            entries = await session_manager.list_sessions()
            assert entries[0]["is_alive"] is True
            assert "note" not in entries[0], "pending 窗口不得探活/加 note"
            assert "responsive" not in entries[0]
        finally:
            listener.close()

    async def test_probe_external_false_skips_liveness_probe(
        self, session_manager: SessionManager, monkeypatch
    ):
        """probe_external=False 同时关掉已管理会话的探活(无网络模式)。"""
        port, listener = _serve_silent_listener()
        try:
            session_manager._sessions["known"] = _FakeKnownSession(port)
            monkeypatch.setattr(sm_module, "_KNOWN_PROBE_TIMEOUT", 0.3)

            entries = await session_manager.list_sessions(probe_external=False)
            assert entries[0]["is_alive"] is True
        finally:
            listener.close()


# --------------------------------------------------------------------------- #
#  spawn 中端口不被误报为 external(pending 集合)
# --------------------------------------------------------------------------- #


class TestPendingSpawnPorts:
    async def test_pending_port_not_reported_as_external(
        self, session_manager: SessionManager, monkeypatch
    ):
        """正在 spawn 的端口(已在 pending 集合)不该被 list_sessions 列为 external。"""
        from vivado_mcp.vivado import gui_session as gs_module

        port, stop, _ = _serve_length_prefix(_vmcp_handler)
        try:
            monkeypatch.setattr(sm_module, "_EXTERNAL_PROBE_PORTS", (port,))
            gs_module._PENDING_SPAWN_PORTS.add(port)
            try:
                entries = await session_manager.list_sessions()
                externals = [e for e in entries if e.get("owner") == "external"]
                assert externals == [], "spawn 中的自己人不该被报成 external"
            finally:
                gs_module._PENDING_SPAWN_PORTS.discard(port)

            # 反例:移出 pending 后,同一端口应正常被发现为 external
            entries = await session_manager.list_sessions()
            externals = [e for e in entries if e.get("owner") == "external"]
            assert len(externals) == 1
        finally:
            stop.set()

    async def test_spawn_registers_then_clears_pending_port(self, monkeypatch):
        """spawn 连接循环期间端口在 pending 集合里;start 退出(含失败)后移除。"""
        import subprocess as subprocess_mod

        from vivado_mcp.vivado import gui_session as gs_module

        port = _find_unused_port()
        observed: dict[str, bool] = {}

        async def fake_exec(*args, **kwargs):
            class _FakeProc:
                pid = 555
                returncode = None

                async def wait(self):
                    return 0

                def kill(self):
                    pass

            return _FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setattr(subprocess_mod, "run", lambda *a, **kw: None)

        orig_sleep = asyncio.sleep

        async def spy_sleep(t):
            observed["pending_during"] = port in gs_module._PENDING_SPAWN_PORTS
            await orig_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", spy_sleep)

        sess = GuiSession(
            vivado_path="/fake/vivado",
            session_id="pending-port",
            port=port,
            attach_only=False,
        )
        with pytest.raises(RuntimeError):
            await sess.start(timeout=0.5)

        assert observed.get("pending_during") is True, "连接循环期间端口应在 pending 集合"
        assert port not in gs_module._PENDING_SPAWN_PORTS, "失败路径也必须移除 pending 端口"


# --------------------------------------------------------------------------- #
#  start() 横幅项目提示(PRD A2)
# --------------------------------------------------------------------------- #


def _make_curproj_handler(project_name: str) -> Callable[[bytes], bytes]:
    """握手反射 token;current_project 查询回指定项目名。"""

    def handler(req: bytes) -> bytes:
        text = req.decode("utf-8", errors="replace")
        if "VMCP_CURPROJ" in text:
            out = f"VMCP_CURPROJ:{project_name}\n"
        else:
            out = text  # 反射:握手 token 校验需要
        return json.dumps({"rc": 0, "output": out}, ensure_ascii=False).encode("utf-8")

    return handler


class TestStartBannerProjectHint:
    async def test_hint_appended_for_new_project(self):
        """current_project='New Project' → 横幅追加 open_project 提示。"""
        port, stop, _ = _serve_length_prefix(_make_curproj_handler("New Project"))
        try:
            sess = GuiSession(
                vivado_path="/fake/vivado",
                session_id="hint-new",
                port=port,
                attach_only=False,
            )
            banner = await sess.start(timeout=5.0)
            assert "提示: 当前 project=New Project" in banner
            assert "close_project -quiet" in banner
            assert "open_project" in banner
            await sess.stop()
        finally:
            stop.set()

    async def test_hint_appended_for_empty_project(self):
        """current_project 为空(无项目)→ 横幅追加提示,显示 <无>。"""
        port, stop, _ = _serve_length_prefix(_make_curproj_handler(""))
        try:
            sess = GuiSession(
                vivado_path="/fake/vivado",
                session_id="hint-empty",
                port=port,
                attach_only=False,
            )
            banner = await sess.start(timeout=5.0)
            assert "提示: 当前 project=<无>" in banner
            assert "close_project -quiet" in banner
            await sess.stop()
        finally:
            stop.set()

    async def test_no_hint_for_real_project(self):
        """反例:已打开真实项目 → 不加提示。"""
        port, stop, _ = _serve_length_prefix(_make_curproj_handler("my_proj"))
        try:
            sess = GuiSession(
                vivado_path="/fake/vivado",
                session_id="hint-real",
                port=port,
                attach_only=False,
            )
            banner = await sess.start(timeout=5.0)
            assert "提示: 当前 project=" not in banner
            await sess.stop()
        finally:
            stop.set()

    async def test_query_failure_degrades_to_no_hint(self, caplog):
        """查询失败(独立短连接被断开)→ 横幅照常返回(无提示),
        warning 记录具体原因。主连接不受任何影响(走 fresh connection)。
        """
        import logging

        def handler(req: bytes) -> bytes | None:
            text = req.decode("utf-8", errors="replace")
            if "VMCP_CURPROJ" in text:
                return None  # 主动断开,模拟查询失败
            return _vmcp_handler(req)

        port, stop, _ = _serve_length_prefix(handler)
        try:
            sess = GuiSession(
                vivado_path="/fake/vivado",
                session_id="hint-degraded",
                port=port,
                attach_only=False,
            )
            with caplog.at_level(
                logging.WARNING, logger="vivado_mcp.vivado.gui_session"
            ):
                banner = await sess.start(timeout=5.0)
            assert "GUI 会话就绪" in banner, "查询失败不能让 start 整体失败"
            assert "提示: 当前 project=" not in banner
            assert "current_project" in caplog.text, "降级必须记录具体原因"

            # 查询失败后主连接必须依旧可用且响应配对正确
            result = await sess.execute("puts AFTER_DEGRADE", timeout=5.0)
            assert "AFTER_DEGRADE" in result.output
            await sess.stop()
        finally:
            stop.set()

    async def test_curproj_timeout_keeps_main_connection_clean(
        self, monkeypatch
    ):
        """P1 回归:current_project 查询超时绝不能污染主连接。

        旧实现在主连接上 execute 查询,超时后迟到的 VMCP_CURPROJ 响应残留
        在流上,后续所有 execute 的结果永久错位一格(fake server 实测复现)。
        新实现走一次性独立短连接:查询超时只意味着 banner 无提示,第一条
        用户命令必须拿到自己的响应。
        """
        import time as time_mod

        from vivado_mcp.vivado import gui_session as gs_module

        def handler(req: bytes) -> bytes:
            text = req.decode("utf-8", errors="replace")
            if "VMCP_CURPROJ" in text:
                # 独立连接上迟到的响应:超过查询超时才回包
                time_mod.sleep(1.0)
                return json.dumps(
                    {"rc": 0, "output": "VMCP_CURPROJ:late_project\n"}
                ).encode("utf-8")
            return _vmcp_handler(req)

        port, stop, _ = _serve_length_prefix(handler)
        try:
            monkeypatch.setattr(gs_module, "_CURPROJ_TIMEOUT", 0.2)
            sess = GuiSession(
                vivado_path="/fake/vivado",
                session_id="curproj-timeout",
                port=port,
                attach_only=False,
            )
            banner = await sess.start(timeout=5.0)
            assert "GUI 会话就绪" in banner, "查询超时不能让 start 整体失败"
            assert "提示: 当前 project=" not in banner

            # 核心断言:第一条用户命令拿到自己的响应,不是迟到的 CURPROJ
            result = await sess.execute("puts USER_COMMAND_1", timeout=5.0)
            assert "USER_COMMAND_1" in result.output
            assert "late_project" not in result.output, (
                "主连接被 A2 查询的迟到响应污染 —— 错位回归!"
            )
            await sess.stop()
        finally:
            stop.set()
