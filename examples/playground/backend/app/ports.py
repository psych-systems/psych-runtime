"""The ports this deployment implements for Psych, per account.

Each class here answers one question the library refuses to answer itself
(DESIGN.md §1, §14): which hosts a tenant may reach, which tools a tenant may
call, and whether model-written code may run at all. Every one reads the
asking account's own ``RuntimeSettings`` fresh on each call rather than
holding a snapshot, so a setting changed in the console applies to the next
call with nothing to invalidate -- the same rule ``app.secrets`` follows and
for the same reason.

None of them is a security boundary between accounts. Isolation comes from
``Scope``, which every call carries; these decide what one account has chosen
for itself.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import sys
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from app.settings_store import RuntimeSettings, SandboxProfileEntry, SettingsStore
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import Version
from psych_runtime.model.egress import HttpTransport
from psych_runtime.sandbox.container import ContainerSandbox
from psych_runtime.sandbox.local import LocalBackend, detect_local_backends, local_sandbox
from psych_runtime.sandbox.port import (
    Sandbox,
    SandboxDescription,
    SandboxFailure,
    SandboxGuarantees,
    SandboxLimits,
    SandboxResult,
    SandboxSetupError,
)
from psych_runtime.sandbox.profiles import DEFAULT_PROFILE, SandboxProfile, SandboxProfiles
from psych_runtime.sandbox.remote import RemoteSandbox
from psych_runtime.tools.policy import Decision
from psych_runtime.tools.secrets import ResolvedCredential


class SecretSource(Protocol):
    async def resolve(self, scope: Scope, name: str) -> ResolvedCredential | None: ...


_LOG = logging.getLogger("psych.playground.ports")


class AccountEgressPolicy:
    """``EgressPolicy`` over each account's own allow-list.

    Consulted by the one ``HttpTransport`` every outbound call goes through,
    which is the property DESIGN.md §14 asks for: a control that covered the
    model but not MCP, or MCP but not an HTTP tool, would be worse than none.
    An empty list allows everything, which is what a fresh account wants.
    """

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    async def allow(self, scope: Scope, url: str) -> bool:
        patterns = (await self._settings.load(scope.tenant)).runtime.egress_allow
        if not patterns:
            return True
        host = httpx.URL(url).host.lower()
        allowed = any(fnmatch.fnmatchcase(host, pattern.lower()) for pattern in patterns)
        if not allowed:
            _LOG.info(
                "egress refused for tenant %s: %s is not on the allow-list", scope.tenant, host
            )
        return allowed


class AccountToolPolicy:
    """``Policy`` over each account's denied-tool list.

    ``allow_run`` says yes to everything: which agents an account may run is
    already decided by ownership in ``app.main``, and answering it twice would
    invite someone to rely on the weaker check. ``allow_tool`` is the part a
    consumer actually writes: a name on the list is refused with a reason the
    model can read and act on, whatever the Spec granted.
    """

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    async def allow_tool(self, scope: Scope, tool: str, args: dict[str, Any]) -> Decision:
        _ = args
        denied = (await self._settings.load(scope.tenant)).runtime.denied_tools
        if tool in denied:
            return Decision.deny(
                f"the tool {tool!r} is switched off for this workspace in Settings; "
                "do something else or tell the user it is unavailable"
            )
        return Decision.allow()

    async def allow_run(self, scope: Scope, version: Version) -> Decision:
        _ = scope, version
        return Decision.allow()


class SandboxProvider:
    """Every sandbox profile this process can offer, per account.

    The ``default`` profile is the host's own local backend
    (``psych_runtime.sandbox.local_sandbox``): the strongest thing this
    operating system can do with nothing installed, decided once at boot. It
    is ``PROCESS``-level on Windows and macOS and on a Linux host without
    bubblewrap, and ``ISOLATED`` where bubblewrap is present; the console
    shows which, from the backend's own report, rather than promising one.
    An unprivileged POSIX worker needs ``PSYCH_PLAYGROUND_SANDBOX_SAME_UID=1``
    to run the local backend at all, because a same-uid child can read the
    worker's environment; the reason is reported when it is missing.

    Further profiles are the account's own configuration
    (``RuntimeSettings.sandbox_profiles``): a container image or a remote
    service. Each is built into a ``SandboxProfile`` per account and cached
    by its configuration, so a backend's ``describe()`` (which probes) runs
    once per configuration rather than once per Attempt, and ``check``
    refreshes it on demand.
    """

    def __init__(
        self,
        *,
        transport: HttpTransport,
        secrets: SecretSource,
        allow_same_uid: bool,
    ) -> None:
        self._transport = transport
        self._secrets = secrets
        self.reason: str | None = None
        self._local: Sandbox | None = None
        self.platform = sys.platform
        try:
            self._local = local_sandbox(allow_same_uid=allow_same_uid)
        except SandboxSetupError as err:
            self.reason = str(err)
        self._profiles: dict[tuple[str, str, str], SandboxProfile] = {}

    @property
    def available(self) -> bool:
        return self.reason is None

    def backends(self) -> tuple[LocalBackend, ...]:
        return detect_local_backends()

    def profile_names(self, runtime: RuntimeSettings) -> list[str]:
        names: list[str] = []
        if self.available and runtime.sandbox_enabled:
            names.append(DEFAULT_PROFILE)
        names.extend(entry.name for entry in runtime.sandbox_profiles if entry.enabled)
        return names

    def profiles_for(self, tenant: str, runtime: RuntimeSettings) -> SandboxProfiles:
        """The registry a Runtime for ``tenant`` resolves ``profile`` names in."""
        profiles = SandboxProfiles()
        default = self._default_profile(tenant, runtime)
        if default is not None:
            profiles.add(default)
        for entry in runtime.sandbox_profiles:
            if not entry.enabled or entry.name == DEFAULT_PROFILE:
                continue
            built = self._extra_profile(tenant, entry)
            if built is not None:
                profiles.add(built)
        return profiles

    def profile(self, tenant: str, runtime: RuntimeSettings, name: str) -> SandboxProfile | None:
        if name == DEFAULT_PROFILE:
            return self._default_profile(tenant, runtime)
        entry = next((e for e in runtime.sandbox_profiles if e.name == name and e.enabled), None)
        return self._extra_profile(tenant, entry) if entry is not None else None

    def _default_profile(self, tenant: str, runtime: RuntimeSettings) -> SandboxProfile | None:
        if self._local is None or not runtime.sandbox_enabled:
            return None
        limits = runtime.sandbox_limits
        signature = f"local|{limits.model_dump_json()}|{runtime.sandbox_allow_network}"
        return self._cached(
            tenant,
            DEFAULT_PROFILE,
            signature,
            lambda: SandboxProfile(
                name=DEFAULT_PROFILE,
                sandbox=self._local,  # type: ignore[arg-type]
                hard_limits=SandboxLimits(**limits.model_dump()),
                allow_network=runtime.sandbox_allow_network,
            ),
        )

    def _extra_profile(self, tenant: str, entry: SandboxProfileEntry) -> SandboxProfile | None:
        signature = entry.model_dump_json()

        def build() -> SandboxProfile:
            sandbox = self._build_backend(tenant, entry)
            return SandboxProfile(
                name=entry.name,
                sandbox=sandbox,
                hard_limits=SandboxLimits(**entry.hard_limits.model_dump()),
                allow_network=entry.allow_network,
            )

        try:
            return self._cached(tenant, entry.name, signature, build)
        except SandboxSetupError as err:
            reason = str(err)
            _LOG.warning(
                "sandbox profile %r for %s cannot be built: %s", entry.name, tenant, reason
            )
            return self._cached(
                tenant,
                entry.name,
                signature,
                lambda: SandboxProfile(name=entry.name, sandbox=_Unbuildable(reason)),
            )

    def _cached(
        self, tenant: str, name: str, signature: str, build: Callable[[], SandboxProfile]
    ) -> SandboxProfile:
        key = (tenant, name, signature)
        found = self._profiles.get(key)
        if found is None:
            found = build()
            # One live profile per (tenant, name): a changed configuration
            # replaces the old one rather than accumulating beside it.
            for stale in [k for k in self._profiles if k[:2] == (tenant, name)]:
                del self._profiles[stale]
            self._profiles[key] = found
        return found

    def _build_backend(self, tenant: str, entry: SandboxProfileEntry) -> Sandbox:
        if entry.backend == "container":
            return ContainerSandbox(
                runtime=entry.runtime,
                image=entry.image or "python:3.12-slim",
            )
        if not entry.base_url:
            raise SandboxSetupError(f"remote profile {entry.name!r} has no base_url")
        scope = Scope(tenant=tenant)
        credential_name = entry.credential

        async def token() -> str | None:
            if credential_name is None:
                return None
            resolved = await self._secrets.resolve(scope, credential_name)
            return resolved.secret.get_secret_value() if resolved is not None else None

        return RemoteSandbox(
            base_url=entry.base_url,
            transport=self._transport,
            scope=scope,
            credential=token if credential_name else None,
        )

    async def check(self, tenant: str, runtime: RuntimeSettings, name: str) -> SandboxDescription:
        """A fresh ``describe()`` for one profile, or a not-ready description
        naming why the profile does not exist."""
        profile = self.profile(tenant, runtime, name)
        if profile is None:
            reason = (
                self.reason
                if name == DEFAULT_PROFILE and self.reason
                else f"no enabled sandbox profile named {name!r} is configured"
            )
            return SandboxDescription(
                backend="none",
                platform=self.platform,
                isolation=None,
                guarantees=SandboxGuarantees(),
                ready=False,
                problems=(reason,),
            )
        profile.refresh()
        return await profile.describe()


class _Unbuildable:
    """A profile whose backend could not be constructed, as a not-ready sandbox.

    Kept as a real ``Sandbox`` so the profile still resolves and the agent
    gets a structured refusal naming the problem, rather than the Attempt
    failing on a configuration typo.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def describe(self) -> SandboxDescription:
        return SandboxDescription(
            backend="none",
            platform=sys.platform,
            isolation=None,
            guarantees=SandboxGuarantees(),
            ready=False,
            problems=(self._reason,),
        )

    async def run(self, program: str, **options: Any) -> SandboxResult:
        _ = program, options
        return SandboxResult(
            duration_seconds=0.0,
            failure=SandboxFailure(kind="backend_not_ready", message=self._reason),
        )


def same_uid_opt_in() -> bool:
    return os.environ.get("PSYCH_PLAYGROUND_SANDBOX_SAME_UID", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
