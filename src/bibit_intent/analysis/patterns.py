"""路由前置规则共享常量。

把原先散落在 routing/intent.py 的少数规则下沉到分析层，供
DeterministicAnalyzer 与 decide_local_intent 共用，避免同一规则两处维护。
"""

from __future__ import annotations

import re


# 上下文依赖输入（指代/省略对象/否定撤销）——端侧单轮无历史上下文，
# 无法确定"它/那个"指什么、"调大一点"的对象、以及是否真的要撤销动作，
# 统一升级云端，避免误执行（"打开它"误当 launch_app、"别重启"误当 reboot）。
CONTEXT_DEPENDENT_RE = re.compile(
    r"^(?:更新|更正|补充|修正|update|correction)\s*[:：]"
    r"|"
    # ① 动词 + 指代词（打开它/关闭那个/启动这个）
    r"(?:打开|启动|运行|开启|关闭|关掉|退出|停止|结束)\s*"
    r"(?:它|他|她|这个|那个|这些|那些)(?:窗口)?$"
    # ② 把/将 + 指代词 + 动词（把它关掉/把它最小化）
    r"|(?:把|将)\s*(?:它|他|她|这个|那个|这些|那些)(?:窗口)?\s*"
    r"(?:打开|启动|运行|开启|关闭|关掉|退出|停止|结束|最小化|最大化|激活|切换)$"
    # ③ 纯指代（它/那个/这个）
    r"|^(?:它|他|她|这个|那个|这些|那些)(?:窗口)?$"
    # ④ 省略对象的相对方向词（调大一点/再调小/调亮点）
    r"|^(?:再)?(?:调|开|关)\s*(?:大|小|亮|暗)(?:一点|一些|点|些)?$"
    # ⑤ 否定撤销（别/先别/不要/不用/不必 + 任意动作）
    r"|^(?:别|先别|不要|不用|不必)\s*[一-龥A-Za-z0-9]+$"
    # English pronouns/ellipsis and negated commands
    r"|^(?:open|close|launch|quit|stop|run)\s+(?:it|that|this|the window)$"
    r"|^(?:it|that|this)$"
    r"|^(?:turn|make)\s+it\s+(?:up|down|brighter|darker)$"
)


# 安装/卸载多个软件包（"安装hello和fio"/"卸载 firefox、chrome"）→ 端侧单工具
# 只能解析一个包名，升级云端逐包拆解，避免只执行第一个包。
MULTI_PKG_TARGET_RE = re.compile(
    r"(?:安装|下载安装|装(?:个|一下|一个)?|卸载|删除|移除)\s*"
    r"[a-zA-Z0-9_+.-]{1,40}\s*"
    r"(?:和|及|以及|与|跟|,|，|、|;|；)\s*"
    r"[a-zA-Z0-9_+.-]{1,40}"
    r"|(?:install|uninstall|remove)\s+[a-zA-Z0-9_+.-]{1,40}\s*(?:and|,|;)\s*[a-zA-Z0-9_+.-]{1,40}"
)


# 否定标记：用于 scope 提示与后续规则引擎/证据闸门接入。
NEGATION_MARKERS: tuple[str, ...] = (
    "别", "先别", "不要", "不用", "不必", "禁止", "勿", "没", "没有",
    "don't", "do not", "doesn't", "does not", "didn't", "did not",
    "cannot", "can't", "can not", "shouldn't", "should not", "never",
    "avoid", "without", "not",
)


# 否定标记 + 短距离内的动作词。用于识别"帮我不要关浏览器"这类句中否定，
# 避免被规则引擎当成普通 close_app 执行。范围控制较短，降低"不要关心"误报。
NEGATED_ACTION_RE = re.compile(
    r"(?:别|先别|不要|不用|不必|禁止|勿)\s*.{0,8}?"
    r"(?:打开|启动|运行|开启|关闭|关掉|退出|停止|结束|重启|关机|休眠|睡眠|待机|挂起|锁屏|截图"
    r"|播放|暂停|搜索|查询|查找|查看|检查|检测|诊断|监控|调节|调整|设置"
    r"|切换|连接|断开|安装|卸载|删除|移除|升级|更新|发送|复制|移动|重命名"
    r"|创建|新建|下载|上传|解压|压缩|备份|恢复|同步|保存|打印|扫描|生成"
    r"|制作|显示|隐藏|加载|计算|换算|翻译|转换|整理|清理|释放|挂载|格式化"
    r"|剪切|粘贴|合并|拆分|提取|调|开|关)"
    r"|(?:don't|do\s+not|never|shouldn't|should\s+not|avoid)\s+(?:open|close|launch|quit|stop|restart|shutdown|hibernate|suspend|install|uninstall|remove|chang\w*|turn|enable|disable)\b"
    r"|(?:I\s+don't\s+want|I\s+do\s+not\s+want)\s+.{0,20}(?:open\w*|close\w*|launch\w*|quit\w*|stop\w*|restart\w*|shutdown\w*|install\w*|uninstall\w*|remove\w*)\b"
    , re.I
)
