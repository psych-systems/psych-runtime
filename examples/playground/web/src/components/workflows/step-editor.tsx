"use client";

import { createContext, useContext } from "react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  PlusIcon,
  Trash2Icon,
} from "lucide-react";

import type {
  AgentSummary,
  BranchCase,
  Condition,
  RetryPolicy,
  Step,
  StepKind,
  ToolInfo,
  WorkflowSummary,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TechnicalDetails } from "@/components/ui/page";
import {
  ConditionEditor,
  JsonField,
  MappingEditor,
  PathField,
} from "@/components/workflows/value-editors";
import {
  CONTROL_KINDS,
  STEP_BLURBS,
  STEP_KINDS,
  changeKind,
  emptyCondition,
  emptyStep,
  stepLabel,
} from "@/components/workflows/step-model";

/**
 * One step, editable, however deep it sits.
 *
 * Recursive, because the definition is: a `parallel` holds branches and each
 * branch is one of these again. The alternative -- a flat list with an indent
 * column -- was tried in the old form and is what made a nested definition
 * impossible to express at all.
 *
 * Two rules hold the screen together. Everything a step *is* stays on the
 * front: its kind, its name, and the one or two fields that say what it does.
 * Everything a step *may* do -- a condition, a retry policy, a timeout, what
 * happens when it fails -- lives behind the same disclosure on every kind, so
 * a simple tool step is still three controls and one line.
 */

/** What the pickers inside a step offer. Passed once at the top rather than
 *  threaded through every level of the recursion. */
export interface StepCatalog {
  tools: ToolInfo[];
  agents: AgentSummary[];
  workflows: WorkflowSummary[];
}

const CatalogContext = createContext<StepCatalog>({ tools: [], agents: [], workflows: [] });

export function StepCatalogProvider({
  catalog,
  children,
}: {
  catalog: StepCatalog;
  children: React.ReactNode;
}) {
  return <CatalogContext.Provider value={catalog}>{children}</CatalogContext.Provider>;
}

export function StepEditor({
  step,
  path,
  onChange,
  onRemove,
  onMove,
  duplicateNames,
  depth = 0,
}: {
  step: Step;
  /** Stable across re-renders, and unique: it is what every input's `id` and
   *  every label's `htmlFor` are built from. */
  path: string;
  onChange: (next: Step) => void;
  onRemove?: () => void;
  onMove?: (by: -1 | 1) => void;
  /** Names used twice anywhere in the whole tree. */
  duplicateNames: string[];
  depth?: number;
}) {
  const catalog = useContext(CatalogContext);
  const nameTaken = step.name.trim() !== "" && duplicateNames.includes(step.name.trim());

  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-3">
      <div className="flex min-w-0 items-start gap-2">
        <div className="grid min-w-0 flex-1 gap-3 sm:grid-cols-[10rem_1fr]">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-kind`}>Kind</Label>
            <Select
              value={step.kind}
              onValueChange={(kind) => onChange(changeKind(step, kind as StepKind))}
            >
              <SelectTrigger id={`${path}-kind`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STEP_KINDS.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {stepLabel(kind)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-name`}>Step name</Label>
            <Input
              id={`${path}-name`}
              value={step.name}
              spellCheck={false}
              placeholder="look_up"
              aria-invalid={nameTaken || undefined}
              className="font-technical"
              onChange={(event) => onChange({ ...step, name: event.target.value })}
            />
          </div>
        </div>
        {(onMove || onRemove) && (
          <div className="mt-6 flex shrink-0 flex-col gap-0.5">
            {onMove && (
              <>
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  aria-label="Move up"
                  onClick={() => onMove(-1)}
                >
                  <ArrowUpIcon />
                </Button>
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  aria-label="Move down"
                  onClick={() => onMove(1)}
                >
                  <ArrowDownIcon />
                </Button>
              </>
            )}
            {onRemove && (
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                aria-label="Remove step"
                onClick={onRemove}
              >
                <Trash2Icon />
              </Button>
            )}
          </div>
        )}
      </div>

      <p className="text-micro text-muted-foreground">{STEP_BLURBS[step.kind]}</p>

      <KindFields step={step} path={path} onChange={onChange} depth={depth} duplicateNames={duplicateNames} catalog={catalog} />

      <StepOptions step={step} path={path} onChange={onChange} />
    </div>
  );
}

function KindFields({
  step,
  path,
  onChange,
  depth,
  duplicateNames,
  catalog,
}: {
  step: Step;
  path: string;
  onChange: (next: Step) => void;
  depth: number;
  duplicateNames: string[];
  catalog: StepCatalog;
}) {
  switch (step.kind) {
    case "tool":
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-tool`}>Tool</Label>
            <Select value={step.tool || undefined} onValueChange={(tool) => onChange({ ...step, tool })}>
              <SelectTrigger id={`${path}-tool`}>
                <SelectValue placeholder="Pick a tool" />
              </SelectTrigger>
              <SelectContent>
                {catalog.tools.map((tool) => (
                  <SelectItem key={tool.name} value={tool.name}>
                    <span className="font-technical">{tool.name}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <JsonField
            id={`${path}-args`}
            label="Arguments, written here"
            value={Object.keys(step.arguments).length > 0 ? step.arguments : null}
            placeholder={argumentPlaceholder(catalog.tools.find((one) => one.name === step.tool))}
            onChange={(next) =>
              onChange({
                ...step,
                arguments: next && typeof next === "object" && !Array.isArray(next)
                  ? (next as Record<string, unknown>)
                  : {},
              })
            }
          />
          <div className="flex flex-col gap-1.5">
            <Label>Arguments read out of the run</Label>
            <MappingEditor
              id={`${path}-argsfrom`}
              value={step.arguments_from}
              addLabel="Add an argument"
              emptyLabel="Nothing read from the run; the arguments above are used as they are."
              onChange={(arguments_from) => onChange({ ...step, arguments_from })}
            />
          </div>
        </div>
      );

    case "agent":
    case "workflow": {
      const isAgent = step.kind === "agent";
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-target`}>{isAgent ? "Agent" : "Workflow"}</Label>
            <Select
              value={(isAgent ? step.agent_id : step.workflow_id) || undefined}
              onValueChange={(id) =>
                onChange(isAgent ? { ...step, agent_id: id } : { ...step, workflow_id: id })
              }
            >
              <SelectTrigger id={`${path}-target`}>
                <SelectValue placeholder={isAgent ? "Pick an agent" : "Pick a workflow"} />
              </SelectTrigger>
              <SelectContent>
                {isAgent
                  ? catalog.agents.map((agent) => (
                      <SelectItem key={agent.agent_id} value={agent.agent_id}>
                        {agent.name}
                      </SelectItem>
                    ))
                  : catalog.workflows.map((one) => (
                      <SelectItem key={one.workflow_id} value={one.workflow_id}>
                        {one.name}
                      </SelectItem>
                    ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Input</Label>
            <MappingEditor
              id={`${path}-input`}
              value={step.input}
              addLabel="Add an input field"
              emptyLabel="Nothing passed in; it sees the run's own message."
              onChange={(input) => onChange({ ...step, input })}
            />
          </div>
        </div>
      );
    }

    case "parallel":
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5 sm:max-w-xs">
            <Label htmlFor={`${path}-branchfail`}>When one branch fails</Label>
            <Select
              value={step.on_branch_failure}
              onValueChange={(mode) =>
                onChange({ ...step, on_branch_failure: mode as "fail_fast" | "wait_all" })
              }
            >
              <SelectTrigger id={`${path}-branchfail`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="fail_fast">Stop the others</SelectItem>
                <SelectItem value="wait_all">Let them all finish</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <ChildList
            label="Branches"
            path={path}
            depth={depth}
            duplicateNames={duplicateNames}
            steps={step.branches}
            onChange={(branches) => onChange({ ...step, branches })}
            addLabel="Add a branch"
          />
        </div>
      );

    case "branch":
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5 sm:max-w-xs">
            <Label htmlFor={`${path}-mode`}>How many arms run</Label>
            <Select
              value={step.mode}
              onValueChange={(mode) => onChange({ ...step, mode: mode as "first" | "all" })}
            >
              <SelectTrigger id={`${path}-mode`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="first">The first that matches</SelectItem>
                <SelectItem value="all">Every one that matches</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label>Cases</Label>
            {step.cases.length === 0 && (
              <p className="text-micro text-muted-foreground">No cases yet.</p>
            )}
            {step.cases.map((one, index) => (
              <div key={index} className="flex flex-col gap-2 rounded-lg border border-border p-2.5">
                <div className="flex items-end gap-2">
                  <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                    <Label htmlFor={`${path}-case-${index}-name`}>Case name</Label>
                    <Input
                      id={`${path}-case-${index}-name`}
                      value={one.name}
                      spellCheck={false}
                      placeholder="approved"
                      onChange={(event) =>
                        replaceCase(step.cases, index, { ...one, name: event.target.value }, (cases) =>
                          onChange({ ...step, cases })
                        )
                      }
                    />
                  </div>
                  <Button
                    type="button"
                    size="icon-sm"
                    variant="ghost"
                    aria-label="Remove this case"
                    onClick={() =>
                      onChange({ ...step, cases: step.cases.filter((_, i) => i !== index) })
                    }
                  >
                    <Trash2Icon />
                  </Button>
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label>Runs when</Label>
                  <ConditionEditor
                    id={`${path}-case-${index}-when`}
                    value={one.when}
                    onChange={(when) =>
                      replaceCase(step.cases, index, { ...one, when }, (cases) =>
                        onChange({ ...step, cases })
                      )
                    }
                  />
                </div>
                <StepEditor
                  step={one.step}
                  path={`${path}-case-${index}-step`}
                  depth={depth + 1}
                  duplicateNames={duplicateNames}
                  onChange={(next) =>
                    replaceCase(step.cases, index, { ...one, step: next }, (cases) =>
                      onChange({ ...step, cases })
                    )
                  }
                />
              </div>
            ))}
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() =>
                  onChange({
                    ...step,
                    cases: [
                      ...step.cases,
                      { name: "", when: emptyCondition(), step: emptyStep("tool") },
                    ],
                  })
                }
              >
                <PlusIcon /> Add a case
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() =>
                  onChange({ ...step, otherwise: step.otherwise ? null : emptyStep("tool") })
                }
              >
                {step.otherwise ? "Remove the otherwise" : "Add an otherwise"}
              </Button>
            </div>
            {step.otherwise && (
              <div className="flex flex-col gap-2 rounded-lg border border-dashed border-border p-2.5">
                <Label>Otherwise</Label>
                <StepEditor
                  step={step.otherwise}
                  path={`${path}-else`}
                  depth={depth + 1}
                  duplicateNames={duplicateNames}
                  onChange={(otherwise) => onChange({ ...step, otherwise })}
                />
              </div>
            )}
          </div>
        </div>
      );

    case "foreach":
      return (
        <div className="flex flex-col gap-3">
          <PathField
            id={`${path}-items`}
            label="The list to walk"
            value={step.items}
            onChange={(items) => onChange({ ...step, items })}
          />
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${path}-conc`}>How many at a time</Label>
              <Input
                id={`${path}-conc`}
                type="number"
                min={1}
                value={step.concurrency}
                onChange={(event) =>
                  onChange({ ...step, concurrency: Math.max(1, Number(event.target.value) || 1) })
                }
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${path}-itemfail`}>When one item fails</Label>
              <Select
                value={step.on_item_failure}
                onValueChange={(mode) =>
                  onChange({ ...step, on_item_failure: mode as "fail_fast" | "wait_all" })
                }
              >
                <SelectTrigger id={`${path}-itemfail`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="fail_fast">Stop the other items</SelectItem>
                  <SelectItem value="wait_all">Let them all finish, then fail</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <BodyEditor
            label="For each item"
            path={`${path}-body`}
            depth={depth}
            duplicateNames={duplicateNames}
            step={step.body}
            onChange={(body) => onChange({ ...step, body })}
          />
        </div>
      );

    case "loop":
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5 sm:max-w-xs">
            <Label htmlFor={`${path}-max`}>At most this many times</Label>
            <Input
              id={`${path}-max`}
              type="number"
              min={1}
              value={step.max_iterations}
              onChange={(event) =>
                onChange({ ...step, max_iterations: Math.max(1, Number(event.target.value) || 1) })
              }
            />
          </div>
          <OptionalCondition
            id={`${path}-until`}
            label="Stop once this is true"
            value={step.until}
            onChange={(until) => onChange({ ...step, until })}
          />
          <OptionalCondition
            id={`${path}-while`}
            label="Keep going while this is true"
            value={step.while}
            onChange={(next) => onChange({ ...step, while: next })}
          />
          <BodyEditor
            label="Each time round"
            path={`${path}-body`}
            depth={depth}
            duplicateNames={duplicateNames}
            step={step.body}
            onChange={(body) => onChange({ ...step, body })}
          />
        </div>
      );

    case "map":
      return (
        <div className="flex flex-col gap-1.5">
          <Label>What it produces</Label>
          <MappingEditor
            id={`${path}-output`}
            value={step.output}
            addLabel="Add an output field"
            emptyLabel="Nothing produced yet."
            onChange={(output) => onChange({ ...step, output })}
          />
        </div>
      );

    case "set_state":
      return (
        <div className="flex flex-col gap-1.5">
          <Label>What it writes into the state</Label>
          <MappingEditor
            id={`${path}-values`}
            value={step.values}
            addLabel="Add a value"
            emptyLabel="Nothing written yet."
            onChange={(values) => onChange({ ...step, values })}
          />
        </div>
      );

    case "sleep":
      return (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-seconds`}>Seconds</Label>
            <Input
              id={`${path}-seconds`}
              type="number"
              min={0}
              value={step.seconds ?? ""}
              placeholder="leave empty to use a time instead"
              onChange={(event) =>
                onChange({
                  ...step,
                  seconds: event.target.value === "" ? null : Number(event.target.value),
                })
              }
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-until`}>Or until a time in the run</Label>
            <Input
              id={`${path}-until`}
              value={step.until?.path ?? ""}
              spellCheck={false}
              placeholder="steps.schedule.output.at"
              className="font-technical text-caption"
              onChange={(event) =>
                onChange({
                  ...step,
                  until: event.target.value === "" ? null : { kind: "path", path: event.target.value },
                })
              }
            />
          </div>
        </div>
      );

    case "wait":
      return (
        <div className="flex flex-col gap-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${path}-event`}>Event name</Label>
              <Input
                id={`${path}-event`}
                value={step.event}
                spellCheck={false}
                placeholder="invoice.paid"
                className="font-technical"
                onChange={(event) => onChange({ ...step, event: event.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${path}-waittimeout`}>Give up after (seconds)</Label>
              <Input
                id={`${path}-waittimeout`}
                type="number"
                min={0}
                value={step.timeout_seconds ?? ""}
                placeholder="never"
                onChange={(event) =>
                  onChange({
                    ...step,
                    timeout_seconds:
                      event.target.value === "" ? null : Number(event.target.value),
                  })
                }
              />
            </div>
          </div>
          <JsonField
            id={`${path}-payload`}
            label="What the event must carry, as JSON Schema"
            value={Object.keys(step.payload_schema).length > 0 ? step.payload_schema : null}
            placeholder='{"type":"object","properties":{"amount":{"type":"number"}}}'
            onChange={(next) =>
              onChange({
                ...step,
                payload_schema:
                  next && typeof next === "object" && !Array.isArray(next)
                    ? (next as Record<string, unknown>)
                    : {},
              })
            }
          />
        </div>
      );

    case "human":
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-prompt`}>What to ask</Label>
            <Input
              id={`${path}-prompt`}
              value={step.prompt}
              placeholder="Does this look right to send?"
              onChange={(event) => onChange({ ...step, prompt: event.target.value })}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-expires`}>Expires after (seconds)</Label>
            <Input
              id={`${path}-expires`}
              type="number"
              min={0}
              className="sm:max-w-40"
              value={step.expires_seconds ?? ""}
              placeholder="the default"
              onChange={(event) =>
                onChange({
                  ...step,
                  expires_seconds: event.target.value === "" ? null : Number(event.target.value),
                })
              }
            />
          </div>
          <JsonField
            id={`${path}-questions`}
            label="Questions, as JSON"
            value={step.questions.length > 0 ? step.questions : null}
            placeholder='[{"question":"Send it?","header":"send","options":[],"multi_select":false}]'
            description="Leave empty to ask the prompt on its own. A person may always answer in their own words."
            onChange={(next) =>
              onChange({ ...step, questions: Array.isArray(next) ? (next as HumanQuestions) : [] })
            }
          />
        </div>
      );
  }
}

type HumanQuestions = Extract<Step, { kind: "human" }>["questions"];

function replaceCase(
  cases: BranchCase[],
  index: number,
  next: BranchCase,
  commit: (cases: BranchCase[]) => void
) {
  commit(cases.map((one, i) => (i === index ? next : one)));
}

/** The body of a `foreach` or a `loop`: exactly one step, boxed and labelled
 *  so the nesting is visible without an indent guide. */
function BodyEditor({
  label,
  path,
  step,
  onChange,
  depth,
  duplicateNames,
}: {
  label: string;
  path: string;
  step: Step;
  onChange: (next: Step) => void;
  depth: number;
  duplicateNames: string[];
}) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed border-border bg-surface/30 p-2.5">
      <span className="w-fit rounded-md bg-muted px-1.5 py-0.5 text-micro font-medium text-muted-foreground">
        {label}
      </span>
      <StepEditor
        step={step}
        path={path}
        depth={depth + 1}
        duplicateNames={duplicateNames}
        onChange={onChange}
      />
    </div>
  );
}

/** A list of sibling steps: a `parallel`'s branches. */
function ChildList({
  label,
  path,
  steps,
  onChange,
  addLabel,
  depth,
  duplicateNames,
}: {
  label: string;
  path: string;
  steps: Step[];
  onChange: (next: Step[]) => void;
  addLabel: string;
  depth: number;
  duplicateNames: string[];
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label>{label}</Label>
      {steps.length === 0 && <p className="text-micro text-muted-foreground">None yet.</p>}
      {steps.map((child, index) => (
        <StepEditor
          key={index}
          step={child}
          path={`${path}-${index}`}
          depth={depth + 1}
          duplicateNames={duplicateNames}
          onChange={(next) => onChange(steps.map((one, i) => (i === index ? next : one)))}
          onRemove={() => onChange(steps.filter((_, i) => i !== index))}
          onMove={(by) => {
            const target = index + by;
            if (target < 0 || target >= steps.length) return;
            const next = [...steps];
            [next[index], next[target]] = [next[target], next[index]];
            onChange(next);
          }}
        />
      ))}
      <div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => onChange([...steps, emptyStep("tool")])}
        >
          <PlusIcon /> {addLabel}
        </Button>
      </div>
    </div>
  );
}

function OptionalCondition({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: Condition | null;
  onChange: (next: Condition | null) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <Label>{label}</Label>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => onChange(value ? null : emptyCondition())}
        >
          {value ? "Remove" : "Add"}
        </Button>
      </div>
      {value && <ConditionEditor id={id} value={value} onChange={onChange} />}
    </div>
  );
}

/**
 * The options every step has, behind the disclosure every step has.
 *
 * One place, one label, one order, whatever the kind. That is what keeps the
 * common case -- a tool step with a name and some arguments -- as short as it
 * was before any of this existed.
 */
function StepOptions({
  step,
  path,
  onChange,
}: {
  step: Step;
  path: string;
  onChange: (next: Step) => void;
}) {
  const count = [
    step.when !== null,
    step.retry !== null,
    step.timeout_seconds !== null,
    step.on_failure !== "fail",
    step.output_schema !== null,
    step.description !== "",
  ].filter(Boolean).length;

  return (
    <TechnicalDetails label={count === 0 ? "Options" : `Options (${count} set)`}>
      <div className="flex flex-col gap-3 pt-1">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${path}-desc`}>What this step is for</Label>
          <Input
            id={`${path}-desc`}
            value={step.description}
            placeholder="Looks the customer up by the id the form gave."
            onChange={(event) => onChange({ ...step, description: event.target.value })}
          />
        </div>

        <OptionalCondition
          id={`${path}-when`}
          label="Run it only when"
          value={step.when}
          onChange={(when) => onChange({ ...step, when })}
        />

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-timeout`}>Timeout (seconds)</Label>
            <Input
              id={`${path}-timeout`}
              type="number"
              min={0}
              value={step.timeout_seconds ?? ""}
              placeholder="none"
              onChange={(event) =>
                onChange({
                  ...step,
                  timeout_seconds: event.target.value === "" ? null : Number(event.target.value),
                })
              }
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${path}-onfail`}>When it fails</Label>
            <Select
              value={step.on_failure}
              onValueChange={(mode) =>
                onChange({ ...step, on_failure: mode as "fail" | "continue" })
              }
            >
              <SelectTrigger id={`${path}-onfail`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="fail">Stop the workflow</SelectItem>
                <SelectItem value="continue">Record it and carry on</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <RetryFields
          path={path}
          value={step.retry}
          onChange={(retry) => onChange({ ...step, retry })}
        />

        <JsonField
          id={`${path}-outschema`}
          label="What its output must look like, as JSON Schema"
          value={step.output_schema}
          placeholder='{"type":"object"}'
          onChange={(next) =>
            onChange({
              ...step,
              output_schema:
                next && typeof next === "object" && !Array.isArray(next)
                  ? (next as Record<string, unknown>)
                  : null,
            })
          }
        />
      </div>
    </TechnicalDetails>
  );
}

export const DEFAULT_RETRY: RetryPolicy = {
  max_attempts: 3,
  backoff_seconds: 1,
  multiplier: 2,
  max_backoff_seconds: 60,
  retry_on: [],
};

export function RetryFields({
  path,
  value,
  onChange,
  label = "Retry",
}: {
  path: string;
  value: RetryPolicy | null;
  onChange: (next: RetryPolicy | null) => void;
  label?: string;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <Label>{label}</Label>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => onChange(value ? null : { ...DEFAULT_RETRY })}
        >
          {value ? "Remove" : "Add"}
        </Button>
      </div>
      {value && (
        <div className="grid gap-3 sm:grid-cols-2">
          <NumberField
            id={`${path}-attempts`}
            label="Attempts at most"
            value={value.max_attempts}
            onChange={(max_attempts) => onChange({ ...value, max_attempts })}
          />
          <NumberField
            id={`${path}-backoff`}
            label="First wait (seconds)"
            value={value.backoff_seconds}
            onChange={(backoff_seconds) => onChange({ ...value, backoff_seconds })}
          />
          <NumberField
            id={`${path}-mult`}
            label="Multiplied each time by"
            value={value.multiplier}
            onChange={(multiplier) => onChange({ ...value, multiplier })}
          />
          <NumberField
            id={`${path}-maxbackoff`}
            label="Longest wait (seconds)"
            value={value.max_backoff_seconds}
            onChange={(max_backoff_seconds) => onChange({ ...value, max_backoff_seconds })}
          />
          <div className="flex flex-col gap-1.5 sm:col-span-2">
            <Label htmlFor={`${path}-retryon`}>Only these failure kinds</Label>
            <Input
              id={`${path}-retryon`}
              value={value.retry_on.join(", ")}
              spellCheck={false}
              placeholder="leave empty for every transient failure"
              className="font-technical text-caption"
              onChange={(event) =>
                onChange({
                  ...value,
                  retry_on: event.target.value
                    .split(",")
                    .map((one) => one.trim())
                    .filter(Boolean),
                })
              }
            />
          </div>
        </div>
      )}
    </div>
  );
}

function NumberField({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  onChange: (next: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="number"
        min={0}
        step="any"
        value={value}
        onChange={(event) => onChange(Number(event.target.value) || 0)}
      />
    </div>
  );
}

function argumentPlaceholder(tool: ToolInfo | undefined): string {
  if (!tool) return "{}";
  const properties =
    (tool.input_schema as { properties?: Record<string, unknown> }).properties ?? {};
  return JSON.stringify(Object.fromEntries(Object.keys(properties).map((key) => [key, "…"])));
}

/** The nine kinds that shape a run rather than do work in it, for the menu
 *  that adds one. The three ordinary kinds keep their own buttons. */
export const CONTROL_STEP_CHOICES = CONTROL_KINDS.map((kind) => ({
  kind,
  label: stepLabel(kind),
  blurb: STEP_BLURBS[kind],
}));
