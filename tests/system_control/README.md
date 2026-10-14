# 硬件与外设控制 Skill 准确率测试

## 背景
用例来自 `tests/Bibit_system_control.xlsx` 的子表「硬件与外设控制」第 1-4 列
（设备 / 硬件支持的动作 / 指令分类 / 所有可能的用户输入），覆盖 8 个设备域、
54 个动作、334 条自然语言用例。

设备域：WiFi、蓝牙、屏幕、音量、有线网络、摄像头、磁盘、USB 设备、电源管理、PCIE 设备。

## 用例组织
`corpus.jsonl` 与 `tests/ubuntu_cmd/corpus.jsonl` 同格式：
```
{"id":"H001","group":"rule","input":"关闭WiFi","expect":"wifi_disconnect","note":"WiFi/关闭 WiFi"}
```

- **group=rule**：表格中「精确指令」——期望规则引擎命中
- **group=model**：表格中「模糊指令」+ 少量 ESCALATE 兜底——期望端侧模型翻译或升级
- **expect**：目标工具名（沿用 `config/tools.yaml` / `skills/*/skill.yaml` 短名规范）
- 表达中含 `[SSID]` `[X]` 等中括号占位符的行不纳入用例（占位符不是真实输入）

**新增 H335~H347（13 条）**：launch_app / close_app 回归用例
- **硬件+后缀豁免**（H335~H339）：测试"打开蓝牙设置""打开WiFi设置"等表达能正确走 launch_app
- **普通应用**（H340~H347）：确保 launch_app / close_app 原有能力不退化

## 工具名清单（56 个）
按设备域分组：

| 域 | 工具 |
|---|---|
| WiFi | wifi_disconnect, wifi_connect, wifi_status, wifi_scan, wifi_ip |
| 蓝牙 | bt_on, bt_off, bt_scan, bt_connect, bt_disconnect, bt_set_recv_dir, bt_forget |
| 屏幕 | set_brightness, set_resolution, set_refresh_rate, rotate_screen, set_aspect_ratio, set_display_mode |
| 音量 | set_volume, switch_audio_out, mute, set_mic_volume, switch_audio_in, mic_mute |
| 有线网络 | eth_up, eth_down, eth_status, eth_ip |
| 摄像头 | cam_on, cam_off, cam_snap, cam_users |
| 磁盘 | disk_eject, disk_mount_ext, disk_free |
| USB 设备 | usb_list, usb_reset |
| 电源管理 | power_save, power_perf, cpu_freq, cpu_max_freq, cpu_min_freq, cpu_boost, cpu_info, battery_status, idle_action, idle_timeout |
| PCIE 设备 | pcie_list, pcie_rescan, pcie_info, pcie_remove, pcie_unbind, pcie_bind |
| **应用控制** | **launch_app, close_app** |

**已实现（可跑端到端）**：
- 蓝牙 7 个：`skills/bluetooth/`（bluetoothctl 后台硬件控制）
- 应用 2 个：`config/tools.yaml`（launch_app / close_app 内置工具）

⚠️ 其他工具（WiFi / 屏幕 / 音量 / 摄像头 / 电源 / PCIE 等）只有用例，未实现 skill。跑完探针会看到"未实现的工具"走 fuzzy_resolve 失败 → 升级云端，正好用来核对待补齐清单。

## 文件
- `corpus.jsonl`  334 条自然语言用例
- `probe.py`      路由准确率探针（与 `tests/ubuntu_cmd/probe.py` 同款逻辑）
- `run_on_device.py`  推送到设备并执行（BIBIT_DEV_PW）

## 运行
```bash
# 设备上（llama-server 在 127.0.0.1:8081）
cd /opt/bibit
set -a; . /etc/bibit-intent/inference.env; set +a
.venv/bin/python tests/system_control/probe.py --n 3

# 或从本机一键推送执行
BIBIT_DEV_PW='***' python3 tests/system_control/run_on_device.py --n 3
```

## 生成脚本
`corpus.jsonl` 由 `/tmp/gen_sys_corpus.py`（一次性脚本）从 xlsx 生成。
需要新增/调整用例时：改回表格 → 重跑生成脚本，或直接编辑本目录下 `corpus.jsonl`。
