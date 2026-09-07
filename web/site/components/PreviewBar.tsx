import { BUILT_AT, IS_PREVIEW } from "@/lib/env";

/**
 * A strip across the top of every page in a preview build, and nothing at all
 * in production.
 *
 * It exists so nobody reviews the wrong tab. A preview is byte-identical to
 * what promotion would publish, which is the point and also the hazard: two
 * open tabs are indistinguishable without this, and "is this my change?" is
 * the question a preview is for. The build time answers it.
 */
export function PreviewBar() {
  if (!IS_PREVIEW) return null;
  return (
    <div className="preview-bar" role="status">
      <span className="shell">
        <b>Preview build</b>
        <span>Not indexed, not psychruntime.com. Built {BUILT_AT}.</span>
      </span>
    </div>
  );
}
