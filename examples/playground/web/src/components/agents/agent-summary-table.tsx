"use client";

import { PlugIcon, WrenchIcon } from "lucide-react";

import { FeatureGroup, FeatureRow, FeatureTable } from "@/components/ui/feature-table";
import { StatusDot } from "@/components/agents/status-dot";
import { answerStyleCopy } from "@/components/agents/answer-style";
import { toolClassTitle as classTitle } from "@/components/agents/policy";
import { DEFAULT_LIMITS, LIMIT_FIELDS } from "@/components/agents/limits";
import { cn } from "@/lib/utils";
import type { AgentSummary } from "@/lib/types";

function Chip({ label, mono, icon: Icon }: { label: string; mono?: boolean; icon?: typeof PlugIcon }) {
  return (
    <span className="inline-flex max-w-56 items-center gap-1.5 rounded-full bg-surface px-2.5 py-1 text-caption text-surface-foreground">
      {Icon && <Icon className="size-3 shrink-0 text-muted-foreground" aria-hidden />}
      <span className={cn("truncate", mono && "font-technical")}>{label}</span>
    </span>
  );
}

/**
 * What a published version is, as one table rather than six sections of
 * prose: what it can reach, what it is allowed to do, and what it may spend.
 * Everything that is off says so, because "this agent cannot ask you
 * anything" is as much a fact as the reverse.
 */
export function AgentSummaryTable({ agent }: { agent: AgentSummary }) {
  const limitsKnown = Object.keys(agent.limits).length > 0;
  const changedLimits = LIMIT_FIELDS.filter(
    (field) => limitsKnown && agent.limits[field.key] !== DEFAULT_LIMITS[field.key],
  );
  const spawn = agent.spawn;

  return (
    <FeatureTable dense>
      <FeatureGroup title="What it can use">
        <FeatureRow
          label="Tools"
          detail={
            agent.tools.length === 0 && agent.http_tools.length === 0
              ? "None. It answers from the conversation alone."
              : undefined
          }
        >
          {(agent.tools.length > 0 || agent.http_tools.length > 0) && (
            <div className="flex flex-wrap gap-1.5">
              {agent.tools.map((name) => (
                <Chip key={name} label={name} mono icon={WrenchIcon} />
              ))}
              {agent.http_tools.map((tool) => (
                <Chip key={tool.name} label={`${tool.name} (${tool.method})`} mono icon={WrenchIcon} />
              ))}
            </div>
          )}
        </FeatureRow>
        <FeatureRow
          label="Connections"
          help="A published agent carries its own copy of every connection's address and credential name. Only the names are reported back."
          detail={
            agent.mcp_servers.length === 0 && agent.a2a_peers.length === 0 ? "None." : undefined
          }
        >
          {(agent.mcp_servers.length > 0 || agent.a2a_peers.length > 0) && (
            <div className="flex flex-wrap gap-1.5">
              {agent.mcp_servers.map((name) => (
                <Chip key={name} label={name} icon={PlugIcon} />
              ))}
              {agent.a2a_peers.map((name) => (
                <Chip key={name} label={`${name} (A2A)`} icon={PlugIcon} />
              ))}
            </div>
          )}
        </FeatureRow>
        <FeatureRow
          label="Skills"
          help="The description sits in every prompt; the instructions load only when the model asks for them."
          detail={agent.skills.length === 0 ? "None." : undefined}
        >
          {agent.skills.length > 0 && (
            <ul className="flex flex-col gap-1.5">
              {agent.skills.map((skill) => (
                <li key={skill.name} className="flex flex-col">
                  <span className="font-technical text-caption font-medium">{skill.name}</span>
                  <span className="text-micro text-muted-foreground">{skill.description}</span>
                  <details className="mt-1">
                    <summary className="cursor-pointer text-micro text-muted-foreground hover:text-foreground">
                      Instructions ({skill.body.length.toLocaleString()} characters)
                    </summary>
                    <pre className="mt-1 max-h-56 overflow-auto rounded-md bg-surface p-2 font-technical text-micro whitespace-pre-wrap text-surface-foreground">
                      {skill.body}
                    </pre>
                  </details>
                </li>
              ))}
            </ul>
          )}
        </FeatureRow>
      </FeatureGroup>

      <FeatureGroup title="How it behaves">
        <StatusDot
          label="Ask the user questions"
          on={agent.may_ask_questions}
          help="Whether it may pause and wait for something from the person."
        />
        <StatusDot
          label="Plan with a task list"
          on={agent.tasks_enabled}
          help="Whether it keeps and updates a visible plan while it works."
        />
        <StatusDot
          label="Show rich components"
          on={agent.components_enabled}
          help="Whether it may answer with cards, charts and tables rather than prose alone."
        />
        <StatusDot
          label="Start sub-agents on its own"
          on={agent.subagents_enabled}
          detail={
            spawn
              ? `Up to ${spawn.max_alive} at once, ${spawn.max_depth} deep, on ${
                  spawn.models.length === 0 ? "its own model" : spawn.models.join(" or ")
                }.`
              : undefined
          }
          help="Sub-agents the model writes itself. A sub-agent only ever gets tools this agent already holds."
        />
        <StatusDot
          label="Sub-agents"
          on={agent.subagents.length > 0}
          detail={
            agent.subagents.length > 0
              ? agent.subagents.map((ref) => ref.name).join(", ")
              : undefined
          }
          help="Agents embedded by name, called and waited for. Each is pinned at the version it was copied from."
        />
        <StatusDot
          label="Summarise long conversations"
          on={agent.compaction !== null}
          detail={
            agent.compaction
              ? `Past ${agent.compaction.trigger_tokens.toLocaleString()} tokens.`
              : undefined
          }
          help="Older messages are replaced by a summary in the prompt. Nothing is deleted from the record."
        />
        <StatusDot
          label="Run code"
          on={agent.code_execution !== null && agent.code_execution.enabled}
          detail={
            agent.code_execution?.enabled
              ? `${agent.code_execution.isolation} in the ${agent.code_execution.profile} sandbox, network ${agent.code_execution.network}.`
              : undefined
          }
          help="Whether it is offered run_code. The sandbox profile decides what actually contains the program."
        />
        <StatusDot
          label="Ask before risky actions"
          on={agent.approval_selectors === null || agent.approval_selectors.length > 0}
          detail={
            agent.approval_selectors === null
              ? "Follows this installation's rule."
              : agent.approval_selectors.length === 0
                ? "Nothing pauses."
                : `Pauses first: ${agent.approval_selectors
                    .map((s) =>
                      classTitle(
                        s.replace(/^@/, "") as Parameters<typeof classTitle>[0],
                      ).toLowerCase(),
                    )
                    .join(", ")}.`
          }
          help="Which kinds of action stop and wait for a person before they happen."
        />
        <FeatureRow
          label="Answer style"
          detail={answerStyleCopy(agent.answer_style).summary}
          control={
            <span className="text-caption text-muted-foreground">
              {answerStyleCopy(agent.answer_style).title}
            </span>
          }
        />
      </FeatureGroup>

      <FeatureGroup title="Limits and waiting">
        <FeatureRow
          label="Spending limits"
          help="What one answer may spend before it is stopped."
          detail={
            !limitsKnown
              ? "Published before limits were reported back."
              : changedLimits.length === 0
                ? "Every one at its default."
                : `${changedLimits.length} changed from the default.`
          }
        >
          {changedLimits.length > 0 && (
            <ul className="grid gap-x-4 gap-y-1 text-caption sm:grid-cols-2">
              {changedLimits.map((field) => (
                <li key={field.key} className="flex items-baseline justify-between gap-2">
                  <span className="text-muted-foreground">{field.label}</span>
                  <span className="tabular font-technical">
                    {agent.limits[field.key].toLocaleString()}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </FeatureRow>
        {agent.suspension !== null && (
          <FeatureRow
            label="Waits at most"
            help="A waiting conversation holds no worker and costs nothing. These say when waiting becomes giving up."
            detail={`${Math.round(agent.suspension.approval_expires_seconds / 3600)}h for an approval, ${Math.round(
              agent.suspension.question_expires_seconds / 3600,
            )}h for an answer, ${Math.round(
              agent.suspension.external_expires_seconds / 3600,
            )}h on something outside, ${Math.round(
              agent.suspension.children_expires_seconds / 60,
            )}m on sub-agents.`}
          />
        )}
      </FeatureGroup>
    </FeatureTable>
  );
}
