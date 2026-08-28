# Tracked jobs

Use `coord-jobs` only for commands whose duration or resource use outlives a
normal agent turn. Give every launch a stable job ID and the parent work ID.

A sidecar may contain bounded public telemetry: state, progress, timestamps,
resource totals, executable class, and an opaque run identity. It must not
contain command bodies, prompts, environment secrets, stdout, private paths, or
source text.

GPU work needs an actual serialized lock or launcher receipt. An environment
variable asserted by the child is not proof that a lock is held. On launch or
runtime failure, finalize the run and release or park the claim in a `finally`
path so stale ownership cannot survive until lease expiry.

Before termination, resolve the exact process group and job identity. Prefer a
graceful group stop, verify descendants are gone, and preserve the final
telemetry and artifact receipt.
