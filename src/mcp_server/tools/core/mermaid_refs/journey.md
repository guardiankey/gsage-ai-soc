# journey — Syntax Reference

**Keyword:** `journey`

## Structure
```
journey
    title Title of the Journey
    section Section Name
        Task name: score: Actor1, Actor2
```

## Rules
- `title` — optional, displayed at top
- `section` — groups tasks into phases
- Each task: `Task name: <score>: <actors>`
  - `score` is an integer 1–5 (satisfaction level)
  - `actors` is a comma-separated list of participant names

## Example

```mermaid
journey
    title User Incident Response
    section Detection
        Alert triggered: 3: System
        Analyst notified: 4: System, Analyst
    section Investigation
        Review logs: 2: Analyst
        Correlate events: 3: Analyst
        Identify root cause: 4: Analyst
    section Resolution
        Apply fix: 5: Analyst, Engineer
        Validate fix: 5: Analyst
        Close ticket: 5: Analyst
```

## Pitfalls
- Score must be 1–5; values outside this range may render unexpectedly
- Actor names are free text; they appear in the legend
- Sections are optional but recommended for grouping
- No edge/arrow syntax — task order is strictly sequential within sections
