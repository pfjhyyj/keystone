"""Import-graph helpers shared by architecture checks."""

import ast
from importlib.util import resolve_name


def imports(tree, package):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            yield module
            yield from (f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
