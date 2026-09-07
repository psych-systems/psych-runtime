"use client";

import { useState } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";

import { FieldError } from "@/components/settings/validation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import type { HttpToolIn } from "@/lib/types";

const METHODS: HttpToolIn["method"][] = ["GET", "POST", "PUT", "PATCH", "DELETE"];

export const BLANK_HTTP_TOOL: HttpToolIn = {
  name: "",
  description: "",
  url: "",
  method: "POST",
  input_schema: {},
  headers: {},
  credential: null,
  timeout_seconds: 30,
  interruptible: true,
};

/**
 * HTTP tools on an agent: an endpoint the model may call, with nobody writing
 * a function for it.
 *
 * The one rule the form has to teach is where a key goes. A published agent
 * is a document that ends up in git and in every trace, so a literal
 * `Authorization` header is refused by the library itself. The credential box
 * takes the *name* of a secret from Settings, and the runtime looks the value
 * up per account at the moment of the call.
 *
 * Arguments map by method: on GET and DELETE they become the query string, on
 * the rest the JSON body, and `{name}` in the URL is filled from an argument
 * of that name first. The schema box is the JSON schema the model is shown.
 */
export function HttpToolsField({
  value,
  onChange,
  fieldErrors,
  secrets,
}: {
  value: HttpToolIn[];
  onChange: (next: HttpToolIn[]) => void;
  fieldErrors: Record<string, string>;
  secrets: string[];
}) {
  function update(index: number, patch: Partial<HttpToolIn>) {
    onChange(value.map((tool, i) => (i === index ? { ...tool, ...patch } : tool)));
  }

  return (
    <div className="flex flex-col gap-3">
      {value.length === 0 && (
        <p className="text-caption text-muted-foreground">
          No HTTP tools. Add one to let the agent call a REST endpoint directly, with a key it
          never sees.
        </p>
      )}
      {value.map((tool, index) => {
        const error = fieldErrors[`http_tools.${index}`] ?? fieldErrors[`tools.${tool.name}`];
        return (
          <div key={index} className="flex flex-col gap-3 rounded-lg border border-border p-3">
            <div className="flex items-start gap-2">
              <div className="grid min-w-0 flex-1 gap-3 sm:grid-cols-[1fr_8rem]">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`http-name-${index}`}>Name</Label>
                  <Input
                    id={`http-name-${index}`}
                    value={tool.name}
                    spellCheck={false}
                    placeholder="get_weather"
                    onChange={(e) => update(index, { name: e.target.value })}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`http-method-${index}`}>Method</Label>
                  <Select
                    value={tool.method}
                    onValueChange={(method) =>
                      update(index, { method: method as HttpToolIn["method"] })
                    }
                  >
                    <SelectTrigger id={`http-method-${index}`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {METHODS.map((method) => (
                        <SelectItem key={method} value={method}>
                          {method}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                className="mt-6 shrink-0"
                aria-label={`Remove ${tool.name || "this tool"}`}
                onClick={() => onChange(value.filter((_, i) => i !== index))}
              >
                <Trash2Icon />
              </Button>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`http-url-${index}`}>URL</Label>
              <Input
                id={`http-url-${index}`}
                value={tool.url}
                spellCheck={false}
                placeholder="https://api.example.com/cities/{city}/weather"
                onChange={(e) => update(index, { url: e.target.value })}
              />
              <p className="text-micro text-muted-foreground">
                <code className="font-technical">{"{name}"}</code> is filled from the argument
                of that name. The rest go in the query string on GET and DELETE, the JSON body
                otherwise.
              </p>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`http-desc-${index}`}>What the model is told it does</Label>
              <Input
                id={`http-desc-${index}`}
                value={tool.description}
                placeholder="Today's weather for a city, in Celsius."
                onChange={(e) => update(index, { description: e.target.value })}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`http-schema-${index}`}>Arguments, as a JSON schema</Label>
              <JsonTextarea
                id={`http-schema-${index}`}
                value={tool.input_schema}
                placeholder={'{"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}'}
                onChange={(input_schema) => update(index, { input_schema })}
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`http-cred-${index}`}>Credential</Label>
                <Select
                  value={tool.credential ?? "__none__"}
                  onValueChange={(name) =>
                    update(index, { credential: name === "__none__" ? null : name })
                  }
                >
                  <SelectTrigger id={`http-cred-${index}`}>
                    <SelectValue placeholder="None" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">None</SelectItem>
                    {secrets.map((name) => (
                      <SelectItem key={name} value={name}>
                        {name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-micro text-muted-foreground">
                  Sent as a bearer token. A secret&apos;s name from Settings, never its value.
                </p>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`http-timeout-${index}`}>Timeout (seconds)</Label>
                <Input
                  id={`http-timeout-${index}`}
                  type="number"
                  className="tabular"
                  min={1}
                  max={600}
                  value={tool.timeout_seconds}
                  onChange={(e) =>
                    update(index, { timeout_seconds: Number(e.target.value) || 30 })
                  }
                />
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`http-headers-${index}`}>Extra headers, as JSON</Label>
              <JsonTextarea
                id={`http-headers-${index}`}
                value={tool.headers}
                placeholder={'{"Accept": "application/json"}'}
                onChange={(headers) =>
                  update(index, { headers: headers as Record<string, string> })
                }
              />
              <p className="text-micro text-muted-foreground">
                Not Authorization, not Cookie. Those are refused at publish; use the credential.
              </p>
            </div>

            <div className="flex items-center justify-between gap-3 rounded-lg bg-surface/60 px-3 py-2">
              <div className="flex min-w-0 flex-col">
                <span className="text-body font-medium">Can be cut off by a stop</span>
                <span className="text-caption text-muted-foreground">
                  Off for a call that must finish once started, such as a payment.
                </span>
              </div>
              <Switch
                checked={tool.interruptible}
                onCheckedChange={(interruptible) => update(index, { interruptible })}
              />
            </div>
            <FieldError message={error} />
          </div>
        );
      })}
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onChange([...value, { ...BLANK_HTTP_TOOL }])}
        >
          <PlusIcon /> Add an HTTP tool
        </Button>
      </div>
    </div>
  );
}

/** A textarea holding a JSON object, kept as text while it is being typed so
 *  a half-written document does not snap back, and parsed on every valid
 *  keystroke. */
function JsonTextarea({
  id,
  value,
  placeholder,
  onChange,
}: {
  id: string;
  value: Record<string, unknown>;
  placeholder: string;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState(() =>
    Object.keys(value).length === 0 ? "" : JSON.stringify(value, null, 2)
  );
  const [invalid, setInvalid] = useState(false);
  return (
    <>
      <Textarea
        id={id}
        value={text}
        spellCheck={false}
        placeholder={placeholder}
        className="min-h-20 font-technical text-caption"
        aria-invalid={invalid || undefined}
        onChange={(e) => {
          const next = e.target.value;
          setText(next);
          if (next.trim() === "") {
            setInvalid(false);
            onChange({});
            return;
          }
          try {
            const parsed: unknown = JSON.parse(next);
            if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
              setInvalid(true);
              return;
            }
            setInvalid(false);
            onChange(parsed as Record<string, unknown>);
          } catch {
            setInvalid(true);
          }
        }}
      />
      {invalid && <FieldError message="Not valid JSON yet. The last valid value is what will be published." />}
    </>
  );
}
