from psych_runtime.tools.mcp_names import mcp_tool_name


def test_ordinary_mcp_tool_name_keeps_server_and_tool() -> None:
    assert mcp_tool_name("eq-notification", "get_tool_context") == (
        "eq-notification__get_tool_context"
    )


def test_long_or_provider_invalid_names_are_stable_and_safe() -> None:
    first = mcp_tool_name("records", "find customer / by email" * 8)
    second = mcp_tool_name("records", "find customer / by email" * 8)
    neighbor = mcp_tool_name("records", "find customer / by email" * 7 + "x")

    assert first == second
    assert first != neighbor
    assert len(first) <= 64
    assert first.replace("_", "").isalnum()
