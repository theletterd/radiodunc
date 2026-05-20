# DJ vs Show — design doc

Status: design phase, not yet implemented.

## Why split

Today a `DJPersona` row conflates two concepts: the DJ as a character
(name, personality, voice, voice_instructions) AND the show they host
(which day/hour slots they own). You can't have one DJ host multiple
distinct shows without duplicating the persona definition — same name,
same voice instructions, repeated for every distinct slot.

End state: a **DJ** is a reusable identity. A **Show** binds one DJ to
a set of shifts. The same DJ can be referenced by many Shows.

## Mental model

Three concepts, cleanly separated:

- **Default DJ** — `station.dj_name`, `personality`, `voice`,
  `voice_instructions`, etc. The fallback. Already exists; unchanged.
- **DJ** — a reusable identity row in `station.djs`:
  `{id, name, personality, voice, voice_instructions}`. No shifts.
- **Show** — a binding row in `station.shows`:
  `{id, dj_id?, shifts: [{day, start_hour, end_hour}, …]}`. If `dj_id`
  is null/missing, the show plays as the Default DJ.

A Show with `dj_id = null` and one shift is the natural way to schedule
the Default DJ explicitly for a slot.

## Resolved design decisions

These were settled before this doc was written. Listed here so they
don't get re-litigated mid-implementation:

1. **Clicking a Show in the schedule edits the SHOW only** — its DJ
   assignment and its shifts. The DJ's identity is edited separately
   via the DJ Roster view.
2. **DJ Roster is a separate sidebar takeover** — not a tab inside the
   schedule editor. Parallel to "The Schedule" and "Station Settings".
3. **Shows own multiple shifts** — `Show.shifts` is a list, same shape
   the current `DJPersona.shifts` uses. One Show can span many days /
   hour ranges.
4. **Deleting a DJ reassigns their Shows to the Default DJ** — by
   unsetting `Show.dj_id`. No orphan Shows, no deletion blockers.

## UI sketch

Three sidebar takeovers in the default sidebar:

```
📅 The Schedule           — edits Shows
🎙 DJ Roster              — NEW: edits DJ identities
⚙  Station Settings      — unchanged
```

### The Schedule (modified)

Grid is structurally unchanged: 7-day × 24-hour. **Blocks now represent
Shows.** Block label and colour both resolve through `show.dj_id → dj`
(or "Default" when unset).

- Legend: one chip per DJ that appears in any Show, plus a "Default"
  chip. Same DJ in two different Shows = one chip (no more duplicates
  from copy-pasted personas — that wart goes away for free).
- "+ New show" button (was "+ New persona").
- "Manage DJs…" shortcut that navigates to the DJ Roster view.

### Show editor drawer (click a block, or + New show)

- **DJ picker** — dropdown:
  - "Default DJ" (first, always present)
  - All roster DJs alphabetical
  - "+ Create new DJ…" at the bottom (opens an inline modal that
    preserves the Show context — see open question 2)
- **Shifts list** — reuses today's row component (day + start + end +
  remove + readout). Multiple shifts allowed.
- **Delete** removes only this Show binding. The DJ behind it stays in
  the roster.

### DJ Roster takeover (new)

List of DJ rows. Each row shows:
- Name
- Personality (preview, truncated)
- Voice (or "(default)" when unset)
- "used in N show(s)" counter — calculated client-side from
  `shows.filter(s => s.dj_id === dj.id).length`
- "⚠ not in any show" badge when N=0. Soft nudge; no auto-delete.

"+ New DJ" button at the bottom.

### DJ editor drawer (click a row, or + New DJ)

The current persona editor **minus the shifts section**. Identity only:
name, personality, voice, voice_instructions, voice preview.

New footer: "Used in N show(s):" with a read-only list of the shift
ranges. (Stretch: click a line to deep-link into editing that Show.)

### Delete-DJ confirmation

```
This DJ hosts N show(s). Deleting will reassign them to the Default DJ.

Affected shows:
  • Friday/Saturday 8pm–midnight
  • …

[Cancel] [Delete DJ]
```

Implementation: on DJ removal, find all `shows[].dj_id == id` and unset
those references. They fall through to the Default DJ at runtime.

## Schema shape (preview)

```python
class DJ(BaseModel):
    id: str                         # stable, slug-from-name on create
    name: str
    personality: str
    voice: str | None
    voice_instructions: str | None
    prompt_template: str | None     # carries over from DJPersona

class Show(BaseModel):
    id: str                         # stable; uuid or slug, TBD
    dj_id: str | None               # None = play as Default DJ
    shifts: list[DJShift]           # reuses existing DJShift model

class StationConfig:
    # ...existing fields...
    djs: list[DJ] = []
    shows: list[Show] = []
    dj_roster: list[DJPersona] = []  # KEPT for back-compat during migration
```

`dj_roster` stays alongside the new fields during migration. On load,
each legacy persona expands into one `DJ` + one `Show` pointing at it.
After the user saves once through the new UI, the migration writes
back without `dj_roster` populated. We can drop the field entirely a
release later.

## Migration

The shape transform on load:

```
For each persona in dj_roster:
  dj = DJ(
    id = slug(persona.name),
    name = persona.name,
    personality = persona.personality,
    voice = persona.voice,
    voice_instructions = persona.voice_instructions,
    prompt_template = persona.prompt_template,
  )
  show = Show(
    id = uuid_or_slug(),
    dj_id = dj.id,
    shifts = persona.shifts,
  )
  djs.append(dj); shows.append(show); dj_roster.clear()
```

Migration is idempotent and happens silently inside the pydantic
validator (same pattern as `DJPersona.migrate_legacy_fields` does
today for `days/start_hour/end_hour → shifts`). No user-visible
upgrade step.

## Active-persona resolution at runtime

Today `pick_active_persona` walks `dj_roster` looking for a persona
whose shifts include the current time. After the split:

```
For each show in shows:
  For each shift in show.shifts:
    if current_time matches shift:
      if show.dj_id is None:
        return None  # caller falls through to station defaults
      return djs[show.dj_id]
return None  # default
```

Shape changes inside the function; callers (everything in
`app/dj_scripts.py` that takes a `DJPersona | None`) keep their
existing signatures by re-using the DJ shape (it has the same
identity fields).

## What this doc does NOT settle

Four open questions. See the discussion thread at the bottom of the
parent conversation; this section will be updated as we resolve them.

### 1. DJ IDs — slug, uuid, or name?

Stable IDs are required (Show.dj_id refers to a DJ). Options:

- **Slug from name (auto on create, never changed on rename)** —
  readable in JSON, predictable, collision handling = append `-2`.
  Tradeoff: a DJ named "Sam" is `sam` even after rename to "Samuel",
  which is fine but might surprise on first encounter.
- **UUID** — bombproof, ugly in JSON, no human meaning.
- **Just use the name** — simplest, breaks on rename without an
  explicit "rename and update references" flow.

Current lean: **slug from name**.

### 2. Where does "+ Create new DJ" inside the Show picker dispatch to?

- **Inline modal over the Show editor** — preserves Show context,
  small extra implementation (a stack of two drawers / a sub-modal).
- **Navigate to DJ Roster** — loses unsaved Show edits, friction.

Current lean: **inline modal**.

### 3. Empty-shifts Show — error or warning?

A Show with zero shifts can't air. Today this would be a validation
error. After the split: should saving a no-shifts Show be allowed
(with a soft "this show won't air" badge) or rejected at save?

Current lean: **allow + soft badge**, easier on the user during edits
("delete all shifts, then re-add"). Doesn't break anything at runtime
because the resolver just skips it.

### 4. Does a Show have a name field?

The legacy persona's `name` was really the DJ's name. After the split
the Show is unnamed — referred to in the UI by its DJ + shifts ("Sam,
Fri/Sat 8pm–midnight"). Adding `Show.name` would let users tag shows
("Late Night Sessions") but adds a field with no clear use case today.

Current lean: **no name field**.

## Implementation order

Once the open questions are resolved, the plan is a series of vertical
slices, each landing as its own PR:

1. Schema + migration + tests (the new models live in `app/config.py`
   alongside `DJPersona`; load-time migration; round-trips cleanly).
2. Backend resolver swap (`pick_active_persona` reads `djs` + `shows`;
   `dj_roster` path kept for one release).
3. Schedule grid renders Shows; legend deduplicates by DJ.
4. Show editor drawer (with DJ picker + inline create modal).
5. DJ Roster takeover view + DJ editor drawer.
6. Drop the `dj_roster` field entirely once configs are written in
   the new shape.

DJ icons (the avatar feature on the TODO list) layer on top of #5 —
the avatar attaches to the DJ identity, not the Show.
