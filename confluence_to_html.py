#!/usr/bin/env python3
"""Export all Confluence pages in a space as a hierarchy of navigable HTML pages."""

from __future__ import annotations

import argparse
import html
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from atlassian import Confluence


PAGE_BATCH_SIZE = 100


@dataclass
class PageNode:
    page_id: str
    title: str
    body_storage: str
    parent_id: Optional[str]
    ancestors: List[str]
    children: List["PageNode"] = field(default_factory=list)
    output_file: Optional[Path] = None


class ExportError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert all Confluence pages in a space into a hierarchy of HTML pages "
            "with sidebar, breadcrumbs, and previous/next navigation."
        )
    )
    parser.add_argument("--base-url", required=True, help="Confluence base URL")
    parser.add_argument(
        "--space-id",
        required=True,
        help="Confluence space key (or numeric ID for Cloud if resolvable)",
    )
    parser.add_argument("--token", required=True, help="Personal access token")
    parser.add_argument(
        "--output-dir",
        default="exported_html",
        help="Output directory for generated HTML files",
    )
    parser.add_argument(
        "--cloud",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Set --no-cloud for Confluence Data Center/Server",
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or "page"


def unique_slug(base: str, used: Dict[str, int]) -> str:
    count = used.get(base, 0)
    used[base] = count + 1
    if count == 0:
        return base
    return f"{base}-{count + 1}"


def resolve_space_key(confluence: Confluence, provided_space: str) -> str:
    # Most installations use a space key; keep direct usage if it resolves.
    try:
        space = confluence.get_space(provided_space)
        if space and space.get("key"):
            return space["key"]
    except Exception:
        pass

    # Attempt Cloud v2 numeric-id lookup when input looks like an ID.
    if provided_space.isdigit():
        try:
            payload = confluence.get(f"api/v2/spaces/{provided_space}")
            key = payload.get("key") if isinstance(payload, dict) else None
            if key:
                return key
        except Exception:
            pass

    raise ExportError(
        f"Could not resolve space '{provided_space}'. Pass a valid space key or numeric ID."
    )


def fetch_all_pages(confluence: Confluence, space_key: str) -> List[dict]:
    pages: List[dict] = []
    start = 0

    while True:
        batch = confluence.get_all_pages_from_space(
            space=space_key,
            start=start,
            limit=PAGE_BATCH_SIZE,
            expand="body.storage,ancestors",
            content_type="page",
        )

        if not batch:
            break

        pages.extend(batch)
        start += len(batch)

        if len(batch) < PAGE_BATCH_SIZE:
            break

    return pages


def build_tree(pages: List[dict]) -> tuple[Dict[str, PageNode], List[PageNode]]:
    nodes: Dict[str, PageNode] = {}

    for page in pages:
        page_id = str(page.get("id"))
        ancestors = [str(item.get("id")) for item in page.get("ancestors", []) if item.get("id")]
        parent_id = ancestors[-1] if ancestors else None
        title = page.get("title", f"Untitled-{page_id}")
        body_storage = (
            page.get("body", {})
            .get("storage", {})
            .get("value", f"<p>Page body unavailable for {html.escape(title)}.</p>")
        )

        nodes[page_id] = PageNode(
            page_id=page_id,
            title=title,
            body_storage=body_storage,
            parent_id=parent_id,
            ancestors=ancestors,
        )

    roots: List[PageNode] = []

    for node in nodes.values():
        if node.parent_id and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
        else:
            roots.append(node)

    sort_tree(roots)
    return nodes, roots


def sort_tree(nodes: List[PageNode]) -> None:
    nodes.sort(key=lambda n: n.title.lower())
    for node in nodes:
        sort_tree(node.children)


def assign_output_paths(roots: List[PageNode], output_root: Path) -> None:
    used_root_slugs: Dict[str, int] = {}

    def recurse(node: PageNode, parent_dir: Path, sibling_used: Dict[str, int]) -> None:
        base = slugify(node.title)
        slug = unique_slug(base, sibling_used)
        node_dir = parent_dir / slug
        node.output_file = node_dir / "index.html"

        child_used: Dict[str, int] = {}
        for child in node.children:
            recurse(child, node_dir, child_used)

    for root in roots:
        recurse(root, output_root, used_root_slugs)


def flatten_preorder(roots: List[PageNode]) -> List[PageNode]:
    ordered: List[PageNode] = []

    def walk(node: PageNode) -> None:
        ordered.append(node)
        for child in node.children:
            walk(child)

    for root in roots:
        walk(root)

    return ordered


def relative_link(from_page: PageNode, to_page: Optional[PageNode]) -> str:
    if not to_page or not from_page.output_file or not to_page.output_file:
        return ""
    rel = os.path.relpath(to_page.output_file, from_page.output_file.parent)
    return rel.replace("\\", "/")


def render_tree_sidebar(
    nodes: List[PageNode],
    current_id: str,
    current_page: PageNode,
) -> str:
    parts: List[str] = ["<ul class='tree'>"]

    for node in nodes:
        link = relative_link(current_page, node)
        cls = " class='active'" if node.page_id == current_id else ""
        parts.append(f"<li{cls}><a href='{html.escape(link)}'>{html.escape(node.title)}</a>")
        if node.children:
            parts.append(render_tree_sidebar(node.children, current_id, current_page))
        parts.append("</li>")

    parts.append("</ul>")
    return "".join(parts)


def build_breadcrumb(node: PageNode, by_id: Dict[str, PageNode]) -> List[PageNode]:
    crumbs: List[PageNode] = []
    for ancestor_id in node.ancestors:
        ancestor = by_id.get(ancestor_id)
        if ancestor:
            crumbs.append(ancestor)
    crumbs.append(node)
    return crumbs


def page_template(
    node: PageNode,
    roots: List[PageNode],
    by_id: Dict[str, PageNode],
    prev_page: Optional[PageNode],
    next_page: Optional[PageNode],
    output_root: Path,
) -> str:
    sidebar = render_tree_sidebar(roots, node.page_id, node)
    breadcrumbs = build_breadcrumb(node, by_id)

    breadcrumb_links: List[str] = []
    for crumb in breadcrumbs:
        if crumb.page_id == node.page_id:
            breadcrumb_links.append(f"<span>{html.escape(crumb.title)}</span>")
        else:
            link = relative_link(node, crumb)
            breadcrumb_links.append(f"<a href='{html.escape(link)}'>{html.escape(crumb.title)}</a>")

    up_page = by_id.get(node.parent_id) if node.parent_id else None

    prev_link = relative_link(node, prev_page)
    next_link = relative_link(node, next_page)
    up_link = relative_link(node, up_page)

    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{html.escape(node.title)}</title>
  <link rel='stylesheet' href='{html.escape(relative_css_link(node, output_root))}'>
</head>
<body>
  <div class='layout'>
    <aside class='sidebar'>
      <h1>Space Pages</h1>
      {sidebar}
    </aside>
    <main class='content'>
      <nav class='breadcrumbs'>{" / ".join(breadcrumb_links)}</nav>
      <h1>{html.escape(node.title)}</h1>
      <nav class='pager'>
        {render_nav_button('Previous', prev_link)}
        {render_nav_button('Up', up_link)}
        {render_nav_button('Next', next_link)}
      </nav>
      <article>
        {node.body_storage}
      </article>
    </main>
  </div>
</body>
</html>
"""


def render_nav_button(label: str, link: str) -> str:
    if not link:
        return f"<span class='btn disabled'>{html.escape(label)}</span>"
    return f"<a class='btn' href='{html.escape(link)}'>{html.escape(label)}</a>"


def relative_css_link(node: PageNode, output_root: Path) -> str:
    if not node.output_file:
        return "style.css"
    css_path = output_root / "style.css"
    rel = os.path.relpath(css_path, node.output_file.parent)
    return rel.replace("\\", "/")


def write_css(output_root: Path) -> None:
    css = """
:root {
  --bg: #f4f6f8;
  --surface: #ffffff;
  --line: #dfe1e6;
  --text: #172b4d;
  --muted: #6b778c;
  --primary: #0052cc;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Segoe UI", Tahoma, sans-serif;
  color: var(--text);
  background: var(--bg);
}
.layout {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  min-height: 100vh;
}
.sidebar {
  border-right: 1px solid var(--line);
  background: var(--surface);
  overflow: auto;
  padding: 20px;
}
.sidebar h1 {
  margin: 0 0 16px;
  font-size: 18px;
}
.content {
  padding: 24px;
}
.tree, .tree ul {
  list-style: none;
  margin: 0;
  padding-left: 16px;
}
.tree > li { padding-left: 0; }
.tree li {
  margin: 4px 0;
}
.tree li.active > a {
  color: var(--primary);
  font-weight: 600;
}
.tree a {
  color: var(--text);
  text-decoration: none;
}
.tree a:hover {
  text-decoration: underline;
}
.breadcrumbs {
  color: var(--muted);
  margin-bottom: 8px;
  font-size: 14px;
}
.breadcrumbs a {
  color: var(--primary);
  text-decoration: none;
}
.pager {
  display: flex;
  gap: 8px;
  margin-bottom: 20px;
}
.btn {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 6px 10px;
  text-decoration: none;
  color: var(--text);
  background: #fff;
  font-size: 14px;
}
.btn.disabled {
  opacity: 0.45;
}
article {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 20px;
  overflow-wrap: anywhere;
}
img {
  max-width: 100%;
  height: auto;
}
@media (max-width: 960px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .sidebar {
    border-right: 0;
    border-bottom: 1px solid var(--line);
    max-height: 40vh;
  }
}
""".strip()

    (output_root / "style.css").write_text(css + "\n", encoding="utf-8")


def write_index(output_root: Path, roots: List[PageNode]) -> None:
    links = []
    for root in roots:
        if root.output_file:
            rel = os.path.relpath(root.output_file, output_root).replace("\\", "/")
            links.append(f"<li><a href='{html.escape(rel)}'>{html.escape(root.title)}</a></li>")

    page = f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Confluence Space Export</title>
  <link rel='stylesheet' href='style.css'>
</head>
<body>
  <main class='content'>
    <h1>Confluence Space Export</h1>
    <p>Select a root page:</p>
    <ul>
      {''.join(links)}
    </ul>
  </main>
</body>
</html>
"""
    (output_root / "index.html").write_text(page, encoding="utf-8")


def export_pages(base_url: str, provided_space: str, token: str, output_dir: Path, cloud: bool) -> None:
    confluence = Confluence(url=base_url, token=token, cloud=cloud)

    space_key = resolve_space_key(confluence, provided_space)
    pages = fetch_all_pages(confluence, space_key)
    if not pages:
        raise ExportError(f"No pages found in space '{space_key}'.")

    by_id, roots = build_tree(pages)
    assign_output_paths(roots, output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_css(output_dir)

    ordered = flatten_preorder(roots)
    index_map = {node.page_id: idx for idx, node in enumerate(ordered)}

    for node in ordered:
        idx = index_map[node.page_id]
        prev_page = ordered[idx - 1] if idx > 0 else None
        next_page = ordered[idx + 1] if idx < len(ordered) - 1 else None

        if not node.output_file:
            raise ExportError(f"Missing output path for page {node.page_id}")

        node.output_file.parent.mkdir(parents=True, exist_ok=True)
        node.output_file.write_text(
            page_template(node, roots, by_id, prev_page, next_page, output_dir),
            encoding="utf-8",
        )

    write_index(output_dir, roots)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    export_pages(
        base_url=args.base_url,
        provided_space=args.space_id,
        token=args.token,
        output_dir=output_dir,
        cloud=args.cloud,
    )
    print(f"Export complete: {output_dir}")


if __name__ == "__main__":
    main()
