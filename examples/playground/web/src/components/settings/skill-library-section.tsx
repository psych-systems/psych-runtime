"use client";

import { useEffect, useState } from "react";
import { LibraryIcon, PencilIcon } from "lucide-react";
import { toast } from "sonner";

import { SkillsField } from "@/components/agents/skills-field";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/page";
import { HelpTip } from "@/components/ui/help";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { listAgents } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { SkillIn } from "@/lib/types";
import { SaveRow } from "@/components/settings/save-row";
import { SettingsSectionBlock } from "@/components/settings/section-nav";

/**
 * Skills written once, attachable to any agent this account builds.
 *
 * ## What "global" means here, and what it does not
 *
 * It means available to every agent you build. It does not mean reaching into
 * every agent you have built, and the word invites that second reading, so the
 * help says which one it is rather than leaving somebody to find out.
 *
 * Attaching a library skill copies it into the published spec, where it joins
 * the version hash. Editing it afterwards changes what the *next* publish
 * gets; an existing agent picks it up when somebody edits and republishes it.
 *
 * The obvious alternative is a library the runtime reads at turn time, which
 * is exactly what an MCP server description does. It is the wrong move here
 * and the two are worth telling apart. A server description is a fact about
 * somebody else's system, so injecting it late is right precisely because the
 * agent did not change. A skill body is instructions the model follows, much
 * closer to the agent's own instructions. Let those change under a published
 * version and two runs of that version behave differently, which is the one
 * thing DESIGN.md §23.1 asks not to happen.
 *
 * The list is a table; the editor is a sheet. Writing a procedure needs room
 * and reading the list does not, and putting both on the page at once is what
 * made this section the longest thing on it.
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
  const [editorOpen, setEditorOpen] = useState(false);
  /** How many published agents carry a skill of each name. Best-effort: a
   *  backend that will not list agents leaves the column empty rather than
   *  keeping the table from rendering. */
  const [usage, setUsage] = useState<Record<string, number> | null>(null);

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

  useEffect(() => {
    let cancelled = false;
    const id = setTimeout(() => {
      void listAgents()
        .then((agents) => {
          if (cancelled) return;
          const counts: Record<string, number> = {};
          for (const agent of agents) {
            for (const skill of agent.skills) {
              counts[skill.name] = (counts[skill.name] ?? 0) + 1;
            }
          }
          setUsage(counts);
        })
        .catch(() => undefined);
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, []);

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
    <SettingsSectionBlock
      id="skills"
      title="Skills"
      description="Procedures written once and attached to as many agents as need them."
      actions={
        <Button size="sm" onClick={() => setEditorOpen(true)}>
          <PencilIcon /> Write one
        </Button>
      }
    >
      {draft.length === 0 ? (
        <EmptyState
          icon={LibraryIcon}
          title="No skills written yet"
          description="A skill is a procedure an agent can load when it needs it, rather than carrying in every prompt."
          action={
            <Button onClick={() => setEditorOpen(true)}>
              <PencilIcon /> Write one
            </Button>
          }
        />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Name</TableHead>
                <TableHead className="hidden sm:table-cell">
                  <span className="inline-flex items-center gap-1.5">
                    Size
                    <HelpTip title="Size" short="How long the body is.">
                      <p>
                        The body is paid for only when the model calls <code>load_skill</code>, so
                        length here is cheap. The one-line description is what every prompt carries.
                      </p>
                    </HelpTip>
                  </span>
                </TableHead>
                <TableHead className="text-right">
                  <span className="inline-flex items-center gap-1.5">
                    Attached to
                    <HelpTip
                      title="Attached to"
                      short="Published agents carrying a skill of this name."
                    >
                      <p>
                        Attaching copies the skill into that agent, so editing here changes what
                        you publish next and never an agent already published.
                      </p>
                    </HelpTip>
                  </span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {draft.map((skill, index) => {
                const count = usage?.[skill.name.trim()] ?? 0;
                return (
                  <TableRow key={`${skill.name}-${index}`}>
                    <TableCell>
                      <span className="flex min-w-0 flex-col gap-0.5">
                        <span className="font-medium">{skill.name || "Unnamed"}</span>
                        <span className="line-clamp-1 text-caption text-muted-foreground">
                          {skill.description || "No description"}
                        </span>
                      </span>
                    </TableCell>
                    <TableCell className="tabular hidden text-caption text-muted-foreground sm:table-cell">
                      {skill.body.length} chars
                    </TableCell>
                    <TableCell className="tabular text-right text-caption text-muted-foreground">
                      {usage === null ? "--" : count === 1 ? "1 agent" : `${count} agents`}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <SaveRow
        dirty={dirty}
        saving={saving}
        onSave={() => void save()}
        onDiscard={() => setDraft(JSON.parse(saved) as SkillIn[])}
        label="Save library"
      />

      <Sheet open={editorOpen} onOpenChange={setEditorOpen}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle>Skill library</SheetTitle>
            <SheetDescription>
              Write the procedure here. Close this, then save the library.
            </SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
            <SkillsField value={draft} onChange={setDraft} fieldErrors={{}} scope="library" />
          </div>
          <SheetFooter>
            {/* Closing is not saving, the same as before this was a sheet:
                the library is written with one deliberate Save. */}
            <Button onClick={() => setEditorOpen(false)}>Done</Button>
          </SheetFooter>
        </SheetContent>
      </Sheet>
    </SettingsSectionBlock>
  );
}
