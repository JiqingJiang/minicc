"""Built-in tool collection and its default registry."""

from pathlib import Path

from src.harness.permission.workspace import Workspace
from src.harness.tools.builtins.bash import BashTool
from src.harness.tools.builtins.edit import EditTool
from src.harness.tools.builtins.glob import GlobTool
from src.harness.tools.builtins.grep import GrepTool
from src.harness.tools.builtins.read import ReadTool
from src.harness.tools.builtins.tool_search import ToolSearchTool
from src.harness.tools.builtins.write import WriteTool
from src.harness.tools.registry import ToolRegistry


def create_builtin_registry(root: Path, *, use_sandbox: bool = True) -> ToolRegistry:
    workspace = Workspace(root)
    registry = ToolRegistry()
    for tool in (ReadTool(workspace), WriteTool(workspace), EditTool(workspace)):
        registry.register(tool)
    registry.register(ToolSearchTool(registry))
    for tool in (BashTool(workspace.root, use_sandbox=use_sandbox), GlobTool(workspace), GrepTool(workspace)):
        registry.register(tool)
    return registry
