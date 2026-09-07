"""The tool registry: functions in, names out.

DESIGN.md §10.1. Code tools are Python functions the consumer registers at boot.
The schema is derived from type hints via Pydantic, and sync functions are
accepted and run in a thread pool so a consumer does not have to rewrite their
existing code to use it.

## The invariant this module exists to protect

**What lands in a Spec is a name, never the callable.** Registration takes the
function and keeps it here; the Spec holds the registered name and looks it up at
call time. The moment a Spec holds a live object it stops being serialisable and
the runtime forks into two execution models (DESIGN.md §4).

So the registry is the one place a name and a function meet, and it is process
state rather than Run state. Two Workers on two machines both need the same
functions registered, which is a property of how the consumer boots their
processes and not something Psych can enforce. Publish-time validation catches
the common case: a Spec naming a tool no process registered.

## Two doors, one ``RegisteredTool``

``register`` derives the schema from a function's own type hints and stays the
documented default: the schema and the function cannot drift apart, because one
is derived from the other. ``register_dynamic`` is the second door, for a tool
whose shape is known only at runtime -- generated from a database row, proxied
from a non-MCP catalogue, built from a customer-authored form -- and has no
signature to read hints from. Both doors end at the same construction (see
``_finalize_model_schema``) and the same duplicate-name check (``_register``),
so the resolver, the access-narrowing intersection and the failure-streak guard
read one ``RegisteredTool`` shape regardless of which door built it.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from psych_runtime.core.errors import PsychError
from psych_runtime.core.messages import ToolDefinition

__all__ = ["RegisteredTool", "ToolRegistry", "ToolSchemaError"]

_F = TypeVar("_F", bound=Callable[..., Any])


class ToolSchemaError(PsychError):
    """A function cannot be described to a model.

    Raised at registration, which is boot, rather than at call time. A tool whose
    schema cannot be derived is a tool the model will be offered and then fail to
    call, and discovering that mid-conversation is the wrong time.
    """


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """One registered function and everything derived from it."""

    name: str
    description: str
    fn: Callable[..., Any]
    input_schema: dict[str, Any]
    arguments_adapter: TypeAdapter[BaseModel]
    """Validates a model's arguments before the function sees them. A model that
    invents a field or omits a required one fails here, with an error the loop
    hands back as data rather than an exception that kills the turn."""
    is_async: bool
    interruptible: bool
    safe_to_retry: bool
    annotations: frozenset[str]

    def definition(self) -> ToolDefinition:
        """How this tool is described to the model."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            annotations=self.annotations,
        )


class ToolRegistry:
    """Names to functions, for one process.

    Not thread-safe for concurrent registration, and deliberately so: registration
    happens at boot on one thread, and a lock here would suggest otherwise.
    Lookups after boot are reads and are safe from anywhere.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    # -- registration -------------------------------------------------------

    @overload
    def register(self, fn: _F) -> _F: ...

    @overload
    def register(
        self,
        fn: None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        interruptible: bool = True,
        safe_to_retry: bool = False,
        annotations: frozenset[str] | set[str] | None = None,
    ) -> Callable[[_F], _F]: ...

    def register(
        self,
        fn: _F | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        interruptible: bool = True,
        safe_to_retry: bool = False,
        annotations: frozenset[str] | set[str] | None = None,
    ) -> _F | Callable[[_F], _F]:
        """Register a function as a code tool.

        Usable bare or with arguments:

        ```python
        @registry.register
        def lookup(order_id: str) -> dict[str, str]: ...


        @registry.register(name="refund", interruptible=False, annotations={"destructive"})
        def issue_refund(order_id: str, cents: int) -> str: ...
        ```

        The decorator returns the original function unchanged, so the consumer's
        own code can still call it directly. What Psych keeps is the name.

        Args:
            fn: the function, when used bare.
            name: the registered name. Defaults to the function's own name.
            description: what the model is told. Defaults to the first paragraph
                of the docstring, because a tool the model cannot understand is a
                tool it will misuse.
            interruptible: whether an abort may cancel this mid-call. False for
                anything with a side effect that must not be half-done: a refund
                tool sets this, and it is the difference between a stopped agent
                and a half-issued refund (DESIGN.md §9).
            safe_to_retry: whether a Worker reclaiming a crashed Run may
                re-execute a call recorded as started. Defaults False, because
                assuming a side effect is repeatable is how double refunds
                happen.
            annotations: MCP-style annotations driving approval selectors:
                ``read-only``, ``write``, ``destructive``. An unannotated tool is
                treated as ``write`` (DESIGN.md §10.9).

        Raises:
            ToolSchemaError: the signature cannot be described to a model.
        """
        marks = frozenset(annotations) if annotations is not None else frozenset()

        def decorate(target: _F) -> _F:
            tool = self._build(
                target,
                name=name,
                description=description,
                interruptible=interruptible,
                safe_to_retry=safe_to_retry,
                annotations=marks,
            )
            self._register(tool)
            return target

        if fn is not None:
            # Bare decorator. Return the function itself, never the
            # RegisteredTool: replacing the consumer's function with our record
            # of it would silently break every direct call in their own code.
            return decorate(fn)
        return decorate

    def register_dynamic(
        self,
        name: str,
        fn: Callable[..., Any],
        arguments_model: type[BaseModel],
        *,
        description: str | None = None,
        interruptible: bool = True,
        safe_to_retry: bool = False,
        annotations: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Register a tool whose schema is a Pydantic model built at runtime,
        for a consumer with no Python signature to annotate.

        ``register`` is the documented default and stays that way: deriving the
        schema from a function's own type hints means the schema and the
        function cannot drift apart. This exists for the case that default
        cannot reach -- a tool generated from a database row, a non-MCP catalogue
        entry, or a customer-authored form definition, none of which have a
        signature. ``pydantic.create_model`` already gets a consumer there; this
        is where the resulting model is handed to Psych.

        ```python
        from pydantic import BaseModel, ConfigDict, create_model

        arguments = create_model(
            "LookupIntegration_Arguments",
            __config__=ConfigDict(extra="forbid"),
            record_id=(str, ...),
        )
        registry.register_dynamic(
            "lookup_integration",
            lookup_integration,
            arguments,
            description="Look up one row from the integrations table.",
        )
        ```

        The result is the exact same ``RegisteredTool`` ``register`` would build
        for an equivalent signature: same fields, same validation, same
        ``TypeAdapter`` construction. Everything downstream -- the resolver, the
        access-narrowing intersection, the failure-streak guard -- reads a
        ``RegisteredTool`` and cannot tell which door it came through.

        Two things this path holds to the same standard as the derived one,
        deliberately, rather than being the looser way in:

        - **A description is still required.** The model chooses tools by their
          descriptions; a schema with none is exactly as unusable as a
          docstring-less function, so it is refused the same way.
        - **``extra="forbid"`` is still required, and is checked rather than
          silently added.** It is what turns a model's hallucinated argument
          into a validation failure the model is told about instead of a field
          quietly dropped. ``create_model`` defaults to ignoring unknown fields,
          so a schema built without ``ConfigDict(extra="forbid")`` is rejected
          rather than rewritten: reconstructing a consumer-supplied model to
          force the setting would mean recreating it field by field from
          ``model_fields``, which loses anything not representable that way --
          a ``@field_validator``, a ``@model_validator``, a computed field. That
          silent loss is worse than telling the consumer once, at registration,
          to add one argument to their own ``create_model`` call.

        Args:
            name: the registered name. Required: unlike a decorated function,
                there is no ``__name__`` to fall back to, and a dynamic
                registration is often the same callable reused under many
                names.
            fn: the function this tool calls. Kept exactly as given; Psych
                never wraps or replaces it.
            arguments_model: a Pydantic model describing the arguments, built
                with ``ConfigDict(extra="forbid")``.
            description: what the model is told. Defaults to the first
                paragraph of ``arguments_model``'s docstring, because a
                runtime-built model is the closest thing this path has to the
                function's own docstring.
            interruptible: see ``register``.
            safe_to_retry: see ``register``.
            annotations: see ``register``.

        Raises:
            ToolSchemaError: no description could be resolved, the model does
                not set ``extra="forbid"``, or the name collides with one
                already registered.
        """
        if not name:
            raise ToolSchemaError(
                "register_dynamic() requires a non-empty name: there is no function "
                "signature to infer one from."
            )
        resolved_description = _resolve_description(
            name, description, arguments_model.__doc__ or ""
        )
        if arguments_model.model_config.get("extra") != "forbid":
            raise ToolSchemaError(
                f'tool {name!r}\'s arguments_model does not set extra="forbid" '
                "(pydantic.create_model defaults to silently ignoring fields it does "
                "not recognise). The derived path never allows a model to invent an "
                "argument that gets quietly dropped, and this path holds to the same "
                "rule. Build the model with "
                '`create_model(..., __config__=ConfigDict(extra="forbid"), ...)` and '
                "register it again."
            )

        marks = frozenset(annotations) if annotations is not None else frozenset()
        adapter, schema = _finalize_model_schema(arguments_model)
        tool = RegisteredTool(
            name=name,
            description=resolved_description,
            fn=fn,
            input_schema=schema,
            arguments_adapter=adapter,
            is_async=inspect.iscoroutinefunction(fn),
            interruptible=interruptible,
            safe_to_retry=safe_to_retry,
            annotations=marks,
        )
        self._register(tool)

    def _register(self, tool: RegisteredTool) -> None:
        """The one place a built ``RegisteredTool`` is checked and stored.

        Shared by ``register`` and ``register_dynamic`` so a name collision is
        caught identically regardless of which door produced the tool.
        """
        if tool.name in self._tools:
            raise ToolSchemaError(
                f"a tool named {tool.name!r} is already registered. Two functions "
                "sharing one name means the model addresses one and gets the other."
            )
        self._tools[tool.name] = tool

    def _build(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None,
        description: str | None,
        interruptible: bool,
        safe_to_retry: bool,
        annotations: frozenset[str],
    ) -> RegisteredTool:
        tool_name = name or getattr(fn, "__name__", None)
        if not tool_name:
            raise ToolSchemaError(
                "this callable has no __name__, so it needs an explicit name= to be "
                "registered. A lambda or a functools.partial usually hits this."
            )

        resolved_description = _resolve_description(
            tool_name, description, inspect.getdoc(fn) or ""
        )
        adapter, schema = _derive_schema(fn, tool_name)
        return RegisteredTool(
            name=tool_name,
            description=resolved_description,
            fn=fn,
            input_schema=schema,
            arguments_adapter=adapter,
            is_async=inspect.iscoroutinefunction(fn),
            interruptible=interruptible,
            safe_to_retry=safe_to_retry,
            annotations=annotations,
        )

    def layered(self) -> ToolRegistry:
        """A copy that can take Run-scoped additions without touching this one.

        Psych's built-ins (``load_skill``, ``remember``, ``forget``) depend on
        the Spec and the Scope, so which of them exist differs per Run. Adding
        them to the consumer's process-wide registry would let two concurrent
        Runs collide over one name, and would leave a Run's built-ins registered
        after it finished. A layer per Run costs one dict copy and cannot.
        """
        copy = ToolRegistry()
        copy._tools.update(self._tools)
        return copy

    # -- lookup -------------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools.values())

    @property
    def names(self) -> frozenset[str]:
        """Every registered name, for publish-time validation."""
        return frozenset(self._tools)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def require(self, name: str) -> RegisteredTool:
        """Look up a tool, or say clearly which one is missing.

        Raises:
            ToolSchemaError: no tool of that name is registered in this process.
                Usually means one Worker booted without registering what another
                did, which publish-time validation cannot catch on its own.
        """
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            raise ToolSchemaError(
                f"no tool named {name!r} is registered in this process. Registered: "
                f"{known}. A Spec that validated at publish can still fail here if a "
                "Worker booted without registering the same tools."
            )
        return tool

    # -- execution ----------------------------------------------------------

    async def call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Validate arguments and run the tool.

        A sync function runs in a worker thread so it cannot block the event loop.
        DESIGN.md §10.1 accepts sync functions on purpose: a consumer with an
        existing codebase should not have to rewrite it to register a tool.

        Raises:
            ValidationError: the arguments do not match the schema. The caller
                turns this into a tool result the model can read and correct,
                never into a failed turn.
        """
        tool = self.require(name)
        validated = tool.arguments_adapter.validate_python(dict(arguments))
        kwargs = validated.model_dump()

        if tool.is_async:
            awaitable: Awaitable[Any] = tool.fn(**kwargs)
            return await awaitable
        return await asyncio.to_thread(tool.fn, **kwargs)

    def definitions(self, names: Iterator[str] | list[str] | None = None) -> list[ToolDefinition]:
        """Tool definitions for the model, for ``names`` or for everything."""
        if names is None:
            return [tool.definition() for tool in self._tools.values()]
        return [self.require(name).definition() for name in names]


_IGNORED_PARAMETERS = frozenset({"self", "cls"})


def _derive_schema(
    fn: Callable[..., Any], tool_name: str
) -> tuple[TypeAdapter[BaseModel], dict[str, Any]]:
    """Build a Pydantic model from a function signature, and a JSON schema from it.

    Going through a real Pydantic model rather than hand-rolling JSON Schema
    means arguments coming back from a model are *validated*, not just described:
    the same definition that tells the model what to send is what rejects what it
    actually sent. Two hand-maintained descriptions of one signature would drift.
    """
    signature = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception as err:
        # Broad on purpose: get_type_hints raises NameError, TypeError and
        # AttributeError depending on how the annotation is broken, and the
        # useful thing here is the message rather than the class.
        raise ToolSchemaError(
            f"tool {tool_name!r} has type hints that cannot be resolved: {err}. Under "
            "`from __future__ import annotations` every hint is a string resolved "
            "against the function's module globals, so a type defined inside another "
            "function is not visible here. Move the type to module level, or drop the "
            "future import in that module."
        ) from err

    fields: dict[str, Any] = {}
    for parameter in signature.parameters.values():
        if parameter.name in _IGNORED_PARAMETERS:
            continue
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            raise ToolSchemaError(
                f"tool {tool_name!r} takes *{parameter.name}, which has no JSON Schema "
                "equivalent. A model cannot be told what to put there, so it will "
                "guess. Declare the arguments explicitly."
            )
        if parameter.name not in hints:
            raise ToolSchemaError(
                f"tool {tool_name!r} parameter {parameter.name!r} has no type hint. The "
                "schema is derived from hints, so an unhinted parameter cannot be "
                "described to the model or validated when it comes back."
            )
        annotation = hints[parameter.name]
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (annotation, default)

    try:
        arguments_model = create_model(
            f"{tool_name}_Arguments",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )
    except (TypeError, ValidationError, RuntimeError) as err:
        raise ToolSchemaError(
            f"tool {tool_name!r} has a signature Pydantic cannot model: {err}"
        ) from err

    return _finalize_model_schema(arguments_model)


def _resolve_description(tool_name: str, description: str | None, doc: str) -> str:
    """One rule for what a tool is allowed to be described by, used by both
    doors into the registry.

    A model chooses tools by their descriptions, so an explicitly-supplied
    schema is held to exactly the standard a docstring-less function already
    is: no description resolves to no registration, regardless of which path
    produced the schema.
    """
    resolved = description if description is not None else doc.split("\n\n", maxsplit=1)[0]
    if not resolved.strip():
        raise ToolSchemaError(
            f"tool {tool_name!r} has no description and no docstring. The model "
            "chooses tools by their descriptions, so an undescribed tool is one it "
            "will either ignore or misuse."
        )
    return resolved.strip()


def _finalize_model_schema(
    arguments_model: type[BaseModel],
) -> tuple[TypeAdapter[BaseModel], dict[str, Any]]:
    """The second half both schema sources share: a real ``TypeAdapter`` off a
    model already known to have ``extra="forbid"``, and the JSON Schema
    derived from it.

    Called once with a model built here (``_derive_schema``, from a function's
    own type hints) and once with a model the consumer built themselves
    (``ToolRegistry.register_dynamic``). Neither caller hand-rolls the
    ``TypeAdapter`` or the schema independently, which is what makes the two
    ``RegisteredTool``s that come out identical in shape rather than two
    parallel implementations that happen to agree today.
    """
    adapter: TypeAdapter[BaseModel] = TypeAdapter(arguments_model)
    schema = arguments_model.model_json_schema()
    # The generated title is the model's class name, which tells the model
    # nothing and costs prompt tokens on every turn.
    schema.pop("title", None)
    return adapter, schema
