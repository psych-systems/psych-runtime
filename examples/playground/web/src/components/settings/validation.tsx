import { AlertTriangleIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { ValidationProblem } from "@/lib/types";

/** First message per dotted field path, so a form can show "this field is
 * wrong" next to the field rather than only in a summary blob. */
export function issuesByPath(issues: ValidationProblem[]): Record<string, string> {
  const byPath: Record<string, string> = {};
  for (const issue of issues) {
    if (!(issue.path in byPath)) byPath[issue.path] = issue.message;
  }
  return byPath;
}

export function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return <p className="mt-1 text-caption text-status-failed">{message}</p>;
}

/** Every issue that isn't already shown next to a field it's known to
 * belong to -- the rejection came back with a path this form doesn't have
 * a specific input for, so it still has to reach the person somehow. */
export function IssuesSummary({
  issues,
  consumedPaths,
}: {
  issues: ValidationProblem[];
  consumedPaths: Set<string>;
}) {
  const rest = issues.filter((issue) => !consumedPaths.has(issue.path));
  if (rest.length === 0) return null;
  return (
    <Alert variant="destructive">
      <AlertTriangleIcon />
      <AlertTitle>{rest.length === 1 ? "One more thing" : `${rest.length} more issues`}</AlertTitle>
      <AlertDescription>
        <ul className="list-disc space-y-0.5 pl-4">
          {rest.map((issue, i) => (
            <li key={`${issue.path}-${i}`}>
              <span className="font-technical font-medium text-foreground">{issue.path}</span>:{" "}
              {issue.message}
            </li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}
