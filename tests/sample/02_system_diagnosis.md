# 意图识别用例:系统诊断（简单系统诊断，高频）

**对应产品需求**: §3.2.2 简单系统诊断  
**场景**: 查询 CPU/内存/温度/磁盘状态、应用是否运行、配置状态查询  
**主要路由**: 规则命中（rule_call）+ 口语查询走端侧模型（to_model）

## 字段说明

- **输入**: 模拟真实用户的口语表达
- **期望路由**: `rule_call` / `to_model` / `escalate`
- **期望工具**: 目标工具名，无则 None。标 `⚠️待实现` 的是目标答案（用例驱动开发）
- **期望参数**: 关键槽位值，无则 `-`
- **说明**: 用例意图与边界标注

---

## 系统信息查询（get_system_info）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 查看CPU使用率 | rule_call | get_system_info | item=cpu | 规范表达 |
| 看下内存占用 | rule_call | get_system_info | item=memory | "看下"口语 |
| 显示系统状态 | rule_call | get_system_info | item=all | 全部信息 |
| 查看磁盘使用 | rule_call | get_system_info | item=disk | 规范 |
| CPU温度多少 | to_model | get_system_info | item=temp | 口语查询 |
| 现在CPU占用高吗 | to_model | get_system_info | item=cpu | 口语疑问句 |
| 内存还剩多少 | to_model | get_system_info | item=memory | 口语 |
| 机器烫不烫 | to_model | get_system_info | item=temp | 极口语，隐含查温度 |
| 系统卡不卡 | to_model | get_system_info | item=cpu | 口语隐含查负载 |
| 硬盘满了没 | to_model | get_system_info | item=disk | 口语查磁盘 |
| 获取内存大小 | to_model | get_system_info | item=memory | 口语，规则够不着 |
| 温度怎么样 | to_model | get_system_info | item=temp | 口语 |
| 现在几点 | to_model | get_system_info | - | 查时间（item 可能需扩展） |
| 电量还有多少 | to_model | get_system_info | - | 查电池（item 可能需扩展） |
| 系统信息 | rule_call | get_system_info | item=all | 关键词 |
| 帮我看看电脑状态 | to_model | get_system_info | item=all | 口语 |

## 应用状态诊断（check_app_status，待实现）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 浏览器打开了吗 | to_model | check_app_status | app_name=浏览器 | ⚠️待实现：查应用是否运行 |
| 终端在运行吗 | to_model | check_app_status | app_name=终端 | ⚠️待实现 |
| 控制中心有没有开 | to_model | check_app_status | app_name=控制中心 | ⚠️待实现 |
| 现在开了哪些应用 | to_model | check_app_status | - | ⚠️待实现：列出运行中应用 |
| 音乐播放器是不是崩了 | to_model | check_app_status | app_name=音乐播放器 | ⚠️待实现，诊断应用状态 |

## 配置状态查询

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 当前亮度多少 | to_model | get_system_info | - | 查亮度状态（item 可能需扩展 brightness） |
| 现在音量是多少 | to_model | get_system_info | - | 查音量状态 |
| 现在静音了吗 | to_model | get_system_info | - | 查静音状态 |
| 屏幕亮度是多少 | to_model | get_system_info | - | 查亮度 |

## 混淆/边界用例（防止误判）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 音量调到60 | rule_call | set_volume | level=60 | 设置而非查询，不应是 get_system_info |
| CPU | to_model | get_system_info | item=cpu | 单词，可能查CPU |
| 亮度调到50 | rule_call | set_brightness | level=50 | 设置而非查询 |
| 内存条多少钱 | escalate | - | - | 问价格，非查系统，应升级 |
| CPU是什么 | escalate | - | - | 开放知识问答，非查状态 |
| 为什么这么卡 | to_model | get_system_info | item=cpu | 诊断意图，查负载（边界：也可能升级分析） |
