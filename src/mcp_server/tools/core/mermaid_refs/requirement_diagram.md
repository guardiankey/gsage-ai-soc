# requirementDiagram — Syntax Reference

**Keyword:** `requirementDiagram`

Visualizes requirements and their relationships following SysML v1.6 conventions. Useful for connecting system requirements to design elements and tracing dependencies.

> ⚠️ **READ PITFALLS FIRST — parser is strict.** Always validate with
> `mermaid_validate` before presenting.

## Golden Rules (violations = parse error)
1. **Always quote `text:` values** — ``text: "..."``. Unquoted text that
   contains punctuation, colons, or long phrases breaks the parser.
2. **`risk` / `verifymethod` values are case-sensitive and come from a
   fixed list** (see tables below). `High` ≠ `high`. `Test` ≠ `test`.
3. **Element `type:` values** that look like a verification method
   (`test`, `verification`, `Test`, …) can collide with parser keywords.
   Prefer descriptive nouns: `"test suite"`, `"integration tests"`,
   `"microservice"`, `"document"`. **Always quote element `type:` and
   `docRef:`** when the value contains spaces or punctuation.
4. **`{` must stay on the same line as the block header** (
   `requirement name {` — no newline before `{`).
5. **No blank lines inside a `{ ... }` block.**
6. Relationship types are **lowercase**: `satisfies`, `verifies`,
   `contains`, `copies`, `derives`, `refines`, `traces`.

## Structure

```
requirementDiagram

<requirementBlock>
<elementBlock>
<relationship>
```

## Requirement Block

```
<type> name {
    id: identifier
    text: "description text"
    risk: <risk>
    verifymethod: <method>
}
```

### Requirement Types

| Keyword | Description |
|---|---|
| `requirement` | Generic requirement |
| `functionalRequirement` | Functional behavior |
| `interfaceRequirement` | Interface specification |
| `performanceRequirement` | Performance criteria |
| `physicalRequirement` | Physical constraints |
| `designConstraint` | Design restriction |

### Risk Levels (case-sensitive)

| Value | |
|---|---|
| `Low` | Low risk |
| `Medium` | Medium risk |
| `High` | High risk |

### Verification Methods (case-sensitive)

| Value | |
|---|---|
| `Analysis` | Analytical verification |
| `Inspection` | Visual inspection |
| `Test` | Test execution |
| `Demonstration` | Live demonstration |

## Element Block

```
element name {
    type: "element type"
    docRef: "reference/path"
}
```

Elements represent external artifacts (components, documents, test suites) that connect to requirements.

## Relationships

```
{source} - <type> -> {destination}
{destination} <- <type> - {source}
```

### Relationship Types

| Keyword | Description |
|---|---|
| `contains` | Source contains destination |
| `copies` | Source copies destination |
| `derives` | Source derives from destination |
| `satisfies` | Source satisfies destination requirement |
| `verifies` | Source verifies destination requirement |
| `refines` | Source refines destination |
| `traces` | Source traces to destination |

## ✅ Safe Example (validated)

```mermaid
requirementDiagram

requirement auth_req {
    id: REQ-001
    text: "The system must authenticate users with MFA."
    risk: High
    verifymethod: Test
}

functionalRequirement session_req {
    id: REQ-002
    text: "Sessions must expire after 30 minutes of inactivity."
    risk: Medium
    verifymethod: Inspection
}

performanceRequirement perf_req {
    id: REQ-003
    text: "API response time must be under 500ms at p95."
    risk: Medium
    verifymethod: Demonstration
}

element auth_service {
    type: "microservice"
    docRef: "docs/auth-service.md"
}

element test_suite {
    type: "integration tests"
    docRef: "tests/auth/"
}

auth_req - contains -> session_req
auth_service - satisfies -> auth_req
test_suite - verifies -> auth_req
perf_req - traces -> auth_req
```

## ❌ Anti-examples (DO NOT USE)

```
# ❌ Unquoted text with colon / punctuation
text: Users must authenticate: see policy   # parser collides on ':'

# ❌ Reserved-sounding bare word as element type
element t1 {
    type: test       # conflicts with verifymethod keyword Test/test
}

# ❌ Blank line inside a block
requirement r1 {

    id: R1
    text: "x"
}

# ❌ Opening brace on next line
requirement r1
{
    id: R1
}

# ❌ Wrong case
risk: high              # must be Low | Medium | High
verifymethod: test      # must be Analysis | Inspection | Test | Demonstration
```

## Pitfalls summary
- Always quote `text:`, `type:`, and `docRef:` values.
- `risk` and `verifymethod` values are case-sensitive and from a fixed list.
- Avoid bare words like `test`, `verification`, `type` as element `type:` values — quote them.
- `{` must be on the same line as the block header.
- No blank lines inside `{ ... }`.
- Relationship keywords are lowercase.
- Arrow direction is free: `A - satisfies -> B` or `B <- satisfies - A`.

