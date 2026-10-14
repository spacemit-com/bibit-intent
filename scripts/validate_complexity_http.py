"""Exercise the real HTTP facade on an ephemeral loopback port with safe doubles."""
import json
import socket
import threading
import time
from urllib.request import Request, urlopen

import uvicorn

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.pipeline import EmbeddedBibitPipeline
from bibit_intent.resources import resource_path
from bibit_intent.routing.router import Router
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.server import BibitIntentService


def main():
    calls = []
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})

    class ForbiddenModel:
        def is_available(self):
            raise AssertionError("acceptance request reached a model")

    class RecordingExecutor(Executor):
        def execute(self, tool_name, params, **kwargs):
            if tool_name != "set_volume":
                raise AssertionError(f"unexpected local tool: {tool_name}")
            calls.append({"tool": tool_name, "params": params})
            return ExecResult(True, "validation stub; no device mutation")

    router = Router(registry, RuleEngine(registry), ForbiddenModel(), RecordingExecutor(registry))
    pipeline = EmbeddedBibitPipeline(router, registered_tool_count=len(registry.all()))
    server = uvicorn.Server(uvicorn.Config(BibitIntentService(pipeline).create_app(), log_level="error"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    results = []
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("validation HTTP server failed to start")
            time.sleep(.05)
        for text, expected in [
            ("把音量调到60", "rule"), ("set volume to 40", "rule"),
            ("安装curl，前提是磁盘空间足够", "cloud"),
            ("install curl provided there is enough disk space", "cloud"),
            ("open Firefox and frobnicate data", "cloud"),
            ("请把我的名字 dec 写进长期记忆，然后告诉我你用了哪个记忆工具。", "cloud"),
        ]:
            before = len(calls)
            request = Request(f"http://127.0.0.1:{port}/v1/runs", method="POST",
                              headers={"Content-Type": "application/json"},
                              data=json.dumps({"prompt": text, "session_id": "acceptance"}).encode())
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
            if result["route"] != expected:
                raise AssertionError((text, result))
            if expected == "cloud" and (result["handled"] or len(calls) != before):
                raise AssertionError("cloud request was handled or partially executed locally")
            results.append({"text": text, "route": result["route"], "reason": result["reason"]})
        if len(calls) != 2 or [call["params"].get("level") for call in calls] != [60, 40]:
            raise AssertionError(calls)
        print(json.dumps({"passed": len(results), "real_socket": True, "real_tools_executed": False,
                          "results": results, "stub_calls": calls}, ensure_ascii=False, indent=2))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("validation server did not stop")


if __name__ == "__main__":
    main()
