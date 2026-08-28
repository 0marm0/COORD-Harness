# Local models

Status: **Preview** in [`feature-status.json`](feature-status.json).

`coord-models` is the explicit, bounded local-inference surface. No model is
selected by default. Configure either `COORD_MODEL_CATALOG=/path/models.json`
or the single-model environment variables beginning with
`COORD_LOCAL_MODEL_`.

Example catalog:

```json
{
  "models": [
    {
      "model_id": "your-account/your-generation-model",
      "runner": "mlx_lm",
      "modes": ["triage", "classify", "draft", "audit", "summarize"],
      "requires_gpu": true,
      "context_tokens": 32768,
      "notes": "operator-managed model"
    }
  ]
}
```

Use `coord-models list` to inspect configuration and `coord-models check` to
probe execution support. `check` exits nonzero when no configured model is
ready and reports the actual platform and dependency state. The packaged MLX
backend is optional: install `coordharness[mlx]` on Apple-silicon macOS.

Run one bounded request only against an existing, non-terminal coordination
work item:

```sh
coord-models run --work-id WORK-1 --mode draft --prompt-file prompt.txt
```

MLX/Metal execution always acquires an operating-system file lock held by the
running process. Environment flags are not lock authority. The run creates a
temporary advisory claim, never completes the work item, and releases its
claim/finalizes its run/ends its owned session even when generation fails.
`--prefer-cpu` refuses MLX-only catalogs; no CPU inference backend is currently
implemented. Embedding catalog entries are visible to `list`/`check`, but
`run --mode embed` fails during preflight before coordination lifecycle writes.
