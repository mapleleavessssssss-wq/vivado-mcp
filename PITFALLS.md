# PITFALLS — 给 vivado-mcp 用户的操作约束

> **本文档面向用户**(用 vivado-mcp + AI 跑 Vivado 工作流的人),不是给 AI 协作者
> 看的(AI 协作约束在 `.trellis/spec/backend/vivado-quirks.md`)。
>
> 这里列的是 **MCP 物理上无法替你做** 的事 —— 不是 bug,不是设计缺陷,是 Vivado
> 本身的限制或 OS 边界,需要你**手动**操作。

---

## C1. XSim Analog 波形显示样式 — 只能 GUI 手动逐路点

**触发**:你让 AI 把某些波形(比如 ADC 输出、DAC 输入、滤波器响应)显示成 Analog
样式(模拟波形,可看趋势),而不是 Digital(0/1 阶梯)。

**为什么 MCP 做不到**:Vivado 2019.1 XSim 的 `wave_design_object` **完全没有**
`wave_style` / `display_style` / `analog_wave` 之类的属性 —— `list_property $w`
全集没这字段,**Tcl 物理上没有接口**(详见 `.trellis/spec/backend/vivado-quirks.md`
§8.9)。

**怎么手动操作**:
1. AI 把信号 `add_wave` 到 wave window
2. 你在 wave window 里:
   - 右键单条波形 → **Waveform Style** → **Analog**
   - 或 Ctrl 多选几条 → 一起右键改样式
3. N 路波形要点 N 次(无法批量)

**别让 AI 浪费时间**:如果 AI 跟你说"我帮你写 Tcl 自动改 Analog",**停下来**告诉它
看 quirks §8.9 —— 它会一直找不到属性,一直试错。

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
| `.trellis/spec/backend/vivado-quirks.md` | **AI 协作者** | "AI 写脚本 / debug 时要知道的 Vivado quirks" |
| `_safe_execute` 里的 W hints | **AI 运行时** | "命令出 err 时自动追加的复制粘贴指引" |

三层互补,不重复。

---

## 新增 pitfall 的规矩

新加一条到本文件之前问自己:
1. 这是 **MCP 物理上做不到** 的吗?(物理边界 / OS 限制 / Vivado 无接口)
2. 还是 **AI 写脚本会踩** 的?(那应该进 vivado-quirks.md,不是这里)
3. 还是 **MCP 可以做但没做** 的?(那是 bug,提 issue,不是 pitfall)

只有满足 #1 才进本文件。
