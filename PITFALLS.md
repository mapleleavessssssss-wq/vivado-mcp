# PITFALLS — 给 vivado-mcp 用户的操作约束

> **本文档面向用户**(用 vivado-mcp + AI 跑 Vivado 工作流的人),不是给 AI 协作者
> 看的(AI 协作约束在项目内部 spec `vivado-quirks.md`,不随仓库分发)。
>
> 这里列的是 **MCP 物理上无法替你做** 的事 —— 不是 bug,不是设计缺陷,是 Vivado
> 本身的限制或 OS 边界,需要你**手动**操作。
>
> 例外:标了 **勘误** 的条目(如 C1)是过去误判"做不到"、现已订正成"能做 + 正确
> 配方"的历史记录,保留是为了纠正还在流传的老笔记。

---

## C1. XSim Analog 波形样式 — 可纯 Tcl 渲染(早期文档误判已订正)

> **勘误**:本条早期写"只能 GUI 手点、MCP 物理上做不到",**是错的**。analog
> 纯 Tcl 完全可做,已不属于"MCP 做不到"那一类。本条保留作勘误 + 正确配方,免得
> 老笔记还在误导。

**触发**:你让 AI 把某些波形(ADC 输出、DAC 输入、滤波器响应)显示成 Analog
样式(模拟波形看趋势),而不是 Digital(0/1 阶梯)。

**真根因**:WaveformStyle 属性不在 `set_property` / `list_property` 全集里(所以
看不到、误以为无接口),要用专用命令 `set_wave_prop`;且当年值写成裸 `ANALOG`,
Vivado 收下不报错但渲染器不认,静默不渲染。**正确值必须带 `STYLE_` 前缀**:

```tcl
# ★ get_waves 按"显示名"(如 adc_out[11:0])/glob 匹配,传全路径 /tb/adc_out 返回空!
#   且 set_wave_prop 对空对象 rc=0 静默接受 → 信号没 add 会伪装成功,务必先判空。
set w [get_waves -quiet adc_out*]   ;# 用显示名/glob;或遍历 get_waves * 按 DESIGN_OBJECT 全路径过滤
if {[llength $w]} {
    set_wave_prop WaveformStyle STYLE_ANALOG $w   ;# ★ 裸 ANALOG 静默吞值不渲染
    set_wave_prop AnalogMin -2048 $w              ;# 贴数据:太宽压平,太窄削顶
    set_wave_prop AnalogMax  2047 $w
    set_wave_prop AnalogInterpolation LINEAR $w
    set_property HEIGHT 80 $w                      ;# 存为 CellHeight
}
```

**关键坑**:重载 wcfg 会冲掉 analog,**先定好 zoom 再实时上 analog**。

> **MCP 已提供 `set_wave_analog` 工具封装此配方**(STYLE_ 前缀补全 + 全路径/显示名
> 寻址 + 空对象判空),不想手拼 Tcl 直接调它;缩放窗用 `set_wave_zoom`。

---

## C2. wave window 截图 — 必须你按 Win+Shift+S 自截

**触发**:你想把 wave 波形截图发给同事 / 贴到报告 / 让 AI 看波形帮你分析。

**为什么 MCP 做不到**:
- Vivado 的 wave window 是 Qt 内嵌的图形组件,**没有 Tcl 截图接口**
- Windows 系统级截图需要前台焦点 + 用户手势(任何"AI 自动截图"都会被安全机制
  blocked 或截到别处)
- 远程 IPC 强行抓帧不可靠(分辨率 / DPI / 多显示器都翻车)

**怎么手动操作**:
1. AI 帮你把信号 add_wave 进去 + 调到合适的时间范围
2. **你按 `Win+Shift+S`**(Windows 系统截图),框选 wave window 区域
3. 截图自动到剪贴板 → 直接粘贴到 Claude Code 对话框,或保存成文件让 AI 看

**为什么不用 Snipping Tool / 第三方截图**:Win+Shift+S 是 Win10/11 内置最快的,
不依赖任何额外软件,5 秒搞定。

---

## 跟 quirks 文档的区别

| 文档 | 给谁看 | 内容性质 |
|---|---|---|
| `PITFALLS.md`(本文) | **用户** | "MCP 物理上做不到 / 你必须手动" |
| `vivado-quirks.md`(项目内部 spec,不随仓库分发) | **AI 协作者** | "AI 写脚本 / debug 时要知道的 Vivado quirks" |
| `_safe_execute` 里的 W hints | **AI 运行时** | "命令出 err 时自动追加的复制粘贴指引" |

三层互补,不重复。

---

## 新增 pitfall 的规矩

新加一条到本文件之前问自己:
1. 这是 **MCP 物理上做不到** 的吗?(物理边界 / OS 限制 / Vivado 无接口)
2. 还是 **AI 写脚本会踩** 的?(那应该进 vivado-quirks.md,不是这里)
3. 还是 **MCP 可以做但没做** 的?(那是 bug,提 issue,不是 pitfall)

只有满足 #1 才进本文件。
