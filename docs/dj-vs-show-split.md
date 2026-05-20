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
    id: str                         # uuid4, generated on create, never changed
    name: str
    personality: str
    voice: str | None
    voice_instructions: str | None
    prompt_template: str | None     # carries over from DJPersona

class Show(BaseModel):
    id: str                         # uuid4
    name: str | None = None         # optional; falls back to DJ name in UI
    dj_id: str | None = None        # None = play as Default DJ
    shifts: list[DJShift]           # reuses existing DJShift model

class StationConfig:
    # ...existing fields...
    djs: list[DJ] = []
    shows: list[Show] = []
    dj_roster: list[DJPersona] = []  # KEPT for back-compat during migration
```

UUIDs (not slugs) because the long-term plan is for config to move into
a database, with `radio_config.json` becoming a seed file for fresh
clones. UUIDs are the right primary-key shape for that future and let
us avoid a second migration when we get there.

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
    id = uuid4(),
    name = persona.name,
    personality = persona.personality,
    voice = persona.voice,
    voice_instructions = persona.voice_instructions,
    prompt_template = persona.prompt_template,
  )
  show = Show(
    id = uuid4(),
    name = None,                 # legacy personas had no separate show name
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

## Resolved (post-design-discussion)

### IDs — UUIDs

DJ and Show both use `uuid4`. Decision tiebreaker: the long-term plan
is for config to migrate into a database, with `radio_config.json`
becoming a seed file for fresh clones. UUIDs are the right primary-key
shape for that future and avoid a second migration when we get there.

Trade-off accepted: JSON readability takes a small hit (no more
`grep saturday-night-sam` to find a DJ's references). Mitigation:
`name` is right next to `id` in every row, so visual scanning still
works.

### "+ Create new DJ" inside the Show picker — inline modal

When the user picks "+ Create new DJ" from the Show editor's DJ
dropdown, a small modal pops over the Show editor with the minimal DJ
fields (name, personality, voice, voice_instructions). Save creates
the DJ, closes the modal, and pre-selects it in the picker. The
in-progress Show edits are preserved throughout.

Implementation: the DJ Roster's full editor drawer already exists by
the time we wire this up. The modal version is a slimmer subset
reusing the same form helpers.

### Empty-shifts Show — allow + soft warning

A Show with `shifts: []` saves cleanly but renders with a
"⚠ no shifts — this show won't air" badge in the schedule view.
The runtime resolver skips it (no shifts = no match), so nothing
breaks.

This makes "delete all shifts to re-enter them" a legitimate transient
state instead of a save error.

### Show.name — yes, optional

A `Show` carries an optional human-readable name distinct from its
DJ's name. The point of the DJ-vs-Show split is exactly that the same
DJ can host different shows — and a name lets the user lean into the
contrast ("Why is the late-night alt-goth host doing the drivetime
segment?").

**Where it surfaces:**

| Surface | Behaviour |
|---|---|
| Schedule grid block | DJ name (primary); show name as a small caption below when set |
| Legend | Per-DJ chip; show names listed in tooltip |
| On-air badge | "On air: [DJ name]" + " — [Show name]" when set |
| Show editor drawer | "Show name" field at the top, optional, placeholder hints at the contrast use case |
| DJ prompt template | New `{show_name}` placeholder available; empty string when unset |

**Field semantics:**
- Optional (`None` = unnamed; UI falls back to DJ-name-only displays).
- No uniqueness constraint — two Shows can both be "Late Night
  Sessions" if you want.
- Soft length cap (~50 chars) for layout sanity in grid captions.

**The sleeper feature:** passing `{show_name}` into the DJ prompt
template lets the LLM acknowledge the mismatch directly. With "Cheerful
Morning Drive" hosted by "Ms. Jessica Danger," the model has a hook
to riff on the tension — the contrast becomes part of the broadcast,
not just the schedule view. This is the strongest argument for the
field.

## Implementation order

The plan is a series of vertical slices, each landing as its own PR:

1. **Schema + migration + tests.** New `DJ` and `Show` models in
   `app/config.py` alongside `DJPersona`. Load-time migration expands
   each legacy persona into one `DJ` + one `Show` (UUIDs generated,
   `Show.name = None`). Round-trips cleanly through save.
2. **Backend resolver swap.** `pick_active_persona` reads `djs` +
   `shows`; legacy `dj_roster` path kept as a fallback for one
   release.
3. **`{show_name}` in the DJ prompt template.** Adds the placeholder
   to the default template + docs. Lands with #2 so the resolver can
   pass it through.
4. **Schedule grid renders Shows.** Block label = DJ name (primary)
   + show name (caption when set). Legend deduplicates by DJ. Tooltip
   on chip lists the DJ's shows by name.
5. **Show editor drawer.** DJ picker (with "+ Create new DJ…" inline
   modal), shifts list, optional Show name field at top. Empty-shifts
   badge.
6. **DJ Roster takeover view + DJ editor drawer.** "Used in N show(s)"
   footer with shift previews. Delete-DJ confirmation that lists
   affected shows.
7. **Drop the `dj_roster` field entirely** once we're confident every
   user's config has been rewritten through the new UI.

DJ icons (the avatar feature on the TODO list) layer on top of #6 —
the avatar attaches to the DJ identity, not the Show.
