import json
import os
from pathlib import Path

from src.harness.agent_loop.run_turn import Complete, Message


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

    def complete(messages: list[Message]) -> str | None:
        try:
            response = completion(
                model=route,
                messages=messages,
                api_key=api_key,
                api_base=api_base,
                extra_body=extra_body,
                timeout=60,
                stream=False,
            )
        except Timeout as error:
            raise TimeoutError("模型请求超时。") from error
        except OpenAIError as error:
            status = getattr(error, "status_code", None)
            reason = f"HTTP {status}" if status else type(error).__name__
            raise RuntimeError(f"模型 {alias} 请求失败（{reason}）。") from error

        if not response.choices:
            raise RuntimeError(f"模型 {alias} 没有返回回复。")

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError("模型回复达到长度限制，请缩小问题范围后重试。")
        if choice.finish_reason != "stop" or choice.message.role != "assistant":
            raise RuntimeError("模型没有返回完整的对话回复。")
        return choice.message.content

    return complete
