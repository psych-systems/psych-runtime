"use client";

import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FeatureRow, FeatureTable, ToggleRow } from "@/components/ui/feature-table";
import { FieldGrid, NumberField } from "@/components/agents/field-bits";
import { HelpTip, LabelWithHelp } from "@/components/ui/help";
import { Textarea } from "@/components/ui/textarea";
import { ModelField } from "@/components/agents/model-field";
import { SpawnEnvelopeFields, SubagentRosterField } from "@/components/agents/subagents-field";
import { CodeExecutionFields } from "@/components/agents/code-execution-fields";
import { buildBindingGroups } from "@/components/agents/bindable-tools";
import { COMPACTION_BOUNDS, DEFAULT_COMPACTION } from "@/components/agents/compaction";
import { ALL_TOOL_CLASSES, TOOL_CLASS_COPY, selectorLabel } from "@/components/agents/policy";
import { ANSWER_STYLE_COPY, CHANGES_THE_AGENT } from "@/components/agents/answer-style";
import type { AnswerStyle } from "@/lib/types";
import type { AgentFormState } from "@/components/agents/use-agent-form";

/**
 * How the agent behaves once it is running.
 *
 * Every toggle here is on to start with except the named roster, which needs
 * other agents to exist, and running code, which needs a sandbox this
 * account actually offers. Each one is part of the version hash, so turning
 * one off publishes a different agent; none of them is a runtime setting.
 */
export function BehaviourTable({ form }: { form: AgentFormState }) {
  const bindingGroups = buildBindingGroups({
    selectedTools: form.selectedTools,
    catalogue: form.tools ?? [],
    httpTools: form.httpTools,
    connections: form.connections,
    presets: form.presets,
    approvalSelectors:
      form.approvalMode === "custom" ? form.approvalClasses.map(selectorLabel) : [],
  });
  const sandboxOffered = form.sandboxProfiles.length > 0;

  return (
    <FeatureTable>
      <ToggleRow
        id="may-ask"
        label="Ask the user questions"
        help="It pauses and waits when it needs something from you rather than guessing. A waiting conversation holds no worker and costs nothing."
        detail="Pauses when it needs something from you."
        checked={form.mayAskQuestions}
        onCheckedChange={form.setMayAskQuestions}
      />

      <ToggleRow
        id="tasks"
        label="Plan with a task list"
        help="It writes down its steps and ticks them off as it goes, so a long job is legible while it runs."
        detail="Shows and updates its steps on longer work."
        checked={form.tasksEnabled}
        onCheckedChange={form.setTasksEnabled}
      />

      <ToggleRow
        id="components"
        label="Show rich components"
        help="Comparisons, orders, itineraries, charts and key figures arrive as components in the conversation rather than as prose."
        detail="Tables, charts and cards instead of paragraphs."
        checked={form.componentsEnabled}
        onCheckedChange={form.setComponentsEnabled}
      />

      <ToggleRow
        id="subagents"
        label="Start sub-agents on its own"
        help="It may write a sub-agent and run it in the background. This is a ceiling, not a roster: a sub-agent only ever gets tools this agent already holds, so it cannot widen what the agent reaches."
        detail="Background sub-agents it writes itself."
        checked={form.subagentsEnabled}
        onCheckedChange={form.setSubagentsEnabled}
      >
        <SpawnEnvelopeFields
          value={form.spawn}
          onChange={form.setSpawn}
          tools={[
            ...form.selectedTools,
            ...form.httpTools.map((tool) => tool.name).filter(Boolean),
          ]}
          models={form.models}
        />
      </ToggleRow>

      <ToggleRow
        id="roster"
        label="Sub-agents"
        help="Agents you already published, embedded into this one by name. It hands a task over and waits. The whole tree is pinned by one version."
        detail={
          form.roster.length === 0
            ? "Off. Needs another published agent."
            : `${form.roster.length} named.`
        }
        checked={form.roster.length > 0}
        onCheckedChange={(next) => {
          if (next) form.setRoster([{ name: "", description: "", agent_id: "" }]);
          else form.setRoster([]);
        }}
        disabled={form.allAgents.filter((a) => a.agent_id !== form.editing).length === 0}
      >
        <SubagentRosterField
          value={form.roster}
          onChange={form.setRoster}
          agents={form.allAgents}
          selfId={form.editing}
          fieldErrors={form.fieldErrors}
        />
      </ToggleRow>

      <ToggleRow
        id="compaction"
        label="Summarise long conversations"
        help="Once a prompt gets large it carries a summary of the older messages instead of the messages themselves, so a conversation can keep going. Nothing is deleted: the replaced messages stay in the record and stay readable."
        detail={`Summarises past ${form.compaction.trigger_tokens.toLocaleString()} tokens.`}
        checked={form.compactionEnabled}
        onCheckedChange={form.setCompactionEnabled}
      >
        <CompactionGrid form={form} />
      </ToggleRow>

      <ToggleRow
        id="code"
        label="Run code"
        help="It may write short programs and run them in a sandbox, so it can loop, filter and join over its tools in one step. The sandbox profile decides how far the program is contained; the terms here are only what the agent asks for."
        detail={
          sandboxOffered
            ? `In the ${form.codeExecution.profile} sandbox.`
            : (form.sandboxReason ?? "No sandbox is available on this installation.")
        }
        checked={form.codeEnabled}
        onCheckedChange={form.setCodeEnabled}
        disabled={!sandboxOffered && !form.codeEnabled}
      >
        <CodeExecutionFields
          value={form.codeExecution}
          onChange={form.setCodeExecution}
          fieldErrors={form.fieldErrors}
          toolGroups={bindingGroups}
          profiles={form.sandboxProfiles.length > 0 ? form.sandboxProfiles : ["default"]}
          runtime={form.settings?.runtime ?? null}
        />
      </ToggleRow>

      <ToggleRow
        id="approvals"
        label="Ask before risky actions"
        help="Which kinds of action stop and wait for a person. Off, this agent follows whatever rule the installation is set to, including if that changes later."
        detail={
          form.approvalMode === "backend"
            ? "Follows this installation's rule."
            : form.approvalClasses.length === 0
              ? "Nothing pauses."
              : form.approvalClasses
                  .map((cls) => TOOL_CLASS_COPY[cls].title.toLowerCase())
                  .join(" and ")
        }
        checked={form.approvalMode === "custom"}
        onCheckedChange={(next) => form.setApprovalMode(next ? "custom" : "backend")}
      >
        <div className="flex flex-col gap-2">
          {ALL_TOOL_CLASSES.map((cls) => (
            <label key={cls} className="flex items-start gap-2.5">
              <Checkbox
                className="mt-0.5"
                checked={form.approvalClasses.includes(cls)}
                onCheckedChange={() =>
                  form.setApprovalClasses(
                    form.approvalClasses.includes(cls)
                      ? form.approvalClasses.filter((c) => c !== cls)
                      : [...form.approvalClasses, cls],
                  )
                }
              />
              <span className="flex min-w-0 flex-col">
                <span className="inline-flex items-center gap-1.5">
                  <span className="text-caption font-medium">{TOOL_CLASS_COPY[cls].title}</span>
                  <HelpTip title={TOOL_CLASS_COPY[cls].title} short={TOOL_CLASS_COPY[cls].short}>
                    <p>{TOOL_CLASS_COPY[cls].blurb}</p>
                    <p>{TOOL_CLASS_COPY[cls].consequence}</p>
                  </HelpTip>
                </span>
                {/* One line. The rest is behind the "?" beside it. */}
                <span className="truncate text-micro text-muted-foreground">
                  {TOOL_CLASS_COPY[cls].short}
                </span>
              </span>
            </label>
          ))}
          {form.approvalClasses.length === 0 && (
            <p className="text-caption text-muted-foreground">
              Nothing pauses. The agent goes ahead with everything it is allowed to use.
            </p>
          )}
        </div>
      </ToggleRow>

      <FeatureRow
        label="Answer style"
        htmlFor="answer-style"
        help={
          <>
            <p>{ANSWER_STYLE_COPY.detailed.blurb}</p>
            <p>Concise: {ANSWER_STYLE_COPY.concise.blurb}</p>
            <p>{CHANGES_THE_AGENT}</p>
          </>
        }
        detail={
          form.answerStyle === "concise"
            ? ANSWER_STYLE_COPY.concise.summary
            : ANSWER_STYLE_COPY.detailed.summary
        }
        control={
          <Select
            value={form.answerStyle ?? "__detailed__"}
            onValueChange={(next) =>
              form.setAnswerStyle(next === "__detailed__" ? null : (next as AnswerStyle))
            }
          >
            <SelectTrigger id="answer-style" className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__detailed__">Detailed</SelectItem>
              <SelectItem value="concise">Concise</SelectItem>
            </SelectContent>
          </Select>
        }
      />
    </FeatureTable>
  );
}

function CompactionGrid({ form }: { form: AgentFormState }) {
  const { compaction, setCompaction, fieldErrors } = form;
  return (
    <div className="flex flex-col gap-4">
      <FieldGrid columns={2}>
        <NumberField
          id="compaction-trigger"
          label="Summarise past"
          suffix="tokens"
          help="Tokens in the last prompt, counted by the provider. Set it well under the model's window: the count arrives one reply late, so a whole further reply has to fit underneath."
          min={1}
          step={1000}
          value={compaction.trigger_tokens}
          error={fieldErrors["compaction.trigger_tokens"]}
          onChange={(next) =>
            setCompaction({
              ...compaction,
              trigger_tokens: Number(next) || compaction.trigger_tokens,
            })
          }
        />
        <NumberField
          id="compaction-keep"
          label="Replies kept word for word"
          help="The most recent replies stay below the summary untouched, with the tool results they produced. Fewer is cheaper and blunter."
          min={COMPACTION_BOUNDS.keep_recent_turns.min}
          max={COMPACTION_BOUNDS.keep_recent_turns.max}
          value={compaction.keep_recent_turns}
          error={fieldErrors["compaction.keep_recent_turns"]}
          onChange={(next) =>
            setCompaction({
              ...compaction,
              keep_recent_turns: Number(next) || compaction.keep_recent_turns,
            })
          }
        />
        <NumberField
          id="compaction-cap"
          label="Longest the summary may be"
          suffix="tokens"
          help="A summary allowed to run as long as the conversation it replaced saves nothing."
          min={COMPACTION_BOUNDS.max_summary_tokens.min}
          max={COMPACTION_BOUNDS.max_summary_tokens.max}
          step={128}
          value={compaction.max_summary_tokens}
          error={fieldErrors["compaction.max_summary_tokens"]}
          onChange={(next) =>
            setCompaction({
              ...compaction,
              max_summary_tokens: Number(next) || compaction.max_summary_tokens,
            })
          }
        />
        <div className="flex min-w-0 flex-col gap-1.5">
          <LabelWithHelp
            htmlFor="compaction-model"
            label="Model that writes it"
            help="Summarising is a mechanical read of a transcript, so a cheaper model here usually costs nothing in quality. Leave it unset to use the agent's own."
          />
          <ModelField
            id="compaction-model"
            value={compaction.model}
            onChange={(model) => setCompaction({ ...compaction, model })}
            models={form.models}
          prices={form.modelPrices}
            detail={null}
            placeholder="The agent's own model"
            invalid={fieldErrors["compaction.model"] !== undefined}
          />
        </div>
      </FieldGrid>
      <div className="flex flex-col gap-1.5">
        <LabelWithHelp
          htmlFor="compaction-instructions"
          label="What it must not lose"
          help="Added to what Psych already keeps, never instead of it. It already keeps the ask, what tool results established, decisions and why, and what is outstanding. Name what your work in particular cannot lose."
        />
        <Textarea
          id="compaction-instructions"
          rows={2}
          value={compaction.summary_instructions}
          placeholder="Always keep every order number and its status."
          aria-invalid={fieldErrors["compaction.summary_instructions"] !== undefined}
          onChange={(event) =>
            setCompaction({ ...compaction, summary_instructions: event.target.value })
          }
        />
        {fieldErrors["compaction.summary_instructions"] && (
          <p className="text-caption text-status-failed">
            {fieldErrors["compaction.summary_instructions"]}
          </p>
        )}
      </div>
      {(compaction.trigger_tokens !== DEFAULT_COMPACTION.trigger_tokens ||
        compaction.keep_recent_turns !== DEFAULT_COMPACTION.keep_recent_turns) && (
        <button
          type="button"
          className="self-start text-micro text-muted-foreground underline underline-offset-2 hover:text-foreground"
          onClick={() => setCompaction({ ...DEFAULT_COMPACTION })}
        >
          Reset to the defaults
        </button>
      )}
    </div>
  );
}
