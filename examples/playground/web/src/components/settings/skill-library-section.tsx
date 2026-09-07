"use client";

import { useState } from "react";
import { LibraryIcon, Loader2Icon, SaveIcon } from "lucide-react";
import { toast } from "sonner";

import { SkillsField } from "@/components/agents/skills-field";
import { Button } from "@/components/ui/button";
import { describeApiError } from "@/lib/errors";
import type { SkillIn } from "@/lib/types";

/**
 * Skills written once, attachable to any agent this account builds.
 *
 * ## What "global" means here, and what it does not
 *
 * It means available to every agent you build. It does not mean reaching into
 * every agent you have built, and the word invites that second reading, so the
 * page says which one it is rather than leaving somebody to find out.
 *
 * Attaching a library skill copies it into the published spec, where it joins
 * the version hash. Editing it afterwards changes what the *next* publish
 * gets; an existing agent picks it up when somebody edits and republishes it.
 *
 * The obvious alternative is a library the runtime reads at turn time, which
 * is exactly what an MCP server description does. It is the wrong
 * move here and the two are worth telling apart. A server description is a
 * fact about somebody else's system, so injecting it late is right precisely
 * because the agent did not change. A skill body is instructions the model
 * follows, much closer to the agent's own instructions. Let those change under
 * a published version and two runs of that version behave differently, which
 * is the one thing DESIGN.md §23.1 asks not to happen.
 */
export function SkillLibrarySection({
  skills,
  onSave,
}: {
  skills: SkillIn[];
  onSave: (next: SkillIn[]) => Promise<void>;
}) {
  const [draft, setDraft] = useState<SkillIn[]>(skills);
  const [saving, setSaving] = useState(false);

  // Reset when the saved list changes underneath, which happens on the refresh
  // that follows a save. Adjusted during render rather than in an effect, the
  // same shape `chat-workspace` uses for a route that changed under it: an
  // effect would render one frame of the stale draft first. Keyed on the
  // serialised list, because the array identity is new on every fetch.
  const saved = JSON.stringify(skills);
  const [prevSaved, setPrevSaved] = useState(saved);
  if (saved !== prevSaved) {
    setPrevSaved(saved);
    setDraft(JSON.parse(saved) as SkillIn[]);
  }

  const dirty = JSON.stringify(draft) !== saved;

  async function save() {
    setSaving(true);
    try {
      await onSave(draft.map((skill) => ({ ...skill, name: skill.name.trim() })));
      toast.success(draft.length === 1 ? "1 skill saved" : `${draft.length} skills saved`);
    } catch (err) {
      toast.error("Could not save the library", { description: describeApiError(err) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h2 className="flex items-center gap-2 text-lg font-semibold">
          <LibraryIcon className="size-4 text-muted-foreground" aria-hidden />
          Skill library
        </h2>
        <p className="text-body text-muted-foreground">
          Procedures written once and attached to as many agents as need them. Attaching one copies
          it into that agent, so editing here changes what you publish next and never changes an
          agent already published.
        </p>
      </div>

      <SkillsField value={draft} onChange={setDraft} fieldErrors={{}} scope="library" />

      <div className="flex items-center gap-2">
        <Button size="sm" disabled={!dirty || saving} onClick={() => void save()}>
          {saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
          {saving ? "Saving" : "Save library"}
        </Button>
        {dirty && !saving && (
          <span className="text-caption text-muted-foreground">Unsaved changes.</span>
        )}
      </div>
    </section>
  );
}
