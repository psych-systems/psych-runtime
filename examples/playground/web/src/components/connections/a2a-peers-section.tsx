"use client";

import { useEffect, useState } from "react";
import { BotIcon, CopyIcon, KeyRoundIcon, Loader2Icon, NetworkIcon, PlusIcon, SaveIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { createA2AToken, listAgents } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { A2APeerPreset } from "@/components/settings/types";
import type { AgentSummary } from "@/lib/types";

const BLANK: A2APeerPreset = {
  name: "",
  url: "",
  description: "",
  credential: null,
  scheme: "Bearer",
  tenant: null,
  allow: [],
  optional: false,
  extensions: [],
};

/**
 * Other agents this account's agents can call, over A2A.
 *
 * Sits beside the MCP connections because it answers the same question from
 * the other side. An MCP server is a system with tools; an A2A peer is another
 * agent with skills. Both are things an agent of yours reaches out to, and
 * both are stored as presets rather than as live connections.
 *
 * ## Nothing here declares what a peer can do
 *
 * There is no tool list to fill in and no catalogue to fetch, deliberately.
 * A peer's skills come from its own Agent Card when a run actually calls it,
 * because what a remote system offers is a fact about that system rather than
 * about your agent. A preset that copied the skill list would be advertising
 * an ability that may since have been withdrawn.
 *
 * ## Attaching copies, it does not link
 *
 * The same rule as the skill library: a published agent carries its own copy
 * of every peer's address and credential name, so editing one here changes
 * what you publish next and never what a running conversation calls.
 */
export function A2APeersSection({
  peers,
  onSave,
  onAddOwnAgent,
}: {
  peers: A2APeerPreset[];
  onSave: (next: A2APeerPreset[]) => Promise<void>;
  /** Adds one of this account's own agents as a peer, minting a token if
   *  none exists yet under the given secret name. This is what lets the
   *  playground call itself over A2A end to end, with no second deployment
   *  to stand up. */
  onAddOwnAgent: (agentId: string) => Promise<void>;
}) {
  const saved = JSON.stringify(peers);
  const [draft, setDraft] = useState<A2APeerPreset[]>(peers);
  const [prevSaved, setPrevSaved] = useState(saved);
  const [saving, setSaving] = useState(false);
  const [ownAgentDialogOpen, setOwnAgentDialogOpen] = useState(false);
  const [tokenDialogOpen, setTokenDialogOpen] = useState(false);

  // Adjusted during render rather than in an effect, the shape used elsewhere
  // in this console: an effect would show one frame of the stale draft first.
  if (saved !== prevSaved) {
    setPrevSaved(saved);
    setDraft(JSON.parse(saved) as A2APeerPreset[]);
  }

  const dirty = JSON.stringify(draft) !== saved;

  function update(index: number, patch: Partial<A2APeerPreset>) {
    setDraft(draft.map((peer, i) => (i === index ? { ...peer, ...patch } : peer)));
  }

  async function save() {
    setSaving(true);
    try {
      await onSave(draft.map((peer) => ({ ...peer, name: peer.name.trim(), url: peer.url.trim() })));
      toast.success(draft.length === 1 ? "1 peer saved" : `${draft.length} peers saved`);
    } catch (err) {
      toast.error("Could not save the peers", { description: describeApiError(err) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h2 className="flex items-center gap-2 text-lg font-semibold">
          <NetworkIcon className="size-4 text-muted-foreground" aria-hidden />
          Other agents
        </h2>
        <p className="text-body text-muted-foreground">
          Agents elsewhere that yours can hand work to, over A2A. Give an address and, if it needs
          one, the name of a credential. What each peer can actually do is read from its own agent
          card when a conversation calls it, so there is nothing to list here.
        </p>
      </div>

      {draft.length === 0 && (
        <p className="text-caption text-muted-foreground">
          No peers yet. Add one and it becomes something you can attach when building an agent.
        </p>
      )}

      {draft.map((peer, index) => (
        <div key={index} className="flex flex-col gap-3 rounded-lg border border-border p-3">
          <div className="flex items-start gap-2">
            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              <Label htmlFor={`peer-name-${index}`}>Name</Label>
              <Input
                id={`peer-name-${index}`}
                value={peer.name}
                spellCheck={false}
                placeholder="research"
                onChange={(e) => update(index, { name: e.target.value })}
              />
              <p className="text-micro text-muted-foreground">
                What your agent calls it. Two peers may both call themselves &quot;research&quot;;
                this name is yours.
              </p>
            </div>
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              className="mt-6 shrink-0"
              aria-label={`Remove ${peer.name || "this peer"}`}
              onClick={() => setDraft(draft.filter((_, i) => i !== index))}
            >
              <Trash2Icon />
            </Button>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`peer-url-${index}`}>Address</Label>
            <Input
              id={`peer-url-${index}`}
              value={peer.url}
              spellCheck={false}
              placeholder="https://agents.example.com"
              onChange={(e) => update(index, { url: e.target.value })}
            />
            <p className="text-micro text-muted-foreground">
              The peer&apos;s A2A address, or its agent card URL directly. A plain address is
              resolved to its card at <code className="font-technical">/.well-known/agent-card.json</code>.
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`peer-desc-${index}`}>What it is for</Label>
            <Input
              id={`peer-desc-${index}`}
              value={peer.description}
              placeholder="Deep research over our internal papers"
              onChange={(e) => update(index, { description: e.target.value })}
            />
            <p className="text-micro text-muted-foreground">
              For you, not for the model. Your agent learns what this peer does from its card.
            </p>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`peer-cred-${index}`}>Credential name</Label>
              <Input
                id={`peer-cred-${index}`}
                value={peer.credential ?? ""}
                spellCheck={false}
                placeholder="research_token"
                onChange={(e) => update(index, { credential: e.target.value || null })}
              />
              <p className="text-micro text-muted-foreground">
                The name of a secret, never the secret. Add the value under Settings.
              </p>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`peer-tenant-${index}`}>Routing id</Label>
              <Input
                id={`peer-tenant-${index}`}
                value={peer.tenant ?? ""}
                spellCheck={false}
                placeholder="Optional"
                onChange={(e) => update(index, { tenant: e.target.value || null })}
              />
              <p className="text-micro text-muted-foreground">
                Only when the peer&apos;s card asks for one, because it hosts many agents behind
                one address.
              </p>
            </div>
          </div>

          <div className="flex items-center justify-between gap-3 rounded-lg bg-surface/60 px-3 py-2">
            <div className="flex min-w-0 flex-col">
              <span className="text-body font-medium">Carry on if it is unreachable</span>
              <span className="text-caption text-muted-foreground">
                Off means a conversation fails when this peer cannot be reached, which is usually
                what you want for a peer the answer depends on.
              </span>
            </div>
            <Switch
              checked={peer.optional}
              onCheckedChange={(value) => update(index, { optional: value })}
              aria-label={`Carry on without ${peer.name || "this peer"}`}
            />
          </div>
        </div>
      ))}

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => setDraft([...draft, { ...BLANK }])}>
          <PlusIcon /> Add a peer
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => setOwnAgentDialogOpen(true)}>
          <BotIcon /> Call one of your own agents
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => setTokenDialogOpen(true)}>
          <KeyRoundIcon /> Mint a token for a peer elsewhere
        </Button>
        <Button size="sm" disabled={!dirty || saving} onClick={() => void save()}>
          {saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
          {saving ? "Saving" : "Save peers"}
        </Button>
        {dirty && !saving && (
          <span className="text-caption text-muted-foreground">Unsaved changes.</span>
        )}
      </div>

      <AddOwnAgentDialog
        open={ownAgentDialogOpen}
        onOpenChange={setOwnAgentDialogOpen}
        onAdd={onAddOwnAgent}
      />
      <MintTokenDialog open={tokenDialogOpen} onOpenChange={setTokenDialogOpen} />
    </section>
  );
}

/**
 * One of this account's own agents, added as a peer of its others.
 *
 * The playground calling itself over A2A end to end, with no second
 * deployment required: the card is fetched from this same process, its
 * skills come from that card, and the call is a real `message/send` over
 * loopback with a token this account minted. Proves the protocol is doing
 * something rather than declared and unused.
 */
function AddOwnAgentDialog({
  open,
  onOpenChange,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdd: (agentId: string) => Promise<void>;
}) {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    // Deferred a tick, as everywhere else in this codebase: this is the
    // effect reacting to `open` becoming true, not work owed to the commit
    // that opened the dialog.
    const id = setTimeout(() => {
      setError(null);
      void listAgents()
        .then((list) => {
          if (cancelled) return;
          setAgents(list);
          setAgentId(list[0]?.agent_id ?? null);
        })
        .catch((err) => {
          if (!cancelled) setError(describeApiError(err));
        });
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [open]);

  async function add() {
    if (agentId === null) return;
    setAdding(true);
    setError(null);
    try {
      await onAdd(agentId);
      toast.success("Added. Attach it to another agent as a peer, above.");
      onOpenChange(false);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setAdding(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Call one of your own agents over A2A</DialogTitle>
          <DialogDescription>
            Adds a peer whose address is this deployment&apos;s own card URL for the agent you
            pick, and a bearer token minted for the purpose. A real A2A round trip, over loopback,
            with nothing else to stand up.
          </DialogDescription>
        </DialogHeader>
        {error && <p className="text-caption text-destructive">{error}</p>}
        {agents === null ? (
          <p className="text-caption text-muted-foreground">Loading your agents...</p>
        ) : agents.length === 0 ? (
          <p className="text-caption text-muted-foreground">
            Publish an agent first, then come back here to add it as a peer of another.
          </p>
        ) : (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="own-agent-pick">Agent</Label>
            <Select value={agentId ?? undefined} onValueChange={setAgentId}>
              <SelectTrigger id="own-agent-pick">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {agents.map((agent) => (
                  <SelectItem key={agent.agent_id} value={agent.agent_id}>
                    {agent.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={agentId === null || adding} onClick={() => void add()}>
            {adding && <Loader2Icon className="animate-spin" />} Add as a peer
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * A bearer token another A2A deployment presents to call one of your agents.
 *
 * The A2A door authenticates with a console session token; a peer elsewhere
 * needs one that outlives a browser session, so this mints one with a chosen
 * lifetime and saves it as a named secret. The value is shown exactly once,
 * the same rule an API key follows anywhere else.
 */
function MintTokenDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [secretName, setSecretName] = useState("a2a-peer");
  const [ttlDays, setTtlDays] = useState(365);
  const [minting, setMinting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);

  async function mint() {
    setMinting(true);
    setError(null);
    try {
      const result = await createA2AToken({ secret_name: secretName.trim(), ttl_days: ttlDays });
      setToken(result.token);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setMinting(false);
    }
  }

  function close(next: boolean) {
    if (!next) {
      setToken(null);
      setError(null);
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Mint a token for a peer elsewhere</DialogTitle>
          <DialogDescription>
            Saved as a secret under the name below, so any of your own HTTP tools, MCP servers or
            A2A peer presets can refer to it by name. Shown once; only its hash is kept after this.
          </DialogDescription>
        </DialogHeader>
        {token !== null ? (
          <div className="flex flex-col gap-2">
            <Label>Token</Label>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded-md bg-surface/60 px-2 py-1.5 font-technical text-caption">
                {token}
              </code>
              <Button
                type="button"
                size="icon-sm"
                variant="outline"
                aria-label="Copy token"
                onClick={() => {
                  void navigator.clipboard.writeText(token);
                  toast.success("Copied");
                }}
              >
                <CopyIcon />
              </Button>
            </div>
            <p className="text-micro text-muted-foreground">
              Hand this to whoever runs the other deployment. It authenticates as this account, the
              same as the browser&apos;s own session cookie does here.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {error && <p className="text-caption text-destructive">{error}</p>}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="mint-secret-name">Secret name</Label>
              <Input
                id="mint-secret-name"
                value={secretName}
                spellCheck={false}
                onChange={(e) => setSecretName(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="mint-ttl">Valid for (days)</Label>
              <Input
                id="mint-ttl"
                type="number"
                className="tabular"
                min={1}
                max={3650}
                value={ttlDays}
                onChange={(e) => setTtlDays(Number(e.target.value) || 365)}
              />
            </div>
          </div>
        )}
        <DialogFooter>
          {token !== null ? (
            <Button onClick={() => close(false)}>Done</Button>
          ) : (
            <>
              <Button variant="ghost" onClick={() => close(false)}>
                Cancel
              </Button>
              <Button disabled={secretName.trim() === "" || minting} onClick={() => void mint()}>
                {minting && <Loader2Icon className="animate-spin" />} Mint token
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
