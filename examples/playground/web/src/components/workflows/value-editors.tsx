"use client";

import { useState } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";

import type { Condition, Mapping, MappingValue, ValuePath } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FieldError } from "@/components/settings/validation";
import {
  CONDITION_OPS,
  emptyCondition,
  isUnaryOp,
  isValuePath,
  mappingRows,
  rowsToMapping,
} from "@/components/workflows/step-model";
import { cn } from "@/lib/utils";

/** What a path may start with, shown wherever one is typed. Guessing the
 *  roots is the single most common way a definition fails at publish. */
export const PATH_HINT =
  "input.…, steps.<step>.output.…, state.…, or item / index / iteration inside a loop";

/**
 * A JSON value, typed.
 *
 * Keeps the text the person typed rather than re-printing the parsed value on
 * every keystroke: re-printing moves the caret and deletes a half-typed
 * string the moment it briefly parses. The parsed value is only handed up
 * when the text parses, and the error line stays until it does.
 */
export function JsonField({
  id,
  label,
  value,
  onChange,
  placeholder = "{}",
  rows = 3,
  allowEmpty = true,
  description,
}: {
  id: string;
  label?: string;
  value: unknown;
  onChange: (value: unknown) => void;
  placeholder?: string;
  rows?: number;
  /** Empty text means `null` rather than an error. */
  allowEmpty?: boolean;
  description?: string;
}) {
  const [text, setText] = useState(() => printJson(value));
  const [error, setError] = useState<string | null>(null);

  // A value replaced from outside (a kind change, a definition loading) has
  // to reach the box; a value this box itself just produced must not, or the
  // caret jumps. Adjusted while rendering rather than in an effect: an effect
  // would render the stale text once first, and React's own guidance is that
  // state derived from a prop belongs here.
  const [lastValue, setLastValue] = useState(value);
  if (value !== lastValue) {
    setLastValue(value);
    if (!textHolds(text, value)) setText(printJson(value));
  }

  return (
    <div className="flex flex-col gap-1.5">
      {label && <Label htmlFor={id}>{label}</Label>}
      <Textarea
        id={id}
        value={text}
        spellCheck={false}
        rows={rows}
        placeholder={placeholder}
        aria-invalid={error !== null || undefined}
        className="font-technical text-caption"
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          if (next.trim() === "") {
            if (allowEmpty) {
              setError(null);
              onChange(null);
            } else {
              setError("This cannot be empty.");
            }
            return;
          }
          try {
            onChange(JSON.parse(next) as unknown);
            setError(null);
          } catch (err) {
            setError(err instanceof Error ? err.message : "Not valid JSON.");
          }
        }}
      />
      {description && <p className="text-micro text-muted-foreground">{description}</p>}
      <FieldError message={error ?? undefined} />
    </div>
  );
}

function printJson(value: unknown): string {
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

/** Whether the box already says this value. Half-typed text counts as
 *  holding it: the incoming value is the older one, and overwriting what
 *  somebody is in the middle of typing is the bug this avoids. */
function textHolds(text: string, value: unknown): boolean {
  if (text.trim() === "") return value === null || value === undefined;
  try {
    return JSON.stringify(JSON.parse(text)) === JSON.stringify(value);
  } catch {
    return true;
  }
}

/** A path into the run, with the roots spelled out under it. */
export function PathField({
  id,
  label,
  value,
  onChange,
  placeholder = "steps.look_up.output.items",
}: {
  id: string;
  label?: string;
  value: ValuePath;
  onChange: (next: ValuePath) => void;
  placeholder?: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {label && <Label htmlFor={id}>{label}</Label>}
      <Input
        id={id}
        value={value.path}
        spellCheck={false}
        placeholder={placeholder}
        className="font-technical text-caption"
        onChange={(event) => onChange({ kind: "path", path: event.target.value })}
      />
      <p className="text-micro text-muted-foreground">{PATH_HINT}</p>
    </div>
  );
}

/**
 * Field name to where its value comes from.
 *
 * Two kinds of source, and the choice between them is the whole control: a
 * path reads something the run produced, a literal is written down here. They
 * are one dropdown rather than two editors because a field flips between them
 * constantly while a workflow is being worked out.
 */
export function MappingEditor({
  id,
  value,
  onChange,
  fieldPlaceholder = "field",
  addLabel = "Add a field",
  emptyLabel = "Nothing mapped.",
}: {
  id: string;
  value: Mapping;
  onChange: (next: Mapping) => void;
  fieldPlaceholder?: string;
  addLabel?: string;
  emptyLabel?: string;
}) {
  // Rows rather than the object itself: an empty or duplicated field name has
  // to survive being typed, and an object cannot hold two blanks.
  const [rows, setRows] = useState<{ field: string; value: MappingValue }[]>(() =>
    mappingRows(value)
  );

  function commit(next: { field: string; value: MappingValue }[]) {
    setRows(next);
    onChange(rowsToMapping(next));
  }

  return (
    <div className="flex flex-col gap-2">
      {rows.length === 0 && <p className="text-micro text-muted-foreground">{emptyLabel}</p>}
      {rows.map((row, index) => (
        <div key={index} className="flex flex-wrap items-start gap-2 sm:flex-nowrap">
          <Input
            aria-label="Field name"
            value={row.field}
            spellCheck={false}
            placeholder={fieldPlaceholder}
            className="w-full font-technical text-caption sm:w-40"
            onChange={(event) =>
              commit(rows.map((one, i) => (i === index ? { ...one, field: event.target.value } : one)))
            }
          />
          <Select
            value={row.value.kind}
            onValueChange={(kind) =>
              commit(
                rows.map((one, i) =>
                  i === index
                    ? {
                        ...one,
                        value:
                          kind === "path"
                            ? { kind: "path", path: "" }
                            : { kind: "literal", value: "" },
                      }
                    : one
                )
              )
            }
          >
            <SelectTrigger aria-label="Where the value comes from" className="w-28 shrink-0">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="path">From the run</SelectItem>
              <SelectItem value="literal">Written here</SelectItem>
            </SelectContent>
          </Select>
          {isValuePath(row.value) ? (
            <Input
              aria-label="Path"
              value={row.value.path}
              spellCheck={false}
              placeholder="steps.look_up.output.id"
              className="min-w-0 flex-1 font-technical text-caption"
              onChange={(event) =>
                commit(
                  rows.map((one, i) =>
                    i === index ? { ...one, value: { kind: "path", path: event.target.value } } : one
                  )
                )
              }
            />
          ) : (
            <LiteralInput
              id={`${id}-literal-${index}`}
              value={row.value.value}
              onChange={(next) =>
                commit(
                  rows.map((one, i) =>
                    i === index ? { ...one, value: { kind: "literal", value: next } } : one
                  )
                )
              }
            />
          )}
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            aria-label="Remove this field"
            onClick={() => commit(rows.filter((_, i) => i !== index))}
          >
            <Trash2Icon />
          </Button>
        </div>
      ))}
      <div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => commit([...rows, { field: "", value: { kind: "path", path: "" } }])}
        >
          <PlusIcon /> {addLabel}
        </Button>
      </div>
      {rows.length > 0 && <p className="text-micro text-muted-foreground">{PATH_HINT}</p>}
    </div>
  );
}

/** A written-down value. Typed as JSON when it is not a plain word, so a
 *  number stays a number and `true` does not become the string "true". */
function LiteralInput({
  id,
  value,
  onChange,
}: {
  id: string;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  const [text, setText] = useState(() => (typeof value === "string" ? value : printJson(value)));
  const [invalid, setInvalid] = useState(false);

  return (
    <Input
      id={id}
      aria-label="Value"
      value={text}
      spellCheck={false}
      placeholder='a word, 12, true, or ["a","b"]'
      aria-invalid={invalid || undefined}
      className="min-w-0 flex-1 font-technical text-caption"
      onChange={(event) => {
        const next = event.target.value;
        setText(next);
        // A bare word is a string, which is what somebody typing one means.
        // Anything that looks like JSON is parsed, so numbers and booleans
        // and lists arrive as themselves.
        if (/^\s*[[{"\d-]|^\s*(true|false|null)\s*$/.test(next)) {
          try {
            onChange(JSON.parse(next) as unknown);
            setInvalid(false);
            return;
          } catch {
            setInvalid(true);
            return;
          }
        }
        setInvalid(false);
        onChange(next);
      }}
    />
  );
}

/**
 * A test on the run so far.
 *
 * Three forms behind one control: a leaf comparing one value, and `all of` /
 * `any of` holding more conditions. The nesting is recursive because the
 * backend's is, and flattening it in the UI would make a condition somebody
 * wrote in JSON unopenable here.
 */
export function ConditionEditor({
  id,
  value,
  onChange,
  depth = 0,
}: {
  id: string;
  value: Condition;
  onChange: (next: Condition) => void;
  depth?: number;
}) {
  const form: "leaf" | "all_of" | "any_of" = value.all_of
    ? "all_of"
    : value.any_of
      ? "any_of"
      : "leaf";

  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-2",
        depth > 0 && "rounded-lg border border-border p-2.5"
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={form}
          onValueChange={(next) =>
            onChange(
              next === "leaf"
                ? emptyCondition()
                : next === "all_of"
                  ? { all_of: value.all_of ?? [emptyCondition()] }
                  : { any_of: value.any_of ?? [emptyCondition()] }
            )
          }
        >
          <SelectTrigger aria-label="Kind of test" className="w-36 shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="leaf">One value</SelectItem>
            <SelectItem value="all_of">All of</SelectItem>
            <SelectItem value="any_of">Any of</SelectItem>
          </SelectContent>
        </Select>
        <label className="flex items-center gap-1.5 text-caption text-muted-foreground">
          <input
            type="checkbox"
            className="size-3.5 accent-primary"
            checked={value.negate ?? false}
            onChange={(event) => onChange({ ...value, negate: event.target.checked })}
          />
          the other way round
        </label>
      </div>

      {form === "leaf" ? (
        <div className="flex flex-wrap items-start gap-2 sm:flex-nowrap">
          <Input
            aria-label="Path"
            value={value.path ?? ""}
            spellCheck={false}
            placeholder="steps.check.output.ok"
            className="min-w-0 flex-1 font-technical text-caption"
            onChange={(event) => onChange({ ...value, path: event.target.value })}
          />
          <Select
            value={value.op ?? "truthy"}
            onValueChange={(op) =>
              onChange({ ...value, op: op as NonNullable<Condition["op"]> })
            }
          >
            <SelectTrigger aria-label="Comparison" className="w-36 shrink-0">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CONDITION_OPS.map((one) => (
                <SelectItem key={one.value} value={one.value}>
                  {one.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {!isUnaryOp(value.op) && (
            <LiteralInput
              id={`${id}-value`}
              value={value.value}
              onChange={(next) => onChange({ ...value, value: next })}
            />
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {(form === "all_of" ? (value.all_of ?? []) : (value.any_of ?? [])).map((child, index) => {
            const list = form === "all_of" ? (value.all_of ?? []) : (value.any_of ?? []);
            const replace = (next: Condition[]) =>
              onChange(form === "all_of" ? { ...value, all_of: next } : { ...value, any_of: next });
            return (
              <div key={index} className="flex min-w-0 items-start gap-2">
                <div className="min-w-0 flex-1">
                  <ConditionEditor
                    id={`${id}-${index}`}
                    value={child}
                    depth={depth + 1}
                    onChange={(next) => replace(list.map((one, i) => (i === index ? next : one)))}
                  />
                </div>
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  aria-label="Remove this test"
                  onClick={() => replace(list.filter((_, i) => i !== index))}
                >
                  <Trash2Icon />
                </Button>
              </div>
            );
          })}
          <div>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                const list = form === "all_of" ? (value.all_of ?? []) : (value.any_of ?? []);
                const next = [...list, emptyCondition()];
                onChange(form === "all_of" ? { ...value, all_of: next } : { ...value, any_of: next });
              }}
            >
              <PlusIcon /> Add a test
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
