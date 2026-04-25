# Mermaid Diagram Types — Quick Reference Index

Use `mermaid_reference` tool with `diagram_type` to get syntax details and examples.

## Stable Diagram Types

| Diagram Type      | Keyword                   | Best For                                          |
|-------------------|---------------------------|---------------------------------------------------|
| flowchart         | `flowchart LR/TD`         | Process flows, decision trees, pipelines          |
| sequenceDiagram   | `sequenceDiagram`         | Protocol flows, API calls, message sequences      |
| classDiagram      | `classDiagram`            | OOP structures, data models                       |
| stateDiagram      | `stateDiagram-v2`         | State machines, lifecycle flows                   |
| erDiagram         | `erDiagram`               | Database schemas, entity relationships            |
| journey           | `journey`                 | User journeys, process satisfaction mapping       |
| gantt             | `gantt`                   | Project timelines, task scheduling                |
| pie               | `pie`                     | Proportions, distributions (no negatives)         |
| mindmap           | `mindmap`                 | Concept maps, hierarchical ideas                  |
| timeline          | `timeline`                | Chronological events, historical sequences        |
| zenuml            | `zenuml`                  | Sequence diagrams (ZenUML syntax)                 |
| sankey            | `sankey-beta`             | Flow quantities, traffic/energy flows             |
| xychart           | `xychart-beta`            | Bar/line charts with numeric axes                 |
| packet            | `packet-beta`             | Network packet structures, bit fields             |
| block             | `block-beta`              | High-level system blocks with manual positioning  |
| gitgraph          | `gitGraph`                | Git commit history and branch visualization       |
| c4                | `C4Context` / `C4Container` / etc. | System architecture (C4 model)         |
| quadrantChart     | `quadrantChart`           | 2D priority/impact/risk quadrant analysis         |
| requirementDiagram| `requirementDiagram`      | System requirements and traceability (SysML)      |

## Experimental / Dev-Only Types (⚠️ NOT in stable Mermaid)

| Diagram Type | Keyword             | Note |
|---|---|---|
| kanban       | `kanban`            | Dev-only — may not render in production |
| architecture | `architecture-beta` | Dev-only — may not render in production |
| radar        | `radar-beta`        | Dev-only — may not render in production |

## Critical Notes on Beta Keywords

> ⚠️ The `-beta` suffix is **REQUIRED** for these types. Using the keyword without it will fail:
> - `sankey-beta` — NOT `sankey`
> - `xychart-beta` — NOT `xychart`
> - `packet-beta` — NOT `packet`
> - `block-beta` — NOT `block`

## Important Aliases / Notes
- `stateDiagram` (v1) works but prefer `stateDiagram-v2`
- `gitgraph` (lowercase) is also valid for `gitGraph`
- `checkout` and `switch` are interchangeable in gitGraph
- `C4Context`, `C4Container`, `C4Component`, `C4Dynamic`, `C4Deployment` — all map to the `c4` reference

## Types That Do NOT Exist
- `barChart` → use `xychart-beta`
- `graph` → use `flowchart` (though `graph` also works as an alias)
- `sankey` (without `-beta`) → use `sankey-beta`
- `xychart` (without `-beta`) → use `xychart-beta`
- `packet` (without `-beta`) → use `packet-beta`
- `block` (without `-beta`) → use `block-beta`

## Global Syntax Rules (apply to ALL diagram types)
- **NEVER use backslash-escaped quotes** inside diagram code. Write `"Label"` not `\"Label\"`.
  - WRONG: `axis fw[\"Firewall\"]`
  - CORRECT: `axis fw["Firewall"]`
- When generating diagram code inside a markdown code block, output literal `"` characters — do NOT escape them.
- The word `end` (lowercase) is a reserved keyword — wrap in quotes or capitalise if used as a label.
