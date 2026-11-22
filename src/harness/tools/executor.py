import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from jsonschema import Draft202012Validator, FormatChecker, SchemaError, ValidationError

from src.harness.tools.base import BaseTool, ToolCall, ToolFailure, ToolResult, ToolTimeout, schema_size


_MAX_RESULT_CHARS = 8_000
_MAX_ARGUMENT_BYTES = 64 * 1024
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass
class PendingApproval:
    call: ToolCall
    preview: str
    _run: Callable[[Callable[[], None]], str] = field(repr=False)
    _arguments: dict = field(default_factory=dict, repr=False)
    _on_start: Callable[[ToolCall, dict], None] | None = field(default=None, repr=False)
    _resolved: bool = field(default=False, init=False, repr=False)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _check_references(value: object) -> None:
    """Reject references which would make a provider dereference the network."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                if not isinstance(inner, str) or not inner.startswith("#"):
                    raise ToolFailure(
                        "INVALID_TOOL_DEFINITION",
                        "Tool schemas may only reference definitions in the same schema.",
                    )
            _check_references(inner)
    elif isinstance(value, list):
        for inner in value:
            _check_references(inner)


def _validated_tool(tool: BaseTool) -> Draft202012Validator:
    """Validate provider-facing metadata and return the argument validator.

    Registration intentionally stays dumb. A tool can be constructed by an
    integration without going through a registry, so this check belongs at
    the boundary where definitions are offered to a model and arguments are
    about to be executed.
    """
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
        raise ToolFailure("INVALID_TOOL_DEFINITION", "Tool name must match [A-Za-z0-9_-]{1,64}.")

    description = getattr(tool, "description", None)
    if not isinstance(description, str) or not description.strip():
        raise ToolFailure("INVALID_TOOL_DEFINITION", "Tool description must be a non-empty string.")

    parameters = getattr(tool, "parameters", None)
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ToolFailure(
            "INVALID_TOOL_DEFINITION",
            "Tool argument schema must be a JSON object schema.",
        )
    try:
        # This is also the check that makes schema_size() safe to use for the
        # provider payload. allow_nan=False prevents NaN/Infinity from
        # silently becoming non-standard JSON.
        json.dumps(parameters, ensure_ascii=False, allow_nan=False)
        _check_references(parameters)
        Draft202012Validator.check_schema(parameters)
        return Draft202012Validator(parameters, format_checker=FormatChecker())
    except ToolFailure:
        raise
    except (TypeError, ValueError, SchemaError, RecursionError) as error:
        raise ToolFailure("INVALID_TOOL_DEFINITION", "Tool argument schema is invalid.") from error


def _validate_arguments(
    raw: str, tool: BaseTool, validator: Draft202012Validator,
) -> dict:
    try:
        if not isinstance(raw, str):
            raise ToolFailure("INVALID_JSON", "Provide tool arguments as a JSON object.")
        if len(raw.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            raise ToolFailure("ARGUMENTS_TOO_LARGE", "Tool arguments exceed 64 KiB.")
        arguments = json.loads(
            raw, object_pairs_hook=_unique_object, parse_float=_finite_float, parse_constant=_finite_float,
        )
    except ToolFailure:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise ToolFailure("INVALID_JSON", "Provide valid JSON without repeated keys or non-finite numbers.") from error
    if not isinstance(arguments, dict):
        raise ToolFailure("INVALID_ARGUMENTS", "Provide tool arguments as a JSON object.")

    try:
        validator.validate(arguments)
    except ValidationError as error:
        field_name = next(iter(error.path), None)
        properties = tool.parameters.get("properties", {})
        label = f"Field {field_name!r}" if field_name in properties else "Tool arguments"
        message = f"{label} failed schema rule {error.validator!r}."
        if error.validator == "required" and not error.path:
            missing = [name for name in error.validator_value if name not in arguments]
            if missing:
                message = "Missing required field: " + ", ".join(missing)
        raise ToolFailure("INVALID_ARGUMENTS", message[:400]) from error
    except Exception as error:
        # An unresolved local reference is a broken tool definition, not a
        # malformed call. Never expose jsonschema's resolver internals to the
        # model or to the user-facing transcript.
        raise ToolFailure("INVALID_TOOL_DEFINITION", "Tool argument schema could not be resolved.") from error
    return arguments


def fingerprint_arguments(raw: str) -> str:
    """Canonicalize only arguments accepted by the executor's strict JSON parser."""
    if not isinstance(raw, str):
        return repr(raw)
    try:
        if len(raw.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            return raw
    except UnicodeError:
        return raw
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_object, parse_float=_finite_float, parse_constant=_finite_float,
        )
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return raw


def _failure(call: ToolCall, error: Exception) -> ToolResult:
    if isinstance(error, ToolFailure):
        return ToolResult(call.call_id, str(error)[:_MAX_RESULT_CHARS], error.code)
    if isinstance(error, ToolTimeout):
        return ToolResult(call.call_id, "本轮执行时间已用完。", "TOOL_TIMEOUT")
    return ToolResult(call.call_id, "工具执行失败。请检查参数或换一种做法。", "TOOL_ERROR")


class ToolExecutor:
    def __init__(self, on_tool_start: Callable[[ToolCall, dict], None] | None = None) -> None:
        self._on_tool_start = on_tool_start

    def offered_tools(
        self, tools: tuple[BaseTool, ...], *, schema_byte_limit: int = 3_600,
    ) -> tuple[BaseTool, ...]:
        """Return valid tools that fit the provider's compact schema budget.

        Invalid definitions are omitted instead of preventing the whole model
        request. The original order is retained, and a tool which does not
        fit never causes later, smaller tools to be dropped.
        """
        if isinstance(schema_byte_limit, bool) or not isinstance(schema_byte_limit, int):
            raise ValueError("schema_byte_limit must be a positive integer.")
        if schema_byte_limit < 1:
            raise ValueError("schema_byte_limit must be a positive integer.")

        visible: list[BaseTool] = []
        for tool in tools:
            try:
                _validated_tool(tool)
                candidate = (*visible, tool)
                if schema_size(candidate) <= schema_byte_limit:
                    visible.append(tool)
            except (ToolFailure, TypeError, ValueError, RecursionError):
                continue
        return tuple(visible)

    def execute(
        self, call: ToolCall, offered: tuple[BaseTool, ...], checkpoint: Callable[[], None],
    ) -> ToolResult | PendingApproval:
        try:
            checkpoint()
            tool = next((candidate for candidate in offered if candidate.name == call.name), None)
            if tool is None:
                raise ToolFailure("TOOL_NOT_OFFERED", "This tool was not offered in this request.")
            validator = _validated_tool(tool)
            arguments = _validate_arguments(call.arguments, tool, validator)
            run = partial(tool.execute, arguments)
            if tool.requires_approval:
                action = tool.approval(arguments, checkpoint)
                preview = action.preview if action is not None else json.dumps(arguments, ensure_ascii=False, indent=2)
                if not preview.strip() or len(preview.encode("utf-8")) > _MAX_RESULT_CHARS:
                    raise ToolFailure("APPROVAL_PREVIEW_INVALID", "无法完整展示该操作，请缩小操作范围。")
                checkpoint()
                return PendingApproval(
                    call, f"{tool.name}\n{preview}", action.execute if action is not None else run,
                    arguments, self._on_tool_start,
                )
            return self._run(call, run, checkpoint, arguments)
        except Exception as error:
            return _failure(call, error)

    def resume(
        self, pending: PendingApproval, approved: bool, checkpoint: Callable[[], None],
    ) -> ToolResult:
        if pending._resolved:
            return ToolResult(pending.call.call_id, "这项审批已经处理。", "APPROVAL_RESOLVED")
        pending._resolved = True
        if not approved:
            return ToolResult(pending.call.call_id, "用户拒绝了这个操作。请勿原样再次请求。", "APPROVAL_DENIED")
        return self._run(pending.call, pending._run, checkpoint, pending._arguments, pending._on_start)

    def _run(
        self,
        call: ToolCall,
        run: Callable[[Callable[[], None]], str],
        checkpoint: Callable[[], None],
        arguments: dict,
        on_start: Callable[[ToolCall, dict], None] | None = None,
    ) -> ToolResult:
        try:
            checkpoint()
            observer = on_start or self._on_tool_start
            if observer is not None:
                try:
                    observer(call, arguments)
                except Exception:
                    # Terminal logging is observational and must not change tool behavior.
                    pass
            content = run(checkpoint)
            # Enforce the deadline even for a tool that forgot its final checkpoint.
            checkpoint()
            if not isinstance(content, str):
                raise ToolFailure("TOOL_ERROR", "工具返回了无法读取的结果。")
            if len(content) > _MAX_RESULT_CHARS:
                suffix = "\n[结果已截断，请缩小范围。]"
                content = content[:_MAX_RESULT_CHARS - len(suffix)] + suffix
            return ToolResult(call.call_id, content)
        except Exception as error:
            return _failure(call, error)
