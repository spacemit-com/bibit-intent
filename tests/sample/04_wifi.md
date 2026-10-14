# 意图识别用例:WiFi 网络控制

**对应产品需求**: §3.2.3 复杂系统控制（网络连接）  
**场景**: 扫描附近网络、连接 WiFi、断开、查看当前连接状态  
**主要路由**: 规范表达 rule_call，口语走 to_model，模糊/歧义升级

## 字段说明

- **输入**: 模拟真实用户的口语表达
- **期望路由**: `rule_call` / `to_model` / `escalate`
- **期望工具**: 目标工具名，无则 None
- **期望参数**: 关键槽位值，无则 `-`
- **说明**: 用例意图与边界标注

---

## 扫描网络（wifi_scan）—— 找附近的网络

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 扫描wifi | rule_call | wifi_scan | - | 规范表达 |
| 搜索网络 | rule_call | wifi_scan | - | "搜索" |
| 列出wifi | rule_call | wifi_scan | - | "列出" |
| 附近有哪些wifi | rule_call | wifi_scan | - | "附近有哪些" |
| 有什么无线网 | rule_call | wifi_scan | - | "有什么" |
| 看看周围有什么网 | to_model | wifi_scan | - | 口语 |
| 搜一下附近的网络 | to_model | wifi_scan | - | 口语 |
| 有哪些能连的wifi | to_model | wifi_scan | - | 口语 |

## 连接网络（wifi_connect）—— 连指定网络

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 连接 JDSK 密码 12345678 | rule_call | wifi_connect | ssid=JDSK, password=12345678 | 规范：名+密码 |
| 连接 MyHome | rule_call | wifi_connect | ssid=MyHome | 仅SSID（开放/已存） |
| 接入 office_5G 密码 abcd1234 | rule_call | wifi_connect | ssid=office_5G, password=abcd1234 | "接入"+下划线SSID |
| 连一下 JDSK | rule_call | wifi_connect | ssid=JDSK | 口语带SSID（"连一下"→规则层） |
| 帮我连上 MyHome 密码是 pass123 | rule_call | wifi_connect | ssid=MyHome, password=pass123 | 口语带密码（"密码是"→规则层） |
| 连接JDSK的wifi，密码是12345678 | rule_call | wifi_connect | ssid=JDSK, password=12345678 | 类别词后置+"密码是"（修复回归） |
| 连接wifi | to_model | wifi_connect | - | 只有类别词，无SSID→应引导补充 |
| 连一下网 | to_model | wifi_connect | - | 口语无SSID→引导 |
| 帮我连个网 | to_model | wifi_connect | - | 口语无SSID→引导 |

## 断开网络（wifi_disconnect）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 断开wifi | rule_call | wifi_disconnect | - | 规范 |
| 断开网络 | rule_call | wifi_disconnect | - | 规范 |
| 断网 | rule_call | wifi_disconnect | - | 简写 |
| 关闭wifi连接 | rule_call | wifi_disconnect | - | "关闭连接" |
| 把网断了 | to_model | wifi_disconnect | - | 口语 |
| 断一下网 | to_model | wifi_disconnect | - | 口语 |
| 不想连网了 | to_model | wifi_disconnect | - | 口语隐含断开 |

## 连接状态（wifi_status）—— 查当前连的是啥

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 当前wifi状态 | rule_call | wifi_status | - | 规范 |
| 查看wifi连接 | rule_call | wifi_status | - | "查看"=查状态，非扫描 |
| 看wifi | rule_call | wifi_status | - | "看"=查状态 |
| 显示wifi连接 | rule_call | wifi_status | - | "显示" |
| 连的什么wifi | rule_call | wifi_status | - | 查当前 |
| 现在连的啥网 | to_model | wifi_status | - | 口语查当前 |
| 我连的是哪个网络 | to_model | wifi_status | - | 口语 |
| 现在有没有连网 | to_model | wifi_status | - | 口语查是否连接 |
| wifi信号怎么样 | to_model | wifi_status | - | 查信号=状态 |

## 混淆/边界用例（防止误判 —— 都是实际踩过的坑）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 查看wifi连接 | rule_call | wifi_status | - | "查看"是查状态，**不是** wifi_scan（扫描） |
| 扫描wifi | rule_call | wifi_scan | - | "扫描"是找附近，与"查看"对比 |
| 连接wifi | to_model | wifi_connect | - | "wifi"是类别词非SSID，不能拿去连 |
| JDSK的密码是xxx | to_model | - | - | JDSK可能是数据库/账号，非必然WiFi，不误判连接 |
| 数据库密码是123 | escalate | - | - | 与WiFi无关，端侧无对应能力，升级 |
| 打开WiFi | to_model | - | - | 模糊：开关/看/连？交模型判断 |
| wifi密码是多少 | to_model | - | - | 歧义：问密码还是要连？应追问澄清，不直接执行 |
| 网速怎么样 | escalate | - | - | 测网速≠查连接状态，端侧无测速工具，升级 |
