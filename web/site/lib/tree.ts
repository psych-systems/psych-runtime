import type * as PageTree from "fumadocs-core/page-tree";
import { source } from "@/lib/source";
import { CURRENT } from "@/lib/versions";

/**
 * The sidebar tree for one version.
 *
 * Every version is a root folder under `content/docs/`, so the whole tree has
 * the versions at the top and the actual pages one level down. Handing that to
 * the sidebar puts every page behind a collapsed folder called "next", which is
 * a documentation site whose navigation starts closed.
 *
 * This returns the branch for the version being read instead, so the sidebar
 * shows Get started, Guides, Concepts and Reference directly, and a reader on an
 * older version sees that version's pages rather than the current one's.
 */
export function treeFor(slug: string | undefined): PageTree.Root {
  const wanted = slug ?? CURRENT.slug;
  const branch = source.pageTree.children.find(
    (node): node is PageTree.Folder =>
      node.type === "folder" && node.$id === wanted,
  );
  if (!branch) return source.pageTree;
  return {
    name: source.pageTree.name,
    children: branch.children,
    $id: branch.$id,
  };
}
