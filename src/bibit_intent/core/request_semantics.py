"""Cheap non-command guards shared by routing and slot extraction."""

import re

from bibit_intent.analysis.patterns import CONTEXT_DEPENDENT_RE


# ── 句子形态归一化：把表面噪声（礼貌/状语/助词/标点/空白）滤掉，供有限状态
#    判定在同一句法骨架上工作。只用于「句子是什么类型」，不参与槽位抽取。

# 礼貌前缀：按最长优先排列，避免「请问」被「请」截断。
_POLITE_PREFIX = re.compile(
    r"^(?:"
    r"请问(?:一下|你|您)?"
    r"|(?:麻烦|劳驾|拜托|烦请)(?:你|您)?\s*(?:帮我|帮忙|帮我一下)?"
    r"|请(?!求|教|示|愿|假|辞|安|帖)(?:你|您)?\s*(?:帮我|帮忙|帮我一下)?"
    r"|(?:帮我|帮忙|帮帮我|帮我一下)"
    r"|(?:你|您)?(?:能不能|可不可以|能否|可否|可以|能)(?:\s*帮我)?"
    r")\s*",
    re.I,
)
# 状语前置：地点/范围状语（在bianbu下/系统里…）与时间状语（当前/现在…）。
# 地点/范围状语必须带方位词（上/下/里/中…），否则「系统已经运行多久」这类
# 主语句会被误当成状语剥掉「系统」，暴露「已经」从而误判为历史上下文。
_ADVERBIAL_PREFIX = re.compile(
    r"^(?:(?:在|于)?\s*"
    r"(?:bianbu|ubuntu|debian|linux|arch|fedora|centos|redhat|windows|macos|android|riscv|risc-v|"
    r"系统|本机|本地|这台机器|这台设备|机器|设备|电脑|服务器|手机|桌面|终端|容器|虚拟机|云)"
    r"(?:上|下|里|中|内|里面|上面|下面|当中|之间)\s*)"
    r"|^(?:当前|现在|目前|眼下|此刻|如今)\s*",
    re.I,
)
# 句末语气助词（吗/呢/吧/啊…）及其组合，后跟句末标点。
_TRAILING_PARTICLE = re.compile(
    r"(?:吗|呢|吧|啊|呀|啦|(?<![什怎这那多])么|嘛|咯|喽|哦|喔|哟|呗|哈|呵|嘞|咧|呐|罢|而已|罢了|来着|就是了|好吗|可以吗|行吗|不)+"
    r"[。！!？?]*\s*$",
    re.I,
)
# 标点 → 空格（保留词边界，供英文 how to / ip route 等识别）。
_PUNCT = re.compile(r"[，。、；：！？…—－·“”‘’「」『』（）【】《》〈〉\[\]{}()<>.,!?;:'\"~`@#$%^&*+=|\\/]+")
_SPACES = re.compile(r"\s+")


def normalize_sentence(text: str) -> str:
    """把句子规约到形态骨架：去标点、压空白、剥礼貌前缀与状语、去句末语气词。"""
    t = _PUNCT.sub(" ", text.strip())
    t = _SPACES.sub(" ", t).strip()
    for _ in range(3):
        m = _POLITE_PREFIX.match(t)
        if not m or not m.group(0):
            break
        t = t[m.end():].strip()
    for _ in range(2):
        m = _ADVERBIAL_PREFIX.match(t)
        if not m or not m.group(0):
            break
        t = t[m.end():].strip()
    t = _TRAILING_PARTICLE.sub("", t).strip()
    return t


# 开放问答里的「怎么做某件事」动词集合。除原有的读/控制动词外，补上
# 改写类写动作（替换/编译/降级/重装/切换…），否则「bianbu下如何替换kernel」
# 这类句首/句中带疑问词的 how-to 会被漏判，落进关键词预筛被只读工具污染。
_HOWTO_VERB_ALT = (
    r"调节|调整|设置|安装|卸载|使用|启动|停止|重启|删除|打开|关闭|清理|挂载|格式化|杀死"
    r"|替换|更换|升级|降级|更新|编译|重装|重刷|刷机|烧录|迁移|切换|重置|修复|修改|改写"
    r"|切|换|转|改|设"
)
_EXPLANATION = re.compile(
    rf"^(?:请|请帮我|帮我|你能|能否|可以)?\s*(?:解释|介绍|讲讲|科普|如何|怎样|怎么(?:样)?(?:{_HOWTO_VERB_ALT}))"
    r"|(?:是什么(?:意思|原理)?|什么是|的原理|有什么(?:风险|影响|作用|危害)|有风险吗|什么原因|会伤眼睛吗)"
    rf"|(?:如何|怎样|怎么)(?:才能|可以|能)?\s*(?:{_HOWTO_VERB_ALT})"
    r"|为什么(?:需要|要|会|能|是)"
    r"|\b(?:how\s+(?:to|do\s+i|can\s+i)|what\s+(?:is|are)|explain|why\s+(?:is|are|do|does|should))\b",
    re.I,
)
_CONDITIONAL = re.compile(
    r"(?:^|[，,；;])\s*(?:如果|假如|假设|除非|若是|若有|若需要)"
    r"|\b(?:if|unless|provided\s+that)\s+\S+", re.I,
)
_HISTORY = re.compile(
    r"^(?:我)?(?:已经|昨天|刚才|刚刚|之前|先前|曾经)"
    r"|(?:安装|卸载|重启|启动|连接|访问|查询|搜索).{0,60}(?:失败了?|出错了?|报错了?|成功了|完成了)[。！!？?]*$"
    r"|^(?:i\s+)?(?:yesterday|previously|already)\b"
    r"|\b(?:install(?:ation)?|uninstall|restart|connection|access)\b.{0,60}\bfailed[.!?]*$", re.I,
)
_DIAGNOSTIC = re.compile(
    r"(?:还是|依然|仍然|仍旧).{0,6}(?:无法|不能|不行|失败|连不上|打不开)"
    r"|(?:无法访问|连接不上|连不上|访问不了)"
    r"|(?:^|[，,；;])\s*(?:我的|本机|本地|这台设备的)?\s*(?:ip(?:地址)?|地址)\s*(?:是|在|为|[:：])\s*[0-9a-f:.]+"
    r"|\b(?:still\s+(?:cannot|can't|unable)|my\s+ip\s+(?:is|address\s+is))\b", re.I,
)

# Questions about the agent's tool/capability surface are meta-questions, not
# requests to search files or execute a capability named in the question.
# Keep a concrete meta-object requirement so real queries such as "本地有没有
# 周报" remain eligible for file search.
_CAPABILITY_QUERY = re.compile(
    r"(?:你|您)?\s*(?:有|有没有|有哪些|支持|具备|提供|能做什么|可以做什么|可用|能否|是否支持)"
    r".{0,24}(?:工具|能力|功能|接口|mcp|filesystem|file\s*search)"
    r".{0,8}(?:吗|么|呢|？|\?)?"
    r"$|"
    r"\b(?:what\s+(?:tools|capabilities|functions)|which\s+tools|"
    r"do\s+you\s+(?:have|support)|can\s+you\s+(?:use|support)|"
    r"support\s+filesystem)\b",
    re.I,
)
_EXPLICIT_QUERY = re.compile(
    r"^(?:请)?(?:帮我)?(?:查询|查一下|查看|显示|检查)\s*.+$"
    r"|^(?:please\s+)?(?:check|show|query)\s+.+$", re.I,
)
_NONFILE_OBJECT = re.compile(
    r"(?:ip(?:地址)?|网络|网卡|软件|程序|应用|服务|CPU|内存|音量|亮度)", re.I,
)
_FILE_OBJECT = re.compile(
    r"文档|文件(?!管理器)|资料|笔记|周报|图片|视频|日志|照片|音乐|报告|表格|附件|脚本"
    r"|\b(?:files?|docs?|documents?|pdfs?|logs?|photos?|music)\b"
    r"|\.(?:txt|log|pdf|md|docx?|xlsx?|png|jpe?g|mp[34])\b", re.I,
)

_ACTION_OR_AUTHORITY = re.compile(
    r"安装|卸载|下载|删除|移除|运行|执行|启动|停止|重启|关机|打开|关闭|修改|设置|读取|访问|上传|连接"
    r"|替换|更换|升级|降级|更新|编译|重装|重刷|刷机|烧录|迁移|切换|重置|修复|改写|格式化"
    r"|密码|口令|凭据|权限|授权|提权|免密"
    r"|\b(?:install(?:ed|ing|ation)?|uninstall(?:ed|ing)?|download(?:ed|ing)?|delet(?:e|ed|ing)|"
    r"remov(?:e|ed|ing)|run|ran|execut(?:e|ed|ing)|open(?:ed|ing)?|clos(?:e|ed|ing)|"
    r"restart(?:ed|ing)?|reboot(?:ed|ing)?|read|access|upload(?:ed|ing)?|connect(?:ed|ing)?|"
    r"replac(?:e|ed|ing)|swap|upgrade(?:d)?|downgrade(?:d)?|update(?:d)?|reinstall(?:ed)?|reflash|compile(?:d)?|"
    r"build|migrat(?:e|ed|ing)|switch(?:ed|ing)?|reset|fix(?:ed|ing)?|rewrit(?:e|ten)|modif(?:y|ied|ying)|format(?:ted|ting)?|"
    r"passwords?|credentials?|permissions?|privileges?|sudo|authori[sz](?:ation|ed))\b", re.I,
)
# 写动作（变更对象状态/内容）：改写类 + 控制类。用于只读工具的关键词护栏——
# 只读工具不应在用户表达「修改/替换/删除/安装…对象」时被注入为候选。
# 用「动词紧邻宾语」判定，定语「的/后/过」会断开相邻，故「查看可升级的软件」
# 这类读查询不会被误判。
_WRITE_VERB_ALT = (
    r"替换|更换|降级|编译|重装|重刷|刷机|烧录|迁移|切换|重置|修复|改写|修改"
    r"|安装|卸载|删除|移除|设置|启动|停止|重启|关机|清理|挂载|杀死|禁用|启用|断开|新建|创建|添加|锁定|解锁|重命名|移动|复制"
    r"|改|换"
    r"|\b(?:replace|swap|downgrade|compile|build|reinstall|reflash|flash|migrate|switch|reset|fix|rewrite|modify|"
    r"install|uninstall|delet|remov|set|start|stop|restart|reboot|clean|kill|disable|enable|disconnect|create|add|lock|unlock|rename|move|copy)\b"
)
_WRITE_ACTION = re.compile(_WRITE_VERB_ALT, re.I)
# 写动词与宾语之间的短修饰词（数量/指示/冠词），不含「的/后/过」这类定语标记。
_WRITE_FILLER = re.compile(r"(?:一下|下|一个|个|这台|该|本|这个|那个|这些|the|a|an)\s*", re.I)
_CAUSAL_QUESTION = re.compile(
    r"为什么|为啥|为何|凭什么|凭啥|怎么|咋"
    r"|\bwhy\b|\bhow\s+(?:come|(?:did|do|does|can|could|would|was|were|is|are|have|has)\s+"
    r"(?:you|it|this|that|the\s+(?:assistant|agent|system|app)))\b", re.I,
)
_AUTHORITY = re.compile(r"密码|口令|凭据|权限|授权|提权|免密|\b(?:passwords?|credentials?|permissions?|privileges?|sudo)\b", re.I)
_AUTHORITY_INQUIRY = re.compile(
    r"(?:知道|知不知道|拥有|拿到|获取|保存|记住|有权|需要|不用|无需|免密|权限|授权).{0,80}(?:吗|么|[？?])\s*$"
    r"|(?:是否|是不是|能否|需不需要|要不要|用不用).{0,80}(?:密码|口令|凭据|权限|授权)"
    r"|\b(?:do|does|did|can|could)\s+(?:you|it|the\s+(?:assistant|agent|system))\s+"
    r"(?:already\s+|still\s+)?(?:know|remember|store|save|need|require|have)\b"
    r"|\b(?:have|has)\s+(?:you|it|the\s+(?:assistant|agent|system))\s+"
    r"(?:already\s+)?(?:stored|saved|remembered|obtained|received)\b", re.I,
)


def is_action_explanation(text: str) -> bool:
    """Recognize causal/authority questions, not polite requests to perform an action.

    An explanation anywhere in a compound request keeps the entire request out
    of deterministic execution. The contextual planner can then separate any
    explicitly requested action from the question without executing a fragment.
    """
    for clause in re.split(r"(?<=[？?])|[，,。；;！!\n]+", text.strip()):
        if _ACTION_OR_AUTHORITY.search(clause) and _CAUSAL_QUESTION.search(clause):
            return True
        if _AUTHORITY.search(clause) and _AUTHORITY_INQUIRY.search(clause):
            return True
    return False


def write_action_targets(text: str, obj: str) -> bool:
    """Whether a write/control verb takes ``obj`` as its direct object.

    Used by the read-only keyword gate (e.g. ``sys_kernel`` only reports the kernel
    version): it must not be injected when the user asks to replace/delete/modify
    the object. Only a write verb immediately followed by ``obj`` counts; the
    attributive particle 的/后/过 breaks the adjacency, so ``查看替换后的内核`` is
    still treated as a read query rather than a replacement.
    """
    obj_l = obj.lower()
    for m in _WRITE_ACTION.finditer(text):
        tail = text[m.end():].lstrip()
        filler = _WRITE_FILLER.match(tail)
        rest = tail[filler.end():] if filler else tail
        if rest.lstrip().lower().startswith(obj_l):
            return True
    # 把字句（宾语在动词前）：把 kernel 替换了 / 把内核降级了
    if re.search(rf"把\s*{re.escape(obj)}\s*(?:{_WRITE_VERB_ALT})", text, re.I):
        return True
    return False


def file_search_object_allowed(text: str) -> bool:
    return not _NONFILE_OBJECT.search(text) or bool(_FILE_OBJECT.search(text))


def is_capability_question(text: str) -> bool:
    """Return whether text asks about available tools/capabilities.

    This must run before semantic tool retrieval. The question's technical
    noun (for example ``filesystem``) is not evidence that the user wants the
    corresponding tool executed.
    """
    return bool(_CAPABILITY_QUERY.search(text.strip()))


def non_command_reason(text: str) -> str | None:
    text = text.strip()
    # 归一化只用于「如何替换kernel」这类 how-to 问句的判定：剥掉状语前置后
    # 才能让 _EXPLANATION 看见句中的疑问动词。否定/条件/历史/诊断模式对句首
    # 位置敏感（^别/^不要/^已经…），若在归一化文本上再跑会误判：
    # 「帮我不要关浏览器」剥掉「帮我」变成句首否定 → 错误升为 context_dependent；
    # 「本地搜索 if.log」把 . 换成空格 → 英文 if 误命中文件名；
    # 「系统已经运行多久」剥掉「系统」→ 误命中「已经」历史模式。
    norm = normalize_sentence(text)
    if CONTEXT_DEPENDENT_RE.search(text.lower()):
        return "context_dependent"
    if is_action_explanation(text) or _EXPLANATION.search(text) or _EXPLANATION.search(norm):
        return "open_question"
    if _CONDITIONAL.search(text):
        return "conditional_request"
    if _HISTORY.search(text):
        return "context_dependent"
    if _DIAGNOSTIC.search(text):
        clauses = re.split(r"[，,；;]", text)
        if len(clauses) > 1 and _EXPLICIT_QUERY.fullmatch(clauses[-1].strip()):
            return None
        return "context_dependent"
    return None
