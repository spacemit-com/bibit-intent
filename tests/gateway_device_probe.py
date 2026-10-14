"""Read-only real-model baseline/comparison probe; never executes generated tools."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time
from urllib.error import HTTPError



def probe(backend: str) -> dict:
    from bibit_intent.models.backends import embedding_client, local_client
    from bibit_intent.pipeline import PipelineSettings
    config = PipelineSettings.from_environment()
    if config.inference_backend != "ai_gateway":
        raise ValueError("Set BIBIT_INTENT_INFERENCE_BACKEND=ai_gateway")
    local = local_client(config)
    embed = embedding_client(config)
    assert local.is_available(), "LLM is not ready"
    assert embed.is_available(), "Embedding model is not ready"
    started = time.monotonic()
    response = local._post("/completion", {
        "prompt": "请输出 CALL mute on=true", "grammar": 'root ::= "CALL mute on=true"',
        "n_probs": 10, "n_predict": 30, "temperature": 0,
    })
    assert response["content"].strip() == "CALL mute on=true", response
    assert response.get("completion_probabilities"), "Missing completion token probabilities"
    token_count = local._tokenize("你好，帮我打开浏览器")
    assert token_count is not None and token_count > 0
    vectors = {}
    for text in ("帮我打开浏览器", "你知道我密码吗，为什么你能安装file2md app", "查看当前系统版本"):
        vector = embed.embed(text)
        assert vector and all(math.isfinite(value) for value in vector)
        vectors[text] = vector
    return {
        "backend": backend, "completion": response["content"],
        "probabilities_count": len(response["completion_probabilities"]),
        "probability_keys": sorted(response["completion_probabilities"][0]),
        "token_count": token_count, "vectors": vectors,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def compare(baseline: dict, current: dict) -> dict:
    assert baseline["token_count"] == current["token_count"]
    assert baseline["completion"] == current["completion"]
    comparisons = {}
    for text, before in baseline["vectors"].items():
        after = current["vectors"][text]
        assert len(before) == len(after)
        cosine = sum(left * right for left, right in zip(before, after)) / (
            math.sqrt(sum(value * value for value in before)) * math.sqrt(sum(value * value for value in after))
        )
        maximum_error = max(abs(left - right) for left, right in zip(before, after))
        assert cosine > 0.99999, (text, cosine)
        assert maximum_error < 0.0001, (text, maximum_error)
        comparisons[text] = {"dimension": len(after), "cosine": cosine, "max_abs_error": maximum_error}
    return comparisons


def check_explanations() -> list[dict]:
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.core.request_semantics import non_command_reason
    from bibit_intent.resources import resource_path
    from bibit_intent.routing.rule_engine import RuleEngine

    rule = RuleEngine(ToolRegistry(resource_path("tools.yaml")))
    results = []
    for prompt in (
        "你知道我密码吗，为什么你能安装file2md app",
        "为什么你可以安装软件，不需要我输入密码吗",
    ):
        reason = non_command_reason(prompt)
        match = rule.match(prompt)
        assert reason == "open_question" and match is None, (prompt, reason, match)
        results.append({"prompt": prompt, "reason": reason})
    return results


def check_gateway_contract() -> dict:
    from bibit_intent.models.gateway import GatewayTransport
    from bibit_intent.pipeline import PipelineSettings

    config = PipelineSettings.from_environment()
    transport = GatewayTransport(config.gateway_url, config.gateway_api_key)
    statuses = {}

    def expect_error(client, path, status, payload=None):
        try:
            client.request(path, payload)
        except HTTPError as exc:
            with exc:
                assert exc.code == status, (path, exc.code, status)
            return
        raise AssertionError(f"Expected HTTP {status} for {path}")

    if config.gateway_api_key:
        path = '/v1/llm/models'
        expect_error(GatewayTransport(config.gateway_url), path, 401)
        expect_error(GatewayTransport(config.gateway_url, "invalid-probe-key"), path, 403)
        statuses["missing_key"] = 401
        statuses["invalid_key"] = 403
    from bibit_intent.models.gateway import GatewayLocalModelClient, GatewayEmbeddingClient

    client = GatewayLocalModelClient(config.gateway_url, config.gateway_llm_model,
                                     api_key=config.gateway_api_key)
    try:
        client._post('/completion', {'stream': True})
    except ValueError:
        statuses['stream_rejected_by_bibit'] = True
    else:
        raise AssertionError('Bibit must reject streaming completions')
    for domain in ('llm', 'embed'):
        model = f"bibit-validation-remote-{time.time_ns()}"
        transport.request(f"/v1/{domain}/models/register", {
            "model": model, "source_type": "remote", "api_base_url": "https://example.invalid/v1",
        })
        try:
            adapter = GatewayLocalModelClient if domain == 'llm' else GatewayEmbeddingClient
            assert not adapter(config.gateway_url, model, api_key=config.gateway_api_key).is_available()
            statuses[f'{domain}_remote_rejected_by_bibit'] = True
        finally:
            transport.request(f"/v1/{domain}/models/deregister", {"model": model})
    return statuses


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("gateway",), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--check-explanations", action="store_true")
    parser.add_argument("--check-contract", action="store_true")
    args = parser.parse_args()
    result = probe(args.backend)
    if args.baseline:
        result["comparison"] = compare(json.loads(args.baseline.read_text()), result)
    if args.check_explanations:
        result["explanations"] = check_explanations()
    if args.check_contract:
        result["contract"] = check_gateway_contract()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "vectors"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
