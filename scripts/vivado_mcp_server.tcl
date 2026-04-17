## vivado-mcp TCL server（注入到 Vivado 的 init.tcl 中运行）
##
## 协议（length-prefix framing，不用 sentinel，简洁可靠）：
##   请求:  [4 字节 big-endian 长度][UTF-8 编码的 Tcl 命令]
##   响应:  [4 字节 big-endian 长度][UTF-8 编码的 JSON：{"rc":<int>,"output":"<string>"}]
##
## 端口池：9999..10003（支持同一台机器多个 Vivado GUI 实例）
## 端口占用守卫：catch socket 失败后静默退出（launch_runs 子进程用同一 init.tcl 不会冲突）
## Tcl 8.5 兼容（Vivado 2019.1 使用 Tcl 8.5）

namespace eval ::vmcp {
    # 默认端口起点；可被注入前的 `set ::VMCP_PORT_PREF <port>` 覆盖
    variable DEFAULT_PORT 9999
    variable POOL_SIZE 5
    variable active_port 0
    variable server_sock {}

    # 每个客户端连接的状态
    variable client_state
    array set client_state {}
}

## JSON 字符串转义：只处理 {"rc", "output"} 两字段够用
## Tcl 8.5 没有内置 JSON，手写简单编码
proc ::vmcp::json_escape {s} {
    set result [string map [list \
        "\\" "\\\\" \
        "\"" "\\\"" \
        "\n" "\\n" \
        "\r" "\\r" \
        "\t" "\\t" \
        "\b" "\\b" \
        "\f" "\\f" \
    ] $s]
    return $result
}

## 发送响应：[4 字节长度][JSON payload]
proc ::vmcp::send_response {chan rc output} {
    set escaped [::vmcp::json_escape $output]
    set json "\{\"rc\":$rc,\"output\":\"$escaped\"\}"
    # UTF-8 编码
    set bytes [encoding convertto utf-8 $json]
    set len [string length $bytes]
    # big-endian 4 字节长度
    puts -nonewline $chan [binary format I $len]
    puts -nonewline $chan $bytes
    flush $chan
}

## 读取 4 字节长度头 + payload，返回 payload 字符串（UTF-8 解码）
## 返回空字符串表示连接关闭或出错
proc ::vmcp::read_request {chan} {
    # 阻塞读 4 字节长度
    set hdr [read $chan 4]
    if {[string length $hdr] != 4} {
        return ""
    }
    binary scan $hdr I len
    if {$len <= 0 || $len > 10485760} {
        # 非法长度或超过 10MB，视为连接异常
        return ""
    }
    # 按长度读 payload
    set payload_bytes [read $chan $len]
    if {[string length $payload_bytes] != $len} {
        return ""
    }
    return [encoding convertfrom utf-8 $payload_bytes]
}

## 捕获 puts 到 stdout 的输出：和 subprocess 模式保持协议一致
## （否则 diagnostic / poll 等依赖 puts VMCP_XXX 的内部命令在 TCP 模式下拿不到输出）
namespace eval ::vmcp {
    variable captured_buf ""
}

## puts 拦截 proc。rename 后会在 global namespace 被调用，
## 因此变量引用必须用绝对路径 ::vmcp::captured_buf（不用 variable 指令）
proc ::vmcp::captured_puts {args} {
    # puts 签名：puts ?-nonewline? ?channelId? string
    set nonewline 0
    set idx 0
    if {[lindex $args $idx] eq "-nonewline"} {
        set nonewline 1
        incr idx
    }
    set remaining [lrange $args $idx end]
    set chan "stdout"
    set text ""
    if {[llength $remaining] >= 2} {
        set chan [lindex $remaining 0]
        set text [lindex $remaining 1]
    } elseif {[llength $remaining] == 1} {
        set text [lindex $remaining 0]
    }

    # 非 stdout（stderr / 用户打开的 channel）透传原 puts
    if {$chan ne "stdout"} {
        catch {eval ::__orig_puts $args}
        return
    }

    # stdout → 捕获到全局 buffer（绝对路径引用）
    append ::vmcp::captured_buf $text
    if {!$nonewline} {
        append ::vmcp::captured_buf "\n"
    }
    # 模仿原生 puts：返回空字符串（否则会把 append 的结果当 return value 造成重复）
    return ""
}

## 执行用户 Tcl 命令并同时捕获 puts 输出
## 返回: [list $rc $merged_output]
proc ::vmcp::exec_with_capture {cmd} {
    # 用绝对路径设置，避免 namespace 陷阱
    set ::vmcp::captured_buf ""

    # 安装 puts 拦截
    rename ::puts ::__orig_puts
    rename ::vmcp::captured_puts ::puts

    set rc [catch {uplevel #0 $cmd} ret __opts]

    # 恢复原 puts
    rename ::puts ::vmcp::captured_puts
    rename ::__orig_puts ::puts

    # 合并：puts 捕获的 stdout + 命令返回值
    set merged $::vmcp::captured_buf
    if {$ret ne ""} {
        if {$merged ne "" && [string index $merged end] ne "\n"} {
            append merged "\n"
        }
        append merged $ret
    }
    return [list $rc $merged]
}

## 客户端可读事件回调
proc ::vmcp::on_readable {chan} {
    if {[eof $chan] || [catch {fblocked $chan} blocked]} {
        catch {close $chan}
        return
    }

    set cmd [::vmcp::read_request $chan]
    if {$cmd eq ""} {
        catch {close $chan}
        return
    }

    # 带 puts 捕获的 eval
    set result [::vmcp::exec_with_capture $cmd]
    set rc [lindex $result 0]
    set output [lindex $result 1]

    # 发送响应
    if {[catch {::vmcp::send_response $chan $rc $output} err]} {
        catch {close $chan}
    }
}

## 新连接到达
proc ::vmcp::on_accept {chan addr port} {
    # 切换为阻塞二进制模式，便于精确按字节读取
    fconfigure $chan -translation binary -buffering none -blocking 1
    # 但注册可读事件要非阻塞，否则 fileevent 不会触发
    # 做法：on_readable 里用阻塞 read，靠 fileevent 唤醒
    fileevent $chan readable [list ::vmcp::on_readable $chan]
}

## 尝试端口池，找到第一个可用的就启动 server
## 端口首选值来自 ::VMCP_PORT_PREF（注入脚本可覆盖），否则用 DEFAULT_PORT
## 池大小 POOL_SIZE，依次尝试 pref..pref+POOL_SIZE-1
proc ::vmcp::start {} {
    variable DEFAULT_PORT
    variable POOL_SIZE
    variable active_port
    variable server_sock

    set pref $DEFAULT_PORT
    if {[info exists ::VMCP_PORT_PREF]} {
        set pref $::VMCP_PORT_PREF
    }
    set port_max [expr {$pref + $POOL_SIZE - 1}]

    for {set p $pref} {$p <= $port_max} {incr p} {
        if {[catch {socket -server ::vmcp::on_accept $p} sock] == 0} {
            set server_sock $sock
            set active_port $p
            puts "vivado-mcp server ready on port $p"
            return $p
        }
    }
    # 端口池全部占用：静默退出（常见于 launch_runs 子进程场景）
    return 0
}

## 启动
::vmcp::start

## Vivado GUI 主事件循环自动驱动 fileevent，无需 vwait
