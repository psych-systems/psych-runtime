import { OG_SIZE, renderSocialImage } from "@/lib/og";

export const dynamic = "force-static";
export const alt = "Psych Runtime: run AI agents inside your Python application";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function Image() {
  return renderSocialImage({
    title: "Run AI agents inside your Python application.",
    kicker: "psych_runtime",
  });
}
