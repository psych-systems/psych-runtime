"use client";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { LabelWithHelp } from "@/components/ui/help";
import { FieldError } from "@/components/settings/validation";
import { ModelField } from "@/components/agents/model-field";
import type { AgentFormState } from "@/components/agents/use-agent-form";

/**
 * The four things a beginner has to answer: what it is called, what it is
 * for, what it should do, and which model writes its replies. Everything
 * else on this page already has an answer.
 */
export function AgentEssentials({ form }: { form: AgentFormState }) {
  const { fieldErrors } = form;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <LabelWithHelp
            htmlFor="agent-name"
            label="Name"
            help="How you will recognise it in the list and in a conversation. Letters, numbers, dots, dashes and underscores, starting with a letter."
          />
          <Input
            id="agent-name"
            placeholder="research-assistant"
            value={form.name}
            spellCheck={false}
            onChange={(event) => form.setName(event.target.value)}
            aria-invalid={fieldErrors.name !== undefined || !form.nameValid}
          />
          {!form.nameValid && (
            <p className="text-caption text-status-failed">
              Letters, numbers, dots, dashes and underscores, starting with a letter.
            </p>
          )}
          <FieldError message={fieldErrors.name} />
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <LabelWithHelp
            htmlFor="agent-description"
            label="What it does"
            help="One line for a person browsing the list, and for another agent reading this one's card. It is not part of the prompt."
          />
          <Input
            id="agent-description"
            value={form.description}
            placeholder="Turns scattered source material into clear, useful briefs."
            onChange={(event) => form.setDescription(event.target.value)}
          />
          <FieldError message={fieldErrors.description} />
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <LabelWithHelp
          htmlFor="agent-instructions"
          label="Instructions"
          help="Written to the agent, in the second person. Say what it does, what it should check first, and what it must never do."
        />
        <Textarea
          id="agent-instructions"
          className="min-h-36"
          placeholder="You investigate the question, use available tools to verify key facts, separate evidence from assumptions, and finish with a clear recommendation."
          value={form.instructions}
          onChange={(event) => form.setInstructions(event.target.value)}
          aria-invalid={fieldErrors.instructions !== undefined}
        />
        <FieldError message={fieldErrors.instructions} />
      </div>

      <div className="flex max-w-md flex-col gap-1.5">
        <LabelWithHelp
          htmlFor="agent-model"
          label="Model"
          help="Which model writes its replies. The provider's own is filled in. Any model id can be typed, because a provider can serve models it does not list."
        />
        {/* A combobox rather than a closed Select: a provider that will not
            say what it serves is ordinary rather than broken, and a closed
            dropdown would be a form nobody could complete against one. */}
        <ModelField
          id="agent-model"
          value={form.modelOverride ?? form.config?.model ?? ""}
          onChange={form.setModelOverride}
          models={form.models}
          prices={form.modelPrices}
          detail={
            form.modelsDetail ??
            (form.config?.provider_label
              ? `From ${form.config.provider_label}, the provider currently in use.`
              : "From the provider currently in use.")
          }
          placeholder={form.config?.model ?? "Loading the configured model"}
          invalid={fieldErrors.model !== undefined}
        />
        <FieldError message={fieldErrors.model} />
      </div>
    </div>
  );
}
