"""
agent/cloud_model.py — 第三段：云端模型客户端

职责：处理第二段升级（ESCALATE）上来的复杂请求。
  - **双模式设计**：区分工具调用模式与对话生成模式
  - 工具调用模式：用 function calling 路由到具体工具(系统控制)
  - 对话生成模式：直接返回生成式文本(开放问答、写作、编程)
  - 使用完整 MCP schema（云端资源不受限）
  - 上云前脱敏（design.md §4.3 + §12）

对照 design.md §7.2（深度 RAG）、product_requirements.md M1。
"""
from __future__ import annotations

import re
from typing import Any

from bibit_intent.core.registry import ToolRegistry


# 简单脱敏规则：上云前遮蔽常见敏感信息
_SANITIZE_PATTERNS = [
    (re.compile(r"\b\d{11}\b"), "[手机号]"),
    (re.compile(r"\b\d{15,19}\b"), "[卡号]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱]"),
    (re.compile(r"\b\d{6}(19|20)\d{2}\d{4}\d{3}[\dxX]\b"), "[身份证]"),
]


def sanitize(text: str) -> str:
    """上云前脱敏。"""
    for pat, repl in _SANITIZE_PATTERNS:
        text = pat.sub(repl, text)
    return text


class CloudModelClient:
    """
    云端客户端骨架。使用 OpenAI 兼容接口 + function calling。
    """

    def __init__(self, cfg, registry: ToolRegistry):
        self._cfg = cfg
        self._registry = registry
        self._client = None  # 惰性初始化

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._cfg.cloud_api_key,
                base_url=self._cfg.cloud_base_url,
            )
        return self._client

    def handle(self, user_input: str, reason: str = "", language: str | None = None) -> dict[str, Any]:
        """
        处理端侧升级上来的请求。双模式设计：

        ① 对话生成模式（优先）：
           - 触发条件：reason 是 "open_question"（开放知识问答）
           - 行为：直接调用 LLM 生成回答，不使用 function calling
           - 返回：{ok, content, mode="chat"}

        ② 工具调用模式：
           - 触发条件：其他 reason（如 local_unavailable, parse_failed）
           - 行为：使用 function calling 路由到具体工具
           - 返回：{ok, message, tool, params, mode="tool"}

        对照 product_requirements.md M1 需求。
        """
        clean = sanitize(user_input)
        client = self._ensure_client()

        # ① 对话生成模式：开放知识问答/写作编程等，直接生成回答
        if reason in ("open_question", "writing_task"):
            return self._chat_mode(client, clean, language=language)

        # ② 工具调用模式：系统控制等，function calling 路由
        return self._tool_mode(client, clean, language=language)

    def _chat_mode(self, client, clean_input: str, language: str | None = None) -> dict[str, Any]:
        """
        对话生成模式：直接返回 LLM 生成的文本，不使用 function calling。
        适用场景：开放知识问答、写作、编程、解释性回答。
        """
        system_prompt = ("You are Bibit, an AI assistant. Answer accurately, concisely, and helpfully in English."
                         if language == "en" else "你是进迭时空的 AI 助手 Bibit，帮助用户解答问题、完成任务。回答要准确、简洁、有帮助。")
        resp = client.chat.completions.create(
            model=self._cfg.cloud_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": clean_input},
            ],
            temperature=0.7,
        )

        content = resp.choices[0].message.content or "(云端无回复)"
        return {
            "ok": True,
            "content": content,
            "mode": "chat",
        }

    def _tool_mode(self, client, clean_input: str, language: str | None = None) -> dict[str, Any]:
        """
        工具调用模式：使用 function calling 路由到具体工具。
        适用场景：系统控制、复杂任务编排。
        """
        # 云端可用完整 MCP schema（token 不受限）
        tools = self._registry.mcp_schemas()

        # 注入已知蓝牙设备列表，帮助云端模型准确填写 bt_connect 的 device 参数。
        # 不注入时模型面对 "连接LOGI M240 B" 可能生成文本而非 tool_call。
        system_base = (
            "You are a device control assistant. Call the appropriate tool."
            if language == "en"
            else "你是设备控制助手，根据用户请求调用合适的工具。"
        )
        bt_ctx = self._registry.get_bt_device_context()
        system_content = f"{system_base}\n{bt_ctx}" if bt_ctx else system_base

        resp = client.chat.completions.create(
            model=self._cfg.cloud_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": clean_input},
            ],
            tools=tools,
            # required：逼模型给出 tool_call。设成 auto 时它可能只回一句
            # "已连接：LOGI M240 B"（照抄之前 bt_scan 的设备行），端侧什么都没执行，
            # 用户却以为连上了（实测 session 01M2MZDYRKF147BJBSG6REW6PM）。
            # 没有可用工具时退回 auto，避免空 tools 配 required 被接口拒绝。
            tool_choice="required" if tools else "auto",
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            import json
            # 收集全部 tool_calls，不再只取第一个（“打开终端和设置”等
            # 多动作会一次返回多个调用，丢后续动作是历史 bug）。
            calls: list[dict] = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except (ValueError, TypeError):
                    args = {}
                calls.append({"tool": tc.function.name, "params": args})
            first = calls[0]
            return {
                "ok": True,
                "message": f"云端决策：调用 {first['tool']}",
                "tool": first["tool"],
                "params": first["params"],
                "calls": calls,   # 完整调用序列，供调用方按序执行/聚合
                "mode": "tool",
            }
        return {
            "ok": True,
            "message": msg.content or "(云端无明确动作)",
            "tool": None,
            "params": {},
            "calls": [],
            "mode": "tool",
        }
