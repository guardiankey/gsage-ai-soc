# Default Knowledge Base

Place `.md` or `.txt` files in this directory to pre-load system-level knowledge
into every new organisation's Weaviate collection.

Files are ingested by the `load_default_knowledge_task` Celery task with
`source = "system"` so they are never surfaced as user-authored content.

## Naming convention

Use descriptive filenames, e.g.:
- `01-platform-overview.md`
- `02-security-policies.md`
- `glossary.md`
