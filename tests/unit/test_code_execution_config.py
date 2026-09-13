"""``CodeExecution`` on a Spec: validation, hashing, and the deployment's answer.

DESIGN.md §4 and §18. A Spec names what it wants in serialisable terms; the
runtime resolves that against what the deployment has. Everything here is
pure: no sandbox is spawned, and the profile registry is driven with a
scripted backend that only describes itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from psych_runtime.core.code_execution import (
    Enforcement,
    IsolationLevel,
    NetworkAccess,
    OutputPreservation,
)
from psych_runtime.core.errors import SpecValidationError
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import (
    AgentSpec,
    ArtifactPolicy,
    CodeExecution,
    CodeExecutionLimits,
    CodeTool,
    ModelRef,
    OutputPolicy,
)
from psych_runtime.core.validation import ValidationContext, collect_issues, validate_spec
from psych_runtime.core.version import compute_hash
from psych_runtime.sandbox.port import (
    SandboxDescription,
    SandboxGuarantees,
    SandboxLimits,
    SandboxSetupError,
    achieved_level,
    describe_sandbox,
)
from psych_runtime.sandbox.profiles import (
    CodeExecutionGrant,
    ExecutionPlan,
    ExecutionRefusal,
    SandboxProfile,
    SandboxProfiles,
    intersect_grants,
    narrow_limits,
    resolve_execution,
)
from psych_runtime.testing.sandbox_service import ScriptedSandbox, scripted_result

pytestmark = pytest.mark.unit

SCOPE = Scope(tenant="acme", principal="user-1")
_CEILING = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)


def agent(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "analyst",
        "model": ModelRef(model="gpt-4o"),
        "tools": (CodeTool(name="lookup"), CodeTool(name="refund")),
    }
    base.update(kwargs)
    return AgentSpec(**base)


class TestTheSpecField:
    def test_off_by_default(self) -> None:
        assert agent().code_execution is None

    def test_defaults_request_real_isolation_and_no_network(self) -> None:
        config = CodeExecution()
        assert config.enabled
        assert config.profile == "default"
        assert config.isolation is IsolationLevel.ISOLATED
        assert config.network is NetworkAccess.DENIED
        assert config.bindings is None
        assert config.output.preserve is OutputPreservation.WHEN_AVAILABLE
        assert config.artifacts.collection == "collect"

    def test_bindings_must_be_tools_the_spec_grants(self) -> None:
        with pytest.raises(ValidationError, match="neither grants"):
            agent(code_execution=CodeExecution(bindings=("lookup", "nope")))

    def test_bindings_are_a_sorted_set(self) -> None:
        spec = agent(code_execution=CodeExecution(bindings=("refund", "lookup")))
        assert spec.code_execution is not None
        assert spec.code_execution.bindings == ("lookup", "refund")
        with pytest.raises(ValidationError, match="more than once"):
            CodeExecution(bindings=("lookup", "lookup"))

    def test_a_preview_larger_than_the_capture_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="cannot exceed max_bytes"):
            OutputPolicy(preview_bytes=65_536, max_bytes=4_096)

    def test_limits_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            CodeExecutionLimits(wall_seconds=100_000)
        with pytest.raises(ValidationError):
            ArtifactPolicy(max_count=10_000)

    def test_a_profile_name_is_an_identifier(self) -> None:
        with pytest.raises(ValidationError):
            CodeExecution(profile="not a name!")

    def test_the_spec_holds_names_and_no_live_objects(self) -> None:
        spec = agent(code_execution=CodeExecution(profile="strict"))
        dumped = spec.model_dump(mode="json")
        assert dumped["code_execution"]["profile"] == "strict"
        assert AgentSpec.model_validate(dumped) == spec


class TestTheVersionHash:
    def test_enabling_code_execution_changes_the_hash(self) -> None:
        assert compute_hash(agent()) != compute_hash(agent(code_execution=CodeExecution()))

    def test_disabling_in_place_still_changes_the_hash(self) -> None:
        on = agent(code_execution=CodeExecution())
        off = agent(code_execution=CodeExecution(enabled=False))
        assert compute_hash(on) != compute_hash(off)
        assert compute_hash(off) != compute_hash(agent())

    def test_a_different_profile_is_a_different_version(self) -> None:
        a = agent(code_execution=CodeExecution(profile="a"))
        b = agent(code_execution=CodeExecution(profile="b"))
        assert compute_hash(a) != compute_hash(b)

    def test_built_from_a_dict_hashes_like_built_in_python(self) -> None:
        built = agent(
            code_execution=CodeExecution(
                limits=CodeExecutionLimits(wall_seconds=5), bindings=("refund", "lookup")
            )
        )
        from_dict = AgentSpec.model_validate(
            {
                "name": "analyst",
                "model": {"model": "gpt-4o"},
                "tools": [{"kind": "code", "name": "lookup"}, {"kind": "code", "name": "refund"}],
                "code_execution": {"limits": {"wall_seconds": 5}, "bindings": ["lookup", "refund"]},
            }
        )
        assert compute_hash(built) == compute_hash(from_dict)


class TestPublishTimeValidation:
    def test_an_unknown_profile_is_refused_when_the_context_names_them(self) -> None:
        spec = agent(code_execution=CodeExecution(profile="strict"))
        issues = collect_issues(spec, ValidationContext(sandbox_profiles={"default"}))
        assert any("strict" in issue.message for issue in issues)
        with pytest.raises(SpecValidationError):
            validate_spec(spec, ValidationContext(sandbox_profiles={"default"}))

    def test_a_known_profile_passes(self) -> None:
        spec = agent(code_execution=CodeExecution(profile="strict"))
        assert not collect_issues(spec, ValidationContext(sandbox_profiles={"strict"}))

    def test_no_context_means_no_profile_check(self) -> None:
        spec = agent(code_execution=CodeExecution(profile="strict"))
        assert not collect_issues(spec)

    def test_a_disabled_config_is_not_checked(self) -> None:
        spec = agent(code_execution=CodeExecution(profile="strict", enabled=False))
        assert not collect_issues(spec, ValidationContext(sandbox_profiles={"default"}))


class TestNarrowing:
    def test_a_request_can_only_lower_a_ceiling(self) -> None:
        requested = CodeExecutionLimits(cpu_seconds=2.0, wall_seconds=90.0, memory_bytes=1)
        effective = narrow_limits(_CEILING, requested)
        assert effective.cpu_seconds == 2.0
        assert effective.wall_seconds == 30.0  # clamped to the ceiling
        assert effective.address_space_bytes == 1
        assert effective.file_size_bytes == _CEILING.file_size_bytes  # unrequested: ceiling

    def test_none_takes_the_ceiling(self) -> None:
        assert narrow_limits(_CEILING, None) == _CEILING
        assert narrow_limits(_CEILING, CodeExecutionLimits()) == _CEILING

    def test_intersecting_grants_never_widens(self) -> None:
        wide = CodeExecutionGrant(
            limits=_CEILING,
            network=True,
            bindings=None,
            isolation=IsolationLevel.PROCESS,
            artifacts=True,
        )
        narrow = CodeExecutionGrant(
            limits=_CEILING.model_copy(update={"wall_seconds": 5.0}),
            network=False,
            bindings=frozenset({"lookup"}),
            isolation=IsolationLevel.ISOLATED,
            artifacts=False,
        )
        both = intersect_grants(wide, narrow)
        assert both.limits.wall_seconds == 5.0
        assert both.network is False
        assert both.bindings == frozenset({"lookup"})
        assert both.isolation is IsolationLevel.ISOLATED
        assert both.artifacts is False
        # Symmetric: the order of the two parties does not matter.
        assert intersect_grants(narrow, wide) == both

    def test_bindings_intersect_when_both_have_an_opinion(self) -> None:
        a = CodeExecutionGrant(limits=_CEILING, bindings=frozenset({"lookup", "refund"}))
        b = CodeExecutionGrant(limits=_CEILING, bindings=frozenset({"refund", "other"}))
        assert intersect_grants(a, b).bindings == frozenset({"refund"})


def _scripted(level: IsolationLevel | None) -> ScriptedSandbox:
    return ScriptedSandbox(isolation=level, default=scripted_result(value=None, isolation=level))


class TestResolution:
    async def test_a_missing_profile_is_a_setup_error(self) -> None:
        profiles = SandboxProfiles([SandboxProfile("default", _scripted(IsolationLevel.ISOLATED))])
        with pytest.raises(SandboxSetupError, match="strict"):
            await resolve_execution(CodeExecution(profile="strict"), profiles, scope=SCOPE)

    async def test_the_plan_is_the_intersection(self) -> None:
        profile = SandboxProfile(
            "default",
            _scripted(IsolationLevel.ISOLATED),
            hard_limits=_CEILING.model_copy(update={"wall_seconds": 20.0}),
        )
        profiles = SandboxProfiles([profile])
        config = CodeExecution(
            limits=CodeExecutionLimits(wall_seconds=60.0, cpu_seconds=1.0),
            bindings=("lookup",),
        )
        plan = await resolve_execution(config, profiles, scope=SCOPE)
        assert isinstance(plan, ExecutionPlan)
        assert plan.limits.wall_seconds == 20.0
        assert plan.limits.cpu_seconds == 1.0
        assert plan.bindings == frozenset({"lookup"})
        assert plan.network is False
        assert plan.isolation is IsolationLevel.ISOLATED
        assert plan.capture.collect_artifacts is True
        assert plan.capture.stream_bytes == config.output.max_bytes

    async def test_a_backend_below_the_requested_level_is_refused(self) -> None:
        profiles = SandboxProfiles([SandboxProfile("default", _scripted(IsolationLevel.PROCESS))])
        refusal = await resolve_execution(CodeExecution(), profiles, scope=SCOPE)
        assert isinstance(refusal, ExecutionRefusal)
        assert refusal.kind == "isolation_unavailable"
        assert "'isolated'" in refusal.message
        assert "'process'" in refusal.message

    async def test_trusted_code_may_ask_for_the_process_level(self) -> None:
        profiles = SandboxProfiles([SandboxProfile("default", _scripted(IsolationLevel.PROCESS))])
        plan = await resolve_execution(
            CodeExecution(isolation=IsolationLevel.PROCESS), profiles, scope=SCOPE
        )
        assert isinstance(plan, ExecutionPlan)

    async def test_network_needs_the_profiles_permission(self) -> None:
        sandbox = _scripted(IsolationLevel.ISOLATED)
        config = CodeExecution(network=NetworkAccess.UNRESTRICTED)
        denied = SandboxProfiles([SandboxProfile("default", sandbox)])
        refusal = await resolve_execution(config, denied, scope=SCOPE)
        assert isinstance(refusal, ExecutionRefusal)
        assert refusal.kind == "network_not_allowed"
        allowed = SandboxProfiles([SandboxProfile("default", sandbox, allow_network=True)])
        plan = await resolve_execution(config, allowed, scope=SCOPE)
        assert isinstance(plan, ExecutionPlan)
        assert plan.network is True

    async def test_a_backend_that_is_not_ready_is_refused(self) -> None:
        class Broken:
            async def describe(self) -> SandboxDescription:
                return SandboxDescription(
                    backend="custom",
                    platform="linux",
                    isolation=IsolationLevel.ISOLATED,
                    guarantees=SandboxGuarantees(),
                    ready=False,
                    problems=("the image is missing",),
                )

            async def run(self, program: str, **options: Any) -> Any:
                raise AssertionError("never reached")

        profiles = SandboxProfiles([SandboxProfile("default", Broken())])
        refusal = await resolve_execution(CodeExecution(), profiles, scope=SCOPE)
        assert isinstance(refusal, ExecutionRefusal)
        assert refusal.kind == "backend_not_ready"
        assert "the image is missing" in refusal.message

    async def test_a_policy_can_only_narrow_and_a_raising_one_refuses(self) -> None:
        profiles = SandboxProfiles([SandboxProfile("default", _scripted(IsolationLevel.ISOLATED))])

        class Widen:
            async def narrow(self, scope: Scope, grant: CodeExecutionGrant) -> CodeExecutionGrant:
                return CodeExecutionGrant(
                    limits=_CEILING.model_copy(update={"wall_seconds": 999.0}),
                    network=True,
                    bindings=frozenset({"lookup", "refund", "extra"}),
                    isolation=IsolationLevel.PROCESS,
                )

        plan = await resolve_execution(
            CodeExecution(bindings=("lookup",)),
            profiles,
            scope=SCOPE,
            policy=Widen(),
        )
        assert isinstance(plan, ExecutionPlan)
        assert plan.limits.wall_seconds == 30.0
        assert plan.network is False
        assert plan.bindings == frozenset({"lookup"})
        assert plan.isolation is IsolationLevel.ISOLATED

        class Narrow:
            async def narrow(self, scope: Scope, grant: CodeExecutionGrant) -> CodeExecutionGrant:
                return CodeExecutionGrant(
                    limits=_CEILING.model_copy(update={"cpu_seconds": 1.0}),
                    bindings=frozenset({"refund"}),
                )

        plan = await resolve_execution(CodeExecution(), profiles, scope=SCOPE, policy=Narrow())
        assert isinstance(plan, ExecutionPlan)
        assert plan.limits.cpu_seconds == 1.0
        assert plan.bindings == frozenset({"refund"})

        class Raises:
            async def narrow(self, scope: Scope, grant: CodeExecutionGrant) -> CodeExecutionGrant:
                raise RuntimeError("policy service down")

        refusal = await resolve_execution(CodeExecution(), profiles, scope=SCOPE, policy=Raises())
        assert isinstance(refusal, ExecutionRefusal)
        assert refusal.kind == "policy_refused"

    async def test_a_legacy_backend_is_process_level_and_unverified(self) -> None:
        class Legacy:
            async def run(
                self,
                program: str,
                *,
                bindings: Any = None,
                limits: Any = None,
                network: bool = False,
            ) -> Any:
                return None

        description = await describe_sandbox(Legacy())
        assert description.backend == "custom"
        assert description.isolation is IsolationLevel.PROCESS
        assert description.guarantees.process_tree is Enforcement.UNVERIFIED
        assert description.guarantees.filesystem is Enforcement.UNAVAILABLE

    def test_the_registry_refuses_a_duplicate_name(self) -> None:
        profiles = SandboxProfiles([SandboxProfile("default", _scripted(IsolationLevel.ISOLATED))])
        with pytest.raises(SandboxSetupError, match="already registered"):
            profiles.add(SandboxProfile("default", _scripted(IsolationLevel.ISOLATED)))
        assert profiles.names == frozenset({"default"})

    async def test_require_ready_names_every_broken_profile(self) -> None:
        class Broken:
            async def describe(self) -> SandboxDescription:
                return SandboxDescription(
                    backend="custom",
                    platform="linux",
                    isolation=None,
                    guarantees=SandboxGuarantees(),
                    ready=False,
                    problems=("no runtime",),
                )

            async def run(self, program: str, **options: Any) -> Any:
                raise AssertionError("never reached")

        profiles = SandboxProfiles(
            [
                SandboxProfile("ok", _scripted(IsolationLevel.ISOLATED)),
                SandboxProfile("broken", Broken()),
            ]
        )
        with pytest.raises(SandboxSetupError, match="broken: no runtime"):
            await profiles.require_ready()


class TestAchievedLevel:
    def _all(self, level: Enforcement) -> SandboxGuarantees:
        return SandboxGuarantees(**dict.fromkeys(SandboxGuarantees.model_fields, level))

    def test_everything_enforced_is_isolated(self) -> None:
        assert achieved_level(self._all(Enforcement.ENFORCED)) is IsolationLevel.ISOLATED

    def test_a_shared_filesystem_caps_it_at_process(self) -> None:
        guarantees = self._all(Enforcement.ENFORCED).model_copy(
            update={"filesystem": Enforcement.UNAVAILABLE}
        )
        assert achieved_level(guarantees) is IsolationLevel.PROCESS

    def test_granted_network_does_not_count_against_isolation(self) -> None:
        guarantees = self._all(Enforcement.ENFORCED).model_copy(
            update={"network": Enforcement.UNAVAILABLE}
        )
        assert achieved_level(guarantees, network_required=True) is IsolationLevel.PROCESS
        assert achieved_level(guarantees, network_required=False) is IsolationLevel.ISOLATED

    def test_no_process_tree_containment_is_no_level_at_all(self) -> None:
        assert achieved_level(self._all(Enforcement.UNAVAILABLE)) is None
        assert achieved_level(self._all(Enforcement.UNVERIFIED)) is None

    def test_levels_are_ordered(self) -> None:
        assert IsolationLevel.ISOLATED.satisfies(IsolationLevel.PROCESS)
        assert not IsolationLevel.PROCESS.satisfies(IsolationLevel.ISOLATED)
