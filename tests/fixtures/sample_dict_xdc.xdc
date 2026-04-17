## 测试 fixture：覆盖 -dict 语法（Basys3 风格）+ 传统语法 + 注释 + 多端口

## -dict 语法：时钟
set_property -dict { PACKAGE_PIN W5   IOSTANDARD LVCMOS33 } [get_ports clk]
create_clock -period 10.000 -name sys_clk [get_ports clk]

## -dict 语法：按钮（嵌套花括号在端口名）
set_property -dict { PACKAGE_PIN U18  IOSTANDARD LVCMOS33 } [get_ports rst_n]

## -dict 语法：向量端口（带花括号转义）
set_property -dict { PACKAGE_PIN U16  IOSTANDARD LVCMOS33 } [get_ports {led[0]}]
set_property -dict { PACKAGE_PIN E19  IOSTANDARD LVCMOS33 } [get_ports {led[1]}]

## 传统语法混用
set_property PACKAGE_PIN U19 [get_ports {led[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[2]}]

## 注释行里的假约束（不应被解析）
# set_property -dict { PACKAGE_PIN ZZ99 IOSTANDARD LVCMOS33 } [get_ports fake]
## set_property PACKAGE_PIN AA1 [get_ports another_fake]

## 带尾注释
set_property -dict { PACKAGE_PIN V19  IOSTANDARD LVCMOS33 } [get_ports {led[3]}]  # LD3

## 空行

## 传统语法 + 花括号空格
set_property PACKAGE_PIN W18 [ get_ports { led[4] } ]
