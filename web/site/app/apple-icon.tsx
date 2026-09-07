import { renderAppleIcon } from "@/lib/og";

export const dynamic = "force-static";
export const size = { width: 180, height: 180 };
export const contentType = "image/png";

export default function Icon() {
  return renderAppleIcon();
}
