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
#  轮询 run 状态(D5 架构:Python 侧轮询代替 wait_on_run,避免冻住 GUI event loop)
#  占位符 {run_name}。输出单行 VMCP_POLL|<STATUS>|<PROGRESS>|<STATS.ELAPSED>
#  flow_tools._poll_run_until_done 是唯一消费方(单一来源,勿在 .py 内重写)
# --------------------------------------------------------------------------- #

POLL_RUN_STATUS = """\
set __r [get_runs {run_name}]
set __s [get_property STATUS $__r]
set __p [get_property PROGRESS $__r]
set __e [get_property STATS.ELAPSED $__r]
puts "VMCP_POLL|$__s|$__p|$__e"
"""

# --------------------------------------------------------------------------- #
#  原子启动 run：同一条 Tcl 命令内检查运行态，再 reset + launch。
#  防止两个并发 MCP 工作流在 Python 多次 execute 之间交错，重置正在运行的同名 run。
#  输出：VMCP_RUN_LAUNCH|started/busy/missing|<STATUS>
# --------------------------------------------------------------------------- #

LAUNCH_RUN_IF_IDLE = """\
set __r [get_runs -quiet {run_name}]
if {{$__r eq ""}} {{
    puts "VMCP_RUN_LAUNCH|missing|"
}} else {{
    set __s [get_property STATUS $__r]
    if {{[string match -nocase "*Running*" $__s]
         || [string match -nocase "*Queued*" $__s]}} {{
        puts "VMCP_RUN_LAUNCH|busy|$__s"
    }} else {{
        reset_run {run_name}
        launch_runs {run_name} -jobs {jobs}
        puts "VMCP_RUN_LAUNCH|started|[get_property STATUS $__r]"
    }}
}}
"""

# --------------------------------------------------------------------------- #
#  查询 sources_1 fileset 上的参数覆盖(generic / verilog_define)
#  用途(PRD B4):_launch_and_wait 启动综合/实现前读取,把 fileset 上设置的参数
#  明示进结果,防止"以为 set_property generic 生效了实际没设上"的隐性坑。
#  注意:fileset 上有值 ≠ 综合实际生效(quirks §3 类型不匹配可走错分支),
#  所以结果行附带核对 runme.log 'bound to:' 的提醒,措辞两边保持一致。
#  无 Python 占位符,直接用单花括号。
# --------------------------------------------------------------------------- #

QUERY_FILESET_OVERRIDES = """\
set __fs [get_filesets sources_1]
puts "VMCP_FS_OVERRIDE:generic=[get_property generic $__fs]"
puts "VMCP_FS_OVERRIDE:verilog_define=[get_property verilog_define $__fs]"
"""

# --------------------------------------------------------------------------- #
#  查询当前项目的 target_simulator(XSim / ModelSim / Questa / VCS / IES ...)
#  用途:_diagnose_sim_run 在日志拍空、走 XSim 专属 bat fallback 之前 guard ——
#  第三方仿真器的日志不在 */xsim/ 下,拍空是预期,误导性结论比没结论更糟。
#  无 Python 占位符,直接用单花括号。
# --------------------------------------------------------------------------- #

QUERY_TARGET_SIMULATOR = """\
if {[catch {get_property target_simulator [current_project]} __ts]} {
    puts "VMCP_TARGET_SIM:error=$__ts"
} else {
    puts "VMCP_TARGET_SIM:$__ts"
}
"""

# --------------------------------------------------------------------------- #
#  查询当前打开的 project(PRD A2:启动横幅项目提示)
#  无项目时 current_project 直接报错 → catch 住置空。输出单行 VMCP_CURPROJ:<name>
#  消费方:gui_session._query_current_project —— 走**一次性独立短连接**发送,
#  绝不在会话主连接上跑(主连接查询超时的残留响应会让后续 execute 永久错位)。
#  无 Python 占位符,直接用单花括号。
# --------------------------------------------------------------------------- #

QUERY_CURRENT_PROJECT = """\
if {[catch {current_project} __p]} { set __p "" }
puts "VMCP_CURPROJ:$__p"
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
#  格式：VMCP_RUNLOG_ERR:行号|原始文本
#  用途：补齐 get_critical_warnings 的严重级别盲区（ERROR > CW）
#
#  注意:前缀用 ``VMCP_RUNLOG_ERR:`` 而非 ``VMCP_ERR:``,避免和内部 sentinel
#  协议的 ``VMCP_ERR: $__out``(见 tcl_utils.py wrap_command)命名冲突 ——
#  SubprocessSession 会对 ``VMCP_ERR:`` 开头的行做前缀剥离,我们应用层输出
#  的 ERROR 详情会被吞掉。0.3.10 field test 发现。
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
            puts "VMCP_RUNLOG_ERR:$__ln|$__line"
        }}
    }}
    close $__fp
    puts "VMCP_RUNLOG_ERR_DONE"
}} else {{
    puts "VMCP_RUNLOG_ERR_MISSING:runme.log not found at $__log"
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
    set __props [list_property $__ip]
    if {{[lsearch -exact $__props VLNV] >= 0}} {{
        set __vlnv [get_property VLNV $__ip]
    }} elseif {{[lsearch -exact $__props IPDEF] >= 0}} {{
        set __vlnv [get_property IPDEF $__ip]
    }} else {{
        set __vlnv "<unknown>"
    }}
    puts "VMCP_IP_INFO:$__vlnv"
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

    # simulation fileset 文件(testbench)。注意:sim_1 的 get_files 会带出
    # 继承自 sources_1 的设计源,这里只算 sim_1 独有的文件(真 testbench)
    if {[llength [get_filesets -quiet sim_1]] > 0} {
        set __sim_only [list]
        foreach __f [get_files -quiet -of_objects [get_filesets sim_1]] {
            if {[lsearch -exact $__srcs $__f] == -1} {
                lappend __sim_only $__f
            }
        }
        puts "VMCP_PROJ:sim_count=[llength $__sim_only]"
        foreach __f $__sim_only {
            set __ft [get_property FILE_TYPE $__f]
            puts "VMCP_PROJ_FILE:sim|$__ft|$__f"
        }
    } else {
        puts "VMCP_PROJ:sim_count=0"
    }

    # IP 列表
    set __ips [get_ips -quiet]
    puts "VMCP_PROJ:ip_count=[llength $__ips]"
    foreach __ip $__ips {
        set __ip_props [list_property $__ip]
        if {[lsearch -exact $__ip_props VLNV] >= 0} {
            set __vlnv [get_property VLNV $__ip]
        } elseif {[lsearch -exact $__ip_props IPDEF] >= 0} {
            set __vlnv [get_property IPDEF $__ip]
        } else {
            set __vlnv "<unknown>"
        }
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


# --------------------------------------------------------------------------- #
#  查询违例路径详情 (setup top 10 + hold top 5)
#  用途: 在 report_timing_summary 显示时序 FAIL 时,追加给出哪些路径违例、可以怎么修
#
#  实现细节:
#  - 不再自己尝试判 WNS/WHS — 调用方(Python 侧 get_timing_report)发现 timing_met
#    为 False 时才跑本脚本,避免浪费 10-30s 在健康工程上
#  - 输出用 VMCP_PATH_START / VMCP_PATH_END 分隔每段 report_timing 结果,
#    Python 侧按块解析
#  - report_timing 的 -return_string 会输出一大段原始文本(和 Slack 块格式相同),
#    所以这里把类型标签在块外面打,正文透传
# --------------------------------------------------------------------------- #

REPORT_VIOLATING_PATHS = """\
# Setup top 10
puts "VMCP_PATH_START:type=setup"
if {[catch {report_timing -delay_type max -max_paths 10 -return_string} __setup_out]} {
    puts "VMCP_PATH_ERROR:setup|$__setup_out"
} else {
    puts $__setup_out
}
puts "VMCP_PATH_END:type=setup"

# Hold top 5
puts "VMCP_PATH_START:type=hold"
if {[catch {report_timing -delay_type min -max_paths 5 -return_string} __hold_out]} {
    puts "VMCP_PATH_ERROR:hold|$__hold_out"
} else {
    puts $__hold_out
}
puts "VMCP_PATH_END:type=hold"

puts "VMCP_PATH_DONE"
"""


# --------------------------------------------------------------------------- #
#  Tail runme.log 末尾 N 行 + run STATUS
#  用途:get_critical_warnings 在 errors=0 且 cw=0 但实际失败时,扫描非标错误
#  (如 TclStackFree / xvlog not in PATH 等不带 ERROR: 前缀的内部异常)
#  格式:
#    VMCP_TAIL:status=<STATUS>     —— run 的 Vivado STATUS
#    VMCP_TAIL:total=<N>           —— 日志总行数
#    VMCP_TAIL:log_missing=1       —— runme.log 不存在时
#    VMCP_TAIL:error=run_not_found —— run 不存在时
#    VMCP_TAIL_LINE:<原行号>|<文本> —— tail 出的每行
#    VMCP_TAIL_DONE                 —— 结束标记
# --------------------------------------------------------------------------- #

TAIL_RUNME_LOG = """\
set __run [get_runs -quiet {run_name}]
if {{$__run eq ""}} {{
    puts "VMCP_TAIL:error=run_not_found"
}} else {{
    set __status [get_property STATUS $__run]
    set __run_dir [get_property DIRECTORY $__run]
    set __log "$__run_dir/runme.log"
    puts "VMCP_TAIL:status=$__status"
    if {{[file exists $__log]}} {{
        set __fp [open $__log r]
        set __lines [list]
        while {{[gets $__fp __l] >= 0}} {{
            lappend __lines $__l
        }}
        close $__fp
        set __total [llength $__lines]
        puts "VMCP_TAIL:total=$__total"
        set __start [expr {{$__total - {tail_n}}}]
        if {{$__start < 0}} {{ set __start 0 }}
        set __ln $__start
        foreach __l [lrange $__lines $__start end] {{
            incr __ln
            puts "VMCP_TAIL_LINE:$__ln|$__l"
        }}
    }} else {{
        puts "VMCP_TAIL:log_missing=1"
    }}
}}
puts "VMCP_TAIL_DONE"
"""


# --------------------------------------------------------------------------- #
#  Tail simulation fileset (sim_*) 下所有 xsim 子日志
#  Vivado launch_simulation 走 .bat/.sh 子进程调 xvlog/xelab/xsim,真错误在
#  <proj>.sim/<sim_fs>/<phase>/xsim/*.log,不进 runme.log。本脚本 glob 扫
#  <sim_fs_dir>/*/xsim/*.log 并 tail 末尾 N 行。
#  格式:
#    VMCP_SIM:sim_dir=<path>       —— fileset 目录
#    VMCP_SIM:log_count=<N>        —— 命中的 log 数
#    VMCP_SIM:error=fileset_not_found / dir_missing=1
#    VMCP_SIM_LOG_START:<log_path>
#    VMCP_SIM_LOG_LINE:<原行号>|<文本>
#    VMCP_SIM_LOG_END:<log_path>
#    VMCP_SIM_DONE
# --------------------------------------------------------------------------- #

TAIL_SIM_LOGS = """\
set __sim_fs [get_filesets -quiet {run_name}]
if {{$__sim_fs eq ""}} {{
    puts "VMCP_SIM:error=fileset_not_found"
}} else {{
    # Vivado 2019.1 sim fileset 的 DIRECTORY 属性是空的,只能按 layout 约定推:
    # <proj_dir>/<proj_name>.sim/<sim_fs>。这是 Vivado 默认 layout,不可改。
    set __proj [current_project]
    set __sim_dir "[get_property DIRECTORY $__proj]/[get_property NAME $__proj].sim/$__sim_fs"
    puts "VMCP_SIM:sim_dir=$__sim_dir"
    if {{[file isdirectory $__sim_dir]}} {{
        set __logs [glob -nocomplain "$__sim_dir/*/xsim/*.log"]
        puts "VMCP_SIM:log_count=[llength $__logs]"
        foreach __log $__logs {{
            puts "VMCP_SIM_LOG_START:$__log"
            if {{[file exists $__log]}} {{
                set __fp [open $__log r]
                set __lines [list]
                while {{[gets $__fp __l] >= 0}} {{
                    lappend __lines $__l
                }}
                close $__fp
                set __total [llength $__lines]
                set __start [expr {{$__total - {tail_n}}}]
                if {{$__start < 0}} {{ set __start 0 }}
                set __ln $__start
                foreach __l [lrange $__lines $__start end] {{
                    incr __ln
                    puts "VMCP_SIM_LOG_LINE:$__ln|$__l"
                }}
            }}
            puts "VMCP_SIM_LOG_END:$__log"
        }}
    }} else {{
        puts "VMCP_SIM:dir_missing=1"
    }}
}}
puts "VMCP_SIM_DONE"
"""


# --------------------------------------------------------------------------- #
#  -scripts_only fallback:launch_simulation 在生成 .bat **之后**、spawn 之前
#  炸时,xsim/*.log 一个都不生成,TAIL_SIM_LOGS 完全拍空。本脚本:
#    1. 如 .bat 集合还没生成,触发 launch_simulation -scripts_only
#    2. glob 出 compile/elaborate/simulate 三步的 .bat / .sh 路径
#  Python 那边拿到列表后,MCP 进程自己 spawn 这几个脚本,捕获 stderr,
#  把 Vivado wrapper 吞掉的真错误暴露出来。
#  格式:
#    VMCP_BAT:sim_dir=<path>
#    VMCP_BAT:scripts_only=already_present / ok / triggering
#    VMCP_BAT:scripts_only_failed=<err>     (catch 到 launch_simulation 异常)
#    VMCP_BAT:error=fileset_not_found / dir_missing=1
#    VMCP_BAT_FILE:<step>|<ext>|<path>      (step ∈ compile/elaborate/simulate)
#    VMCP_BAT_DONE
# --------------------------------------------------------------------------- #

LAUNCH_SCRIPTS_AND_GLOB = """\
set __sim_fs [get_filesets -quiet {run_name}]
if {{$__sim_fs eq ""}} {{
    puts "VMCP_BAT:error=fileset_not_found"
}} else {{
    # 同 TAIL_SIM_LOGS:DIRECTORY 属性在 sim fileset 上空,按 layout 约定推。
    set __proj [current_project]
    set __sim_dir "[get_property DIRECTORY $__proj]/[get_property NAME $__proj].sim/$__sim_fs"
    puts "VMCP_BAT:sim_dir=$__sim_dir"
    if {{[file isdirectory $__sim_dir]}} {{
        # Vivado layout 实际是 <sim_fs>/<phase>/xsim/<step>.{{bat,sh}}(三层),
        # 跟 TAIL_SIM_LOGS 的 *.log glob 一致。0.3.16 漏了 /xsim,导致 fallback
        # 永远 glob 空,触发 -scripts_only 但拿不到 .bat,被 0.3.17 实测打出。
        set __existing [glob -nocomplain \\
            "$__sim_dir/*/xsim/compile.bat" "$__sim_dir/*/xsim/compile.sh"]
        if {{[llength $__existing] == 0}} {{
            puts "VMCP_BAT:scripts_only=triggering"
            if {{[catch {{launch_simulation -scripts_only -simset $__sim_fs}} __err]}} {{
                puts "VMCP_BAT:scripts_only_failed=$__err"
            }} else {{
                puts "VMCP_BAT:scripts_only=ok"
            }}
        }} else {{
            puts "VMCP_BAT:scripts_only=already_present"
        }}
        foreach __pat {{compile elaborate simulate}} {{
            foreach __ext {{bat sh}} {{
                set __gp "$__sim_dir/*/xsim/$__pat.$__ext"
                foreach __f [glob -nocomplain $__gp] {{
                    puts "VMCP_BAT_FILE:$__pat|$__ext|$__f"
                }}
            }}
        }}
    }} else {{
        puts "VMCP_BAT:dir_missing=1"
    }}
}}
puts "VMCP_BAT_DONE"
"""


# --------------------------------------------------------------------------- #
#  在 **Vivado session 内** exec 单步 .bat。理由:
#    - Vivado 自己 spawn .bat 时不传完整路径,Win 11 24H2 默认开启
#      NoDefaultCurrentDirectoryInExePath 导致 'compile.bat' not recognized
#    - MCP 进程的 PATH 不含 vivado/bin,Python subprocess 跑 .bat 也会 xvlog
#      not recognized,假阳性误导诊断
#    - **Vivado session 内** PATH 含 vivado/bin,exec cmd /c <完整路径> 既绕开
#      cwd-in-PATH 安全策略,又能让 .bat 内 call xvlog/xelab 找到
#  edge case:Tcl exec 在子进程写过 stderr 时即使 rc=0 也会 catch 抛,要看
#  errorcode 是不是 CHILDSTATUS 才算真失败。
#  格式:
#    VMCP_BAT_RUN:rc=<int>
#    VMCP_BAT_RUN_OUT_START
#    VMCP_BAT_RUN_LINE:<文本>     (合并 stdout+stderr,逐行)
#    VMCP_BAT_RUN_OUT_END
# --------------------------------------------------------------------------- #

RUN_BAT_STEP = """\
set __bat "{bat_path}"
set __out ""
set __rc 0
# .bat 内部 xvlog/xelab 用 **相对路径** 引 .prj / .ini(Vivado 生成时假设
# cwd=xsim_dir)。Vivado launch_simulation 自己 spawn 时会先 cd 进去,我们
# 复刻它,否则 fallback 抓到的"真错"实际是 cwd 错误诱发的假错(0.3.17 实测)。
set __orig_pwd [pwd]
set __xsim_dir [file dirname $__bat]
cd $__xsim_dir
# Windows 跑 .bat 用 cmd /c;Linux 下 glob 到的是 .sh,cmd 不存在,
# 硬编码 cmd /c 会必然 POSIX ENOENT,被误判成"子进程失败"假结论
if {{$::tcl_platform(platform) eq "windows"}} {{
    set __runner [list cmd /c $__bat]
}} else {{
    set __runner [list sh $__bat]
}}
if {{[catch {{exec {{*}}$__runner}} __out __opt]}} {{
    set __ec [dict get $__opt -errorcode]
    if {{[llength $__ec] >= 3 && [lindex $__ec 0] eq "CHILDSTATUS"}} {{
        set __rc [lindex $__ec 2]
    }} elseif {{[lindex $__ec 0] eq "NONE"}} {{
        # rc=0 但子进程写过 stderr,Tcl exec 也抛 error,errorcode=NONE
        set __rc 0
    }} else {{
        # POSIX(exec 自身 spawn 失败)/ CHILDKILLED(被杀,如杀软拦截)等:
        # 子进程没有正常退出码。此前这里被并进 rc=0,fallback 会输出
        # "全部正常退出" 的假结论,把杀软拦截误判成 wrapper 问题。
        set __rc -4
        append __out "\\n(Tcl exec errorcode: $__ec)"
    }}
}}
cd $__orig_pwd
puts "VMCP_BAT_RUN:rc=$__rc"
puts "VMCP_BAT_RUN_OUT_START"
foreach __l [split $__out "\\n"] {{
    puts "VMCP_BAT_RUN_LINE:$__l"
}}
puts "VMCP_BAT_RUN_OUT_END"
"""


# --------------------------------------------------------------------------- #
#  波形(wave)工具 Tcl 片段(0.3.2x)
# --------------------------------------------------------------------------- #

# 解析当前 wcfg 的磁盘路径。
#   - 没有当前 wave_config           → VMCP_WCFG:none
#   - 有 wave_config 但 FILE_PATH 空 → VMCP_WCFG:unsaved(从未存盘,改 XML 无目标)
#   - 有磁盘路径                     → VMCP_WCFG:path=<绝对路径>
# 无 Python 占位符,直接用单花括号。
# ★磁盘路径属性是 FILE_PATH(实测 Vivado 2019.1,list_property 全集:CLASS
#   DISPLAY_LIMIT FILE_PATH HAS_SAMPLES HAS_TIME NAME NEEDS_SAVE)。**不是**
#   FILE_NAME —— 那个属性不存在,get_property FILE_NAME 会报 [Common 17-54]。
RESOLVE_WCFG_PATH = """\
set __wc [current_wave_config]
if {[llength $__wc] == 0} {
    puts "VMCP_WCFG:none"
} else {
    set __f [get_property FILE_PATH $__wc]
    if {$__f eq ""} {
        puts "VMCP_WCFG:unsaved"
    } else {
        puts "VMCP_WCFG:path=$__f"
    }
}
"""

# 重载 wcfg:close -force 后再 open。占位符 {wcfg_path}——**必须传
# to_tcl_path() 转义后的带引号值**(裸路径含空格会被 Tcl 分词、含 $/[ 会触发
# 替换;close 已成功时 open 失败会让用户 wave_config 处于被关闭状态)。
#   - close 漏 -force → [Wavedata 42-26](内存 wave_config 脏,拒绝丢弃)
#   - 同名 wcfg 已 open 时直接 open → [Wavedata 42-52](重复打开),故必须先 close
# close_wave_config 不接受 wcfg 名参数(关的是 current),所以无名直接 close。
# OK 行不内插路径(路径含 $/[ 在双引号里会再替换一次;Python 侧本来就知道路径)。
RELOAD_WCFG = """\
if {{[catch {{close_wave_config -force}} __e]}} {{
    puts "VMCP_RELOAD_ERR:close|$__e"
}} else {{
    if {{[catch {{open_wave_config {wcfg_path}}} __e2]}} {{
        puts "VMCP_RELOAD_ERR:open|$__e2"
    }} else {{
        puts "VMCP_RELOAD_OK:done"
    }}
}}
"""

# 把一批 wave 对象设为 Analog 显示。占位符:
#   {signals_block} —— Python 生成的 `lappend __sigs {<信号>}` 行(每信号一行)
#   {amin} {amax}   —— AnalogMin / AnalogMax(数字字面量)
#   {interp}        —— AnalogInterpolation 值(LINEAR / HOLD)
#   {height}        —— set_property HEIGHT(存盘后叫 CellHeight)
#
# ★信号→wave 对象解析(实测 Vivado 2019.1,两个静默坑):
#   1. get_waves <pattern> 只匹配 DISPLAY_NAME(如 y0[15:0])/glob,**不匹配全路径**
#      /tb/y0;直接传全路径 → 返回空。故先按 DESIGN_OBJECT(=全路径)/FULL_NAME/
#      DISPLAY_NAME 精确匹配 get_waves *,再退回 get_waves glob。
#   2. set_wave_prop 对空对象 **rc=0 静默接受**(不报错!),所以必须先判空再 set,
#      否则"信号没 add"会伪装成成功。
#   wave 属性无 get 接口(get_wave_prop 不存在、report_wave_props 不可捕获),无法
#   Tcl 读回校验 WaveformStyle;工具固定写已验证的 STYLE_ANALOG 值,渲染靠人眼确认。
# 输出:
#   VMCP_ANALOG:sig=<信号>|found=0          —— 解析不到 wave 对象(信号没 add 进波形)
#   VMCP_ANALOG:sig=<信号>|set_err=<msg>    —— set_wave_prop 抛错
#   VMCP_ANALOG:sig=<信号>|ok=<resolved>    —— 设置成功,resolved 为命中的全路径
#   VMCP_ANALOG_DONE
SET_ANALOG_PROPS = """\
set __sigs [list]
{signals_block}
foreach __s $__sigs {{
    set __w ""
    foreach __cand [get_waves -quiet *] {{
        if {{$__s eq [get_property DESIGN_OBJECT $__cand]
             || $__s eq [get_property FULL_NAME $__cand]
             || $__s eq [get_property DISPLAY_NAME $__cand]}} {{
            set __w $__cand
            break
        }}
    }}
    if {{$__w eq ""}} {{
        set __g [get_waves -quiet $__s]
        if {{[llength $__g] > 0}} {{ set __w [lindex $__g 0] }}
    }}
    if {{$__w eq ""}} {{
        puts "VMCP_ANALOG:sig=$__s|found=0"
        continue
    }}
    if {{[catch {{
        set_wave_prop WaveformStyle STYLE_ANALOG $__w
        set_wave_prop AnalogMin {amin} $__w
        set_wave_prop AnalogMax {amax} $__w
        set_wave_prop AnalogInterpolation {interp} $__w
        set_property HEIGHT {height} $__w
    }} __serr]}} {{
        puts "VMCP_ANALOG:sig=$__s|set_err=$__serr"
        continue
    }}
    puts "VMCP_ANALOG:sig=$__s|ok=[get_property DESIGN_OBJECT $__w]"
}}
puts "VMCP_ANALOG_DONE"
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
