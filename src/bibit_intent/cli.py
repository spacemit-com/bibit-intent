"""bibit-intent CLI entrypoint."""
from __future__ import annotations

import os
import sys
from dataclasses import replace

from bibit_intent.resources import resource_path
from bibit_intent.execution.executor import Executor
from bibit_intent.knowledge.knowledge import BM25KnowledgeBase
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.core.session import SessionState
from bibit_intent.core.registry import ToolRegistry


def _confirm(action: str) -> bool:
    try:
        ans = input(f"⚠️  确认执行「{action}」? [y/N] ").strip().lower()
        return ans in ("y", "yes")
    except EOFError:
        return False


def build_router(cfg) -> Router:
    registry = ToolRegistry(resource_path("tools.yaml"))

    from bibit_intent.execution.system_tools import HANDLERS as _HANDLERS
    from bibit_intent.execution.skill_loader import load_skills
    handlers = dict(_HANDLERS)
    loaded = load_skills(resource_path("skills"), registry, handlers)
    for ls in loaded:
        warn = f"（缺少依赖: {', '.join(ls.missing_deps)}）" if ls.missing_deps else ""
        print(f"[Skill] 加载 {ls.name} v{ls.version}: {len(ls.tool_names)} 工具{warn}", file=sys.stderr)

    rule = RuleEngine(registry)
    local = local_client(cfg)
    executor = Executor(registry, confirm_callback=_confirm, handlers=handlers)

    from bibit_intent.routing.embedding import EmbeddingRetriever
    embed = EmbeddingRetriever(
        embedding_client(cfg),
        [resource_path(p.strip()) for p in cfg.embed_index.split(",") if p.strip()],
    )
    from bibit_intent.routing.question_detector import QuestionDetector
    rule.attach_question_detector(
        QuestionDetector(
            EmbeddingRetriever(
                embedding_client(cfg),
                [resource_path("question_corpus.jsonl")],
            )
        )
    )

    kb = BM25KnowledgeBase()
    kb_path = resource_path(cfg.kb_dir)
    if kb_path.is_dir():
        n = kb.load_dir(kb_path)
        print(f"[知识库] 加载 {n} 篇文档", file=sys.stderr)

    cloud = None
    if cfg.cloud_api_key:
        from bibit_intent.models.cloud_model import CloudModelClient
        cloud = CloudModelClient(cfg, registry)

    return Router(registry, rule, local, executor, kb=kb, cloud_client=cloud, embed=embed,
                  complexity_thresholds=cfg.complexity_thresholds)


_STAGE_LABEL = {
    Stage.RULE: "①规则",
    Stage.LOCAL: "②端侧",
    Stage.CLOUD: "③云端",
    Stage.SLOT_FILL: "⓪回填",
}


def handle_once(router: Router, text: str, session: SessionState | None = None) -> None:
    r = router.handle(text, session=session)
    label = _STAGE_LABEL.get(r.stage, r.stage)
    res = r.exec_result
    if res and res.need_param:
        print(f"[{label} {r.latency_ms:.0f}ms] ✗ {res.message}")
        if session is not None and session.pending is not None:
            print("    （已挂起该指令，直接回复补充值即可，如 60 / vim / pkg=值）")
        else:
            print(f"    （请补充参数 {res.need_param} 后重发指令）")
        return
    icon = "✓" if (res and res.ok) else "✗"
    msg = res.message if res else "(无结果)"
    print(f"[{label} {r.latency_ms:.0f}ms] {icon} {msg}")
    if res and res.detail:
        print(f"    {res.detail}")


def repl(router: Router) -> None:
    print("端侧 Agent 已就绪，输入指令（exit 退出）")
    session = SessionState()
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text in ("exit", "quit", "退出"):
            break
        if text:
            handle_once(router, text, session=session)


def _cmd_serve(argv: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="bibit-intent serve",
        description="Run bibit-intent as a local HTTP/JSON service",
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get("BIBIT_INTENT_DATA_ROOT", "/opt/bibit-intent"),
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("BIBIT_INTENT_SOCKET", ""),
        help="Unix domain socket path; overrides --bind/--host/--port",
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("BIBIT_INTENT_BIND", ""),
        help="listen address as HOST:PORT",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    from bibit_intent.config import load_config, export_cloud_env
    from bibit_intent.pipeline import EmbeddedBibitPipeline, PipelineSettings
    from bibit_intent.server import run_server

    # 桥接 .env → 环境变量：serve 路径不构造云端客户端，但预检门与
    # 本地诊断都只读环境变量（见 config.export_cloud_env）。
    config = load_config()
    export_cloud_env(config)

    settings = replace(PipelineSettings.from_environment(), complexity_thresholds=config.complexity_thresholds)
    pipeline = EmbeddedBibitPipeline.from_package(
        data_root=args.data_root,
        settings=settings,
    )

    socket = args.socket or None
    host = args.host
    port = args.port
    if args.bind:
        host_port = args.bind.rsplit(":", 1)
        host = host_port[0] or "127.0.0.1"
        if len(host_port) > 1:
            try:
                port = int(host_port[1])
            except ValueError as exc:
                parser.error("--bind must be HOST:PORT")

    run_server(
        pipeline,
        data_root=args.data_root,
        settings=settings,
        socket=socket,
        host=host,
        port=port,
    )


def selftest() -> None:
    registry = ToolRegistry(resource_path("tools.yaml"))
    rule = RuleEngine(registry)
    cases = [
        ("把音量调到60", "set_volume", {"level": 60}),
        ("静音", "mute", {"on": True}),
        ("取消静音", "mute", {"on": False}),
        ("亮度调到30", "set_brightness", {"level": 30}),
        ("重启", "reboot", {}),
        ("今天天气怎么样", None, None),
    ]
    passed = 0
    for text, want_tool, want_slots in cases:
        m = rule.match(text)
        if want_tool is None:
            ok = (m is None) or (m.confidence < rule.CONFIDENCE_THRESHOLD)
        else:
            ok = m is not None and m.tool_name == want_tool and \
                 all(m.slots.get(k) == v for k, v in (want_slots or {}).items())
        print(f"  {'✓' if ok else '✗'} {text!r} → "
              f"{m.tool_name if m else None} {m.slots if m else {}}")
        passed += ok
    print(f"\n规则引擎自检：{passed}/{len(cases)} 通过")
    sys.exit(0 if passed == len(cases) else 1)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        selftest()
        return
    if args and args[0] == "serve":
        _cmd_serve(args[1:])
        return

    from bibit_intent.config import load_config, export_cloud_env
    cfg = load_config()
    export_cloud_env(cfg)
    router = build_router(cfg)

    if args:
        handle_once(router, " ".join(args))
    else:
        repl(router)


if __name__ == "__main__":
    main()
