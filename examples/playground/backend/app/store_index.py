"""The playground's own bookkeeping: agents published and runs dispatched.

DESIGN.md §7 keeps ``Store`` deliberately thin: conditional writes and range
reads, no ``list_versions``, no ``list_runs``. That is a considered omission,
not a gap -- an index over "every Version ever published" or "every Run for
this tenant" is exactly the kind of thing a platform builds on its own
storage, shaped for its own product, and Psych refuses to guess that shape
for every consumer (it is the same refusal that keeps a database out of
``psych`` at all). This backend IS the platform for the purposes of this
example, so it keeps the two small indexes ``GET /api/agents`` and
``GET /api/runs`` need, alongside -- never inside -- Psych's own Store.

That index is durable. It used to be two dicts rebuilt empty on every boot,
which made a restart read as data loss: the Versions, Runs and Records were
all still in Postgres and still fetchable by id, but the lists that let a
person *find* them came back empty, so the UI showed nothing. Persisting it
here is the honest fix, because the missing durability was always the
platform's to provide -- adding ``list_runs`` to ``Store`` to solve it would
have moved the example's problem into the library and broken §7.

The file is this index's own, separate from ``app.settings_store``'s: that
one holds credentials and is rewritten when a person changes a setting, this
one holds no secrets and is rewritten on every publish and dispatch. Both use
the same atomic ``<path>.tmp`` write, so a crash mid-write never leaves a
truncated file for the next boot to fail on.

Runs also carry the ``approval_selectors`` their agent was created with, read
back by ``app.runtime_router`` at execution time. See that module's docstring
for why that lives here instead of in the Spec Psych itself validates.

## An agent is a name; a Version is what it points at

A Version is immutable and always will be: ``psych_runtime.runtime.execute`` reloads
the pinned Version at the top of every Attempt, including a reclaiming
Worker's, so a Run that could see its Spec change under it would resume a
conversation the model never had. That is DESIGN.md §23's second item and it
is not negotiable.

What *was* negotiable is that this console had no idea an agent existed apart
from a version of one. The list showed Versions, editing meant duplicating,
and flipping one flag produced a second row that looked like a second agent.
So this index now holds two things:

``AgentPointer`` is the agent. It has a stable ``agent_id`` that survives every
edit, a name, the Version it currently points at, and the ordered history of
everything it has ever pointed at. Editing an agent publishes a new Version
and moves the pointer. Docker tags and Git branches are the same arrangement,
and nobody calls a Git branch friction.

``AgentEntry`` is one published Version of one agent, and never changes.

Runs still pin a hash, never a pointer. ``POST /api/runs`` resolves the
pointer once, at admission, and writes the hash it got onto the ``RunEntry``.
Editing the agent afterwards leaves every conversation already underway
running exactly what it started on.

## Owned, and why an entry needs a compound key

Every entry belongs to one account. ``RunEntry`` already carried ``tenant``
and simply had nobody filtering on it; ``AgentEntry`` gained ``owner``.

Version entries are keyed by ``(owner, agent_id, version_hash)`` rather than by
hash alone, and that is not defensive. A Version *is* its content hash, so two
accounts -- or two agents of one account -- that publish byte-identical Specs
get the same hash by design, which is the whole point of content addressing
and correct at the Psych layer: one Version, stored once. At this layer it is
wrong. Keyed by hash alone, the second publish overwrites the first's entry --
its ``published_at``, its ``approval_selectors``, its name -- and deleting one
deletes the other, because they were never two entries.

## This index does not authorize anything

``list_*`` filters by account so a person sees their own work. It is not the
access control, and must never be mistaken for it. A run id is not a secret,
so a request naming one directly is checked against ``RunHeader.scope`` in
Psych's own Store, which is the authoritative record of whose Run it is and
the one thing this file cannot drift from. See ``app.main``'s run lookup.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.ids import RunId, VersionHash
from psych_runtime.store.port import Store

_LOG = logging.getLogger(__name__)


def new_branch_id() -> str:
    """A fresh identity for a branch of a conversation.

    Minted rather than derived. The obvious derivation -- "the id of the Run
    the branch starts at" -- reads fine until a fork is dispatched twice from
    one message, which is exactly what somebody comparing two agents does, and
    then the two branches share a name. An opaque id has no such collision and
    nothing anywhere is tempted to parse it.
    """
    return uuid.uuid4().hex[:12]


def new_conversation_id() -> str:
    """A fresh identity for a whole conversation.

    A conversation is the tree, not a line through it. Every Run in it carries
    this: the first message mints one, a later message inherits it, and so does
    a *branch*, because re-asking a question you already asked is still the
    same conversation. Only a fork mints a new one, because a fork is a new
    conversation by definition.

    This is what the history list is keyed by, and what deleting a chat
    deletes. See ``RunEntry.conversation_id``.
    """
    return uuid.uuid4().hex[:12]


def new_workflow_id() -> str:
    """As ``new_agent_id``, for a workflow."""
    return "wf_" + uuid.uuid4().hex[:12]


def new_agent_id() -> str:
    """A stable, unguessable id for a newly created agent.

    Not the Version hash and not the name. The hash changes on every edit,
    which is the thing this exists to stop mattering; a name is a person's to
    change, and a URL that breaks when somebody fixes a typo is a URL nobody
    trusts.
    """
    return uuid.uuid4().hex[:12]


_PRE_ACCOUNTS_TENANT = "playground"
"""The process-wide tenant every Run was dispatched under before accounts.

Named here only so `load` can recognise those Runs and stop listing them. See
that method for why they cannot simply be reassigned to an account.
"""


class SkillEntry(BaseModel):
    """One skill as this backend remembers it (DESIGN.md §16).

    Held here as well as in the published Spec because the agent list is what
    the console's edit form reads: a Version is immutable and editing an agent
    means publishing a new one, so the form has to be filled from somewhere,
    and reading the Spec back out of the Store for every row of a list would be
    a read per agent per page load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    body: str


class CompactionEntry(BaseModel):
    """An agent's compaction policy as this backend remembers it.

    Held here for the reason `SkillEntry` is: the edit form is filled from the
    agent list, and a policy the list cannot report is four numbers the next
    edit would have to invent. Mirrors `psych_runtime.CompactionPolicy` without
    re-stating its bounds -- what is written here came off a Spec that already
    passed them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_tokens: int
    keep_recent_turns: int
    model: str | None
    max_summary_tokens: int
    summary_instructions: str | None = None
    """Defaulted so an index written before this field existed still loads."""


class SubagentRefEntry(BaseModel):
    """One author-written subagent, as the parent's edit form needs it back:
    which agent it was copied from, and the child hash the parent pins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    agent_id: str
    version_hash: str


class HttpToolEntry(BaseModel):
    """One ``HttpTool`` as published, field for field, for the edit form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    url: str
    method: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    credential: str | None = None
    timeout_seconds: float = 30.0
    interruptible: bool = True


class AgentEntry(BaseModel):
    """One published Version of one agent. Never edited after it is written."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version_hash: VersionHash
    agent_id: str = ""
    """Which agent this is a version of.

    Defaulted so an index written before agents had identities still loads.
    ``PlaygroundIndex.load`` mints an id for every such entry and gives each
    one its own pointer, which is exactly what those entries were: one agent
    apiece, with no history.
    """
    owner: str = ""
    """The account this agent belongs to.

    Defaulted so an index written before accounts existed still loads. An
    entry with no owner is invisible to every account until
    ``PlaygroundIndex.adopt_legacy`` hands it to the first one created, which
    happens at the same moment the settings file's legacy workspace is adopted
    (``app.settings_store.SettingsStore.adopt_legacy``).
    """
    name: str
    description: str = ""
    instructions: str
    model: str
    model_options: dict[str, Any] = Field(default_factory=dict)
    """``ModelRef`` less ``model`` and ``temperature``, as published. A dict
    because it is only ever read back into the form that wrote it."""
    tools: tuple[str, ...]
    http_tools: tuple[HttpToolEntry, ...] = ()
    skills: tuple[SkillEntry, ...] = ()
    """Defaulted so an index written before skills existed still loads."""
    mcp_servers: tuple[str, ...]
    subagents: tuple[SubagentRefEntry, ...] = ()
    spawn: dict[str, Any] | None = None
    """The ``SpawnEnvelope`` as published, or ``None``. Read back whole for
    the reason ``compaction`` is."""
    suspension: dict[str, Any] = Field(default_factory=dict)
    """``SuspensionPolicy``'s four expiries. Empty means an index written
    before they were reported, whose agents published the defaults."""
    limits: dict[str, Any] = Field(default_factory=dict)
    """Every ``Limits`` field as published. Empty means the defaults."""
    a2a_peers: tuple[str, ...] = ()
    """Peer names, read back so the edit form can restore them. Defaulted so an
    index written before A2A existed still loads."""
    answer_style: Literal["concise"] | None = None
    may_ask_questions: bool = False
    tasks_enabled: bool = False
    components_enabled: bool = False
    subagents_enabled: bool = False
    """Read back so the edit form can be filled in with them.

    Defaulted so an index written before they existed still loads, and read
    from the Spec rather than from the request for the same reason every other
    field here is: the Spec is what was published.

    Not carrying these is how an edit silently turned a feature back off. All
    three join the Version hash, deliberately -- an agent that can park a
    conversation, that keeps a plan, or that can answer with a chart is offered
    a different tool set from one that cannot -- so an edit form that forgot
    them published a different agent from the one somebody thought they were
    editing.
    """
    compaction: CompactionEntry | None = None
    """The agent's compaction policy, or `None` when it has none.

    Defaulted so an index written before compaction existed still loads, and
    `None` there is right rather than merely convenient: those agents were
    published without one.
    """
    code_execution: dict[str, Any] | None = None
    """``psych_runtime.CodeExecution`` as published, or ``None``. A dict for
    the reason ``spawn`` is: only ever read back into the form that wrote
    it. Defaulted so an index written before it existed still loads."""
    published_at: datetime
    approval_selectors: tuple[str, ...]


class WorkflowStepEntry(BaseModel):
    """One step as the edit form needs it back: what it was built from, and
    for an embedded agent or workflow, the hash the parent actually pins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["tool", "agent", "workflow"]
    name: str
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    workflow_id: str | None = None
    version_hash: str | None = None


class WorkflowEntry(BaseModel):
    """One workflow of one account, at the Version it currently runs.

    A workflow keeps its whole history on the one entry rather than one entry
    per Version the way agents do: the Store holds every Version anyway, and
    what the console needs back is the current one and the list of hashes it
    has been.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    owner: str
    version_hash: VersionHash
    history: tuple[VersionHash, ...]
    name: str
    description: str = ""
    steps: tuple[WorkflowStepEntry, ...]
    tools: tuple[str, ...] = ()
    limits: dict[str, Any] = Field(default_factory=dict)
    published_at: datetime
    created_at: datetime
    updated_at: datetime


class AgentPointer(BaseModel):
    """The agent itself: a name a person edits, and what it currently runs.

    Everything mutable about an agent lives here, and nothing here reaches a
    Run. ``version_hash`` is read once, at dispatch, and the hash it yields is
    written onto the ``RunEntry``; moving the pointer afterwards leaves that
    Run pinned where it started.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str
    owner: str
    name: str
    """The current Version's name. Cached here so the list does not need a
    second lookup per row, and rewritten on every publish."""
    version_hash: VersionHash
    """What a new conversation with this agent runs today."""
    history: tuple[VersionHash, ...]
    """Every Version this agent has pointed at, oldest first, ending in the
    current one.

    Recorded rather than derived. The obvious derivation is "order the entries
    by ``published_at``", and it is wrong: republishing a Spec byte-identical
    to an earlier one returns the *existing* Version, carrying the timestamp it
    was first published at, so an agent that went A, B, A would sort as A, B
    and claim to be running B.

    A hash republished after an edit moves to the end rather than appearing
    twice, so the length of this is how many distinct configurations the agent
    has had.
    """
    created_at: datetime
    updated_at: datetime


class RunEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    agent_id: str = ""
    """Which agent this Run is a Run of, stamped at admission.

    Not derivable from ``version_hash``, and that is the whole reason it is
    here. A Version is content, so two agents of one account built from the
    same Spec -- which is what "Duplicate, then publish unchanged" produces --
    are two agents over one hash. Attributing a Run by hash then shows one
    agent's conversations under the other, which is the same mistake the
    compound key on ``AgentEntry`` exists to prevent, left unfixed on the Run
    side.

    A continued Run inherits it from the Run it continues rather than
    re-resolving: a second message belongs to the agent the conversation
    started with, whatever has been published since.

    Defaulted so an index written before agents had identities still loads.
    Such a Run cannot be attributed better than by hash, and the console says
    so by falling back to exactly that.
    """
    conversation_id: str = ""
    """Which conversation this Run belongs to, stamped at dispatch.

    The history list shows one row per conversation, and deleting a chat
    deletes one conversation. Branching does not change it and forking does.

    That split is the whole point, and it took getting wrong once to see it.
    Branching and forking were one operation here: every branch became its own
    row in the history list, so asking a question a second way appeared to
    start a second conversation, and deleting either one had to reason about
    which Runs the other still needed. Neither half matched what anyone else
    does. Editing a message makes a *variant inside
    the conversation you are in* -- the one you page between with the small
    ``1/2`` arrows -- while "branch in new chat" makes a genuinely separate
    conversation that keeps the history up to that point. Two operations, and
    a conversation id is what tells them apart.

    Defaulted so an index written before conversations had ids still loads.
    ``app.main._Tree`` reads such a Run's conversation as the root of its own
    chain, which is what a conversation was before forking existed and is
    therefore exactly right for anything recorded then.
    """
    branch_id: str = ""
    """Which branch of a conversation this Run is on, stamped at dispatch.

    A conversation is a chain of Runs linked by ``continues_run_id``, and
    forking makes that chain a *tree*: two Runs may name the same
    predecessor, because a person asked the same question twice and wanted to
    keep both answers. A parent link alone cannot say which of two futures a
    Run belongs to, so walking a conversation forward from any Run before the
    fork point picks whichever child it met first -- an arbitrary branch, with
    nothing in the data to say it was arbitrary.

    A branch id says it. Every Run on one branch carries the same one: a new
    conversation mints one, a continuation inherits its predecessor's, and a
    Run continuing a message that has already been answered onward mints a
    fresh one, because it is a second answer to a question the tree has
    answered once. Within a branch, then, each Run has at most one child on
    that branch, which is the invariant that makes the forward walk in
    ``app.main``'s newest-in-thread single-valued again.

    It is *not* a conversation id, and the two must not be confused: several
    branches share one ``conversation_id`` and are shown as one chat, paged
    between rather than listed separately. A branch holds only the Runs after
    the point it diverged; the shared prefix before it belongs to the branch it
    was originally dispatched on. What a branch identifies is one readable line
    through the tree.

    Defaulted so an index written before conversations could branch still
    loads. Those Runs share the empty branch and are walked exactly as they
    were, which is correct: they cannot have forked, because nothing could.
    """
    name: str
    tenant: str
    started_at: datetime
    version_hash: VersionHash
    approval_selectors: tuple[str, ...] = ()
    """The approval policy this Run was dispatched under, copied from the agent
    at dispatch rather than looked up at execution time.

    ``ApprovalRoutedRuntime`` read it from the agent entry keyed by version
    hash, and a Version hash is content: republishing the same Spec with
    different selectors overwrote the entry, so every Run of that Version --
    including ones already in flight -- silently changed policy, and deleting
    the agent dropped them to the process default. A Run's policy is decided
    once, when it is admitted."""
    listed: bool = True
    """Whether this Run is still one of the account's own conversations, or is
    only kept because a surviving branch reads it as history.

    Deleting a chat deletes the whole conversation, every branch of it. What
    it must not delete is a *fork*, which is a separate conversation that kept
    the history up to the message it was taken from and replays those Runs as
    its own past. Those Runs belong to the conversation being deleted and are
    read by one that is not, so Git's rule decides them: deleting a branch
    removes a ref, never a commit another ref still reaches. A Run a surviving
    fork reads is kept and unlisted. Unlisted is exactly that state: reachable,
    readable, and no longer a conversation of its own.

    Defaulted so an index written before conversations could be deleted still
    loads, and so the only way to become unlisted is to be deliberately
    unlisted.
    """
    message: str
    """The dispatch input, verbatim. ``GET /api/runs`` echoes it back so a
    history list has something to show without fetching every run's
    messages."""
    workflow_id: str = ""
    """Which workflow this Run is a Run of, when it is one. Empty for an
    agent's Run, which is every Run dispatched before workflows existed."""
    settled_at: datetime | None = None
    """When the Run settled, cached the first time anyone looked. See
    ``mark_settled``."""


class IndexState(BaseModel):
    """The whole index, verbatim on disk."""

    model_config = ConfigDict(extra="forbid")

    agents: list[AgentEntry] = Field(default_factory=list)
    pointers: list[AgentPointer] = Field(default_factory=list)
    """Absent in a file written before agents had identities. ``load`` mints
    one pointer per orphaned entry when it finds none."""
    runs: list[RunEntry] = Field(default_factory=list)
    workflows: list[WorkflowEntry] = Field(default_factory=list)
    """Absent in a file written before workflows could be published."""


def _reconcile_agents(
    entries: list[AgentEntry], pointers: list[AgentPointer]
) -> tuple[dict[tuple[str, str, VersionHash], AgentEntry], dict[tuple[str, str], AgentPointer]]:
    """Rebuild both agent maps from what survived the Store check.

    Three jobs, and they have to happen in this order.

    First the upgrade. An index written before agents had identities has
    entries with no ``agent_id`` and no pointers at all. Each of those *was* an
    agent as far as anyone using the console was concerned, so each gets a
    fresh id and its own single-Version pointer. Nothing is merged by name:
    two entries called "support" may well have been two different agents, and
    guessing wrong would fuse somebody's history.

    Then the pruning. A pointer whose Versions are all gone -- which happens
    against the in-memory store, since it does not outlive the process -- is
    dropped rather than left naming a hash nobody can fetch, and a pointer
    whose *current* Version is gone falls back to the newest survivor in its
    history rather than taking the whole agent with it.

    Last the orphans. A Version entry whose agent no longer exists is
    unreachable by any route, so it goes too.
    """
    by_id: dict[tuple[str, str, VersionHash], AgentEntry] = {}
    minted: list[AgentPointer] = []
    for original in entries:
        entry = (
            original
            if original.agent_id != ""
            else original.model_copy(update={"agent_id": new_agent_id()})
        )
        if entry is not original:
            minted.append(
                AgentPointer(
                    agent_id=entry.agent_id,
                    owner=entry.owner,
                    name=entry.name,
                    version_hash=entry.version_hash,
                    history=(entry.version_hash,),
                    created_at=entry.published_at,
                    updated_at=entry.published_at,
                )
            )
        by_id[(entry.owner, entry.agent_id, entry.version_hash)] = entry

    kept: dict[tuple[str, str], AgentPointer] = {}
    for pointer in [*pointers, *minted]:
        history = tuple(h for h in pointer.history if (pointer.owner, pointer.agent_id, h) in by_id)
        if not history:
            continue
        current = pointer.version_hash if pointer.version_hash in history else history[-1]
        kept[(pointer.owner, pointer.agent_id)] = pointer.model_copy(
            update={"history": history, "version_hash": current}
        )

    return (
        {key: entry for key, entry in by_id.items() if (key[0], key[1]) in kept},
        kept,
    )


class PlaygroundIndex:
    """Guarded by one lock -- the same shape ``InMemoryStore`` itself uses, and
    enough for a backend that runs as one process for one person.

    Constructed with no ``path`` the index is in-memory only, which is what a
    test wanting a clean slate per case should ask for. Given one, every
    mutation is written through immediately rather than flushed periodically:
    a publish or a dispatch is exactly the moment a person expects to be able
    to kill the process and still find their work, and the file is small
    enough that the write costs nothing worth optimising.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = asyncio.Lock()
        self._versions: dict[tuple[str, str, VersionHash], AgentEntry] = {}
        self._pointers: dict[tuple[str, str], AgentPointer] = {}
        self._runs: dict[RunId, RunEntry] = {}
        self._workflows: dict[tuple[str, str], WorkflowEntry] = {}
        self._path = path

    async def load(self, store: Store) -> None:
        """Read the index back and drop whatever ``store`` can no longer resolve.

        Reconciliation is not tidying, it is what makes persistence correct at
        all. This index and the Store have independent lifetimes: the default
        ``InMemoryStore`` loses every Run when the process exits, so replaying a
        saved index against it would list agents that cannot be dispatched and
        runs whose log is gone -- worse than the empty list it replaced,
        because the entries would look real until clicked. Against
        ``PostgresStore`` nothing is dropped and the list survives intact.
        Either way what is listed is what is actually there, which is the only
        property worth having.
        """
        if self._path is None or not self._path.exists():
            return
        state = IndexState.model_validate_json(self._path.read_text())

        surviving = [
            entry
            for entry in state.agents
            if await store.get_version(entry.version_hash) is not None
        ]
        entries, pointers = _reconcile_agents(surviving, state.pointers)
        runs = {
            entry.run_id: entry
            for entry in state.runs
            if await store.get_run(entry.run_id) is not None
        }
        self._workflows = {
            (entry.owner, entry.workflow_id): entry
            for entry in state.workflows
            if await store.get_version(entry.version_hash) is not None
        }

        dropped = (len(state.agents) - len(entries)) + (len(state.runs) - len(runs))
        if dropped:
            _LOG.info(
                "index: dropped %d entr%s the store no longer holds "
                "(expected with the in-memory store, which does not outlive this process)",
                dropped,
                "y" if dropped == 1 else "ies",
            )

        # A Run dispatched before accounts existed is stamped in Psych's own
        # Store with the old process-wide tenant, and a RunHeader's Scope is
        # not this index's to rewrite. So such a Run can never pass the
        # ownership check in `app.main`, and listing it would show a row that
        # opens into a 403. Dropping it from the list is the honest outcome:
        # the Run and its Records are still in the Store, unharmed, and still
        # readable by anything that knows to look under the old tenant.
        orphans = [entry for entry in runs.values() if entry.tenant == _PRE_ACCOUNTS_TENANT]
        for entry in orphans:
            del runs[entry.run_id]
        if orphans:
            _LOG.warning(
                "index: %d run%s predate accounts and are no longer listed. Their records are "
                "still in the store under tenant %r; nothing was deleted.",
                len(orphans),
                "" if len(orphans) == 1 else "s",
                _PRE_ACCOUNTS_TENANT,
            )

        async with self._lock:
            self._versions = entries
            self._pointers = pointers
            self._runs = runs
            self._flush()

    def _flush(self) -> None:
        """Write the current index out. Callers hold ``self._lock``."""
        if self._path is None:
            return
        state = IndexState(
            agents=list(self._versions.values()),
            pointers=list(self._pointers.values()),
            runs=list(self._runs.values()),
            workflows=list(self._workflows.values()),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.replace(self._path)

    async def adopt_legacy(self, account_id: str) -> None:
        """Hand every unowned agent to ``account_id``, once.

        The index half of the same upgrade
        ``app.settings_store.SettingsStore.adopt_legacy`` performs, called at
        the same moment for the same reason: on a machine where somebody had
        already published agents before accounts existed, that somebody is the
        one signing up. A no-op when nothing is unowned.

        Runs are not adopted; ``load`` explains why they cannot be.
        """
        async with self._lock:
            orphans = [key for key in self._versions if key[0] == ""]
            if not orphans:
                return
            for key in orphans:
                entry = self._versions.pop(key)
                owned = entry.model_copy(update={"owner": account_id})
                self._versions[(account_id, owned.agent_id, owned.version_hash)] = owned
            for pointer_key in [key for key in self._pointers if key[0] == ""]:
                pointer = self._pointers.pop(pointer_key)
                owned_pointer = pointer.model_copy(update={"owner": account_id})
                self._pointers[(account_id, owned_pointer.agent_id)] = owned_pointer
            self._flush()

    async def record_publish(self, entry: AgentEntry, *, now: datetime) -> bool:
        """Store one published Version and point its agent at it.

        Returns ``True`` when this created the agent rather than moving an
        existing one, which is what ``created`` in the ``POST /api/agents``
        response now means. It used to mean "this owner had not published this
        exact hash before", which was the only question the console could ask
        when it had no notion of an agent apart from a Version, and which
        answered ``False`` for a genuine edit that happened to land back on an
        earlier configuration.
        """
        if entry.agent_id == "":
            raise ValueError("an AgentEntry must name the agent it is a version of")
        async with self._lock:
            key = (entry.owner, entry.agent_id)
            self._versions[(entry.owner, entry.agent_id, entry.version_hash)] = entry
            existing = self._pointers.get(key)
            # Moved to the end rather than appended blindly: re-publishing a
            # configuration this agent has already had is one Version, and
            # listing it twice would claim an edit that never happened.
            history = tuple(
                h for h in (existing.history if existing else ()) if h != entry.version_hash
            )
            self._pointers[key] = AgentPointer(
                agent_id=entry.agent_id,
                owner=entry.owner,
                name=entry.name,
                version_hash=entry.version_hash,
                history=(*history, entry.version_hash),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._flush()
            return existing is None

    async def remove_agent(self, account_id: str, agent_id: str) -> bool:
        """Stop offering an agent, with every Version it ever had. ``True`` if
        it was there.

        This removes it from *this backend's own* index, not from the Store:
        ``Store`` has no delete, because a Version is an immutable
        content-hashed publication and a Run that pinned it must still be able
        to read the Spec it actually ran (DESIGN.md §4). So a deleted agent
        stops being offered, while every Run it already produced stays
        readable -- which is the honest meaning of "delete" for a platform's
        own catalogue, and worth saying plainly in the UI rather than implying
        the Version is gone.
        """
        async with self._lock:
            if self._pointers.pop((account_id, agent_id), None) is None:
                return False
            for key in [k for k in self._versions if k[0] == account_id and k[1] == agent_id]:
                del self._versions[key]
            self._flush()
            return True

    async def remove_run(self, run_id: RunId) -> bool:
        """Drop one Run from what ``GET /api/runs`` lists. ``True`` if it was
        there. Same caveat as ``remove_agent``: the Run and its Records stay in
        the Store, still reachable by id.

        Unscoped, because the caller has already proven ownership against
        ``RunHeader.scope`` -- see ``app.main``. Adding a second, weaker check
        here would invite someone to rely on this one instead.
        """
        async with self._lock:
            existed = self._runs.pop(run_id, None) is not None
            if existed:
                self._flush()
            return existed

    def _resolve(self, pointer: AgentPointer) -> tuple[AgentPointer, AgentEntry] | None:
        """A pointer beside the Version it currently names. Callers hold the lock."""
        entry = self._versions.get((pointer.owner, pointer.agent_id, pointer.version_hash))
        return None if entry is None else (pointer, entry)

    async def list_agents(self, account_id: str) -> list[tuple[AgentPointer, AgentEntry]]:
        """This account's agents, most recently edited first.

        One row per agent, not per Version. That is the whole point of the
        pointer: an agent someone has edited eleven times is one thing they
        recognise, not eleven rows differing in a flag.
        """
        async with self._lock:
            resolved = [
                pair
                for (owner, _), pointer in self._pointers.items()
                if owner == account_id and (pair := self._resolve(pointer)) is not None
            ]
        return sorted(resolved, key=lambda pair: pair[0].updated_at, reverse=True)

    async def get_agent(
        self, account_id: str, agent_id: str
    ) -> tuple[AgentPointer, AgentEntry] | None:
        """One agent of this account, with what it runs today."""
        async with self._lock:
            pointer = self._pointers.get((account_id, agent_id))
            return None if pointer is None else self._resolve(pointer)

    async def list_versions(self, account_id: str, agent_id: str) -> list[AgentEntry]:
        """Every Version this agent has been, newest first.

        Ordered by ``AgentPointer.history`` rather than by ``published_at``,
        for the reason that field's docstring gives.
        """
        async with self._lock:
            pointer = self._pointers.get((account_id, agent_id))
            if pointer is None:
                return []
            return [
                entry
                for version_hash in reversed(pointer.history)
                if (entry := self._versions.get((account_id, agent_id, version_hash))) is not None
            ]

    async def find_version(self, account_id: str, version_hash: VersionHash) -> AgentEntry | None:
        """Any Version of any of this account's agents, by hash.

        What ``POST /api/runs`` checks when it is handed a hash directly: a
        Version hash is content and therefore guessable by anyone holding the
        same Spec, so the Store alone would let one account dispatch against an
        agent they were never shown. A superseded Version still answers here,
        deliberately -- pinning an older one is a legitimate thing to do, and
        the agent's owner is still its owner.
        """
        async with self._lock:
            return next(
                (
                    entry
                    for (owner, _, candidate), entry in self._versions.items()
                    if owner == account_id and candidate == version_hash
                ),
                None,
            )

    async def any_version(self, version_hash: VersionHash) -> AgentEntry | None:
        """Any account's entry for this Version, for the execution path only.

        ``app.runtime_router`` runs inside a Worker, after admission, and needs
        the approval selectors a Version was published with. It is not serving a
        request and has nobody to be scoped to. Since a Version is content and
        every entry for one hash therefore describes the same Spec, any of them
        answers that question.

        Never call this from a route. It is deliberately named to read wrongly
        in one.
        """
        async with self._lock:
            return next(
                (entry for (_, _, h), entry in self._versions.items() if h == version_hash), None
            )

    async def find_agent_any_owner(self, agent_id: str) -> tuple[AgentPointer, AgentEntry] | None:
        """An agent by id alone, for the *public* Agent Card route.

        The A2A discovery card is served without a credential (§8.2 puts it
        at a well-known path precisely so a client can read it before it
        knows how to authenticate), so there is no account to scope this to.
        An agent id is opaque and unguessable, and a card says what an agent
        is called and what it offers, never what it was told or what it
        remembered. Nothing else may use this.
        """
        async with self._lock:
            pointer = next((p for (_, a), p in self._pointers.items() if a == agent_id), None)
            return None if pointer is None else self._resolve(pointer)

    async def count_agents(self) -> int:
        async with self._lock:
            return len(self._pointers)

    async def find_only_agent(self) -> tuple[AgentPointer, AgentEntry] | None:
        """The one agent this deployment serves, when there is exactly one.
        For the public card with no selector; see ``find_agent_any_owner``."""
        async with self._lock:
            if len(self._pointers) != 1:
                return None
            return self._resolve(next(iter(self._pointers.values())))

    async def record_workflow(
        self,
        *,
        owner: str,
        workflow_id: str,
        version_hash: VersionHash,
        name: str,
        description: str,
        steps: tuple[WorkflowStepEntry, ...],
        tools: tuple[str, ...],
        limits: dict[str, Any],
        published_at: datetime,
        now: datetime,
    ) -> tuple[WorkflowEntry, bool]:
        """Point a workflow at a published Version. ``True`` when this
        created it rather than moving an existing one."""
        async with self._lock:
            key = (owner, workflow_id)
            existing = self._workflows.get(key)
            history = tuple(h for h in (existing.history if existing else ()) if h != version_hash)
            entry = WorkflowEntry(
                workflow_id=workflow_id,
                owner=owner,
                version_hash=version_hash,
                history=(*history, version_hash),
                name=name,
                description=description,
                steps=steps,
                tools=tools,
                limits=limits,
                published_at=published_at,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._workflows[key] = entry
            self._flush()
            return entry, existing is None

    async def list_workflows(self, account_id: str) -> list[WorkflowEntry]:
        async with self._lock:
            return sorted(
                (e for (owner, _), e in self._workflows.items() if owner == account_id),
                key=lambda e: e.updated_at,
                reverse=True,
            )

    async def get_workflow(self, account_id: str, workflow_id: str) -> WorkflowEntry | None:
        async with self._lock:
            return self._workflows.get((account_id, workflow_id))

    async def find_workflow_version(
        self, account_id: str, version_hash: VersionHash
    ) -> WorkflowEntry | None:
        """Any workflow of this account that is, or has been, this Version."""
        async with self._lock:
            return next(
                (
                    e
                    for (owner, _), e in self._workflows.items()
                    if owner == account_id and version_hash in e.history
                ),
                None,
            )

    async def remove_workflow(self, account_id: str, workflow_id: str) -> bool:
        async with self._lock:
            existed = self._workflows.pop((account_id, workflow_id), None) is not None
            if existed:
                self._flush()
            return existed

    async def put_run(self, entry: RunEntry) -> None:
        async with self._lock:
            self._runs[entry.run_id] = entry
            self._flush()

    async def get_run(self, run_id: RunId) -> RunEntry | None:
        """One Run's entry, unscoped. Same rule as ``remove_run``: ownership is
        settled against the Store's ``RunHeader.scope`` before this is called,
        and by ``app.runtime_router`` after admission where no caller exists."""
        async with self._lock:
            return self._runs.get(run_id)

    async def list_runs(self, account_id: str, *, include_unlisted: bool = False) -> list[RunEntry]:
        """This account's conversations, newest first.

        Unlisted Runs are excluded by default: they are history a surviving
        branch reads rather than conversations of their own, and showing one
        would put a stub row on the page carrying an opening question and
        nothing else. ``include_unlisted`` is for the walks that need the whole
        tree -- reachability, and rebuilding a thread across a fork point.
        """
        async with self._lock:
            mine = [
                entry
                for entry in self._runs.values()
                if entry.tenant == account_id and (include_unlisted or entry.listed)
            ]
        return sorted(mine, key=lambda r: r.started_at, reverse=True)

    async def unlist_run(self, run_id: RunId) -> None:
        """Keep a Run readable and stop calling it a conversation.

        What deleting a branch does to the Runs another branch still reaches.
        """
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or not entry.listed:
                return
            self._runs[run_id] = entry.model_copy(update={"listed": False})
            self._flush()

    async def mark_settled(self, run_id: RunId, settled_at: datetime) -> None:
        """Remember when a Run settled, once.

        ``GET /api/runs`` used to read every settled Run's whole log to find
        this one timestamp, on every poll, for every row. Stamping it here the
        first time anyone asks turns that into one read per Run for the life of
        the index.
        """
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or entry.settled_at is not None:
                return
            self._runs[run_id] = entry.model_copy(update={"settled_at": settled_at})
            self._flush()
