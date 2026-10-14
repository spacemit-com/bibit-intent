# 意图识别用例:应用控制

**对应产品需求**: §3.8 应用控制、§3.2.1 打开/关闭应用  
**场景**: 启动应用、关闭应用、重启应用（关掉重开）  
**主要路由**: 规则命中（rule_call）+ 口语走端侧模型（to_model）

## 字段说明

- **输入**: 模拟真实用户的口语表达
- **期望路由**: `rule_call` / `to_model` / `escalate`
- **期望工具**: 目标工具名，无则 None。标 `⚠️待实现` 的是目标答案（用例驱动开发）
- **期望参数**: 关键槽位值，无则 `-`
- **说明**: 用例意图与边界标注

---

## 启动应用（launch_app）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 打开终端 | rule_call | launch_app | app_name=终端 | 规范表达 |
| 帮我打开浏览器 | rule_call | launch_app | app_name=浏览器 | 带"帮我" |
| 启动控制中心 | rule_call | launch_app | app_name=控制中心 | 中文名映射 |
| 打开bianbu-control-center | rule_call | launch_app | app_name=bianbu-control-center | 英文命令名 |
| 运行文件管理器 | rule_call | launch_app | app_name=文件管理器 | "运行" |
| 开启系统设置 | rule_call | launch_app | app_name=系统设置 | "开启"+映射 |
| 打开记事本 | rule_call | launch_app | app_name=记事本 | 中文名映射 |
| 我想用一下浏览器 | to_model | launch_app | app_name=浏览器 | 口语，无"打开" |
| 帮我把终端调出来 | to_model | launch_app | app_name=终端 | 口语 |
| 给我开个文件管理器 | to_model | launch_app | app_name=文件管理器 | 口语 |
| 启动一下那个控制中心 | to_model | launch_app | app_name=控制中心 | 口语带语气词 |

## 关闭应用（close_app）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 关闭终端 | rule_call | close_app | app_name=终端 | 规范表达 |
| 退出浏览器 | rule_call | close_app | app_name=浏览器 | "退出" |
| 关掉文件管理器 | rule_call | close_app | app_name=文件管理器 | "关掉" |
| 停止控制中心 | rule_call | close_app | app_name=控制中心 | "停止" |
| 结束记事本 | rule_call | close_app | app_name=记事本 | "结束" |
| 把浏览器关了 | to_model | close_app | app_name=浏览器 | 口语 |
| 不用终端了关掉吧 | to_model | close_app | app_name=终端 | 口语 |
| 浏览器别开着了 | to_model | close_app | app_name=浏览器 | 口语隐含关闭 |

## 重启应用（reboot_app，待实现）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 重启浏览器 | rule_call | reboot_app | app_name=浏览器 | ⚠️待实现：关掉重开，非设备重启 |
| 重新启动终端 | rule_call | reboot_app | app_name=终端 | ⚠️待实现 |
| 把文件管理器重启下 | to_model | reboot_app | app_name=文件管理器 | ⚠️待实现，口语 |
| 控制中心卡了重开一下 | to_model | reboot_app | app_name=控制中心 | ⚠️待实现，口语隐含重启 |
| 浏览器崩了帮我重开 | to_model | reboot_app | app_name=浏览器 | ⚠️待实现 |

## 混淆/边界用例（防止误判）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 重启 | rule_call | reboot | - | 无应用名=设备重启，非 reboot_app |
| 重新启动 | rule_call | reboot | - | 设备重启 |
| 重启系统 | rule_call | reboot | - | 明确设备 |
| 重启浏览器 | rule_call | reboot_app | app_name=浏览器 | 有应用名=重启应用，与"重启"对比 |
| 打开静音 | rule_call | mute | on=True | "打开静音"是静音功能，非启动应用 |
| 打开WiFi | to_model | - | - | 模糊：可能是开WiFi开关/看网络/连网，交本地模型判断，不直接映射某工具 |
| 打开 | to_model | launch_app | - | 不完整，缺应用名，应追问 |
| 关闭 | to_model | - | - | 不完整，关什么不明 |
| 打开一个能写代码的软件 | escalate | - | - | 模糊需求，需云端理解推荐（端侧无法确定具体应用） |
| 安装微信 | rule_call | install_app | app_name=微信 | ⚠️待实现：软件管理 skill，调用安装 app 能力 |
