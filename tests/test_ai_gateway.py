import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from bibit_intent.config import AgentConfig
from bibit_intent.execution.cancellation import EdgeExecutionCancelled
from bibit_intent.models.backends import embedding_client, local_client, model_manager
from bibit_intent.models.gateway import GatewayEmbeddingClient, GatewayLocalModelClient, GatewayModelManager
from bibit_intent.models.gateway_setup import register_model
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.models.gateway_artifacts import MODEL_LOAD_ARGS
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.embedding import EmbeddingClient, EmbeddingRetriever


@pytest.fixture
def gateway():
    state = SimpleNamespace(calls=[], loaded=set(), rows={
        "llm": {"id": "glm-edge-1.5b-chat-q4_0", "source_type": "local_url", "status": "downloaded"},
        "embed": {"id": "qwen3-embedding-0.6b-q4_0", "source_type": "local_url", "status": "downloaded"},
    }, embedding=[0.6, 0.8], api_key="test-secret", headers=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length)) if length else None
            state.calls.append((self.command, self.path, payload))
            state.headers.append(dict(self.headers))
            if self.headers.get("X-API-Key") != state.api_key:
                return self.respond(401, {"detail": "unauthorized"})
            if self.path == "/v1/embed/embeddings":
                return self.respond(200, {"data": [{"index": 0, "embedding": state.embedding}]})
            if self.path == "/v1/llm/chat/completions":
                return self.respond(200, {"choices": [{"message": {"content": "[3,4]"}}]})
            if self.path == "/v1/llm/completions":
                return self.respond(200, {
                    'choices': [{'text': 'CALL mute on=true', 'logprobs': {'content': [
                        {"token": "C", "logprob": math.log(0.9), "top_logprobs": [
                        {"token": "C", "logprob": math.log(0.9)}, {"token": "E", "logprob": math.log(0.1)},
                    ]}]}}], 'usage': {'prompt_tokens': 3},
                })
            if '/native/' in self.path or '/local/' in self.path or self.path.endswith('/health'):
                return self.respond(404, {})
            domain = self.path.split("/")[2]
            row = state.rows[domain]
            if self.path.endswith("/models"):
                return self.respond(200, [row] if row else [])
            if self.path.endswith("/load"):
                state.loaded.add(domain)
                row["status"] = "loaded"
            elif self.path.endswith("/unload"):
                state.loaded.discard(domain)
                row["status"] = "downloaded"
            elif self.path.endswith("/register"):
                state.rows[domain] = {"id": payload["model"], **payload, "status": "downloaded"}
            return self.respond(200, {"status": "ok"})

        def respond(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    state.config = PipelineSettings(inference_backend="ai_gateway", gateway_url=state.url,
                                    gateway_api_key=state.api_key)
    yield state
    server.shutdown()
    server.server_close()
    worker.join()


def test_factory_defaults_are_gateway_only():
    settings = PipelineSettings.from_environment({})
    assert settings.gateway_api_key == ""
    assert type(local_client(settings)) is GatewayLocalModelClient
    assert type(embedding_client(settings)) is GatewayEmbeddingClient
    assert isinstance(model_manager(settings), GatewayModelManager)
    cli = AgentConfig(_env_file=None)
    assert local_client(cli).timeout == 30
    assert embedding_client(cli).timeout == 10
    with pytest.raises(ValueError):
        AgentConfig(_env_file=None, inference_backend="llama_cpp")
    with pytest.raises(ValueError, match="Only ai_gateway"):
        PipelineSettings.from_environment({"BIBIT_INTENT_INFERENCE_BACKEND": "llama_cpp"})
    with pytest.raises(TypeError):
        LocalModelClient()
    with pytest.raises(TypeError):
        EmbeddingClient()


def test_gateway_configuration_and_factories(gateway, tmp_path):
    assert isinstance(local_client(gateway.config), GatewayLocalModelClient)
    assert isinstance(embedding_client(gateway.config), GatewayEmbeddingClient)
    assert isinstance(model_manager(gateway.config), GatewayModelManager)
    assert gateway.api_key not in repr(gateway.config)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kwargs", [
    {"inference_backend": "typo"},
    {"gateway_url": "file:///tmp/foo"},
    {"gateway_url": "http://user:password@localhost"},
    {"gateway_url": "http://localhost/v1"},
    {"gateway_llm_model": ""},
    {"gateway_embed_model": "../escape"},
])
def test_bad_configuration_fails_closed(kwargs):
    with pytest.raises(ValueError):
        PipelineSettings(**{"inference_backend": "ai_gateway", **kwargs})


def test_loopback_deployment_defaults_to_no_key():
    import yaml

    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "deploy/ai-gateway-bibit.yaml").read_text())
    assert config["app"]["host"] == "127.0.0.1"
    assert config["auth"] == {"enabled": False, "api_keys": []}
    for args in (config["embed"]["default_args"], MODEL_LOAD_ARGS["embed"]):
        assert args.count("--cache-ram") == 1
        assert args[args.index("--cache-ram") + 1] == "0"
    assert "--cache-ram" not in MODEL_LOAD_ARGS["llm"]
    assert "BIBIT_INTENT_GATEWAY_API_KEY=\n" in (
        root / "deploy/ai-gateway-inference.env.example").read_text()


def test_empty_key_omits_authentication_header(gateway):
    gateway.api_key = None
    settings = PipelineSettings(gateway_url=gateway.url, gateway_api_key="")
    assert embedding_client(settings).embed("你好") == [0.6, 0.8]
    assert local_client(settings).is_available()
    assert gateway.headers
    assert all("X-API-Key" not in headers for headers in gateway.headers)


def test_cli_and_service_use_same_backend_environment(monkeypatch):
    monkeypatch.setenv("BIBIT_INTENT_INFERENCE_BACKEND", "ai_gateway")
    monkeypatch.setenv("BIBIT_INTENT_GATEWAY_LLM_MODEL", "my-model")
    monkeypatch.setenv("BIBIT_INTENT_GATEWAY_API_KEY", "secret")
    cli = AgentConfig(_env_file=None)
    service = PipelineSettings.from_environment()
    assert cli.inference_backend == service.inference_backend == "ai_gateway"
    assert cli.gateway_llm_model == service.gateway_llm_model == "my-model"
    assert cli.gateway_api_key == service.gateway_api_key == "secret"
    assert "secret" not in repr(cli)


def test_public_completion_preserves_grammar_probabilities_and_token_budget(gateway):
    client = local_client(gateway.config)
    assert client.is_available()
    result = client.infer("静音", "mute on=bool", n_probs=10)
    assert result.is_call and result.tool_name == "mute"
    assert result.p_call == pytest.approx(0.9)
    completion = next(body for method, path, body in gateway.calls if body and 'grammar' in body)
    assert completion["model"] == "glm-edge-1.5b-chat-q4_0"
    assert completion['logprobs'] == 10
    assert "root" in completion["grammar"]
    assert completion['max_tokens'] > 0
    assert 'n_probs' not in completion and 'n_predict' not in completion
    assert any(body and body.get('max_tokens') == 1 for _, path, body in gateway.calls)
    assert not any('/native/' in path or path.endswith('/health') for _, path, _ in gateway.calls)
    assert not any("download" in path for _, path, _ in gateway.calls)


def test_gateway_restart_reloads_with_original_arguments(gateway):
    client = local_client(gateway.config)
    assert client.is_available()
    gateway.loaded.clear()
    gateway.rows['llm']['status'] = 'downloaded'
    assert client.is_available()
    loads = [payload for _, path, payload in gateway.calls if path.endswith("/load")]
    assert len(loads) == 2
    assert all(payload["extra_args"] == list(MODEL_LOAD_ARGS["llm"]) for payload in loads)


def test_gateway_without_custom_routes_is_supported(gateway):
    assert local_client(gateway.config).is_available()
    assert embedding_client(gateway.config).embed('hello') == [0.6, 0.8]
    assert not any('/native/' in path or '/local/' in path or path.endswith('/health')
                   for _, path, _ in gateway.calls)


def test_classic_completion_probabilities_are_converted():
    result = GatewayLocalModelClient._probabilities({
        'tokens': ['C'], 'token_logprobs': [math.log(0.9)],
        'top_logprobs': [{'C': math.log(0.9), 'E': math.log(0.1)}],
    })
    assert result[0]['token'] == 'C'
    assert result[0]['top_logprobs'][1] == {'token': 'E', 'logprob': math.log(0.1)}


@pytest.mark.parametrize('scores', [
    ['bad'], {'content': ['bad']}, {'content': [{'token': 'C', 'logprob': float('nan')}]},
    {'tokens': ['C'], 'token_logprobs': [], 'top_logprobs': []},
])
def test_invalid_completion_probabilities_fail_closed(scores):
    with pytest.raises(ValueError):
        GatewayLocalModelClient._probabilities(scores)


def test_missing_probabilities_escalates_instead_of_inventing_confidence(gateway, monkeypatch):
    client = local_client(gateway.config)
    request = client.transport.request

    def missing_scores(path, payload=None, **kwargs):
        if path == '/v1/llm/completions':
            return {'choices': [{'text': 'CALL mute on=true'}], 'usage': {'prompt_tokens': 3}}
        return request(path, payload, **kwargs)

    monkeypatch.setattr(client.transport, 'request', missing_scores)
    result = client.infer('静音', 'mute on=bool', n_probs=10)
    assert result.action == 'ESCALATE'
    assert result.reason == 'local_server_unavailable'


def test_token_budget_falls_back_when_usage_is_missing(gateway, monkeypatch):
    client = local_client(gateway.config)
    monkeypatch.setattr(client, 'is_available', lambda: True)
    monkeypatch.setattr(client.transport, 'request', lambda *args, **kwargs: {})
    assert client._tokenize('hello') is None


def test_direct_completion_rejects_remote_registration(gateway):
    from urllib.error import URLError

    gateway.rows['llm']['source_type'] = 'remote'
    with pytest.raises(URLError):
        local_client(gateway.config)._post('/completion', {'prompt': 'hello'})
    assert not any(path.endswith('/completions') for _, path, _ in gateway.calls)


def test_remote_models_are_never_loaded(gateway):
    gateway.rows["llm"]["source_type"] = "remote"
    gateway.rows["embed"]["source_type"] = "remote"
    assert not local_client(gateway.config).is_available()
    assert embedding_client(gateway.config).embed("hello") is None
    assert not any(path.endswith("/load") or path.endswith("/embeddings") for _, path, _ in gateway.calls)


def test_embedding_openai_schema_and_explicit_model(gateway):
    client = embedding_client(gateway.config)
    assert client.embed("hello") == [0.6, 0.8]
    payload = next(body for _, path, body in gateway.calls if path == "/v1/embed/embeddings")
    assert payload == {"model": "qwen3-embedding-0.6b-q4_0", "input": "hello", "encoding_format": "float"}
    load = next(body for _, path, body in gateway.calls if path.endswith("/load"))
    assert load["extra_args"] == list(MODEL_LOAD_ARGS["embed"])


@pytest.mark.parametrize("vector", [[], [float("nan")], [float("inf")], [[0.1]], ["bad"]])
def test_malformed_embedding_is_rejected(gateway, vector):
    gateway.embedding = vector
    assert embedding_client(gateway.config).embed("hello") is None


def test_cancellation_is_not_swallowed(gateway, monkeypatch):
    client = embedding_client(gateway.config)
    def cancel(*args, **kwargs):
        raise EdgeExecutionCancelled()
    monkeypatch.setattr(client.transport, "request", cancel)
    with pytest.raises(EdgeExecutionCancelled):
        client.embed("hello")


def test_cache_isolated_by_backend_model_and_revision(tmp_path, gateway):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("")
    cache = tmp_path / "cache.json"
    native = EmbeddingRetriever(SimpleNamespace(cache_identity="llama_cpp:http://127.0.0.1:8082"), corpus, cache_path=cache)
    native._save_cache([])
    gateway_client = embedding_client(gateway.config)
    migrated = EmbeddingRetriever(gateway_client, corpus, cache_path=cache)
    assert migrated._load_cache() is None
    migrated._save_cache([])
    assert migrated._load_cache() == []
    gateway_client.cache_identity += ":new-revision"
    assert migrated._load_cache() is None
    assert native._load_cache() is None


@pytest.mark.asyncio
async def test_lifecycle_is_forwarded_without_legacy_processes(gateway, tmp_path):
    manager = model_manager(gateway.config)
    assert (await manager.invoke("models.start", {"model_type": "llm"}))["accepted"]
    state = await manager.describe()
    assert state["models"][0]["server"] == {"status": "running", "managed": True}
    assert state["models"][0]["port"] is None
    assert (await manager.invoke("models.stop", {"model_type": "llm"}))["accepted"]
    assert not gateway.loaded
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_downloads_are_explicit_and_only_for_url_models(gateway, tmp_path):
    manager = model_manager(gateway.config)
    assert (await manager.invoke("models.download", {"model_type": "llm"}))["accepted"]
    assert (await manager.invoke("models.cancel", {"model_type": "llm"}))["accepted"]
    assert ("POST", "/v1/llm/models/glm-edge-1.5b-chat-q4_0/download", None) in gateway.calls
    assert ("DELETE", "/v1/llm/models/glm-edge-1.5b-chat-q4_0/download", None) in gateway.calls


@pytest.mark.asyncio
async def test_lifecycle_rejects_remote_models_and_unknown_actions(gateway, tmp_path):
    manager = model_manager(gateway.config)
    gateway.rows["llm"]["source_type"] = "remote"
    with pytest.raises(ValueError, match="local"):
        await manager.invoke("models.start", {"model_type": "llm"})
    with pytest.raises(KeyError):
        await manager.invoke("models.delete", {"model_type": "llm"})
    with pytest.raises(ValueError):
        await manager.invoke("models.start", {"model_type": []})
    gateway.rows["llm"] = None
    assert (await manager.invoke("models.start", {"model_type": "llm"}))["code"] == "model_not_registered"


def test_registration_is_idempotent_and_does_not_download(gateway, tmp_path):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"GGUFtest")
    gateway.rows["llm"] = None
    transport = local_client(gateway.config).transport
    register_model(transport, "llm", "bibit-llm", path)
    register_model(transport, "llm", "bibit-llm", path)
    assert len([call for call in gateway.calls if call[1].endswith("/register")]) == 1
    assert not any("download" in path or path.endswith("/load") for _, path, _ in gateway.calls)
    different = tmp_path / "different.gguf"
    different.write_bytes(b"GGUFtest")
    with pytest.raises(ValueError, match="not overwritten"):
        register_model(transport, "llm", "bibit-llm", different)


def test_deployment_is_gateway_only():
    root = Path(__file__).resolve().parents[1]
    for name in ("bibit-intent-llm.service", "bibit-intent-embed.service", "select-model.sh",
                 "download_llm_model.sh", "download_embed_model.sh", "model-priority.conf"):
        assert not (root / "deploy" / name).exists()
    unit = (root / "deploy/bibit-intent.service").read_text()
    assert "EnvironmentFile=-/etc/bibit-intent/inference.env" in unit
    assert "bibit-intent-llm.service" not in unit
    assert "bibit-intent-embed.service" not in unit
    for filename in ("debian/postinst", "deploy/deploy.sh"):
        source = (root / filename).read_text()
        assert "retire-legacy-models.sh" in source
        assert "download_${kind}_model.sh" not in source
    control = (root / "debian/control").read_text()
    assert "spacemit-ai-gateway," in control
    assert "llama.cpp-tools-spacemit," not in control
    assert not (root / "src/bibit_intent/models/model_manager.py").exists()


def test_local_summary_chat_preserves_schema_and_local_transport(gateway, monkeypatch):
    client = local_client(gateway.config)
    calls = []
    original = client.transport.request
    def request(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        return original(path, payload, **kwargs)
    monkeypatch.setattr(client.transport, "request", request)
    schema = {"type": "array", "items": {"type": "integer"}}
    result = client._post("/chat/completions", {
        "messages": [{"role": "user", "content": "文档事实"}], "n_predict": 128,
        "response_format": {"type": "json_schema", "json_schema": {"name": "sentence_ids", "schema": schema}},
    }, local_only=True)
    assert result["content"] == "[3,4]"
    path, payload, options = next(call for call in calls if call[0].endswith("/chat/completions"))
    assert path == "/v1/llm/chat/completions"
    assert options["local_only"] is True
    assert payload["max_tokens"] == 128
    assert payload["response_format"]["json_schema"]["schema"] == schema
