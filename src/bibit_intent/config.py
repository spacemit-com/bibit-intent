"""
agent/config.py — 配置管理
从环境变量 / .env 读取运行时配置。
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings
from pydantic import AliasChoices, Field

from bibit_intent.routing.embedding import DEFAULT_INDEXES
from bibit_intent.routing.complexity import ComplexityThresholds


class AgentConfig(BaseSettings):
    complexity_long_cjk_chars: int = Field(default=30, ge=1, le=4096, validation_alias="BIBIT_INTENT_COMPLEXITY_LONG_CJK_CHARS")
    complexity_long_english_words: int = Field(default=20, ge=1, le=4096, validation_alias="BIBIT_INTENT_COMPLEXITY_LONG_ENGLISH_WORDS")
    complexity_conjunction_count: int = Field(default=1, ge=1, le=4096, validation_alias="BIBIT_INTENT_COMPLEXITY_CONJUNCTION_COUNT")
    complexity_punctuation_count: int = Field(default=2, ge=1, le=4096, validation_alias="BIBIT_INTENT_COMPLEXITY_PUNCTUATION_COUNT")
    complexity_verb_count: int = Field(default=3, ge=1, le=4096, validation_alias="BIBIT_INTENT_COMPLEXITY_VERB_COUNT")
    complexity_question_count: int = Field(default=2, ge=1, le=4096, validation_alias="BIBIT_INTENT_COMPLEXITY_QUESTION_COUNT")
    complexity_score: int = Field(default=2, ge=1, le=4096, validation_alias="BIBIT_INTENT_COMPLEXITY_SCORE")

    @property
    def complexity_thresholds(self) -> ComplexityThresholds:
        return ComplexityThresholds(
            long_cjk_chars=self.complexity_long_cjk_chars,
            long_english_words=self.complexity_long_english_words,
            conjunction_count=self.complexity_conjunction_count,
            punctuation_count=self.complexity_punctuation_count,
            verb_count=self.complexity_verb_count,
            question_count=self.complexity_question_count,
            score=self.complexity_score,
        )

    inference_backend: Literal["ai_gateway"] = Field(
        default="ai_gateway",
        validation_alias=AliasChoices("BIBIT_INTENT_INFERENCE_BACKEND", "inference_backend"),
    )
    gateway_url: str = Field(
        default="http://127.0.0.1:18790",
        validation_alias=AliasChoices("BIBIT_INTENT_GATEWAY_URL", "gateway_url"),
    )
    gateway_llm_model: str = Field(
        default="glm-edge-1.5b-chat-q4_0",
        validation_alias=AliasChoices("BIBIT_INTENT_GATEWAY_LLM_MODEL", "gateway_llm_model"),
    )
    gateway_embed_model: str = Field(
        default="qwen3-embedding-0.6b-q4_0",
        validation_alias=AliasChoices("BIBIT_INTENT_GATEWAY_EMBED_MODEL", "gateway_embed_model"),
    )
    gateway_api_key: str = Field(
        default="", repr=False,
        validation_alias=AliasChoices("BIBIT_INTENT_GATEWAY_API_KEY", "gateway_api_key"),
    )
    gateway_embed_revision: str = Field(
        default="1",
        validation_alias=AliasChoices("BIBIT_INTENT_GATEWAY_EMBED_REVISION", "gateway_embed_revision"),
    )
    context_limit: int = 512

    # embedding 语义召回（口语路由增强，agent/embedding.py）
    # 逗号分隔的多份语料（相对项目根）。默认值来自 embedding.DEFAULT_INDEXES，
    # 那里是索引源的唯一真源，probe 也从同一处取，改索引只改那一处。
    embed_index: str = ",".join(DEFAULT_INDEXES)

    # 知识库目录（相对于项目根目录）
    kb_dir: str = "kb"

    # 云端（可选；留空则离线模式）
    cloud_api_key: str = ""
    cloud_base_url: str = "https://api.rvcompute.com:60000/v1"
    cloud_model: str = "claude-opus-4-6"

    # 日志
    log_level: str = "INFO"
    debug: bool = False

    # SoftGate 软置信门控（off / shadow / on）
    soft_gate_mode: str = "shadow"
    soft_gate_post_mode: str = "shadow"
    soft_gate_logprob_threshold: float = -2.2
    soft_gate_embed_top1_threshold: float = 0.72
    soft_gate_embed_margin_threshold: float = 0.05
    soft_gate_n_probs: int = 10

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def load_config(env_file: str | Path | None = None) -> AgentConfig:
    if env_file:
        return AgentConfig(_env_file=str(env_file))
    return AgentConfig()


def export_cloud_env(cfg: AgentConfig) -> None:
    """把 config 里的云端地址/密钥桥接进 os.environ（单一来源）。

    routing 层（`routing/intent._cloud_reachable` 预检门）与工具层
    （`skills/ubuntu_cmd/handlers._cloud_probe_target` 诊断第 3 跳）都在各自
    层里**只读环境变量**，为的是不让 routing 依赖 config/pydantic。
    于是云端地址就有了两个来源：环境变量（含 systemd drop-in）与 `.env`。
    若两者不一致，会出现「门探 A、云端客户端连 B」这类自相矛盾
    （详见 docs/intent/offline_network_diagnosis_plan.md §11.2）。

    这里做一次桥接：`.env` 里配置的值补进环境变量，使三处读到同一地址。
    只补空缺 —— 已存在的环境变量优先，部署侧 drop-in 不会被覆盖。
    """
    for env_name, value in (
        ("CLOUD_BASE_URL", cfg.cloud_base_url),
        ("CLOUD_API_KEY", cfg.cloud_api_key),
    ):
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value
