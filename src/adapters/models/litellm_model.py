"""LiteLLM model adapter and lazy on-device FastEmbed factory."""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import warnings

# Prevent LiteLLM from blocking startup on remote price-map network requests
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
warnings.filterwarnings("ignore", message=".*TypedDict class.*ReadOnly.*", category=UserWarning)

from src.harness.agent_loop.run_turn import Complete, Message, ModelReply, ModelRequestError, RequestUsage
from src.harness.tools.base import BaseTool, ToolCall, schema_size, tool_definitions


def _wire_message(message: Message) -> dict:
    wire: dict[str, object] = {"role": message["role"], "content": message["content"]}
    if "call_id" in message:
        wire["tool_call_id"] = message["call_id"]
    if "tool_calls" in message:
        wire["tool_calls"] = [
            {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
            for call in message["tool_calls"]
        ]
    return wire


def load_model_config(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    if not isinstance(config, dict) or not isinstance(config.get("models"), dict):
        raise ValueError("模型配置需要包含 models 对象。")
    return config


def model_labels(config: dict) -> dict[str, str]:
    labels = {}
    for alias, model in config["models"].items():
        if not isinstance(model, dict) or not isinstance(model.get("model"), str):
            labels[alias] = "配置有误"
            continue

        route = model["model"]
        provider, separator, name = route.partition("/")
        label = f"{model.get('provider_label') or provider} / {name}" if separator else route
        key_env = model.get("api_key_env")
        if not isinstance(key_env, str) or not os.environ.get(key_env, "").strip():
            label += "（缺少密钥）"
        labels[alias] = label
    return labels


def create_model(config: dict, alias: str) -> Complete:
    model = config["models"].get(alias)
    if not isinstance(model, dict):
        raise ValueError(f"模型 {alias} 未注册，请检查模型配置。")

    route = model.get("model")
    api_key_env = model.get("api_key_env")
    api_base = model.get("api_base")
    api_base_env = model.get("api_base_env")
    extra_body = model.get("extra_body", {})
    if not isinstance(route, str) or not route.strip():
        raise ValueError(f"模型 {alias} 缺少有效的 model 路由。")
    if not isinstance(api_key_env, str) or not api_key_env.strip():
        raise ValueError(f"模型 {alias} 缺少有效的 api_key_env。")
    if api_base is not None and (not isinstance(api_base, str) or not api_base.strip()):
        raise ValueError(f"模型 {alias} 的 api_base 应为非空字符串。")
    if api_base_env is not None:
        if not isinstance(api_base_env, str) or not api_base_env.strip():
            raise ValueError(f"模型 {alias} 的 api_base_env 应为非空字符串。")
        api_base = os.environ.get(api_base_env, "").strip() or api_base
    if not isinstance(extra_body, dict):
        raise ValueError(f"模型 {alias} 的 extra_body 应为对象。")

    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"模型 {alias} 需要环境变量 {api_key_env}。")

    try:
        from litellm import Timeout, completion
        from openai import OpenAIError
    except ModuleNotFoundError as error:
        raise RuntimeError("请先运行 python3 -m pip install -r requirements.txt。") from error

    def complete(messages: list[Message], tools: tuple[BaseTool, ...], timeout: float) -> ModelReply:
        options = {}
        if tools:
            options["tools"] = tool_definitions(tools)
        try:
            response = completion(
                model=route,
                messages=[_wire_message(message) for message in messages],
                api_key=api_key,
                api_base=api_base,
                extra_body=extra_body,
                timeout=timeout,
                stream=False,
                num_retries=0,
                **options,
            )
        except Timeout as error:
            raise ModelRequestError("模型请求超时。") from error
        except OpenAIError as error:
            status = getattr(error, "status_code", None)
            reason = f"HTTP {status}" if status else type(error).__name__
            raise ModelRequestError(f"模型 {alias} 请求失败（{reason}）。") from error

        if not response.choices:
            raise RuntimeError(f"模型 {alias} 没有返回回复。")

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError("模型回复达到长度限制，请缩小问题范围后重试。")
        if choice.finish_reason not in {"stop", "tool_calls"} or choice.message.role != "assistant":
            raise RuntimeError("模型没有返回完整的对话回复。")
        calls = []
        for call in choice.message.tool_calls or ():
            if call.type != "function" or not isinstance(call.id, str) or not isinstance(call.function.name, str):
                raise RuntimeError("模型返回了无法识别的工具调用。")
            arguments = call.function.arguments
            if not isinstance(arguments, str):
                raise RuntimeError("模型返回了无法回传的工具参数。")
            calls.append(ToolCall(call.id, call.function.name, arguments))
        if choice.finish_reason == "tool_calls" and not calls:
            raise RuntimeError("模型报告了工具调用，但响应中缺少调用内容。")

        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None)
        if cached is None:
            cached = getattr(usage, "prompt_cache_hit_tokens", None)
        return ModelReply(
            choice.message.content,
            tuple(calls),
            RequestUsage(
                model=alias,
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                cached_input_tokens=cached,
                tool_schema_bytes=schema_size(tools) if tools else 0,
            ),
        )

    return complete


def create_memory_embedder(config: dict):
    """Build a lazy, on-device FastEmbed model for memory vectors."""
    settings = config.get("memory_embeddings")
    if settings is False:
        return None
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("memory_embeddings 配置必须是对象。")
    settings = settings or {}
    model_name = settings.get("model", "BAAI/bge-small-zh-v1.5")
    cache_dir = settings.get("cache_dir")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("memory_embeddings.model 必须是非空字符串。")
    if cache_dir is not None and (not isinstance(cache_dir, str) or not cache_dir.strip()):
        raise ValueError("memory_embeddings.cache_dir 必须是非空路径。")

    class LocalMemoryEmbedder:
        @lru_cache(maxsize=1)
        def _model(self):
            try:
                from fastembed import TextEmbedding
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "本地 Memory 向量检索需要安装 FastEmbed；请重新安装 requirements.txt 中的依赖。"
                ) from error
            return TextEmbedding(model_name=model_name.strip(), cache_dir=cache_dir)

        def __call__(self, text: str) -> tuple[str, list[float]] | None:
            return self.embed_many([text])[0]

        def embed_many(self, texts: list[str]) -> list[tuple[str, list[float]] | None]:
            vectors = self._model().embed(texts)
            return [
                (self.model_name, [float(value) for value in vector.tolist()])
                for vector in vectors
            ]

    LocalMemoryEmbedder.model_name = f"fastembed:{model_name.strip()}"
    return LocalMemoryEmbedder()
