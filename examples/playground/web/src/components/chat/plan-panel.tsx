"use client";

import { CheckIcon, CircleIcon, LoaderIcon } from "lucide-react";

import type { Task } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The agent's plan, while it is working through it.
 *
 * This is the one thing that makes a twelve-turn Run legible while it runs.
 * Without it a long job is an opaque stretch of tool calls and the only honest
 * thing the console can say is "still going".
 *
 * ## Why the running step reads differently
 *
 * A plan is a list of imperatives like "Look up the order", and imperatives are
 * the right form for something not yet done. The step happening *now* is not
 * an instruction to anybody, it is a status, so it renders from `active_form`:
 * "Looking up the order". That field exists for exactly this line and nothing
 * else, and a model that leaves it blank falls back to the title rather than
 * showing nothing.
 *
 * ## Only three states
 *
 * Pending, running, done. The agent has no vocabulary for blocked or deferred
 * and should not: a status set a model has to choose between spends its
 * attention on the taxonomy rather than on the work.
 */
export function PlanPanel({ tasks }: { tasks: Task[] }) {
  if (tasks.length === 0) return null;

  const done = tasks.filter((task) => task.status === "completed").length;

  return (
    <section className="flex w-full flex-col gap-2 rounded-xl border border-border bg-surface/40 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-caption font-medium">Plan</h3>
        <span className="tabular text-micro text-muted-foreground">
          {done} of {tasks.length} done
        </span>
      </div>
      <ol className="flex flex-col gap-1.5">
        {tasks.map((task, index) => (
          <li key={index} className="flex items-start gap-2">
            <TaskIcon status={task.status} />
            <span className="flex min-w-0 flex-col gap-0.5">
              <span
                className={cn(
                  "text-body",
                  task.status === "completed" && "text-muted-foreground line-through",
                  task.status === "in_progress" && "font-medium"
                )}
              >
                {task.status === "in_progress"
                  ? task.active_form.trim() || task.title
                  : task.title}
              </span>
              {task.description && (
                <span className="text-caption text-muted-foreground">{task.description}</span>
              )}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function TaskIcon({ status }: { status: Task["status"] }) {
  if (status === "completed") {
    return <CheckIcon className="mt-1 size-3.5 shrink-0 text-status-completed" aria-label="Done" />;
  }
  if (status === "in_progress") {
    return (
      <LoaderIcon
        className="mt-1 size-3.5 shrink-0 animate-spin text-status-running"
        aria-label="In progress"
      />
    );
  }
  return (
    <CircleIcon className="mt-1 size-3.5 shrink-0 text-muted-foreground/50" aria-label="Pending" />
  );
}
