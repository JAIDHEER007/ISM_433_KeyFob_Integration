# 04 — Lost presses: the `repeat > 0` filter drops whole bursts

**Date found:** 2026-08-20
**Branch:** `bug-fix/repeat-frame-drop` (from `dev`)
**Status:** Fixed — implemented and verified by replaying the capture

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

## The fix

**Dedupe on the hopping code, not on the repeat bit.** Accept the first frame
of a burst whatever its `repeat` value; drop later frames because their
hopping code was already seen, not because a bit is set.

Properties this has that the old filter didn't:

- A press survives losing its first frame, second frame, or any subset —
  whichever frame arrives first wins. That is what the retransmissions are for.
- Two distinct presses 0.52s apart stay two presses; their codes differ.
- Replayed frames are dropped for free, since a replay carries an already-seen
  code.

### What was implemented

`rtl433_listener.py` keeps an `OrderedDict` of `"<device id>:<hop code>" ->
first-seen time`, and `_seen_before()` does check-and-insert atomically under a
lock. A frame whose code is already there is dropped; anything else is
forwarded.

- **Eviction: 10s TTL plus a 512-entry cap.** A code only has to outlive its
  own burst, and the widest observed burst is 2.62s. The cap is a memory
  ceiling, not a tuning knob — TTL eviction keeps the real cache at a handful
  of entries.
- **TTL measured on `time.monotonic()`, not `time.time()`.** A wall-clock step
  — NTP correcting a Pi that boots with no RTC, which is this host — would
  otherwise expire codes early or strand them indefinitely. Trail rows still
  record wall-clock time.
- **Keyed on device id, not on button.** A frame whose button bits decoded
  wrong would otherwise look like a fresh press and fire a second, wrong
  action. HCS200 frames carry no CRC, so dropping such a frame as a duplicate
  is the safe direction to be wrong in.
- **Cache is module-level and survives an `rtl_433` restart**, so a burst
  straddling a restart is still one press. It's lock-guarded because the
  supervisor can start a new stdout reader while the previous one is still
  draining a closed pipe.
- **Fallback when the frame carries no hop code** (a future producer, or a
  decoder that omits it): the old `repeat > 0` filter still applies, confined
  to that branch so it can never affect a fob that does report a code.
- **`DROPPED_REPEAT` now means what its name always claimed** — same press,
  already forwarded. The payload gained `hop_code` and `dedup_by`
  (`"hop_code"` / `"repeat_bit"`), so a regression, or a fob silently falling
  back, is one query away. `RECEIVED` rows carry `hop_code` too, so both
  states group on the same `json_extract` path.

**Queue-full interaction, found while implementing.** The listener drops a
press when the downstream queue is full. Claiming the hop code before that
check would have traded one silent loss for another: the first frame claims
the press, fails to queue it, and every retransmission that could still have
carried it is then dropped as a duplicate. The code is now released on
`QUEUE_FULL`, so a later frame of the same burst can retry — the fob's
redundancy applied to backpressure as well as to lost frames.

**Counter desync stays out of scope.** The receiver never validates the KeeLoq
counter, so nothing here can drift out of sync; presses are matched by
identity, not by sequence.

### Verification

Replaying the 134 captured frames from `data/events.db` through the patched
`_read_stdout()`:

| | before | after |
|---|---|---|
| Presses forwarded downstream | 22 / 46 | **46 / 46** |
| Duplicate dispatches | 0 | **0** |

Every dropped frame was dropped by the `hop_code` rule; none fell back to the
repeat bit. Seven new tests in `test_event_trail.py` cover it: a press whose
leading `repeat=0` frame never arrives, two distinct presses 0.52s apart not
merging, a full burst collapsing to one dispatch, TTL expiry, the cache bound,
the queue-full retry, and the no-hop-code fallback.

### Cheaper alternative, and why it wasn't taken

Removing the `repeat > 0` filter entirely and letting the existing 3s debounce
in `event_processor.py` absorb the retransmissions would work on this
capture — every burst spans less than 3s. It was rejected:

- 2.62s against a 3s window is 87% of the budget. A slightly longer burst
  fires the action twice.
- It makes the debounce load-bearing for protocol dedup, so `DEBOUNCED` rows
  stop distinguishing "human pressed twice" from "radio sent the same frame
  five times" — which throws away the diagnostic value entry 02 was built for.

Still worth reaching for if the hopping code turns out to be unreliable in
wider testing, but nothing so far suggests it will be.

### Unrelated bug found and fixed on this branch

Adding those tests made the suite segfault mid-run, with no failing test to
point at. It was not caused by the new tests — six trivial extra DB-backed
tests reproduce it on unmodified `dev`.

`isolated_db` opened a fresh connection per test and abandoned each test's
writer thread. Because `_writer_loop()` read the module-global `_write_queue`
and `_conn` is opened with `check_same_thread=False`, those orphaned writers
kept picking up later tests' jobs and running them against a connection they
did not own. Two threads on one SQLite connection segfaults the interpreter
rather than raising, and the suite only crossed the threshold at roughly thirty
DB-backed tests — so it would have landed on whoever next added a few.

Fixed at the root: `_writer_loop()` now takes its queue as an argument, so a
retired writer is unreachable, and `db_handler.shutdown_writer()` drains and
stops it (queued jobs run first — the sentinel goes through the same FIFO).
The fixture calls that instead of closing the connection out from under a live
thread. Production behaviour is unchanged: it calls `init_db()` once and never
shuts the writer down.

## Follow-on question, not part of this fix

The 3s `IGNORE_DURATION` debounce is armed *before* dispatch and is never
extended, so a deliberate second press within 3s is suppressed even if the
first press's Govee call subsequently failed. `event_processor.py:82-88`
already flags this and the `suppressed_by_event_id` field makes it provable.
Once the listener stops losing presses, it is worth re-checking whether any
remaining "nothing happened" reports are this instead — the query is
`DEBOUNCED` rows joined to a `FAILED` outcome on `suppressed_by_event_id`.

## Built on

Entry 02 — the diagnosis rests entirely on the `event_log` trail and the
`payload` column it introduced, and the fix extends the `DROPPED_REPEAT` rows
it added. The bug was found by the very evidence that entry was written to
collect.
