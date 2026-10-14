# 意图识别用例:系统控制（简单控制功能，高频）

**对应产品需求**: §3.2.1 简单控制功能  
**场景**: 音量、亮度、静音、重启等高频系统控制  
**主要路由**: 规则引擎直接命中（rule_call）为主，口语变体走端侧模型（to_model）

## 字段说明

- **输入**: 模拟真实用户的口语表达
- **期望路由**: `rule_call`(规则直接执行) / `to_model`(端侧模型) / `escalate`(升级云端)
- **期望工具**: 目标工具名，无则 None。标 `⚠️待实现` 的是**目标答案**（用例驱动开发），
  对应工具尚未实现，用例先定义正确行为，实现后应能通过
- **期望参数**: 关键槽位值，无则 `-`
- **说明**: 用例意图与边界标注

---

## 音量控制（set_volume）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 音量调到60 | rule_call | set_volume | level=60 | 规范表达，精确命中 |
| 把音量调到80 | rule_call | set_volume | level=80 | 带"把"前缀 |
| 声音设为50 | rule_call | set_volume | level=50 | "声音"同义 |
| 音量调成100 | rule_call | set_volume | level=100 | 边界值上限 |
| 音量调到0 | rule_call | set_volume | level=0 | 边界值下限 |
| 音量调到150 | rule_call | set_volume | level=100 | 超上限，应钳制到100 |
| 声音开大一点 | to_model | set_volume | - | 口语相对调节，无具体值 |
| 声音太小了 | to_model | set_volume | - | 隐含调大 |
| 音量太大了 | to_model | set_volume | - | 隐含调小 |
| 帮我把声音关小点 | to_model | set_volume | - | 口语，无具体值 |
| 大点声 | to_model | set_volume | - | 极简口语 |
| 声音开到最大 | to_model | set_volume | - | "最大"需模型理解为100 |

## 静音控制（mute）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 静音 | rule_call | mute | on=True | 精确词 |
| 取消静音 | rule_call | mute | on=False | 精确词 |
| 关闭静音 | rule_call | mute | on=False | 精确词 |
| 开启静音 | rule_call | mute | on=True | 正则匹配 |
| 把声音静了 | to_model | mute | on=True | 口语 |
| 别出声了 | to_model | mute | on=True | 口语隐含静音 |
| 恢复声音 | to_model | mute | on=False | 口语取消静音 |
| 静一下 | to_model | mute | on=True | 极简口语 |

## 亮度控制（set_brightness）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 亮度调到30 | rule_call | set_brightness | level=30 | 规范表达 |
| 屏幕亮度设为70 | rule_call | set_brightness | level=70 | 完整表达 |
| 把屏幕调亮到90 | rule_call | set_brightness | level=90 | 带"把" |
| 亮度调到200 | rule_call | set_brightness | level=100 | 超限钳制 |
| 屏幕太暗了 | to_model | set_brightness | - | 隐含调亮 |
| 屏幕太暗了看不清 | to_model | set_brightness | - | 口语，关键词够不着 |
| 屏幕调暗点 | to_model | set_brightness | - | 相对调节 |
| 太亮了刺眼 | to_model | set_brightness | - | 隐含调暗 |
| 把屏幕弄亮一点 | to_model | set_brightness | - | 口语 |

## 重启（reboot）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 重启 | rule_call | reboot | - | 精确词（执行需二次确认） |
| 重新启动 | rule_call | reboot | - | 精确词 |
| 重启设备 | rule_call | reboot | - | 正则匹配 |
| 重启系统 | rule_call | reboot | - | 正则匹配 |
| 帮我重启一下 | to_model | reboot | - | 口语 |
| 把机器重启下 | to_model | reboot | - | 口语 |
| 关机重来 | to_model | reboot | - | 口语，需模型理解 |

## 混淆/边界用例（防止误判）

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 音量是多少 | to_model | get_system_info | - | 查询而非设置，不应是 set_volume |
| 现在亮度多少 | to_model | get_system_info | - | 查询状态 |
| 重启浏览器 | rule_call | reboot_app | app_name=浏览器 | "重启应用"(关掉重开)非"重启设备"，边界。⚠️reboot_app 待实现 |
| 声音 | to_model | - | - | 单词无意图，模型判断/追问 |
| 把音量 | to_model | set_volume | - | 不完整指令 |
