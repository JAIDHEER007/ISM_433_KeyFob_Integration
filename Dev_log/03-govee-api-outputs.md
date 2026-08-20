# 03 — Govee API response logging

**Branch:** `feature/govee_api_outputs` (to branch from `dev` after entry 02 merges)
**Status:** Planned — not started

Record what the Govee API actually returns, so the trail from
[entry 02](02-e2e-event-store.md) carries real API detail instead of only an
exception string.

## Why

`apis.py` currently calls `.json()`, checks `message == "Success"`, throws the
body away, and raises a bare `Exception`:

```python
response = requests.request("PUT", GOVEE_DEVICE_CONTROL_URL, ...).json()
if response.get("message") != "Success":
    raise Exception("Failed to control Govee device")
```

`api_handler._on_complete()` only ever sees that string, so a `FAILED` trail
row reads `"Failed to control Govee device"` with no HTTP status, no Govee
error code, and no indication whether it was a rate limit, an auth failure, an
offline device, or a timeout. Those need different responses, and right now
they are indistinguishable.

## Scope

- Return structured results from the `apis.py` functions: HTTP status, Govee
  `message` / `code`, latency, which device and which command.
- Thread those into the `SUCCESS` / `FAILED` payloads the `API_HANDLER` stage
  already writes. No schema change needed — this is what the JSON `payload`
  column is for.
- Fix the two stale tests in `test_apis_request_shapes.py` carried over from
  [entry 01](01-initial-build.md), which still reference the pre-split
  `toggle_govee_study_light_bright_white` and
  `GOVEE_DEVICE_STUDY_LIGHT_MAC_ADDR`. They belong to this branch because it
  is the one that touches `apis.py`.

## Notes

The six `apis.py` functions share a lot of duplicated request-building and
response-checking code. Structuring the return values means touching all of
them, so factoring the common request/verify path into one helper is likely
worth doing in the same pass.

Distinguishing a timeout from an HTTP error matters for the payload: a
`requests.Timeout` has no response body at all, so the shape has to tolerate
its absence rather than assume a JSON body exists.

## Prerequisite

Entry 02 must be merged first — this builds directly on the `API_HANDLER`
stage rows and the `payload` column it introduced.
