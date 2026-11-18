"""AI Gateway adapters; inference and model downloads are delegated to the Gateway."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import ipaddress
import math
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

from bibit_intent.execution.cancellation import interruptible_call
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.models.gateway_artifacts import MODEL_LOAD_ARGS
from bibit_intent.routing.embedding import EmbeddingClient


def validate_gateway_config(backend: str, url: str, *models: str) -> None:
    if backend != "ai_gateway":
        raise ValueError("Only ai_gateway is supported; remove legacy inference configuration")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("gateway_url must be an HTTP(S) origin without credentials")
    if any(not model.strip() or "/" in model or model in {".", ".."} for model in models):
        raise ValueError("Gateway model IDs must be nonempty single path segments")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayTransport:
    def __init__(self, base_url: str, api_key: str = "", timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request(self, path: str, payload: dict | None = None, *, method: str | None = None,
                timeout: int | None = None, local_only: bool = False):
        opener = urlopen
        if local_only:
            parsed = urlsplit(self.base_url)
            try:
                loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                loopback = False
            if not loopback or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                raise ValueError("文件总结只允许连接本机 IP 地址上的模型网关")
            opener = build_opener(ProxyHandler({}), _NoRedirect()).open
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = Request(
            f"{self.base_url}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers=headers, method=method,
        )

        def read():
            with opener(request, timeout=timeout or self.timeout) as response:
                return json.load(response)

        return interruptible_call(read)

    def health(self, domain: str, model: str, *, load: bool = False) -> bool:
        path = f"/v1/{domain}/models"
        rows = self.request(path, timeout=3)
        row = next((item for item in rows if item['id'] == model), None)
        if (not row or row.get('source_type') not in {'local_path', 'local_url'}
                or row.get('api_base_url')):
            return False
        if row.get('status') != 'loaded' and load:
            self.request(f"/v1/{domain}/models/load", {
                "model": model, "extra_args": list(MODEL_LOAD_ARGS[domain]),
            }, timeout=150)
            rows = self.request(path, timeout=3)
            row = next((item for item in rows if item['id'] == model), None)
        return bool(row and row.get('status') == 'loaded'
                    and row.get('source_type') in {'local_path', 'local_url'}
                    and not row.get('api_base_url'))


class GatewayLocalModelClient(LocalModelClient):
    def __init__(self, base_url: str, model: str, *, api_key: str = "",
                 context_limit: int = 512, timeout: int = 20):
        super().__init__(context_limit, timeout)
        self.model = model
        self.transport = GatewayTransport(base_url, api_key, timeout)

    def is_available(self) -> bool:
        try:
            return self.transport.health("llm", self.model, load=True)
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def _post(self, path: str, payload: dict, *, timeout: int | None = None, local_only: bool = False) -> dict:
        if path not in {'/completion', '/chat/completions'} or payload.get('stream'):
            raise ValueError('Only non-streaming completion is supported')
        if not self.is_available():
            raise URLError('Configured local Gateway model is not loaded')
        request = {**payload, 'model': self.model, 'stream': False}
        if 'n_predict' in request:
            request['max_tokens'] = request.pop('n_predict')
        probabilities = request.pop('n_probs', 0)
        if probabilities:
            request['logprobs'] = probabilities
        endpoint = '/v1/llm/chat/completions' if path == '/chat/completions' else '/v1/llm/completions'
        response = self.transport.request(endpoint, request, timeout=timeout, **({'local_only': True} if local_only else {}))
        try:
            choice = response['choices'][0]
            content = choice['message']['content'] if path == '/chat/completions' else choice['text']
            if not isinstance(content, str):
                raise ValueError('Invalid completion text')
            scores = self._probabilities(choice.get('logprobs'))
            if probabilities and not scores:
                raise ValueError('Gateway completion lacks token probabilities')
            return {'content': content, 'completion_probabilities': scores}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise URLError(f'Invalid Gateway completion response: {exc}') from exc

    @staticmethod
    def _probabilities(logprobs) -> list[dict]:
        if not logprobs:
            return []
        if not isinstance(logprobs, dict):
            raise ValueError('Invalid token probability object')
        if 'content' in logprobs:
            scores = logprobs['content']
        else:
            tokens = logprobs['tokens']
            values = logprobs['token_logprobs']
            alternatives = logprobs.get('top_logprobs') or [{} for _ in tokens]
            if len(tokens) != len(values) or len(tokens) != len(alternatives):
                raise ValueError('Inconsistent token probability lengths')
            scores = [{'token': token, 'logprob': value, 'top_logprobs': [
                {'token': candidate, 'logprob': probability}
                for candidate, probability in (options or {}).items()
            ]} for token, value, options in zip(tokens, values, alternatives)]
        if not isinstance(scores, list):
            raise ValueError('Invalid token probability list')
        for score in scores:
            if not isinstance(score, dict) or not isinstance(score.get('top_logprobs', []), list):
                raise ValueError('Invalid token probability entry')
            for item in [score, *(score.get('top_logprobs') or [])]:
                if not isinstance(item, dict):
                    raise ValueError('Invalid alternative token probability')
                value = item.get('logprob')
                if (not isinstance(item.get('token'), str) or isinstance(value, bool)
                        or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise ValueError('Invalid token probability')
        return scores

    def _tokenize(self, text: str) -> int | None:
        try:
            if not self.is_available():
                return None
            response = self.transport.request('/v1/llm/completions', {
                'model': self.model, 'prompt': text, 'max_tokens': 1,
                'temperature': 0, 'stream': False,
            })
            count = response.get('usage', {}).get('prompt_tokens')
            return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
        except (OSError, ValueError, TypeError, KeyError):
            return None


class GatewayEmbeddingClient(EmbeddingClient):
    def __init__(self, base_url: str, model: str, *, api_key: str = "",
                 timeout: int = 20, revision: str = "1"):
        super().__init__(base_url, timeout)
        self.model = model
        self.transport = GatewayTransport(base_url, api_key, timeout)
        self.cache_identity = f"ai_gateway:{self.base_url}:{model}:{revision}:mean:l2"

    def is_available(self) -> bool:
        try:
            return self.transport.health("embed", self.model, load=True)
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def embed(self, text: str) -> list[float] | None:
        try:
            if not self.is_available():
                return None
            response = self.transport.request("/v1/embed/embeddings", {
                "model": self.model, "input": text, "encoding_format": "float",
            })
            vector = [float(value) for value in response["data"][0]["embedding"]]
            if not vector or not all(math.isfinite(value) for value in vector):
                return None
            return vector
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return None


class GatewayModelManager:
    def __init__(self, base_url: str, llm_model: str, embed_model: str, api_key: str = ""):
        self.transport = GatewayTransport(base_url, api_key)
        self.models = {"llm": llm_model, "embed": embed_model}
        self._locks = {domain: asyncio.Lock() for domain in self.models}

    def _row(self, domain: str) -> dict | None:
        rows = self.transport.request(f"/v1/{domain}/models")
        return next((row for row in rows if row["id"] == self.models[domain]), None)

    @staticmethod
    def _require_local(row: dict) -> None:
        if row["source_type"] not in {"local_path", "local_url"} or row.get("api_base_url"):
            raise ValueError("Bibit requires a local gateway model")

    def _describe(self) -> dict:
        models = []
        for domain, model in self.models.items():
            row = self._row(domain)
            status = row["status"] if row else "unregistered"
            ready = False
            if row:
                self._require_local(row)
                if status == "loaded":
                    try:
                        ready = self.transport.health(domain, model)
                    except (OSError, ValueError):
                        ready = False
            complete = status in {"downloaded", "loading", "loaded"}
            progress = 100 if complete else int(float((row or {}).get("download_progress") or 0) * 100)
            models.append({
                "model_type": domain, "name": model, "port": None,
                "download": {
                    "status": "completed" if complete else {
                        "available": "idle", "unregistered": "idle",
                    }.get(status, status),
                    "downloaded": 0, "total": 0, "progress": max(0, min(100, progress)),
                },
                "server": {"status": "running" if ready else "starting" if status == "loading" else "stopped",
                           "managed": True},
                "backend": "ai_gateway", "registered": row is not None,
            })
        return {"models": models, "actions": (
            "models.download", "models.cancel", "models.start", "models.stop",
        )}

    async def describe(self) -> Mapping[str, object]:
        return await asyncio.to_thread(self._describe)

    def _invoke(self, action: str, domain: str) -> dict:
        row = self._row(domain)
        if row is None:
            return {"accepted": False, "code": "model_not_registered"}
        self._require_local(row)
        model = self.models[domain]
        prefix = f"/v1/{domain}"
        if action == "models.start":
            self.transport.request(f"{prefix}/models/load", {
                "model": model, "extra_args": list(MODEL_LOAD_ARGS[domain]),
            }, timeout=150)
        elif action == "models.stop":
            self.transport.request(f"{prefix}/models/unload", {"model": model})
        else:
            if row["source_type"] != "local_url":
                return {"accepted": False, "code": "download_not_supported"}
            self.transport.request(
                f"{prefix}/models/{quote(model, safe='')}/download",
                method="POST" if action == "models.download" else "DELETE",
            )
        return {"accepted": True, "backend": "ai_gateway"}

    async def invoke(self, action: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        if (set(payload) != {"model_type"} or not isinstance(payload.get("model_type"), str)
                or payload["model_type"] not in self.models):
            raise ValueError("payload must contain only model_type: llm or embed")
        if action not in {"models.download", "models.cancel", "models.start", "models.stop"}:
            raise KeyError(action)
        domain = str(payload["model_type"])
        async with self._locks[domain]:
            return await asyncio.to_thread(self._invoke, action, domain)
