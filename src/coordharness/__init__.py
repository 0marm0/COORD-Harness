"""A coordination control plane for fleets of AI coding agents.

Agents claim work, hold leases, hand off to each other, and record what they
did in one SQLite database. Status is derived from live signals rather than
stored, so nothing can report "running" without a process behind it.
"""

__version__ = "0.1.0"
