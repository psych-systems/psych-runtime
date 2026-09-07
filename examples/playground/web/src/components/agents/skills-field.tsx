"use client";

import { LibraryIcon, PlusIcon, Trash2Icon } from "lucide-react";

import { FieldError } from "@/components/settings/validation";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { SkillIn } from "@/lib/types";

/**
 * Skills on an agent.
 *
 * The two-field split is the whole mechanism, and the form has to teach it,
 * because getting it backwards is silent and expensive. The **description**
 * goes into the system prompt on every turn of every conversation, so it is
 * paid for constantly and belongs on one line. The **body** is paid for only
 * when the model decides it needs the procedure and calls `load_skill`, so it
 * can be as long as the procedure really is.
 *
 * Someone who writes their whole procedure into the description gets the cost
 * of a long prompt with none of the benefit of deferral, and nothing about
 * the result looks wrong. So the counters below are not decoration: the
 * description's is a budget, and the body's is reassurance that length is
 * fine there.
 */
export function SkillsField({
  value,
  onChange,
  fieldErrors,
  library = [],
  scope = "agent",
}: {
  value: SkillIn[];
  onChange: (next: SkillIn[]) => void;
  /** Keyed `skills.<name>.<field>`, matching what publish-time validation
   *  names, so a dangling `[[skill:x]]` link lands on the body that wrote
   *  it rather than in the summary at the top of the form. */
  fieldErrors: Record<string, string>;
  /** This account's skill library, offered as a shortcut.
   *
   *  Attaching one **copies** its three fields into this form, and from there
   *  into the published spec. There is no link back: editing the library
   *  afterwards changes nothing about what gets published here, and once
   *  published, nothing at all. That is deliberate and it is why the copy
   *  below says so out loud. A skill body is instructions the model follows,
   *  and instructions that could change under a published version would make
   *  two runs of one version behave differently. */
  library?: SkillIn[];
  /** Where this editor is. On an agent form a `[[skill:x]]` link is checked at
   *  publish against that agent's own skills; in the library there is no agent
   *  yet and nothing to check it against, so the hint would be a lie. */
  scope?: "agent" | "library";
}) {
  const attached = new Set(value.map((skill) => skill.name.trim()));
  const available = library.filter((skill) => !attached.has(skill.name.trim()));

  function update(index: number, patch: Partial<SkillIn>) {
    onChange(value.map((skill, i) => (i === index ? { ...skill, ...patch } : skill)));
  }

  return (
    <div className="flex flex-col gap-3">
      {value.length === 0 ? (
        <p className="text-caption text-muted-foreground">
          {scope === "library"
            ? "Nothing in the library yet. A skill is a procedure an agent loads only when it needs it, so a long one costs nothing on the turns that do not use it."
            : "No skills. A skill is a procedure the agent loads only when it needs it, so a long one costs nothing on the turns that do not use it."}
        </p>
      ) : null}

      {value.map((skill, index) => {
        const named = skill.name.trim();
        const descriptionError = fieldErrors[`skills.${named}.description`];
        const bodyError = fieldErrors[`skills.${named}.body`];
        const nameError = fieldErrors[`skills.${named}.name`] ?? fieldErrors[`skills.${index}.name`];
        return (
          <div key={index} className="flex flex-col gap-3 rounded-lg border border-border p-3">
            <div className="flex items-start gap-2">
              <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                <Label htmlFor={`skill-name-${index}`}>Name</Label>
                <Input
                  id={`skill-name-${index}`}
                  value={skill.name}
                  onChange={(e) => update(index, { name: e.target.value })}
                  placeholder="refund-policy"
                  spellCheck={false}
                  aria-invalid={nameError ? true : undefined}
                />
                <FieldError message={nameError} />
              </div>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                className="mt-6 shrink-0"
                aria-label={`Remove ${named || "this skill"}`}
                onClick={() => onChange(value.filter((_, i) => i !== index))}
              >
                <Trash2Icon />
              </Button>
            </div>

            <div className="flex flex-col gap-1.5">
              <div className="flex items-baseline justify-between gap-2">
                <Label htmlFor={`skill-description-${index}`}>Description</Label>
                <span className="font-technical text-micro text-muted-foreground">
                  {skill.description.length} characters, in every prompt
                </span>
              </div>
              <Input
                id={`skill-description-${index}`}
                value={skill.description}
                onChange={(e) => update(index, { description: e.target.value })}
                placeholder="When a refund is allowed, and how to issue one"
                aria-invalid={descriptionError ? true : undefined}
              />
              <p className="text-micro text-muted-foreground">
                One line. This is all the model sees until it asks for the rest, so it has to say
                what the skill covers and when to reach for it.
              </p>
              <FieldError message={descriptionError} />
            </div>

            <div className="flex flex-col gap-1.5">
              <div className="flex items-baseline justify-between gap-2">
                <Label htmlFor={`skill-body-${index}`}>Instructions</Label>
                <span className="font-technical text-micro text-muted-foreground">
                  {skill.body.length} characters, loaded on demand
                </span>
              </div>
              <Textarea
                id={`skill-body-${index}`}
                value={skill.body}
                onChange={(e) => update(index, { body: e.target.value })}
                rows={6}
                placeholder={
                  "1. Confirm the order is under 30 days old.\n" +
                  "2. Refund to the original payment method.\n" +
                  "3. Over $500 needs a manager. See [[skill:escalation]]."
                }
                aria-invalid={bodyError ? true : undefined}
              />
              <p className="text-micro text-muted-foreground">
                As long as it needs to be. Link another skill with{" "}
                <code className="rounded bg-surface px-1 font-technical">[[skill:name]]</code>
                {scope === "library"
                  ? "; the link is checked against the agent you attach this to, when you publish it."
                  : "; a link to a skill this agent does not have is refused when you publish."}
              </p>
              <FieldError message={bodyError} />
            </div>
          </div>
        );
      })}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onChange([...value, { name: "", description: "", body: "" }])}
        >
          <PlusIcon />
          Write one here
        </Button>
        {library.length > 0 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button type="button" variant="outline" size="sm" disabled={available.length === 0}>
                <LibraryIcon />
                {available.length === 0 ? "All library skills attached" : "Add from library"}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="max-w-sm">
              {available.map((skill) => (
                <DropdownMenuItem
                  key={skill.name}
                  className="flex-col items-start gap-0.5"
                  onSelect={() => onChange([...value, { ...skill }])}
                >
                  <span className="font-technical font-medium">{skill.name}</span>
                  <span className="text-caption text-muted-foreground">{skill.description}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      {library.length > 0 && (
        <p className="text-micro text-muted-foreground">
          A library skill is <strong>copied</strong> in, not linked. Edit it here and only this
          agent changes; edit it in Settings and only agents you publish afterwards change. That is
          what makes it safe to attach: instructions that could be rewritten under a running agent
          would mean two conversations with the same version behaving differently.
        </p>
      )}
    </div>
  );
}
