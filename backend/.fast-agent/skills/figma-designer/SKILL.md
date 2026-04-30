---
name: figma-designer
description: "Design UI directly on Figma Desktop using figma-ui-mcp tools. Trigger when creating wireframes, visual designs, design systems, components, or design tokens."
---

# Figma Designer Guidelines

1. Always check the Figma bridge connection with `figma_status` before starting any design work. If the bridge is disconnected, inform the team and provide text-based design specs as a fallback.

2. Before writing any non-trivial design code, call `figma_docs` to read the full API reference with all available operations and code examples.

3. After completing each major design section, take a screenshot with `figma_read(operation: "screenshot")` and inspect for overlaps, alignment issues, or text overflow. Fix issues and re-screenshot until clean.

## Getting Started

When beginning a new design task, follow this bootstrap sequence:

1. Call `figma_status` to confirm the plugin bridge is connected
2. Call `figma_docs` to read the complete API reference
3. Run `setupDesignTokens` to bootstrap your design system (colors, spacing, radius)
4. Build a variable lookup map from `get_variables()` for binding tokens to nodes

## Design System & Tokens

All designs must use Figma Variables (Design Tokens) rather than hardcoded hex values. This ensures global consistency — changing a token value updates all bound nodes instantly.

- **Bootstrap tokens**: Use `figma.setupDesignTokens()` with your color palette and spacing scale
- **Apply tokens**: After creating nodes, bind variables with `figma.applyVariable()`
- **Modify globally**: Use `figma.modifyVariable()` to update all bound nodes at once
- **Design Library frame**: Call `figma.ensure_library()` to create a visual reference at x:-2000

## Components

When building repeated elements (buttons, badges, cards, nav items), create reusable Components:

1. Check if the component exists with `figma.listComponents()`
2. If not, create the frame, then convert with `figma.createComponent()`
3. Use `figma.instantiate()` to place instances — changes to the component propagate to all instances

## Layout & Auto Layout

Use Auto Layout (`layoutMode`) for all containers that need centering or consistent spacing. Avoid manual x/y positioning when auto-layout can handle it.

- **Horizontal rows** (icon + text): `layoutMode: "HORIZONTAL"`, `counterAxisAlignItems: "CENTER"`
- **Vertical stacks** (title + subtitle): `layoutMode: "VERTICAL"`, `itemSpacing: 8`
- **Centered buttons**: `primaryAxisAlignItems: "CENTER"`, `counterAxisAlignItems: "CENTER"`
- **Overlapping elements** (progress bars, badges): Wrap in a non-auto-layout frame for absolute positioning

## Images & Icons

Use server-side helpers for loading images and icons. Never use emoji characters as icons.

- **Images**: `figma.loadImage(url, { parentId, width, height, scaleMode: "FILL" })`
- **Icons**: `figma.loadIcon(name, { parentId, size, fill })` — auto-fallback across icon libraries
- **Icons with background**: `figma.loadIconIn(name, { parentId, containerSize, fill, bgOpacity })`

## Reading & Inspecting Designs

Use `figma_read` to understand existing designs before modifying them:

| Operation | Purpose |
|-----------|---------|
| `get_selection` | Read what the user has selected in Figma |
| `get_design` | Full node tree for a frame (use `depth` and `detail` params) |
| `get_page_nodes` | Top-level frames on the current page |
| `screenshot` | PNG preview of a node for visual QA |
| `scan_design` | Progressive scan for large designs without token overflow |
| `get_node_detail` | CSS-like properties for a single node |
| `search_nodes` | Find nodes by type, name pattern, fill color, font, etc. |

## Naming Convention

- Frame names: PascalCase (e.g. "Agent Dashboard", "Login Screen")
- Component names: kebab-case with type prefix (e.g. "btn/primary", "badge/status")
- Color tokens: descriptive (e.g. "accent", "bg-surface", "text-muted")

## Layer Order

In Figma, the last child drawn renders on top. Always draw background/hero images first, then overlays and content on top.

## Delivering Designs

After completing the design:
1. Take a final screenshot for reference
2. Email the team with the Figma frame name and key design decisions
3. Include component specs for Dev (spacing, colors, responsive breakpoints)

## References

| Topic | File |
|-------|------|
| Meeting protocol | [MEETING_PROTOCOL.md](references/MEETING_PROTOCOL.md) |

