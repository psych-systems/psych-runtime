"""Publishing an agent, in one place, for the three doors that do it.

``POST /api/agents`` is the original. Two more arrived with the catalogue: the
seeder (``app.catalogue_seed``), which publishes twenty-six agents into a new
account, and the ``create_agent``/``update_agent`` tools, which let an agent
build one from a conversation. All three must publish *identically* -- same
validation context, same subagent resolution, same index entry -- or an agent
built one way would be subtly not the agent built another, and the difference
would surface as a Run that behaves unlike the one somebody tested.

So the body of the route moved here, and the route calls it. Nothing in this
module knows about FastAPI or about ``AppState``: it takes the four
collaborators it actually uses, which is also what makes it callable from
``lifespan``-time code that has no request.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

import psych_runtime
from app.errors import ApiProblem, from_pydantic, from_spec_validation
from app.schemas import CreateAgentRequest
from app.spec_builder import build_agent_spec
from app.store_index import (
    AgentEntry,
    CompactionEntry,
    HttpToolEntry,
    PlaygroundIndex,
    SkillEntry,
    SubagentRefEntry,
    new_agent_id,
)
from psych_runtime.core.errors import SpecValidationError
from psych_runtime.core.ids import VersionHash
from psych_runtime.core.spec import CodeTool, HttpTool
from psych_runtime.core.validation import ValidationContext
from psych_runtime.store.port import Store
from psych_runtime.tools.registry import ToolRegistry


class PublishedAgent:
    """What publishing produced, for a caller that wants to answer with it."""

    __slots__ = ("agent_id", "created", "name", "version_hash")

    def __init__(
        self, *, agent_id: str, version_hash: VersionHash, name: str, created: bool
    ) -> None:
        self.agent_id = agent_id
        self.version_hash = version_hash
        self.name = name
        self.created = created


async def publish_agent(
    body: CreateAgentRequest,
    *,
    owner: str,
    store: Store,
    index: PlaygroundIndex,
    registry: ToolRegistry,
    sandbox_profiles: Sequence[str],
    oauth_redirect_uri: str | None,
    default_approval_selectors: Sequence[str],
    catalogue_id: str | None = None,
) -> PublishedAgent:
    """Publish ``body`` as ``owner``'s agent and record it in the index.

    Args:
        body: the request shape, whoever built it.
        owner: the account id. Ownership is this index's dimension, not
            Psych's: ``psych_runtime.publish`` is unscoped because a Version is
            immutable content, so two accounts publishing the same Spec
            correctly share one Version in the Store.
        sandbox_profiles: the profiles this account can actually offer, so an
            agent naming one that is not configured is refused now rather than
            at its first program.
        catalogue_id: provenance, when this agent came from
            ``app.catalogue``. Recorded on the entry so re-seeding skips it and
            a console can label it as shipped rather than hand-built.

    Raises:
        ApiProblem: the same statuses the route raised before this moved --
            404 for an agent or subagent this account does not have, 409 for a
            child with no published Version, 400 for a Spec that failed
            validation.
    """
    # Resolved before anything is published, so an edit naming an agent that is
    # not this account's fails without leaving a Version behind that nothing
    # points at.
    if body.agent_id is None:
        agent_id = new_agent_id()
    else:
        editing = await index.get_agent(owner, body.agent_id)
        if editing is None:
            raise ApiProblem(404, f"no agent {body.agent_id!r}")
        agent_id = body.agent_id
        # Provenance is inherited, not re-declared. `record_publish` writes a
        # fresh entry per Version, so an edit that did not carry this forward
        # would quietly un-mark a catalogue agent -- and the next seed, seeing
        # no `github`, would publish a second one. Every edit path benefits
        # (the route, `update_agent`, a re-seed) without each caller having to
        # remember, which is the point of putting it here.
        if catalogue_id is None:
            catalogue_id = editing[1].catalogue_id

    # Each child is this account's own agent, copied in at its *current*
    # Version. Resolved before anything is published so a roster naming an
    # agent that is not this account's fails without a Version left behind.
    subagent_specs: dict[str, psych_runtime.AgentSpec] = {}
    child_hashes: dict[str, VersionHash] = {}
    for ref in body.subagents:
        if ref.agent_id == agent_id:
            raise ApiProblem(400, f"subagent {ref.name!r} cannot be the agent being published")
        found = await index.get_agent(owner, ref.agent_id)
        if found is None:
            raise ApiProblem(404, f"subagent {ref.name!r} names no agent {ref.agent_id!r}")
        child_version = await store.get_version(found[0].version_hash)
        if child_version is None or not isinstance(child_version.spec, psych_runtime.AgentSpec):
            raise ApiProblem(409, f"agent {ref.agent_id!r} has no published agent Version")
        subagent_specs[ref.agent_id] = child_version.spec
        child_hashes[ref.agent_id] = child_version.hash

    try:
        spec = build_agent_spec(
            body, oauth_redirect_uri=oauth_redirect_uri, subagent_specs=subagent_specs
        )
    except ValidationError as err:
        raise from_pydantic("the agent's fields did not validate", err) from err

    try:
        version = await psych_runtime.publish(
            store,
            spec,
            context=ValidationContext(
                registered_tools=registry.names, sandbox_profiles=tuple(sandbox_profiles)
            ),
        )
    except SpecValidationError as err:
        raise from_spec_validation(err) from err

    approval_selectors = (
        tuple(body.approval_selectors)
        if body.approval_selectors is not None
        else tuple(default_approval_selectors)
    )
    by_name = {ref.name: ref.agent_id for ref in body.subagents}
    created = await index.record_publish(
        AgentEntry(
            version_hash=version.hash,
            agent_id=agent_id,
            owner=owner,
            catalogue_id=catalogue_id,
            name=spec.name,
            description=spec.description,
            instructions=spec.instructions,
            model=spec.model.model,
            model_options=spec.model.model_dump(exclude={"model", "temperature"}),
            tools=tuple(tool.name for tool in spec.tools if isinstance(tool, CodeTool)),
            http_tools=tuple(
                HttpToolEntry(**tool.model_dump(exclude={"kind"}))
                for tool in spec.tools
                if isinstance(tool, HttpTool)
            ),
            # Joined by name, never zipped. `AgentSpec` normalises and sorts
            # its own tuples, so `spec.subagents` is not necessarily in the
            # order the request wrote them, and pairing the two lists
            # positionally silently filed every roster entry's `agent_id`
            # against somebody else's name -- invisible on the two-child
            # rosters this had until the catalogue published one with
            # twenty-five.
            subagents=tuple(
                SubagentRefEntry(
                    name=ref.name,
                    description=ref.description,
                    agent_id=by_name[ref.name],
                    version_hash=str(child_hashes[by_name[ref.name]]),
                )
                for ref in spec.subagents
            ),
            spawn=spec.spawn.model_dump() if spec.spawn is not None else None,
            suspension=spec.suspension.model_dump(exclude={"may_ask_questions"}),
            limits=spec.limits.model_dump(),
            # From the Spec, not from the request body: the Spec is what was
            # actually published, and its validator normalises and sorts. An
            # entry built from the request would disagree with the Version the
            # moment either of those did anything.
            skills=tuple(
                SkillEntry(name=skill.name, description=skill.description, body=skill.body)
                for skill in spec.skills
            ),
            mcp_servers=tuple(server.name for server in spec.mcp_servers),
            a2a_peers=tuple(peer.name for peer in spec.a2a_peers),
            answer_style=spec.answer_style,
            may_ask_questions=spec.suspension.may_ask_questions,
            tasks_enabled=spec.tasks_enabled,
            components_enabled=spec.components_enabled,
            subagents_enabled=spec.spawn is not None,
            compaction=(
                CompactionEntry(
                    trigger_tokens=spec.compaction.trigger_tokens,
                    keep_recent_turns=spec.compaction.keep_recent_turns,
                    model=spec.compaction.model,
                    max_summary_tokens=spec.compaction.max_summary_tokens,
                    summary_instructions=spec.compaction.summary_instructions,
                )
                if spec.compaction is not None
                else None
            ),
            code_execution=(
                spec.code_execution.model_dump(mode="json")
                if spec.code_execution is not None
                else None
            ),
            published_at=version.published_at,
            approval_selectors=approval_selectors,
        ),
        now=datetime.now(UTC),
    )
    return PublishedAgent(
        agent_id=agent_id, version_hash=version.hash, name=spec.name, created=created
    )
