"""Turns the HTTP request shape into the ``psych_runtime.AgentSpec`` Psych validates.

Kept separate from ``app.main`` so the one place a request body becomes a
Spec is easy to find, and so ``CreateAgentRequest`` -- a convenience shape for
JSON -- never has to pretend to be Spec-shaped itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psych_runtime
from app.schemas import CompactionIn, CreateAgentRequest, HttpToolIn, McpServerIn


def build_agent_spec(
    body: CreateAgentRequest,
    *,
    oauth_redirect_uri: str | None = None,
    subagent_specs: Mapping[str, psych_runtime.AgentSpec] | None = None,
) -> psych_runtime.AgentSpec:
    """The Spec ``POST /api/agents`` publishes.

    ``oauth_redirect_uri`` is this process's own callback route, supplied by
    the caller rather than typed into a request: the playground knows its own
    address, and an ``authorization_code`` grant published without one fails
    at connect time inside a Run -- after the settings page's own test
    succeeded, because that path supplied it and this one did not.

    ``subagent_specs`` maps each ``body.subagents[*].agent_id`` to that agent's
    current Spec, read by the caller. The child is embedded whole (DESIGN.md
    §17): one Version hash pins the tree, so a crash-recovering Worker replays
    exactly the children the parent was published with.
    """
    resolved = subagent_specs or {}
    options = body.model_options
    return psych_runtime.AgentSpec(
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        model=psych_runtime.ModelRef(
            model=body.model,
            temperature=body.temperature,
            top_p=options.top_p if options else None,
            max_output_tokens=options.max_output_tokens if options else None,
            reasoning_effort=options.reasoning_effort if options else None,
            fallbacks=tuple(options.fallbacks) if options else (),
        ),
        tools=(
            *(psych_runtime.CodeTool(name=name) for name in body.tools),
            *(_build_http_tool(entry) for entry in body.http_tools),
        ),
        subagents=tuple(
            psych_runtime.SubagentRef(
                name=entry.name,
                description=entry.description,
                spec=resolved[entry.agent_id],
            )
            for entry in body.subagents
        ),
        # Sorted here rather than left to the caller: `AgentSpec.skills` is a
        # sorted set, so publishing the same three skills in two different
        # orders must produce one Version rather than two (DESIGN.md §4). The
        # Spec's own validator would sort them anyway; doing it here means the
        # entry this backend records for its own list matches what was
        # published rather than the order somebody happened to type.
        skills=tuple(
            sorted(
                (
                    psych_runtime.Skill(
                        name=item.name, description=item.description, body=item.body
                    )
                    for item in body.skills
                ),
                key=lambda skill: skill.name,
            )
        ),
        mcp_servers=tuple(_build_mcp_server(entry, oauth_redirect_uri) for entry in body.mcp),
        a2a_peers=tuple(
            psych_runtime.A2APeer(
                name=entry.name,
                url=entry.url,
                credential=entry.credential,
                scheme=entry.scheme,
                tenant=entry.tenant,
                allow=tuple(entry.allow),
                optional=entry.optional,
                extensions=tuple(entry.extensions),
            )
            for entry in body.a2a
        ),
        limits=psych_runtime.Limits(**body.limits) if body.limits else psych_runtime.Limits(),
        suspension=psych_runtime.SuspensionPolicy(
            may_ask_questions=body.may_ask_questions,
            **(body.suspension.model_dump() if body.suspension is not None else {}),
        ),
        tasks_enabled=body.tasks_enabled,
        components_enabled=body.components_enabled,
        answer_style=body.answer_style,
        # An envelope, never a roster. Empty `tools` means "whatever this agent
        # itself holds", which is `psych_runtime.tools.narrowing`'s rule everywhere
        # else, and empty `models` means its own model and no other -- a model
        # the author never named is a model they never priced. A child is
        # narrowed against this *and* against what the parent actually holds, so
        # turning the flag on can never widen anything.
        spawn=_build_spawn(body),
        # Absent means off, and off is the default: an agent whose Runs are
        # short should never pay a summarising call it did not need. Built
        # here rather than passed through as a dict so the four bounds are
        # `psych_runtime.CompactionPolicy`'s own and a bad number is rejected at
        # publish rather than at the turn that would have compacted.
        compaction=_build_compaction(body.compaction),
    )


def _build_spawn(body: CreateAgentRequest) -> psych_runtime.SpawnEnvelope | None:
    if not body.subagents_enabled and body.spawn is None:
        return None
    if body.spawn is None:
        return psych_runtime.SpawnEnvelope()
    return psych_runtime.SpawnEnvelope(
        tools=tuple(body.spawn.tools),
        models=tuple(body.spawn.models),
        max_depth=body.spawn.max_depth,
        max_alive=body.spawn.max_alive,
        may_message=body.spawn.may_message,
    )


def _build_http_tool(entry: HttpToolIn) -> psych_runtime.HttpTool:
    return psych_runtime.HttpTool(
        name=entry.name,
        description=entry.description,
        url=entry.url,
        method=entry.method,
        input_schema=entry.input_schema,
        headers=entry.headers,
        credential=entry.credential,
        timeout_seconds=entry.timeout_seconds,
        interruptible=entry.interruptible,
    )


def limits_as_dict(limits: psych_runtime.Limits) -> dict[str, Any]:
    """Every ``Limits`` field, for reporting a published agent back whole."""
    return limits.model_dump()


def _build_compaction(entry: CompactionIn | None) -> psych_runtime.CompactionPolicy | None:
    if entry is None:
        return None
    return psych_runtime.CompactionPolicy(
        trigger_tokens=entry.trigger_tokens,
        keep_recent_turns=entry.keep_recent_turns,
        model=entry.model,
        max_summary_tokens=entry.max_summary_tokens,
        summary_instructions=entry.summary_instructions,
    )


def _build_mcp_server(
    entry: McpServerIn, oauth_redirect_uri: str | None
) -> psych_runtime.McpServer:
    oauth = (
        psych_runtime.McpOAuth(
            grant=entry.oauth.grant,
            preregistered_client_id=entry.oauth.preregistered_client_id,
            client_secret_credential=entry.oauth.client_secret_credential,
            cimd_url=entry.oauth.cimd_url,
            allow_dynamic_registration=entry.oauth.allow_dynamic_registration,
            application_type=entry.oauth.application_type,
            client_name=entry.oauth.client_name,
            # Only for the grant that needs one. `client_credentials` ignores
            # redirect URIs, and putting one in the Spec anyway would change
            # the Version hash of every agent for no behavioural reason.
            redirect_uris=(
                (oauth_redirect_uri,)
                if entry.oauth.grant == "authorization_code" and oauth_redirect_uri
                else ()
            ),
        )
        if entry.oauth is not None
        else None
    )
    return psych_runtime.McpServer(
        name=entry.name,
        url=entry.url,
        transport=entry.transport,
        credential=entry.credential,
        allow=tuple(entry.allow),
        optional=entry.optional,
        preload=entry.preload,
        oauth=oauth,
    )
