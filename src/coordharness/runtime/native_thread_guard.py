from __future__ import annotations

import os
from collections.abc import MutableMapping


THREAD_ENV_KEYS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

_FALSE_VALUES = {"0", "false", "no", "off"}
_TRUE_VALUES = {"1", "true", "yes", "on"}

_PROFILE_THREADS = {
    "serial": "1",
    "test": "1",
    "safe": "1",
    "balanced": "4",
    "job": "4",
    "cpu": "4",
    "perf": "8",
    "training": "8",
    "fit": "8",
}


def guard_enabled(env: MutableMapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    value = environ.get("COORD_NATIVE_THREAD_GUARD", "1").strip().lower()
    return value not in _FALSE_VALUES


def respect_overrides(env: MutableMapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    value = environ.get("COORD_NATIVE_THREAD_GUARD_RESPECT_OVERRIDES", "").strip().lower()
    return value in _TRUE_VALUES


def canonical_profile(profile: str | None) -> str:
    value = (profile or "serial").strip().lower()
    if not value:
        return "serial"
    if value in {"inherit", "respect", "none"}:
        return "inherit"
    return value if value in _PROFILE_THREADS else "serial"


def profile_defaults(profile: str | None) -> dict[str, str]:
    canonical = canonical_profile(profile)
    defaults = {"KMP_DUPLICATE_LIB_OK": "TRUE"}
    if canonical == "inherit":
        return defaults
    threads = _PROFILE_THREADS[canonical]
    defaults.update({key: threads for key in THREAD_ENV_KEYS})
    return defaults


def apply_startup_guard(env: MutableMapping[str, str] | None = None) -> dict[str, str]:

    environ = os.environ if env is None else env
    if not guard_enabled(environ):
        environ["COORD_NATIVE_THREAD_GUARD_ACTIVE"] = "0"
        return {}

    profile = canonical_profile(environ.get("COORD_NATIVE_THREAD_PROFILE"))
    defaults = profile_defaults(profile)
    if respect_overrides(environ):
        for key, value in defaults.items():
            environ.setdefault(key, value)
    else:
        for key, value in defaults.items():
            environ[key] = value

    environ["COORD_NATIVE_THREAD_PROFILE_ACTIVE"] = profile
    environ["COORD_NATIVE_THREAD_GUARD_ACTIVE"] = "1"
    return defaults
