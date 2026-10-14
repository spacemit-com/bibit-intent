"""Construct matching inference and lifecycle clients from one configuration."""
from .gateway import (
    GatewayEmbeddingClient,
    GatewayLocalModelClient,
    GatewayModelManager,
    validate_gateway_config,
)


def validate_backend(config) -> None:
    validate_gateway_config(config.inference_backend, config.gateway_url,
                            config.gateway_llm_model, config.gateway_embed_model)


def local_client(config):
    validate_backend(config)
    timeout = getattr(config, "local_timeout_seconds", 30)
    return GatewayLocalModelClient(
        config.gateway_url, config.gateway_llm_model,
        api_key=config.gateway_api_key, context_limit=config.context_limit, timeout=timeout,
    )


def embedding_client(config):
    validate_backend(config)
    timeout = getattr(config, "local_timeout_seconds", 10)
    return GatewayEmbeddingClient(
        config.gateway_url, config.gateway_embed_model,
        api_key=config.gateway_api_key, timeout=timeout, revision=config.gateway_embed_revision,
    )


def model_manager(config):
    validate_backend(config)
    return GatewayModelManager(config.gateway_url, config.gateway_llm_model,
                               config.gateway_embed_model, config.gateway_api_key)
