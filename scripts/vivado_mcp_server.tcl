## vivado-mcp TCL server（注入到 Vivado 的 init.tcl 中运行）
##
## 协议（length-prefix framing，不用 sentinel，简洁可靠）：
##   请求:  [4 字节 big-endian 长度][UTF-8 编码的 Tcl 命令]
##   响应:  [4 字节 big-endian 长度][UTF-8 编码的 JSON：{"rc":<int>,"output":"<string>"}]
##
## 端口策略（B 方案 / 端口精确化）：
##   - 若 Python 注入了 `set ::VMCP_PORT_PREF <port>`，绑这个**确切端口**，绑不上就退出
##     （绝不静默滑到别人的端口 → 杜绝"新 Vivado 监听在没人连的端口=孤儿"）。
##     Python 端在 spawn 前已用 socket.bind(("",0)) 抢一个空闲端口号再释放注入进来。
##   - 若 VMCP_PORT_PREF 未定义（老 install 的 init.tcl 手动启动 / launch_runs 子进程），
##     fallback DEFAULT_PORT=9999 单端口绑 —— 保证外部 attach / list_sessions probe 不被破坏。
## 端口占用守卫：catch socket 失败后静默退出（launch_runs 子进程用同一 init.tcl 不会冲突）
## Tcl 8.5 兼容（Vivado 2019.1 使用 Tcl 8.5）

namespace eval ::vmcp {
    # 默认端口；可被注入前的 `set ::VMCP_PORT_PREF <port>` 覆盖为确切目标端口
    variable DEFAULT_PORT 9999
    variable active_port 0
    variable server_sock {}

    # 每个客户端连接的状态
    variable client_state
    array set client_state {}
}

## JSON 字符串转义：只处理 {"rc", "output"} 两字段够用
## Tcl 8.5 没有内置 JSON，手写简单编码
## JSON 标准要求 U+0000..U+001F 控制字符**全部**转义：Vivado 输出可能混入
## ESC(0x1B 等 ANSI 序列)/VT 等裸控制字符，漏转会让 Python 端 json.loads
## 直接抛 "Invalid control character" 丢掉整条命令结果。
## 映射表在脚本加载时生成一次（string map 单遍替换，不会二次转义）。
namespace eval ::vmcp {
    variable JSON_ESC_MAP [list \
        "\\" "\\\\" \
        "\"" "\\\"" \
        "\n" "\\n" \
        "\r" "\\r" \
        "\t" "\\t" \
        "\b" "\\b" \
        "\f" "\\f" \
    ]
    # 其余 0x00-0x1F 一律 \u00XX；0x08-0x0A / 0x0C-0x0D 已有可读映射，跳过
    for {set __i 0} {$__i < 32} {incr __i} {
        if {$__i == 8 || $__i == 9 || $__i == 10 || $__i == 12 || $__i == 13} {
            continue
        }
        lappend JSON_ESC_MAP [format %c $__i] [format "\\u%04x" $__i]
    }
    unset __i
}

proc ::vmcp::json_escape {s} {
    variable JSON_ESC_MAP
    return [string map $JSON_ESC_MAP $s]
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

## 绑**确切端口**启动 server，绑不上就退出（B 方案 / 端口精确化）。
## 端口来自 ::VMCP_PORT_PREF（Python spawn 前注入的确切空闲端口）；
## 未注入时 fallback DEFAULT_PORT=9999 单端口绑（老 install / launch_runs 子进程）。
## 不再做"池滑动"——占用即退，绝不静默滑到别人的端口造成孤儿。
proc ::vmcp::start {} {
    variable DEFAULT_PORT
    variable active_port
    variable server_sock

    set port $DEFAULT_PORT
    if {[info exists ::VMCP_PORT_PREF]} {
        set port $::VMCP_PORT_PREF
    }

    ## 重入守卫:已 install 的机器上 Vivado_init.tcl 先 source 本脚本绑 9999,
    ## Python spawn 的 -source 临时脚本随后再次 source → start 二次执行。
    ## 不关旧 socket 就会一个进程同时监听两个端口:默认 9999 的 probe/attach
    ## 会串到这台本应独立的实例上(0.3.22 审计 docs P1 根因)。先关旧再绑新。
    ## 运行期 puts 保持纯 ASCII:Vivado 2019.1 按系统编码 source 本脚本,
    ## 中文输出在 cp936/cp1252 控制台会乱码(本文件注释不输出,不受影响)
    if {$server_sock ne ""} {
        puts "vivado-mcp: rebind: closing old port $active_port, rebinding to $port"
        catch {close $server_sock}
        set server_sock {}
        set active_port 0
    }

    # -myaddr 127.0.0.1:只绑回环。Tcl socket -server 默认绑 0.0.0.0(全部接口),
    # 该通道执行任意 Tcl 且无鉴权,绑全接口 = 把任意代码执行开放给局域网。
    # Python 客户端全程只连 127.0.0.1,绑回环零功能损失。
    if {[catch {socket -server ::vmcp::on_accept -myaddr 127.0.0.1 $port} sock] == 0} {
        set server_sock $sock
        set active_port $port
        puts "vivado-mcp server ready on port $port"
        return $port
    }
    # 端口被占用：静默退出（launch_runs 子进程场景），绝不滑到别人端口
    puts "vivado-mcp: port $port busy, exiting (not sliding to another port)"
    return 0
}

## 启动
::vmcp::start

## Vivado GUI 主事件循环自动驱动 fileevent，无需 vwait
