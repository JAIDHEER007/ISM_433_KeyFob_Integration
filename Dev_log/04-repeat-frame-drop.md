# 04 — Lost presses: the `repeat > 0` filter drops whole bursts

**Date found:** 2026-08-20
**Branch:** `feature/repeat_frame_drop` (to branch from `dev` after entry 02 merges)
**Status:** Diagnosed — confirmed against live data, fix not started

The "had to press it three times" symptom from
[entry 02](02-e2e-event-store.md) has a cause, and the trail proves it.

**52% of physical button presses (24 of 46) never left the listener.** Each one
was discarded by the `repeat > 0` filter in `rtl433_listener.py` because the
leading `repeat=0` frame of its burst was lost on the RF side and only
retransmissions were decoded. Zero of those 24 presses produced a single
downstream row — no `PROCESSED`, no `DEBOUNCED`, no `FAILED`. From the user's
side, nothing happened and nothing was logged as having gone wrong.

This is exactly the failure the `DROPPED_REPEAT` row was added to catch, and
the first data collected caught it.

## The bug

`rtl433_listener.py:136-154`:

```python
repeat = data.get("repeat", 0)

if repeat > 0:
    # Protocol-level retransmission of the same physical press, not a new one
    db_handler.log_event(..., db_handler.STATE_DROPPED_REPEAT, ...)
    continue
```

The filter treats `repeat` as *"this press was already handled"*. It isn't.
The HCS200 sets the repeat bit on **every frame after the first in a burst**,
independent of whether anything received the first one. `repeat=1` means "this
is not the first frame the fob *sent*", not "this is not the first frame we
*received*".

So when the leading frame is corrupted or missed — the ordinary case at the
edge of range, with a weak battery, or under interference — the entire burst is
silently dropped, taking the press with it. The retransmissions exist precisely
to make the press survive a lost first frame, and this filter throws away every
one of them.

## Evidence

The `encrypted` field is what makes this provable. It is the KeeLoq hopping
code, and it changes on every press, so **one hopping code = one physical
press** — no time-window guesswork needed to decide which frames belong
together.

Grouping the `RTL_LISTENER` rows in `data/events.db` by hopping code:

| | presses |
|---|---|
| Distinct physical presses seen on air | 46 |
| Delivered (a `repeat=0` frame was decoded) | 22 |
| **Lost (only `repeat=1` frames decoded → all dropped)** | **24 (52%)** |

Of the 112 `DROPPED_REPEAT` rows, **78 (70%) are not retransmissions of anything
we received** — they are the only trace of a press that was thrown away.

Reproduce with:

```sql
-- Every press that was lost: frames decoded, but no repeat=0 head among them.
SELECT json_extract(payload,'$.raw.encrypted')         AS hop_code,
       event_key,
       count(*)                                        AS frames_dropped,
       datetime(min(timestamp),'unixepoch','localtime') AS first_seen
FROM event_log
WHERE stage = 'RTL_LISTENER'
GROUP BY hop_code
HAVING sum(state = 'RECEIVED') = 0
ORDER BY min(timestamp);
```

A representative run, all of it invisible to the user and to every stage after
the listener:

```
hop_code  event_key  frames_dropped  first_seen
3E38DBC2  00E1278:4  1               2026-08-20 15:18:59
A2B1B830  00E1278:4  3               2026-08-20 15:19:02
07ECB111  00E1278:4  5               2026-08-20 15:19:09
B1F66AC9  00E1278:4  5               2026-08-20 15:19:13
```

Four consecutive presses of button 4, 14 frames received, nothing dispatched.
That is the reported symptom, verbatim.

**Worst observed streak: 8 consecutive lost presses**, 15:21:02 → 15:21:48,
across buttons 2, 4 and 8 — 45 seconds of a fob that appears dead.

Not a marginal-signal artifact either: `battery_ok` is 1 on every dropped
frame, and the same session has presses delivered normally, so the receiver
was working.

## Why the obvious fix doesn't work

The tempting fix is *"drop `repeat=1` frames only if we saw a `repeat=0` frame
for this button in the last N seconds."* **The data rules this out.** From the
same capture:

- Widest single burst: **2.62s** (`07ECB111`, 5 frames, 15:19:09 → 15:19:12).
- Closest two *genuinely distinct* presses of the same button: **0.52s**
  (`7959CA04` ends 15:21:28.879, `9D3DD2D1` starts 15:21:29.403).

There is no threshold `N` that keeps a burst together without also merging two
real presses — the intervals overlap by a factor of five. Any purely
time-based dedup will either keep losing presses or start swallowing
deliberate ones.

The hopping code has no such ambiguity, which is why the fix should key on it.

## Proposed fix

**Dedupe on the hopping code, not on the repeat bit.** Accept the first frame
of a burst whatever its `repeat` value; drop later frames because their
hopping code was already seen, not because a bit is set.

```python
# Sketch. SEEN_CODES: OrderedDict[str, float], code -> first-seen timestamp.
code = data.get("encrypted")
if code is not None and code in SEEN_CODES:
    db_handler.log_event(..., STATE_DROPPED_REPEAT,
                         payload={"repeat": repeat, "hop_code": code, "raw": data})
    continue
SEEN_CODES[code] = time.time()   # evict entries older than ~10s, cap the size
```

Properties this has that the current filter doesn't:

- A press survives losing its first frame, second frame, or any subset —
  whichever frame arrives first wins. That is what the retransmissions are for.
- Two distinct presses 0.52s apart stay two presses; their codes differ.
- Replayed frames are dropped for free, since a replay carries an already-seen
  code.

Points to settle when implementing:

- **Eviction.** ~10s and a bounded size is ample — the widest burst is 2.62s,
  and codes only need to outlive their own burst. Must be bounded; an
  unbounded set grows for the life of the process.
- **Missing `encrypted`.** A future producer, or a fob whose decoder omits the
  field, needs a fallback. Falling back to the *current* behaviour for those
  frames keeps the change contained.
- **`DROPPED_REPEAT` keeps its meaning** but gains precision: after the fix it
  means "same press, already forwarded", which is what it was always supposed
  to mean. Record `hop_code` in the payload so a future regression is one
  query away.
- **Counter desync is out of scope.** The receiver never validates the KeeLoq
  counter, so nothing here can drift out of sync; presses are matched by
  identity, not by sequence.

### Cheaper alternative, and why it isn't the recommendation

Removing the `repeat > 0` filter entirely and letting the existing 3s debounce
in `event_processor.py` absorb the retransmissions would work on this
capture — every burst spans less than 3s. It is not the recommendation:

- 2.62s against a 3s window is 87% of the budget. A slightly longer burst
  fires the action twice.
- It makes the debounce load-bearing for protocol dedup, so `DEBOUNCED` rows
  stop distinguishing "human pressed twice" from "radio sent the same frame
  five times" — which throws away the diagnostic value entry 02 was built for.

Worth keeping as a fallback if the hopping code turns out to be unreliable in
wider testing, but the ordering is clear.

## Follow-on question, not part of this fix

The 3s `IGNORE_DURATION` debounce is armed *before* dispatch and is never
extended, so a deliberate second press within 3s is suppressed even if the
first press's Govee call subsequently failed. `event_processor.py:82-88`
already flags this and the `suppressed_by_event_id` field makes it provable.
Once the listener stops losing presses, it is worth re-checking whether any
remaining "nothing happened" reports are this instead — the query is
`DEBOUNCED` rows joined to a `FAILED` outcome on `suppressed_by_event_id`.

## Prerequisite

Entry 02 must be merged first — the diagnosis above is built entirely on the
`event_log` trail and the `payload` column it introduced, and the fix extends
the `DROPPED_REPEAT` rows it added.
