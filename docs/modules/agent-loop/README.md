# Agent Loop

当前切片支持在同一个终端会话里进行多轮文本对话。

- [Runtime](../../../src/runtime/session/chat_session.py) 接收输入，每次组合已完成的历史和新的用户消息。同一会话同时处理一轮。成功时一起提交用户消息和完整回复，失败时可重发原消息。
- [Agent Loop](../../../src/harness/agent_loop/run_turn.py) 用给定消息调用模型能力，返回一条 assistant 消息。本轮没有工具结果需要回填，因此一次模型调用就能结束一轮。
- [模型适配器](../../../src/adapters/models/litellm_model.py) 调用 LiteLLM 的 `completion()`，由 SDK 按模型路由处理上游请求协议，向 Loop 返回统一的文本回复。请求使用非流式模式。
- [启动入口](../../../src/app/chat.py) 将终端、Runtime 和模型适配器装配在同一个本地进程里。会话历史保存在进程内存中。

## 运行与体验

使用 Python 3.10 或以上版本。在虚拟环境中从仓库根目录安装依赖。启动默认模型前，将 DeepSeek API 密钥配置到环境变量 `DEEPSEEK_API_KEY`。

```sh
python3 -m pip install -r requirements.txt
python3 -B -m src.app.chat
```

先发「请在这段对话里记住代号是松果七号，只回复收到。」再发「刚才的代号是什么？」。第二轮应答出「松果七号」。输入 `Ctrl+D` 结束会话。`Ctrl+C` 也会退出，并中止正在等待的请求。

## 注册和切换模型

在 [models.json](../../../models.json) 的 `models` 中按别名添加条目。`model` 填 LiteLLM 路由，`api_key_env` 填供应商密钥所在的环境变量名。`api_base_env` 指定服务地址所在的环境变量，有值时优先使用，否则依次使用 `api_base` 和供应商默认地址。额外请求字段可用 `extra_body`。密钥通过环境变量读取。

默认别名由 `default` 指定。启动前可用 `MINICC_MODEL` 选择已注册的别名，用 `MINICC_MODELS_CONFIG` 指定另一份 JSON 配置。`provider_label` 可以自定义菜单中显示的供应商名称。

聊天中输入 `/model`，再输入编号或模型别名，空行返回。也可以直接输入 `/model bailian`、`/model deepseek-flash` 或 `/model glm`。切换从下一条消息生效，新模型会收到当前会话的历史。缺少密钥的模型会在菜单里标出，切换失败时继续使用当前模型。

| 厂商 | `MINICC_MODEL` | 密钥变量 | 地址变量 |
| --- | --- | --- | --- |
| 阿里云百炼 | `bailian` | `BAILIAN_API_KEY` | `BAILIAN_OPENAI_BASE_URL` |
| DeepSeek | `deepseek-flash` 或 `deepseek-v4-pro` | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` |
| 智谱 GLM | `glm` | `GLM_API_KEY` | `GLM_BASE_URL` |

例如，运行 `MINICC_MODEL=glm python3 -B -m src.app.chat` 启动智谱 GLM。百炼地址使用 Key 所在地域的 OpenAI 兼容地址。GLM Coding Plan 地址通过 `GLM_BASE_URL` 指定。参见 [百炼接口说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) 和 [LiteLLM 的智谱接入说明](https://docs.litellm.ai/docs/providers/bigmodel)。

| 上游协议 | 路由示例 |
| --- | --- |
| Chat Completions | `dashscope/qwen-flash`、`deepseek/deepseek-flash`、`bigmodel/glm-5.3`、`openai/gpt-4.1-mini`，或使用 `openai/<模型 ID>` 加 `api_base` 接入兼容服务 |
| Anthropic Messages | `anthropic/claude-sonnet-5` |
| OpenAI Responses | `openai/responses/gpt-6-luna` |

路由和协议转换由 [LiteLLM Python SDK](https://docs.litellm.ai/docs/python_sdk) 提供。OpenAI Responses 使用 [SDK 的 Chat Completions 到 Responses 转换](https://docs.litellm.ai/docs/providers/openai#openai-chat-completion-to-responses-api-bridge)。本阶段的历史只包含用户和模型的公开文本，后续接入工具或推理状态时需要验证各路由的往返转换。
