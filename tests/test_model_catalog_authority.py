from __future__ import annotations

from coordharness.coord.modeld_lite import LocalModelSpec, select_model


def test_catalog_cannot_downgrade_mlx_into_unlocked_cpu_entry() -> None:
    malicious = LocalModelSpec(
        model_id="operator/misconfigured-model",
        runner="mlx_lm",
        modes=["draft"],
        requires_gpu=False,
        context_tokens=4096,
        notes="attempted lock bypass",
    )
    assert malicious.requires_gpu is True
    assert select_model("draft", prefer_gpu=False, catalog=[malicious]) is None
