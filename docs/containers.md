# Containers

This page covers the container path: a devcontainer for trying the harness without
a local Python setup, and a plain `docker build`/`docker run` path for the same
image, useful in Linux CI runners. Neither one is a hosted deployment -- COORD is
local-first, and this project does not claim or ship a hosted service. See
[standalone setup](standalone-setup.md) for the native install and
[getting started](getting-started.md) for the CLI walkthrough this image mirrors.

## What the image does

`.devcontainer/Dockerfile` builds a slim Python image (matching the range
`pyproject.toml` declares in `requires-python`), installs the package with its
`mcp` and `dev` extras from the checkout, and seeds the deterministic synthetic
demo board (`coord demo` -- the same fictional board described in
[getting started](getting-started.md#explore-a-synthetic-board)) so opening the
container gives you a populated board immediately, not an empty one. It then runs
the read-only board viewer on the port the harness uses everywhere else,
`7870` (`src/coordharness/board/server.py`, `DEFAULT_PORT`).

## Devcontainer

Open the repository in an editor or tool that supports the
[devcontainer spec](https://containers.dev/) and it will build
`.devcontainer/Dockerfile`, forward port `7870`, and run `coord doctor` once the
container is created so a broken image announces itself in the creation log
instead of failing silently the first time you run a command. A non-zero exit
from that step means something in the image is wrong -- read the report it
prints, not just the exit code.

Once it opens, `coord`, `coord-board`, and `coord-mcp` are all on `PATH` inside
the container, pointed at the container-local database the image seeded at
build time.

## Plain Docker

The same image builds and runs without any devcontainer tooling:

```bash
docker build -f .devcontainer/Dockerfile -t coordharness-demo .
docker run --rm -p 7870:7870 coordharness-demo
```

Open `http://localhost:7870` for the seeded demo board. To run the test suite
instead of the board server, override the command:

```bash
docker run --rm coordharness-demo \
  python -m pytest -q -p no:cacheprovider tests/coord
```

This is the shape a CI runner uses: build once, then run whatever verification
command that job needs against the same image, without installing Python or the
package's dependencies on the runner itself.

## What persists and what does not

- **The seeded demo board is baked into the image layer**, not created at
  container start. Every container from a given build starts from the exact same
  synthetic board (`coord demo` uses a fixed synthetic clock, so the seed is
  reproducible -- see `src/coordharness/demo.py`).
- **Nothing you do inside a running container persists past that container.**
  There is no volume mount in `.devcontainer/devcontainer.json` or the plain
  `docker run` command above. Claims, new work rows, and anything else written
  during a session live only in that container's writable layer and are gone
  when it is removed. Mount a host path onto `/var/lib/coordharness` (the value
  of `COORD_HOME` set in the Dockerfile) if you want a board that survives
  `docker run` invocations; nothing here does that for you by default.
- **The image never contains a local database, virtualenv, or git history from
  the machine that built it.** `.dockerignore` excludes `.coordharness/`, `*.db*`,
  `.venv/`, `build/`, and `.git/` from the build context so none of that can leak
  into a layer, however the image is built.

## Why the board binds `0.0.0.0` here and not by default

Everywhere else in this project, the board binds loopback only
(`src/coordharness/board/server.py`, `DEFAULT_HOST = "127.0.0.1"`) and refuses a
wider bind without `--allow-remote` and an explicit `--allowed-host` allowlist --
see [security and privacy](security-and-privacy.md). Inside a container that
posture is self-defeating: a process bound to `127.0.0.1` inside the container is
unreachable through Docker's published-port forwarding, which connects to the
container's own address, not its loopback interface. The image's `CMD` therefore
passes `--host 0.0.0.0 --allow-remote --allowed-host localhost --allowed-host
127.0.0.1` so the port published with `-p 7870:7870` (or the devcontainer's
`forwardPorts`) actually reaches the process. This relaxation is scoped to the
container's own network namespace; publishing that port further, onto a
non-loopback host interface or the open internet, is not something this image is
built or reviewed for.

## Not a production deployment

This image exists to let someone try the harness with one command and to give CI
runners a reproducible environment. It is not hardened, sized, or documented for
running as a long-lived service, does not manage backups or upgrades of the
database it seeds, and runs a single unauthenticated read-only viewer with no
concept of multiple tenants. Treat it the way you would treat running `coord
board` on a laptop: useful for one person or one job to look at one board, not a
hosted product.
