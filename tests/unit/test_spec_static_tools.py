"""Static tools are validated at publish as strictly as the model will be.

DESIGN.md §4: validation happens at publish. A ``CodeTool`` or ``HttpTool``
name reaches the provider verbatim, an HTTP tool's URL template decides
where a credential is sent, and a static name can wear the prefix an MCP
server or A2A peer mints for its own tools. Each of these used to publish
cleanly and fail, or misbehave, at the first turn.
"""

from __future__ import annotations

import pytest

from psych_runtime.core.spec import A2APeer, AgentSpec, CodeTool, HttpTool, McpServer, ModelRef

pytestmark = pytest.mark.unit


def _agent(**kwargs: object) -> AgentSpec:
    base: dict[str, object] = {
        "name": "support",
        "instructions": "Help.",
        "model": ModelRef(model="fake-standard"),
    }
    base.update(kwargs)
    return AgentSpec(**base)  # type: ignore[arg-type]


def _http(name: str = "fetch", url: str = "https://api.example.com/items/{id}") -> HttpTool:
    return HttpTool(name=name, description="fetch a thing", url=url)


class TestStaticToolNamesMatchTheProvidersConstraint:
    def test_a_dotted_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="pattern"):
            CodeTool(name="a.b")

    def test_a_name_over_64_characters_is_refused(self) -> None:
        CodeTool(name="x" * 64)
        with pytest.raises(ValueError, match="pattern"):
            CodeTool(name="x" * 65)

    def test_the_same_limit_applies_to_http_tools(self) -> None:
        with pytest.raises(ValueError, match="pattern"):
            _http(name="search.issues")

    def test_ordinary_names_still_pass(self) -> None:
        assert CodeTool(name="look_up-order").name == "look_up-order"


class TestHttpToolPlaceholdersStayInThePath:
    def test_a_placeholder_in_the_host_is_refused(self) -> None:
        with pytest.raises(ValueError, match="host"):
            _http(url="https://{region}.api.example.com/items")

    def test_a_placeholder_for_the_scheme_is_refused(self) -> None:
        with pytest.raises(ValueError, match="scheme"):
            _http(url="{scheme}://api.example.com/items")

    def test_a_placeholder_in_the_path_or_query_is_fine(self) -> None:
        assert _http(url="https://api.example.com/items/{id}?q={q}").url.endswith("{q}")


class TestStaticNamesDoNotCollideWithMintedOnes:
    def test_a_code_tool_named_for_an_mcp_server_is_refused(self) -> None:
        with pytest.raises(ValueError, match="minted"):
            _agent(
                tools=(CodeTool(name="github__search_issues"),),
                mcp_servers=(McpServer(name="github", url="https://mcp.example.com/"),),
            )

    def test_a_code_tool_named_for_an_a2a_peer_is_refused(self) -> None:
        with pytest.raises(ValueError, match="minted"):
            _agent(
                tools=(CodeTool(name="research__summarise"),),
                a2a_peers=(A2APeer(name="research", url="https://peer.example.com/"),),
            )

    def test_a_similar_but_unrelated_name_is_fine(self) -> None:
        spec = _agent(
            tools=(CodeTool(name="github_search"),),
            mcp_servers=(McpServer(name="github", url="https://mcp.example.com/"),),
        )
        assert spec.tools[0].name == "github_search"
