# gSage Pandoc bundle

This directory ships with `gsage-ai-soc` and provides a ready-to-use
**Pandoc + LaTeX** bundle for `generate_document` (PDF output).

Files:

- `defaults.yaml` — Pandoc defaults file (cover page, TOC, gSage colors).
- `gsage.latex` — KOMA-Script (`scrartcl`) based LaTeX template derived
  from the open-source [Eisvogel](https://github.com/Wandmalfarbe/pandoc-latex-template)
  template family. Adapted for gSage with neutral branding (no logo).

## Usage

In `generate_document` parameters, omit `template_id` and pass:

```json
{ "output_format": "pdf", "pandoc": true, "content": "<markdown here>" }
```

The tool runs:

```
pandoc --defaults=<this dir>/defaults.yaml --output=- < input.md
```

with `cwd = <this dir>` so all referenced files (`gsage.latex`, etc.) are
resolved relative to the bundle.

## Requirements (in the API container)

- `pandoc`
- A LaTeX engine (`pdflatex` / `xelatex`) with the packages required by
  `gsage.latex` (KOMA-Script, hyperref, xcolor, graphicx, listings, …).
