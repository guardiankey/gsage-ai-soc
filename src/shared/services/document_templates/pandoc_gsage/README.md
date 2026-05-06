# gSage Pandoc bundle

This directory ships with `gsage-ai-soc` and provides a ready-to-use
**Pandoc + LaTeX** bundle for `generate_document` (PDF output).

Files:

- `defaults.yaml` — Pandoc defaults file (cover page, TOC, gSage colors,
  diagram.lua filter).
- `gsage.latex` — KOMA-Script (`scrartcl`) based LaTeX template derived
  from the open-source [Eisvogel](https://github.com/Wandmalfarbe/pandoc-latex-template)
  template family. Adapted for gSage with neutral branding (no logo).
- `diagram.lua` — Lua filter that converts fenced code blocks tagged with
  a diagram language (`mermaid`, `dot`, `plantuml`, …) into images
  embedded in the PDF. Upstream:
  [pandoc-ext/diagram](https://github.com/pandoc-ext/diagram). A small
  gSage patch makes the `mermaid` engine honour
  `PANDOC_DIAGRAM_PUPPETEER_CONFIG`.
- `puppeteer-config.json` — Chromium launch flags (`--no-sandbox`, …)
  consumed by `mmdc` (Mermaid CLI) when running inside the container.

## Usage

In `generate_document` parameters, omit `template_id` and pass:

```json
{ "output_format": "pdf", "pandoc": true, "content": "<markdown here>" }
```

The tool runs:

```
pandoc --defaults=<this dir>/defaults.yaml --output=- < input.md
```

with `cwd = <this dir>` so all referenced files (`gsage.latex`,
`diagram.lua`, `puppeteer-config.json`, …) are resolved relative to the
bundle.

## Diagrams

The `diagram.lua` filter converts fenced code blocks into images. Example:

````markdown
```mermaid
flowchart TD
    A[Start] --> B{Decision}
    B -->|Yes| C[OK]
    B -->|No|  D[Fail]
```
````

Engines supported by the filter and their binary requirements:

| Engine     | Fence tag    | Binary required in the worker container |
|------------|--------------|------------------------------------------|
| Mermaid    | `mermaid`    | `mmdc` (`@mermaid-js/mermaid-cli`) + `chromium` |
| Graphviz   | `dot`        | `dot` (`graphviz`)                       |
| PlantUML   | `plantuml`   | `plantuml`                               |
| TikZ       | `tikz`       | `pdflatex` (already present)             |
| Asymptote  | `asymptote`  | `asy` (`asymptote`)                      |
| Cetz       | `cetz`       | `typst`                                  |

In the gSage runtime images we bake **mermaid** and **graphviz**. TikZ also
works because LaTeX is already installed. The other engines will fail with
a clean error if the corresponding binary is missing — they can be enabled
by extending `docker/Dockerfile`.

> **Tip for AI agents:** validate Mermaid syntax with the
> `mermaid_validate` MCP tool *before* embedding a diagram in the
> `content` parameter of `generate_document`.

## Requirements (in the worker container)

- `pandoc`
- A LaTeX engine (`pdflatex` / `xelatex`) with the packages required by
  `gsage.latex` (KOMA-Script, hyperref, xcolor, graphicx, listings, …).
- `mmdc` + `chromium` (for the `mermaid` engine).
- `graphviz` (for the `dot` engine).
