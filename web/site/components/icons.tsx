/**
 * Interface icons, from lucide-react.
 *
 * One import point rather than each page reaching into the library, so the set
 * the site actually uses is visible in one file and sized consistently. lucide
 * tree-shakes per icon, so this costs only what is listed here.
 *
 * The Trident is not in this file and never will be: it is the brand mark and
 * lives in `components/Trident.tsx`. Everything else on the site is an
 * interface affordance and should come from here rather than be drawn by hand.
 */
export {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Menu,
  Pause,
  Play,
  RotateCcw,
  X,
} from "lucide-react";

/** The size every inline icon uses unless it has a reason not to. */
export const ICON = 16;
