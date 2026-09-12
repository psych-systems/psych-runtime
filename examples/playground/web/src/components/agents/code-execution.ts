import type { CodeExecutionIn, IsolationLevel } from "@/lib/types";

/**
 * The form's view of an agent's code-execution terms, mirroring
 * `psych_runtime.CodeExecution` field for field with the same defaults the
 * server applies, so a value this form accepts is a value the publish accepts.
 *
 * Every number here is a request. The sandbox profile the agent names carries
 * the ceiling, and the effective cap for a run is the smaller of the two on
 * every dimension; the form says so beside each field rather than letting a
 * person believe a larger number here buys a larger budget.
 */
export interface CodeExecutionFormState {
  profile: string;
  isolation: IsolationLevel;
  network: "denied" | "unrestricted";
  /** Empty string means "the profile's ceiling". */
  cpu_seconds: string;
  wall_seconds: string;
  memory_mb: string;
  process_count: string;
  /** `null` means every tool the agent holds; a list narrows. */
  bindings: string[] | null;
  preview_bytes: number;
  preserve_output: "when_available" | "required" | "never";
  collect_artifacts: boolean;
  max_artifacts: number;
}

export const DEFAULT_CODE_EXECUTION: CodeExecutionFormState = {
  profile: "default",
  isolation: "isolated",
  network: "denied",
  cpu_seconds: "",
  wall_seconds: "",
  memory_mb: "",
  process_count: "",
  bindings: null,
  preview_bytes: 4000,
  preserve_output: "when_available",
  collect_artifacts: true,
  max_artifacts: 16,
};

export const ISOLATION_COPY: Record<
  IsolationLevel,
  { label: string; description: string }
> = {
  isolated: {
    label: "Isolated",
    description:
      "A kernel boundary: private filesystem, no network unless granted, the whole process tree contained. The safe choice for code the model writes. A profile that cannot provide it refuses to run the program rather than running it weaker.",
  },
  process: {
    label: "Process only",
    description:
      "A fresh process with resource limits, a scrubbed environment and a temporary working directory, but the host's files stay readable and the network may be reachable. For code you trust; never chosen for you.",
  },
};

export const PRESERVE_COPY: Record<
  CodeExecutionFormState["preserve_output"],
  { label: string; description: string }
> = {
  when_available: {
    label: "Keep when a blob store is wired",
    description: "Output beyond the preview is kept in full where there is somewhere to keep it, otherwise only the preview survives and the result says so.",
  },
  required: {
    label: "Always keep in full",
    description: "A program whose output cannot be kept fails the call rather than losing bytes.",
  },
  never: {
    label: "Preview only",
    description: "Only what the model sees is kept. Cheapest, and nothing can be read back later.",
  },
};

function optionalNumber(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export function validateCodeExecution(value: CodeExecutionFormState): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [key, label] of [
    ["cpu_seconds", "CPU seconds"],
    ["wall_seconds", "Wall clock seconds"],
    ["memory_mb", "Memory"],
    ["process_count", "Processes"],
  ] as const) {
    const text = value[key].trim();
    if (text === "") continue;
    const parsed = Number(text);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      errors[`code_execution.${key}`] = `${label} must be a positive number, or empty for the profile's limit.`;
    }
  }
  if (value.preview_bytes < 256 || value.preview_bytes > 65_536) {
    errors["code_execution.preview_bytes"] = "Between 256 and 65,536 bytes.";
  }
  if (value.max_artifacts < 0 || value.max_artifacts > 256) {
    errors["code_execution.max_artifacts"] = "Between 0 and 256 files.";
  }
  if (value.profile.trim() === "") {
    errors["code_execution.profile"] = "Name a sandbox profile.";
  }
  return errors;
}

export function codeExecutionToRequest(value: CodeExecutionFormState): CodeExecutionIn {
  const memory = optionalNumber(value.memory_mb);
  return {
    enabled: true,
    profile: value.profile.trim(),
    isolation: value.isolation,
    network: value.network,
    limits: {
      cpu_seconds: optionalNumber(value.cpu_seconds),
      wall_seconds: optionalNumber(value.wall_seconds),
      memory_bytes: memory === null ? null : Math.round(memory * 1024 * 1024),
      file_size_bytes: null,
      process_count: optionalNumber(value.process_count),
    },
    bindings: value.bindings,
    preview_bytes: value.preview_bytes,
    max_output_bytes: 8 * 1024 * 1024,
    preserve_output: value.preserve_output,
    collect_artifacts: value.collect_artifacts,
    max_artifacts: value.max_artifacts,
    max_artifact_bytes: 16 * 1024 * 1024,
  };
}

export function codeExecutionFromSummary(terms: CodeExecutionIn): CodeExecutionFormState {
  const text = (n: number | null | undefined) => (n === null || n === undefined ? "" : String(n));
  return {
    profile: terms.profile,
    isolation: terms.isolation,
    network: terms.network,
    cpu_seconds: text(terms.limits.cpu_seconds),
    wall_seconds: text(terms.limits.wall_seconds),
    memory_mb:
      terms.limits.memory_bytes === null || terms.limits.memory_bytes === undefined
        ? ""
        : String(Math.round(terms.limits.memory_bytes / (1024 * 1024))),
    process_count: text(terms.limits.process_count),
    bindings: terms.bindings,
    preview_bytes: terms.preview_bytes,
    preserve_output: terms.preserve_output,
    collect_artifacts: terms.collect_artifacts,
    max_artifacts: terms.max_artifacts,
  };
}
