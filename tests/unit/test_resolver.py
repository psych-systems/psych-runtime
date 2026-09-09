import pytest

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, ModelRef
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver


async def test_duplicate_final_tool_names_are_refused() -> None:
    resolver = ToolResolver(ToolRegistry())
    spec = AgentSpec(name="agent", model=ModelRef(model="fake-standard"))
    duplicate = ToolDefinition(name="same_name")

    with pytest.raises(AccessDenied, match="more than one callable tool"):
        await resolver.resolve(
            spec,
            Scope(tenant="acme"),
            extra=(duplicate, duplicate),
        )
