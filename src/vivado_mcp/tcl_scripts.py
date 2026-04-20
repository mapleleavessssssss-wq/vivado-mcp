"""诊断用 Tcl 脚本模板。

所有 Tcl 脚本要求：
- Tcl 8.5 兼容（Vivado 2019.1）
- 输出带 VMCP_ 前缀的结构化标记，便于 Python 解析
- 使用 string match 而非高级正则（性能 + 兼容性）
- Python 的 {run_name} / {impl_run} 占位符由 .format() 填充
  （注意：Tcl 花括号在 Python f-string 中需要双写 {{ }}）
"""

# --------------------------------------------------------------------------- #
#  轻量计数：统计 runme.log 中 error / critical_warning / warning 数量
#  用于 _launch_and_wait 后快速诊断（<2s）
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
#  通用:列出项目 constrs_1 下所有 XDC 文件路径
#  诊断工具多处共用,抽出避免重复
# --------------------------------------------------------------------------- #

LIST_PROJECT_XDC_FILES = (
    'foreach __f [get_files -of_objects [get_filesets constrs_1] '
    '-filter {FILE_TYPE == XDC}] { puts "VMCP_XDC_FILE:$__f" }'
)


COUNT_WARNINGS = """\
set __run_dir [get_property DIRECTORY [get_runs {run_name}]]
set __log "$__run_dir/runme.log"
if {{[file exists $__log]}} {{
    set __fp [open $__log r]
    set __err 0
    set __cw 0
    set __w 0
    while {{[gets $__fp __line] >= 0}} {{
        if {{[string match "CRITICAL WARNING:*" $__line]}} {{
            incr __cw
        }} elseif {{[string match "ERROR:*" $__line]}} {{
            incr __err
        }} elseif {{[string match "WARNING:*" $__line]}} {{
            incr __w
        }}
    }}
    close $__fp
    puts "VMCP_DIAG:errors=$__err,critical_warnings=$__cw,warnings=$__w"
}} else {{
    puts "VMCP_DIAG:errors=-1,critical_warnings=-1,warnings=-1"
}}
"""

# --------------------------------------------------------------------------- #
#  提取 CRITICAL WARNING 详情：逐行扫描 runme.log，只输出 CW 行
#  格式：VMCP_CW:行号|原始文本
# --------------------------------------------------------------------------- #

EXTRACT_CRITICAL_WARNINGS = """\
set __run_dir [get_property DIRECTORY [get_runs {run_name}]]
set __log "$__run_dir/runme.log"
if {{[file exists $__log]}} {{
    set __fp [open $__log r]
    set __ln 0
    while {{[gets $__fp __line] >= 0}} {{
        incr __ln
        if {{[string match "CRITICAL WARNING:*" $__line]}} {{
            puts "VMCP_CW:$__ln|$__line"
        }}
    }}
    close $__fp
    puts "VMCP_CW_DONE"
}} else {{
    puts "VMCP_CW_ERROR:runme.log not found at $__log"
}}
"""

# --------------------------------------------------------------------------- #
#  提取 ERROR 详情：逐行扫描 runme.log，只输出 ERROR: 前缀行
#  格式：VMCP_ERR:行号|原始文本
#  用途：补齐 get_critical_warnings 的严重级别盲区（ERROR > CW）
# --------------------------------------------------------------------------- #

EXTRACT_ERRORS = """\
set __run_dir [get_property DIRECTORY [get_runs {run_name}]]
set __log "$__run_dir/runme.log"
if {{[file exists $__log]}} {{
    set __fp [open $__log r]
    set __ln 0
    while {{[gets $__fp __line] >= 0}} {{
        incr __ln
        if {{[string match "ERROR:*" $__line]}} {{
            puts "VMCP_ERR:$__ln|$__line"
        }}
    }}
    close $__fp
    puts "VMCP_ERR_DONE"
}} else {{
    puts "VMCP_ERR_ERROR:runme.log not found at $__log"
}}
"""

# --------------------------------------------------------------------------- #
#  提取 XDC 中的 PACKAGE_PIN 约束
#  自动读取项目 constrs_1 中所有 XDC 文件，过滤 PACKAGE_PIN 行
#  格式：VMCP_XDC_PIN:文件路径|行号|引脚|端口
# --------------------------------------------------------------------------- #

EXTRACT_XDC_PACKAGE_PINS = """\
set __xdc_files [get_files -of_objects [get_filesets constrs_1] -filter {{FILE_TYPE == XDC}}]
foreach __xf $__xdc_files {{
    set __fp [open $__xf r]
    set __ln 0
    while {{[gets $__fp __line] >= 0}} {{
        incr __ln
        set __re {{set_property\\s+PACKAGE_PIN\\s+(\\S+)\\s+\\[get_ports\\s+(.+?)\\]}}
        if {{[regexp -nocase $__re $__line -> __pin __port]}} {{
            set __port [string trim $__port "{{ }}"]
            puts "VMCP_XDC_PIN:$__xf|$__ln|$__pin|$__port"
        }}
    }}
    close $__fp
}}
puts "VMCP_XDC_PIN_DONE"
"""

# --------------------------------------------------------------------------- #
#  Bitstream 前置检查：查询实现状态 + CRITICAL WARNING 计数 + 前 10 条样本
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
#  查询 IP 实例的所有 CONFIG.* 参数
#  格式：VMCP_IP_INFO:vlnv / VMCP_IP_PARAM:name|value / VMCP_IP_PARAM_DONE
# --------------------------------------------------------------------------- #

INSPECT_IP_PARAMS = """\
set __ip [get_ips {ip_name}]
if {{$__ip eq ""}} {{
    puts "VMCP_IP_PARAM_ERROR:IP '{ip_name}' not found"
}} else {{
    set __vlnv [get_property VLNV $__ip]
    puts "VMCP_IP_INFO:$__vlnv"
    set __props [list_property $__ip]
    foreach __p $__props {{
        if {{[string match "CONFIG.*" $__p]}} {{
            set __val [get_property $__p $__ip]
            puts "VMCP_IP_PARAM:$__p|$__val"
        }}
    }}
    puts "VMCP_IP_PARAM_DONE"
}}
"""

# --------------------------------------------------------------------------- #
#  查询当前设计的阶段来源(post-synth / post-place / post-route)
#  用途:get_timing_report 报告头部元信息,区分估算时序 vs 最终时序
#  格式:VMCP_STAGE:stage=<stage>|synth_status=<s>|impl_status=<i>
# --------------------------------------------------------------------------- #

# 注意: 下面两个脚本没有 Python 占位符,直接用单花括号 `{` `}`(不走 .format())。
# 其他脚本(如 EXTRACT_ERRORS)因为要通过 .format(run_name=...) 传参,所以必须用双花括号转义。
QUERY_DESIGN_STAGE = """\
set __synth_status ""
set __impl_status ""
set __stage "unknown"
# 查询 synth_1 / impl_1 状态(优先考虑 current_project 里的 active run)
if {[llength [get_runs -quiet synth_1]] > 0} {
    set __synth_status [get_property STATUS [get_runs synth_1]]
}
if {[llength [get_runs -quiet impl_1]] > 0} {
    set __impl_status [get_property STATUS [get_runs impl_1]]
}
# 判断 current_design 处于哪个阶段
# Vivado STATUS 典型值:
#   "Not started"
#   "synth_design Complete!"
#   "place_design Complete!" / "place_design ERROR"
#   "route_design Complete!" / "route_design ERROR"
#   "write_bitstream Complete!"
if {[string match "*route_design Complete*" $__impl_status] ||
     [string match "*write_bitstream*" $__impl_status]} {
    set __stage "post-route"
} elseif {[string match "*place_design Complete*" $__impl_status]} {
    set __stage "post-place"
} elseif {[string match "*synth_design Complete*" $__synth_status]} {
    set __stage "post-synth"
}
puts "VMCP_STAGE:stage=$__stage|synth_status=$__synth_status|impl_status=$__impl_status"
"""

# --------------------------------------------------------------------------- #
#  一次性查询项目综合信息
#  输出多行 VMCP_PROJ:key=value,Python 侧解析
# --------------------------------------------------------------------------- #

QUERY_PROJECT_INFO = """\
# 项目基本信息
if {[catch {current_project} __proj]} {
    puts "VMCP_PROJ:error=no_project_open"
} else {
    set __name [get_property NAME [current_project]]
    set __dir  [get_property DIRECTORY [current_project]]
    set __part [get_property PART [current_project]]
    puts "VMCP_PROJ:project_name=$__name"
    puts "VMCP_PROJ:project_dir=$__dir"
    puts "VMCP_PROJ:part=$__part"

    # 顶层
    set __top [get_property TOP [current_fileset]]
    puts "VMCP_PROJ:top=$__top"

    # 源文件列表(sources_1)
    set __srcs [get_files -quiet -of_objects [get_filesets sources_1]]
    puts "VMCP_PROJ:source_count=[llength $__srcs]"
    foreach __f $__srcs {
        set __ft [get_property FILE_TYPE $__f]
        puts "VMCP_PROJ_FILE:source|$__ft|$__f"
    }

    # XDC 约束文件
    set __xdcs [get_files -quiet -of_objects [get_filesets constrs_1] \
                -filter {FILE_TYPE == XDC}]
    puts "VMCP_PROJ:xdc_count=[llength $__xdcs]"
    foreach __f $__xdcs {
        puts "VMCP_PROJ_FILE:xdc|XDC|$__f"
    }

    # IP 列表
    set __ips [get_ips -quiet]
    puts "VMCP_PROJ:ip_count=[llength $__ips]"
    foreach __ip $__ips {
        set __vlnv [get_property VLNV $__ip]
        puts "VMCP_PROJ_IP:$__ip|$__vlnv"
    }

    # Run 状态
    if {[llength [get_runs -quiet synth_1]] > 0} {
        puts "VMCP_PROJ:synth_status=[get_property STATUS [get_runs synth_1]]"
    } else {
        puts "VMCP_PROJ:synth_status=No run"
    }
    if {[llength [get_runs -quiet impl_1]] > 0} {
        puts "VMCP_PROJ:impl_status=[get_property STATUS [get_runs impl_1]]"
    } else {
        puts "VMCP_PROJ:impl_status=No run"
    }
    puts "VMCP_PROJ_DONE"
}
"""


# --------------------------------------------------------------------------- #
#  查询指定 run 的运行进度:状态 / PROGRESS / log 大小 / Phase 行 / 尾部 N 行
#  用于 get_run_progress 工具:长任务等待时看"走到哪一步"
#  格式:
#    VMCP_RUN:key=value            —— 元信息(status/progress/dir/log_*/total_lines)
#    VMCP_RUN_PHASE:lineno|text    —— Phase/Starting/Finished 关键阶段行
#    VMCP_RUN_TAIL:lineno|text     —— 日志尾部若干行
#    VMCP_RUN_DONE                 —— 结束标记
# --------------------------------------------------------------------------- #

QUERY_RUN_PROGRESS = """\
set __run [get_runs -quiet {run_name}]
if {{$__run eq ""}} {{
    puts "VMCP_RUN_ERROR:run '{run_name}' not found"
}} else {{
    set __status [get_property STATUS $__run]
    set __progress [get_property PROGRESS $__run]
    set __dir [get_property DIRECTORY $__run]
    set __log "$__dir/runme.log"
    puts "VMCP_RUN:status=$__status"
    puts "VMCP_RUN:progress=$__progress"
    puts "VMCP_RUN:dir=$__dir"
    if {{[file exists $__log]}} {{
        set __size [file size $__log]
        set __mtime [file mtime $__log]
        puts "VMCP_RUN:log_exists=1"
        puts "VMCP_RUN:log_size=$__size"
        puts "VMCP_RUN:log_mtime=$__mtime"
        set __fp [open $__log r]
        set __lines [list]
        while {{[gets $__fp __line] >= 0}} {{
            lappend __lines $__line
        }}
        close $__fp
        set __total [llength $__lines]
        puts "VMCP_RUN:total_lines=$__total"
        # 扫描 Phase / Starting / Finished / Running 关键阶段行
        set __ln 0
        foreach __line $__lines {{
            incr __ln
            if {{[regexp {{^Phase [0-9]+}} $__line]
              || [regexp {{^Starting }} $__line]
              || [regexp {{^Finished }} $__line]
              || [regexp {{^Running }} $__line]}} {{
                puts "VMCP_RUN_PHASE:$__ln|$__line"
            }}
        }}
        # tail 最后 {tail_n} 行
        set __start [expr {{$__total - {tail_n}}}]
        if {{$__start < 0}} {{ set __start 0 }}
        set __ln $__start
        foreach __line [lrange $__lines $__start end] {{
            incr __ln
            puts "VMCP_RUN_TAIL:$__ln|$__line"
        }}
    }} else {{
        puts "VMCP_RUN:log_exists=0"
    }}
    puts "VMCP_RUN_DONE"
}}
"""


CHECK_PRE_BITSTREAM = """\
set __impl [get_runs {impl_run}]
set __status [get_property STATUS $__impl]
set __dir [get_property DIRECTORY $__impl]
set __log "$__dir/runme.log"
set __cw 0
set __samples [list]
if {{[file exists $__log]}} {{
    set __fp [open $__log r]
    while {{[gets $__fp __line] >= 0}} {{
        if {{[string match "CRITICAL WARNING:*" $__line]}} {{
            incr __cw
            if {{$__cw <= 10}} {{
                lappend __samples $__line
            }}
        }}
    }}
    close $__fp
}}
puts "VMCP_PRE_BIT:status=$__status,critical_warnings=$__cw"
foreach __s $__samples {{
    puts "VMCP_PRE_BIT_CW:$__s"
}}
puts "VMCP_PRE_BIT_DONE"
"""
