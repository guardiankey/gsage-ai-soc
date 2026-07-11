# timeline — Syntax Reference

**Keyword:** `timeline`

## Structure
```
timeline
    title Optional Title
    section Optional Section Name
        Time Period : Event
        Time Period : Event 1 : Event 2
        Time Period : Event
                    : More events on same period
```

## Rules
- `title` — optional, displayed at top
- `section` — optional grouping; all subsequent entries belong to it until next section
- Time periods and events are **plain text** — no strict format required
- Multiple events on the same period: repeat the period or use `:` continuation
- Direction: always left to right (chronological order of declaration)

## Example

```mermaid
timeline
    title gSage Threat Timeline
    section Initial Access
        Day 0 : Phishing email received
        Day 1 : Credential harvested
    section Lateral Movement
        Day 2 : Internal scan detected
        Day 3 : Admin share accessed
    section Impact
        Day 5 : Ransomware deployed
        Day 5 : Files encrypted
    section Response
        Day 6 : Incident declared
        Day 7 : Systems isolated
        Day 10 : Recovery completed
```

## Pitfalls
- **Do NOT use `:` in time period labels** — `:` is the time/event delimiter and will break parsing.
  This is the #1 cause of invalid timeline diagrams. Use ``h``/``m``/``s`` suffix format instead.
- **Time format conversion table:**

  | ❌ Wrong (colon) | ✅ Correct (suffix) |
  |---|---|
  | `08:01 : event` | `08h01m : event` |
  | `10:18:30 : event` | `10h18m30s : event` |
  | `10:18:30-10:19:25 : event` | `10h18m30s-10h19m25s : event` |
  | `13:22 : event` | `13h22m : event` |
  | `08:01-12:00 : event` | `08h01m-12h00m : event` |

- **Mixed format (h + :) is also wrong:** `10h18:30` → use `10h18m30s` instead.
- **Always include the `m` suffix** even for whole-hour:minutes: `08h01m` not just `08h01`.
- Events are free text; they render as bubbles on the timeline
- Sections group entries visually with a colored band
- No numeric or date validation — purely decorative chronology
- Each section gets a different colour automatically; to disable this use `disableMulticolor`:
  ```
  %%{init: {"timeline": {"disableMulticolor": true}}}%%
  ```
- Theme variables `cScale0` through `cScale11` control section colors:
  ```
  %%{init: {"themeVariables": {"cScale0": "#ff0000", "cScale1": "#00ff00"}}}%%
  ```
