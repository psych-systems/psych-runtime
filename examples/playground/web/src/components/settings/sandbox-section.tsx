"use client";

import { useState } from "react";
import {
  CheckCircle2Icon,
  CircleAlertIcon,
  Loader2Icon,
  PlusIcon,
  SaveIcon,
  ShieldCheckIcon,
  ShieldIcon,
  StethoscopeIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import { describeApiError } from "@/lib/errors";
import { formatBytes } from "@/lib/format";
import type {
  RuntimeSettings,
  RuntimeSettingsIn,
  SandboxGuarantees,
  SandboxLimitsIn,
  SandboxProfileHealth,
  SandboxProfileIn,
} from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Section } from "@/components/ui/page";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

const GUARANTEE_LABELS: Record<keyof SandboxGuarantees, string> = {
  filesystem: "Host files hidden",
  network: "Network denied",
  process_tree: "Process tree contained",
  identity: "Separate account",
  cpu: "CPU capped",
  memory: "Memory capped",
  file_size: "File size capped",
  process_count: "Process count capped",
  wall_clock: "Wall clock enforced",
  environment: "Environment scrubbed",
};

const PLATFORM_NAMES: Record<string, string> = {
  linux: "Linux",
  darwin: "macOS",
  win32: "Windows",
};

const DEFAULT_LIMITS: SandboxLimitsIn = {
  cpu_seconds: 10,
  address_space_bytes: 512 * 1024 * 1024,
  file_size_bytes: 10 * 1024 * 1024,
  process_count: 64,
  wall_seconds: 30,
};

function toIn(runtime: RuntimeSettings): RuntimeSettingsIn {
  return {
    cost_policy: runtime.cost_policy,
    blob_offload_bytes: runtime.blob_offload_bytes,
    catalogue_budget_chars: runtime.catalogue_budget_chars,
    sandbox_enabled: runtime.sandbox_enabled,
    sandbox_limits: runtime.sandbox_limits,
    sandbox_allow_network: runtime.sandbox_allow_network,
    sandbox_profiles: runtime.sandbox_profiles.map((p) => ({ ...p })),
    egress_allow: runtime.egress_allow,
    denied_tools: runtime.denied_tools,
  };
}

/**
 * Where an agent's programs run, and what that is worth on this host.
 *
 * Two kinds of thing on one screen, and they are kept visibly apart. The
 * `default` profile is this machine's own backend: what it is, and the level
 * it reaches, come from the process's own detection and probe rather than
 * from anything written here, because the difference between "isolated" and
 * "a process on the same account" is the whole question a person is asking.
 * Further profiles are configuration: a container image, or a service by
 * URL with the name of the secret that authenticates to it. Each has a
 * check that asks the backend what it can actually do, without running any
 * agent's code.
 */
export function SandboxSection({
  runtime,
  secrets,
  onSave,
  onCheck,
}: {
  runtime: RuntimeSettings;
  secrets: string[];
  onSave: (runtime: RuntimeSettingsIn) => Promise<void>;
  onCheck: (name: string) => Promise<SandboxProfileHealth>;
}) {
  const [draft, setDraft] = useState<RuntimeSettingsIn>(() => toIn(runtime));
  const [saving, setSaving] = useState(false);
  const [health, setHealth] = useState<Record<string, SandboxProfileHealth | "checking">>({});

  const dirty = JSON.stringify(draft) !== JSON.stringify(toIn(runtime));
  const platform = PLATFORM_NAMES[runtime.sandbox_platform] ?? runtime.sandbox_platform;
  const local = runtime.sandbox_backends.find((b) => b.available) ?? null;

  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft);
      toast.success("Sandbox settings saved");
    } catch (err) {
      toast.error(describeApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const check = async (name: string) => {
    setHealth((prev) => ({ ...prev, [name]: "checking" }));
    try {
      const report = await onCheck(name);
      setHealth((prev) => ({ ...prev, [name]: report }));
    } catch (err) {
      toast.error(describeApiError(err));
      setHealth((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  };

  const updateProfile = (index: number, patch: Partial<SandboxProfileIn>) => {
    setDraft({
      ...draft,
      sandbox_profiles: draft.sandbox_profiles.map((p, i) =>
        i === index ? { ...p, ...patch } : p
      ),
    });
  };

  const addProfile = (backend: "container" | "remote") => {
    const base = backend === "container" ? "pods" : "cluster";
    const taken = new Set(draft.sandbox_profiles.map((p) => p.name));
    let name = base;
    let n = 2;
    while (taken.has(name) || name === "default") name = `${base}-${n++}`;
    setDraft({
      ...draft,
      sandbox_profiles: [
        ...draft.sandbox_profiles,
        {
          name,
          backend,
          enabled: true,
          hard_limits: { ...DEFAULT_LIMITS },
          allow_network: false,
          image: backend === "container" ? "python:3.12-slim" : null,
          runtime: null,
          base_url: backend === "remote" ? "" : null,
          credential: null,
        },
      ],
    });
  };

  return (
    <Section
      title="Where agents run code"
      description="Sandbox profiles an agent can name. An agent asks for a profile and an isolation level; a profile decides what actually runs the program and reports what it can guarantee, never more."
    >
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-4 rounded-xl border border-border p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 flex-col gap-0.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-technical text-body font-medium">default</span>
                <Badge variant="outline">this machine</Badge>
                {local && <LevelBadge level={local.isolation} />}
              </div>
              <span className="text-caption text-muted-foreground">
                {runtime.sandbox_available
                  ? `${localDescription(local?.name ?? null, platform)}`
                  : `Unavailable on this ${platform} host.`}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <CheckButton
                state={health.default}
                disabled={!runtime.sandbox_available}
                onClick={() => void check("default")}
              />
              <Switch
                checked={draft.sandbox_enabled}
                onCheckedChange={(sandbox_enabled) => setDraft({ ...draft, sandbox_enabled })}
                aria-label="Offer the default profile"
                disabled={!runtime.sandbox_available}
              />
            </div>
          </div>

          {!runtime.sandbox_available && (
            <Alert>
              <CircleAlertIcon />
              <AlertTitle>No local backend can be built here</AlertTitle>
              <AlertDescription>
                {runtime.sandbox_unavailable_reason ??
                  "This host cannot run programs locally."}{" "}
                Add a container or remote profile below, or fix the host.
              </AlertDescription>
            </Alert>
          )}

          {local && local.isolation === "process" && (
            <p className="flex items-start gap-2 rounded-lg bg-surface/60 px-3 py-2 text-caption text-muted-foreground">
              <ShieldIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                Process-level only: the program is a fresh process with resource limits, a scrubbed
                environment and a temporary working directory, but it shares this account&apos;s
                view of the filesystem and the network is not denied. Agents that require{" "}
                <strong>isolated</strong> will be refused here, with the reason returned to them.{" "}
                {runtime.sandbox_backends
                  .filter((b) => !b.available && b.isolation === "isolated")
                  .map((b) => b.reason)
                  .filter(Boolean)
                  .join(" ")}
              </span>
            </p>
          )}

          {typeof health.default === "object" && <HealthReport report={health.default} />}

          {draft.sandbox_enabled && runtime.sandbox_available && (
            <LimitsGrid
              id="default"
              value={draft.sandbox_limits}
              allowNetwork={draft.sandbox_allow_network}
              onChange={(sandbox_limits) => setDraft({ ...draft, sandbox_limits })}
              onAllowNetwork={(sandbox_allow_network) =>
                setDraft({ ...draft, sandbox_allow_network })
              }
            />
          )}
        </div>

        {draft.sandbox_profiles.map((profile, index) => (
          <div key={index} className="flex flex-col gap-4 rounded-xl border border-border p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row sm:items-center">
                <div className="flex flex-col gap-1">
                  <Label htmlFor={`sb-name-${index}`} className="sr-only">
                    Profile name
                  </Label>
                  <Input
                    id={`sb-name-${index}`}
                    value={profile.name}
                    spellCheck={false}
                    className="h-8 font-technical sm:w-44"
                    onChange={(e) => updateProfile(index, { name: e.target.value })}
                  />
                </div>
                <Badge variant="outline">
                  {profile.backend === "container" ? "container" : "remote service"}
                </Badge>
                <LevelBadge level="isolated" hint="as the backend reports it" />
              </div>
              <div className="flex items-center gap-2">
                <CheckButton
                  state={health[profile.name]}
                  disabled={dirty}
                  title={dirty ? "Save first, then check" : undefined}
                  onClick={() => void check(profile.name)}
                />
                <Switch
                  checked={profile.enabled}
                  onCheckedChange={(enabled) => updateProfile(index, { enabled })}
                  aria-label={`Offer ${profile.name}`}
                />
                <Button
                  size="icon-xs"
                  variant="ghost"
                  aria-label={`Remove ${profile.name}`}
                  onClick={() =>
                    setDraft({
                      ...draft,
                      sandbox_profiles: draft.sandbox_profiles.filter((_, i) => i !== index),
                    })
                  }
                >
                  <Trash2Icon />
                </Button>
              </div>
            </div>

            {profile.backend === "container" ? (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1">
                  <Label htmlFor={`sb-image-${index}`}>Image</Label>
                  <Input
                    id={`sb-image-${index}`}
                    value={profile.image ?? ""}
                    spellCheck={false}
                    className="font-technical"
                    placeholder="python:3.12-slim"
                    onChange={(e) => updateProfile(index, { image: e.target.value })}
                  />
                  <p className="text-micro text-muted-foreground">
                    Pulled or built ahead of time; nothing pulls it for you. Prefer an image
                    digest for a reproducible deployment.
                  </p>
                </div>
                <div className="flex flex-col gap-1">
                  <Label htmlFor={`sb-runtime-${index}`}>Runtime</Label>
                  <Select
                    value={profile.runtime ?? "auto"}
                    onValueChange={(v) =>
                      updateProfile(index, {
                        runtime: v === "auto" ? null : (v as "docker" | "podman"),
                      })
                    }
                  >
                    <SelectTrigger id={`sb-runtime-${index}`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">Detect (docker, then podman)</SelectItem>
                      <SelectItem value="docker">docker</SelectItem>
                      <SelectItem value="podman">podman</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1">
                  <Label htmlFor={`sb-url-${index}`}>Service URL</Label>
                  <Input
                    id={`sb-url-${index}`}
                    value={profile.base_url ?? ""}
                    spellCheck={false}
                    className="font-technical"
                    placeholder="https://sandboxes.internal.example.com"
                    onChange={(e) => updateProfile(index, { base_url: e.target.value })}
                  />
                  <p className="text-micro text-muted-foreground">
                    A service speaking the sandbox protocol, yours or a provider&apos;s. Every
                    call to it passes the egress policy above.
                  </p>
                </div>
                <div className="flex flex-col gap-1">
                  <Label htmlFor={`sb-cred-${index}`}>Credential</Label>
                  <Select
                    value={profile.credential ?? "none"}
                    onValueChange={(v) =>
                      updateProfile(index, { credential: v === "none" ? null : v })
                    }
                  >
                    <SelectTrigger id={`sb-cred-${index}`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">No credential</SelectItem>
                      {secrets.map((name) => (
                        <SelectItem key={name} value={name}>
                          {name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-micro text-muted-foreground">
                    The name of a secret from the list below, sent as a bearer token. The value
                    never appears here, in an agent, or in a record.
                  </p>
                </div>
              </div>
            )}

            {typeof health[profile.name] === "object" && (
              <HealthReport report={health[profile.name] as SandboxProfileHealth} />
            )}

            <LimitsGrid
              id={`p${index}`}
              value={profile.hard_limits}
              allowNetwork={profile.allow_network}
              onChange={(hard_limits) => updateProfile(index, { hard_limits })}
              onAllowNetwork={(allow_network) => updateProfile(index, { allow_network })}
            />
          </div>
        ))}

        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => addProfile("container")}>
            <PlusIcon /> Container profile
          </Button>
          <Button size="sm" variant="outline" onClick={() => addProfile("remote")}>
            <PlusIcon /> Remote profile
          </Button>
          <span className="ml-auto flex items-center gap-2">
            {dirty && !saving && (
              <span className="text-caption text-muted-foreground">Unsaved changes.</span>
            )}
            <Button size="sm" disabled={!dirty || saving} onClick={() => void save()}>
              {saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
              {saving ? "Saving" : "Save sandbox settings"}
            </Button>
          </span>
        </div>
      </div>
    </Section>
  );
}

function localDescription(backend: string | null, platform: string): string {
  switch (backend) {
    case "bubblewrap":
      return `Linux namespaces through bubblewrap: a private root, no network, kernel-enforced limits.`;
    case "windows-job":
      return `A job object on ${platform}: the process tree is contained and capped, on this account, with the host's files visible.`;
    case "subprocess":
      return `A fresh process on ${platform} with resource limits and a scrubbed environment, on this account, with the host's files visible.`;
    default:
      return `This host's own backend.`;
  }
}

function LevelBadge({ level, hint }: { level: "isolated" | "process"; hint?: string }) {
  const Icon = level === "isolated" ? ShieldCheckIcon : ShieldIcon;
  return (
    <Badge
      variant="outline"
      className={cn("gap-1", level === "isolated" ? "text-status-done" : "text-status-waiting")}
      title={hint}
    >
      <Icon className="size-3" aria-hidden />
      {level === "isolated" ? "isolated" : "process only"}
    </Badge>
  );
}

function CheckButton({
  state,
  disabled,
  title,
  onClick,
}: {
  state: SandboxProfileHealth | "checking" | undefined;
  disabled?: boolean;
  title?: string;
  onClick: () => void;
}) {
  const checking = state === "checking";
  return (
    <Button
      size="sm"
      variant="outline"
      disabled={disabled || checking}
      title={title}
      onClick={onClick}
      aria-live="polite"
    >
      {checking ? (
        <Loader2Icon className="animate-spin" />
      ) : typeof state === "object" ? (
        state.ready ? (
          <CheckCircle2Icon className="text-status-done" />
        ) : (
          <CircleAlertIcon className="text-status-failed" />
        )
      ) : (
        <StethoscopeIcon />
      )}
      {checking ? "Checking" : "Check"}
    </Button>
  );
}

/** What the backend said about itself, laid out so a reader can tell a
 *  promise from an observation: the ten guarantees each carry the state the
 *  backend reported, not a tick invented here. */
function HealthReport({ report }: { report: SandboxProfileHealth }) {
  const platform = PLATFORM_NAMES[report.platform] ?? report.platform;
  return (
    <div
      role="status"
      className={cn(
        "flex flex-col gap-3 rounded-lg border px-3 py-2.5",
        report.ready ? "border-status-done/30 bg-status-done/5" : "border-status-failed/30 bg-status-failed/5"
      )}
    >
      <div className="flex flex-wrap items-center gap-2 text-caption">
        {report.ready ? (
          <CheckCircle2Icon className="size-4 text-status-done" aria-hidden />
        ) : (
          <CircleAlertIcon className="size-4 text-status-failed" aria-hidden />
        )}
        <span className="font-medium">
          {report.ready ? "Ready" : report.configured ? "Not ready" : "Not configured"}
        </span>
        {report.configured && (
          <>
            <span className="text-muted-foreground">
              {report.backend} on {platform}
            </span>
            {report.isolation && <LevelBadge level={report.isolation} />}
          </>
        )}
        <span className="ml-auto text-micro text-muted-foreground">
          checked in {report.checked_in_ms} ms
        </span>
      </div>
      {report.problems.length > 0 && (
        <ul className="list-disc pl-5 text-caption text-status-failed">
          {report.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {report.configured && (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-micro sm:grid-cols-2">
          {(Object.keys(GUARANTEE_LABELS) as (keyof SandboxGuarantees)[]).map((key) => (
            <div key={key} className="flex items-center justify-between gap-2">
              <dt className="text-muted-foreground">{GUARANTEE_LABELS[key]}</dt>
              <dd
                className={cn(
                  "font-technical",
                  report.guarantees[key] === "enforced" && "text-status-done",
                  report.guarantees[key] === "unavailable" && "text-muted-foreground",
                  report.guarantees[key] === "unverified" && "text-status-waiting"
                )}
              >
                {report.guarantees[key]}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {report.mechanisms.length > 0 && (
        <p className="text-micro text-muted-foreground">
          Mechanisms: {report.mechanisms.join(", ")}.
          {report.network_grant_supported ? " Network can be granted." : " Network cannot be granted."}
          {report.artifacts_supported ? " Files are collected." : " Files are not collected."}
        </p>
      )}
      {report.notes.map((note) => (
        <p key={note} className="text-micro text-muted-foreground">
          {note}
        </p>
      ))}
    </div>
  );
}

function LimitsGrid({
  id,
  value,
  allowNetwork,
  onChange,
  onAllowNetwork,
}: {
  id: string;
  value: SandboxLimitsIn;
  allowNetwork: boolean;
  onChange: (next: SandboxLimitsIn) => void;
  onAllowNetwork: (next: boolean) => void;
}) {
  const number = (key: keyof SandboxLimitsIn, label: string, step = 1) => (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`sb-${id}-${key}`}>{label}</Label>
      <Input
        id={`sb-${id}-${key}`}
        type="number"
        className="tabular"
        min={1}
        step={step}
        value={value[key]}
        onChange={(e) => {
          const parsed = e.target.valueAsNumber;
          onChange({ ...value, [key]: Number.isFinite(parsed) ? parsed : value[key] });
        }}
      />
    </div>
  );
  return (
    <div className="flex flex-col gap-3">
      <p className="text-caption text-muted-foreground">
        Ceilings for every agent on this profile. An agent&apos;s own request can only lower them.
      </p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {number("cpu_seconds", "CPU seconds")}
        {number("wall_seconds", "Wall clock seconds")}
        <div className="flex flex-col gap-1">
          <Label htmlFor={`sb-${id}-memory`}>Memory (MB)</Label>
          <Input
            id={`sb-${id}-memory`}
            type="number"
            className="tabular"
            min={1}
            value={Math.round(value.address_space_bytes / (1024 * 1024))}
            onChange={(e) => {
              const parsed = e.target.valueAsNumber;
              if (Number.isFinite(parsed) && parsed > 0) {
                onChange({ ...value, address_space_bytes: Math.round(parsed * 1024 * 1024) });
              }
            }}
          />
          <span className="text-micro text-muted-foreground">
            {formatBytes(value.address_space_bytes)}
          </span>
        </div>
        {number("process_count", "Processes")}
      </div>
      <label className="flex items-center gap-3 text-caption">
        <Switch checked={allowNetwork} onCheckedChange={onAllowNetwork} />
        <span>
          Let agents ask for raw network access on this profile. Off keeps every fetch behind a
          host tool and the egress policy.
        </span>
      </label>
    </div>
  );
}
