"""有界离线 VCD 解析及查询；不执行表达式，也不判断仿真是否通过。"""

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOKEN_LENGTH = 8192
MAX_TOKENS = 2_000_000
MAX_DECLARATIONS = 10_000
MAX_WIDTH = 4096
MAX_SELECTIONS = 32
MAX_RESULTS = 1000
MAX_OUTPUT_BYTES = 512 * 1024


class VcdError(ValueError):
    """带机器可读错误码的 VCD 输入错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _ScanLimit(Exception):
    pass


@dataclass
class _Tokens:
    text: str
    count: int = 0

    def __iter__(self):
        for match in re.finditer(r"\S+", self.text):
            self.count += 1
            if self.count > MAX_TOKENS:
                raise _ScanLimit
            if match.end() - match.start() > MAX_TOKEN_LENGTH:
                raise VcdError("limit_exceeded", "VCD token 超过长度限制。")
            yield match.group()


def _required(tokens):
    try:
        return next(tokens)
    except StopIteration:
        raise VcdError("malformed_vcd", "VCD 内容意外结束。") from None


def _directive(tokens):
    parts = []
    while (token := _required(tokens)) != "$end":
        parts.append(token)
        if len(parts) > 1024:
            raise VcdError("limit_exceeded", "VCD 指令过长。")
    return parts


def _header(tokens):
    scopes, declarations, paths, widths = [], [], set(), {}
    timescale = None
    while True:
        command = _required(tokens)
        if command not in {
            "$date", "$version", "$comment", "$timescale", "$scope", "$upscope",
            "$var", "$enddefinitions",
        }:
            raise VcdError("malformed_vcd", "VCD 头部包含不支持或非法指令。")
        parts = _directive(tokens)
        if command == "$timescale":
            match = re.fullmatch(r"(1|10|100)(s|ms|us|ns|ps|fs)", "".join(parts))
            if not match or timescale is not None:
                raise VcdError("malformed_vcd", "VCD timescale 无效或重复。")
            timescale = {"magnitude": int(match[1]), "unit": match[2]}
        elif command == "$scope":
            if len(parts) != 2 or len(scopes) >= 64:
                raise VcdError("malformed_vcd", "VCD scope 无效或嵌套过深。")
            scopes.append(parts[1])
        elif command == "$upscope":
            if parts or not scopes:
                raise VcdError("malformed_vcd", "VCD upscope 不匹配。")
            scopes.pop()
        elif command == "$var":
            if len(parts) < 4:
                raise VcdError("malformed_vcd", "VCD var 声明无效。")
            kind, size, identifier, reference, *suffix = parts
            if kind not in {
                "event", "integer", "parameter", "real", "realtime", "reg", "supply0",
                "supply1", "time", "tri", "triand", "trior", "trireg", "tri0", "tri1",
                "wand", "wire", "wor", "logic", "bit", "byte", "shortint", "int",
                "longint", "shortreal", "string",
            }:
                raise VcdError("malformed_vcd", "不支持的 VCD 变量类型。")
            if not identifier.isascii() or any(not 33 <= ord(char) <= 126 for char in identifier):
                raise VcdError("malformed_vcd", "VCD identifier 必须为可打印 ASCII。")
            if not size.isascii() or not size.isdecimal() or len(size) > 5:
                raise VcdError("malformed_vcd", "VCD 信号宽度无效。")
            width = int(size)
            if not 1 <= width <= MAX_WIDTH:
                raise VcdError("limit_exceeded", "VCD 信号宽度超出限制。")
            bit_range = "".join(suffix) or None
            if bit_range and not re.fullmatch(r"\[-?\d+(?::-?\d+)?\]", bit_range):
                raise VcdError("malformed_vcd", "VCD 位范围无效。")
            if bit_range and ":" not in bit_range:
                reference += bit_range
            path = ".".join([*scopes, reference])
            if len(path) > 512 or len(declarations) >= MAX_DECLARATIONS:
                raise VcdError("limit_exceeded", "VCD 信号数量或路径长度超出限制。")
            if path in paths or (identifier in widths and widths[identifier] != width):
                raise VcdError("malformed_vcd", "VCD 路径重复或别名宽度冲突。")
            paths.add(path)
            widths[identifier] = width
            declarations.append({
                "path": path, "identifier": identifier, "width": width,
                "type": kind, "range": bit_range,
            })
        elif command == "$enddefinitions":
            if parts or scopes or not declarations:
                raise VcdError("malformed_vcd", "VCD 头部不完整或没有信号声明。")
            return declarations, widths, timescale


def _bits(value, width):
    if not isinstance(value, str) or not re.fullmatch(r"[01xXzZ]+", value):
        raise VcdError("invalid_value", "信号值必须为二进制字符串，可包含 x/z。")
    value = value.lower()
    if len(value) > width:
        raise VcdError("invalid_value", "信号值超过声明宽度。")
    return value.rjust(width, value[0] if value[0] in "xz" else "0")


def _condition(condition, selected):
    if condition is None:
        return None
    if not isinstance(condition, dict):
        raise VcdError("invalid_input", "condition 必须为结构化对象。")
    op = condition.get("op")
    if not isinstance(op, str):
        raise VcdError("invalid_input", "condition op 必须为字符串。")
    if op == "all_equals":
        if set(condition) != {"op", "values"} or not isinstance(condition["values"], dict):
            raise VcdError("invalid_input", "all_equals 需要 values 信号值映射。")
        values = condition["values"]
        if not 1 <= len(values) <= MAX_SELECTIONS:
            raise VcdError("invalid_input", "all_equals 必须包含有界且非空的条件。")
    elif op in {"equals", "change", "unknown"}:
        keys = {"op", "signal", "value"} if op == "equals" else {"op", "signal"}
        if set(condition) != keys or not isinstance(condition.get("signal"), str):
            raise VcdError("invalid_input", "condition 字段无效。")
        values = {condition["signal"]: condition.get("value")}
    else:
        raise VcdError("invalid_input", "不支持的 condition op。")
    if any(path not in selected for path in values):
        raise VcdError("invalid_input", "条件信号必须包含在 signals 中。")
    if op in {"equals", "all_equals"}:
        values = {path: _bits(value, selected[path]["width"]) for path, value in values.items()}
    return op, values


def _matches(condition, values, changed):
    op, expected = condition
    if op == "change":
        return next(iter(expected)) in changed
    if op == "unknown":
        value = values[next(iter(expected))]
        return value is not None and ("x" in value or "z" in value)
    return all(values[path] == value for path, value in expected.items())


def _validate_query(signals, start_time, end_time, max_events, condition):
    if type(start_time) is not int or not 0 <= start_time <= 2**63 - 1:
        raise VcdError("invalid_input", "start_time 必须为非负 64 位 VCD tick。")
    if end_time is not None and (
        type(end_time) is not int or not start_time <= end_time <= 2**63 - 1
    ):
        raise VcdError("invalid_input", "end_time 必须不小于 start_time。")
    if type(max_events) is not int or not 1 <= max_events <= MAX_RESULTS:
        raise VcdError("invalid_input", f"max_events 必须为 1..{MAX_RESULTS}。")
    if signals is not None and (
        not isinstance(signals, list) or len(signals) > MAX_SELECTIONS
        or any(not isinstance(path, str) or not path or len(path) > 512 for path in signals)
        or len(set(signals)) != len(signals)
    ):
        raise VcdError("invalid_input", "signals 必须为最多 32 个不重复的精确信号路径。")
    if condition is not None and not signals:
        raise VcdError("invalid_input", "condition 查询必须选择 signals。")


def query_vcd(
    file_path: str,
    signals: list[str] | None = None,
    start_time: int = 0,
    end_time: int | None = None,
    max_events: int = 100,
    condition: dict | None = None,
) -> dict:
    """查询 VCD 声明、初值及同一时刻合并后的变化；时间单位为 VCD tick。"""
    _validate_query(signals, start_time, end_time, max_events, condition)
    if not isinstance(file_path, str) or not file_path or len(file_path) > 4096:
        raise VcdError("invalid_input", "file_path 必须为非空本地路径。")
    path = Path(file_path).expanduser()
    if path.suffix.lower() != ".vcd":
        raise VcdError("unsupported_format", "仅支持 .vcd，不支持 FST/WDB 或压缩文件。")
    # 先检查大小，读取仍设置上限，避免 stat/read 之间文件增长绕过限制。
    file_stat = path.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise VcdError("invalid_input", "VCD 输入必须为普通文件。")
    if file_stat.st_size > MAX_FILE_BYTES:
        raise VcdError("file_too_large", "VCD 文件超过 32 MiB 限制。")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise VcdError("file_too_large", "VCD 文件超过 32 MiB 限制。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise VcdError("malformed_vcd", "VCD 必须为 UTF-8/ASCII 文本。") from None
    if "\x00" in text:
        raise VcdError("malformed_vcd", "VCD 文本包含 NUL 字节。")
    token_source = _Tokens(text)
    tokens = iter(token_source)
    try:
        declarations, widths, timescale = _header(tokens)
    except _ScanLimit:
        raise VcdError("limit_exceeded", "VCD 头部超过扫描限制。") from None
    by_path = {item["path"]: item for item in declarations}
    if any(name not in by_path for name in signals or []):
        raise VcdError("signal_not_found", "signals 包含不存在的路径；先执行信号发现。")
    selected = {name: by_path[name] for name in signals or []}
    # 别名共享 identifier，也共享值语义；不能用 wire 别名把瞬时 event 当持久位值。
    unsupported_ids = {
        item["identifier"] for item in declarations
        if item["type"] in {"event", "real", "realtime", "shortreal", "string"}
    }
    if any(item["identifier"] in unsupported_ids for item in selected.values()):
        raise VcdError(
            "unsupported_value_type", "查询仅支持持久数字逻辑信号，不支持 event/real/string。",
        )
    predicate = _condition(condition, selected)
    result = {
        "schema_version": 1, "success": True, "format": "vcd",
        "file_path": str(path.resolve()), "file_size_bytes": len(raw),
        "timescale": timescale, "time_unit": "vcd_ticks",
        "query": {"start_time": start_time, "end_time": end_time, "condition": condition},
        "signals": list(selected.values()) if selected else declarations[:max_events],
        "signal_count": len(declarations),
        "initial_values": {}, "events": [], "matches": [],
        "scan_truncated": False, "result_truncated": False,
        "simulation_verdict": "not_evaluated",
        "warnings": [] if timescale else ["未声明 timescale，无法转换为物理时间。"],
    }
    if not selected:
        result["result_truncated"] = len(declarations) > max_events
        result["hierarchy"] = sorted({
            item["path"].rsplit(".", 1)[0] for item in result["signals"] if "." in item["path"]
        })
    _scan(tokens, widths, selected, predicate, result, start_time, end_time, max_events)
    result["tokens_scanned"] = token_source.count
    _bound_output(result, bool(selected))
    return result


def _bound_output(result, selected):
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
        result["result_truncated"] = True
        for key in ("events", "matches", "signals" if not selected else "events"):
            if result[key]:
                result[key] = result[key][:len(result[key]) // 2]
                if key == "signals":
                    result["hierarchy"] = sorted({
                        item["path"].rsplit(".", 1)[0]
                        for item in result["signals"] if "." in item["path"]
                    })
                break
        else:
            raise VcdError("limit_exceeded", "初值或元数据超过输出字节限制。")


def _scan(tokens, widths, selected, predicate, result, start, end, limit):
    values = {name: None for name in selected}
    identifiers = {}
    for name, item in selected.items():
        identifiers.setdefault(item["identifier"], []).append(name)
    pending = {}
    current_time = 0
    initial_set = False
    data_seen = False
    block_open = False
    output_size = 0
    last_complete_time = None

    def append(bucket, item):
        nonlocal output_size
        # 值仅为有界 ASCII 位串，repr 大小可保守估算 JSON 开销。
        size = len(repr(item).encode("utf-8")) + 128
        if len(result[bucket]) >= limit or output_size + size > MAX_OUTPUT_BYTES // 2:
            result["result_truncated"] = True
        else:
            result[bucket].append(item)
            output_size += size

    def initial():
        nonlocal initial_set
        if not initial_set:
            result["initial_values"] = dict(values)
            initial_set = True
            if predicate and _matches(predicate, values, set()):
                append("matches", {"time": start, "values": dict(values)})

    def flush():
        nonlocal last_complete_time
        changed = {}
        for identifier, value in pending.items():
            for name in identifiers.get(identifier, []):
                if values[name] != value:
                    changed[name] = value
        if current_time > start:
            initial()
        values.update(changed)
        if current_time > start and (end is None or current_time <= end) and changed:
            if predicate:
                if _matches(predicate, values, changed):
                    append("matches", {"time": current_time, "values": dict(values)})
            else:
                append("events", {"time": current_time, "values": changed})
        pending.clear()
        last_complete_time = current_time

    try:
        for token in tokens:
            if token.startswith("#"):
                stamp = token[1:]
                if not stamp.isascii() or not stamp.isdecimal() or len(stamp) > 19:
                    raise VcdError("malformed_vcd", "非法 VCD 时间戳。")
                timestamp = int(stamp)
                if timestamp < current_time or timestamp > 2**63 - 1 or block_open:
                    raise VcdError("malformed_vcd", "VCD 时间倒退、溢出或 dump 块未结束。")
                if timestamp != current_time:
                    flush()
                    current_time = timestamp
            elif token in {"$dumpvars", "$dumpall", "$dumpon", "$dumpoff"}:
                if block_open:
                    raise VcdError("malformed_vcd", "VCD dump 块嵌套。")
                block_open = True
            elif token == "$end":
                if not block_open:
                    raise VcdError("malformed_vcd", "VCD 多余的 $end。")
                block_open = False
            elif token == "$comment":
                _directive(tokens)
            else:
                if token[0] in "bBrRsS":
                    identifier = _required(tokens)
                    bit_value = token[1:]
                    is_binary = token[0] in "bB"
                elif token[0] in "01xXzZ" and len(token) > 1:
                    identifier, bit_value, is_binary = token[1:], token[0], True
                else:
                    raise VcdError("malformed_vcd", "不支持或非法 VCD 值变化记录。")
                if identifier not in widths:
                    raise VcdError("malformed_vcd", "VCD 值变化使用了未声明的 identifier。")
                if is_binary:
                    bit_value = _bits(bit_value, widths[identifier])
                elif not bit_value:
                    raise VcdError("malformed_vcd", "VCD real/string 值为空。")
                elif token[0] in "rR" and not re.fullmatch(
                    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", bit_value,
                ):
                    raise VcdError("malformed_vcd", "VCD real 值无效。")
                data_seen = True
                if identifier in identifiers:
                    if not is_binary:
                        raise VcdError(
                            "unsupported_value_type", "不支持所选信号的 real/string 值。",
                        )
                    pending[identifier] = bit_value
        if block_open:
            raise VcdError("malformed_vcd", "VCD dump 块缺少 $end。")
        flush()
        if start <= current_time:
            initial()
        else:
            result["warnings"].append("start_time 超过最后记录时刻；不外推初值或条件匹配。")
    except _ScanLimit:
        # 当前时刻的值可能尚未读完，不能将 pending 当作完整采样返回。
        result["scan_truncated"] = True
        result["warnings"].append("达到扫描 token 限制；当前时刻及后续数据未纳入结果。")
    result["last_scanned_time"] = current_time
    result["last_complete_time"] = last_complete_time
    result["initial_values_complete"] = initial_set
    result["data_seen"] = data_seen
    if not data_seen:
        result["warnings"].append("没有值变化数据；不能据此判断仿真通过。")
