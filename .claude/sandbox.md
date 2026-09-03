# Sandboxed Claude Code

Runs Claude Code with `--dangerously-skip-permissions` inside a container that
can see this repository and nothing else of the host filesystem. The agent gets
full internet access and full write access to `/workspace` (this repo); it has
no path by which to write or delete anything else on the machine.

## Spinning up the containers

### The Claude Code sandbox

```bash
scripts/claude-sandbox.sh login    # once, to authenticate
scripts/claude-sandbox.sh          # every time after that
```

The wrapper builds the image on first use. Full command set:

```bash
scripts/claude-sandbox.sh                          # interactive agent
scripts/claude-sandbox.sh -p "run the unit tests"  # one-shot prompt
scripts/claude-sandbox.sh shell                    # bash, no agent
scripts/claude-sandbox.sh --port 8000              # publish a dev server on 127.0.0.1:8000
scripts/claude-sandbox.sh build                    # rebuild: upgrades Claude Code, adds tools
scripts/claude-sandbox.sh reset                    # wipe stored credentials and history
scripts/claude-sandbox.sh --help
```

Without the wrapper, the equivalent raw commands are:

```bash
docker compose -f compose.claude.yaml build claude
docker compose -f compose.claude.yaml run --rm claude claude --dangerously-skip-permissions
```

### The application stack

The sandbox has no Docker access, so bring Postgres, the API and the workers up
from the **host**, not from inside the agent:

```bash
cp .env.example .env             # first time only, then fill in secrets
docker compose up -d --build     # postgres, api, worker-code-review,
                                 # worker-academic-planner, worker-finance
docker compose ps
docker compose logs -f api
docker compose down              # add -v to also drop the data volumes
```

The API is published on `127.0.0.1:8000` (`API_PORT`). Every value in
`compose.yaml` has a working default, so `docker compose up -d` succeeds with no
`.env` at all — you only need one for the GitHub and Discord credentials.

From inside the sandbox those services are reachable at
`host.docker.internal:8000`, and Ollama at `host.docker.internal:11434`.

## What enforces the isolation

The guarantee is structural, not a matter of the agent behaving well. It comes
from `compose.claude.yaml`:

| Control | Effect |
| --- | --- |
| A single bind mount, `.:/workspace` | The repository is the only host path that exists inside the container. There is no mount of `$HOME`, `/`, or anything else, so host files outside the repo are not merely protected — they are absent. |
| `claude_home` named volume on `/home/node` | Credentials, session history and caches persist between runs, but they live inside the Docker VM's disk rather than on the host filesystem. |
| No `/var/run/docker.sock` | Access to the Docker socket is equivalent to root on the host and would let a container mount any host path. It is deliberately absent. |
| `cap_drop: [ALL]` | Verified `CapEff: 0`. The process cannot mount filesystems, change ownership, or use any other privileged operation. |
| `user: "1000:1000"` | Unprivileged. Also required by Claude Code, which refuses `--dangerously-skip-permissions` when running as root. |
| `security_opt: [no-new-privileges:true]` | Blocks privilege escalation through setuid binaries. |
| Docker Desktop's Linux VM | On macOS, containers run inside a VM, so even a container escape lands in the VM rather than on the host. |

Verify it yourself at any time:

```bash
scripts/claude-sandbox.sh shell -lc \
  'findmnt -rno TARGET,SOURCE | grep -v "^/\(proc\|sys\|dev\)"; grep CapEff /proc/self/status'
```

Exactly one row should have a host-backed source — `/run/host_mark/...` on
macOS — and it must be `/workspace` pointing at this repository's path.
Everything else resolves to the overlay filesystem, a tmpfs, or `/dev/vda1`,
all of which live inside the Docker VM. `CapEff` should be all zeroes.

## What is deliberately *not* protected

- **The repository itself is fully writable.** That is the point of the
  sandbox, but it means `rm -rf`, a bad `git reset --hard`, or a rewritten
  history inside `/workspace` are real losses on the host. Commit and push
  often; the container is not a substitute for a branch.
- **Network egress is unrestricted**, so that the agent has working internet.
  An agent with internet and a copy of `.env` can send secrets anywhere. If you
  want the sandbox to reach only an allowlist of hosts, that needs an iptables
  rule set inside the container plus `cap_add: [NET_ADMIN]`, which is a
  meaningfully different setup.
- **Host local ports are reachable** via `host.docker.internal`, so the sandbox
  can talk to Ollama on 11434 and anything else listening locally. Drop the
  `extra_hosts` entry in `compose.claude.yaml` if you would rather it could
  not.

## Authentication

`scripts/claude-sandbox.sh login` starts Claude Code without the permission
bypass so you can complete the OAuth flow; open the printed URL on the host and
paste the code back. The credentials land in the `lifeagent_claude_home` volume
and survive later runs and image rebuilds.

Host credentials cannot be reused: macOS stores them in the Keychain, which the
container has no access to. Alternatively, export `ANTHROPIC_API_KEY` before
launching and it is passed through.

## Git identity

The image sets `safe.directory` for `/workspace` but no identity, so commits
fail until you provide one:

```bash
export GIT_USER_NAME="Richard Liu"
export GIT_USER_EMAIL="richardliuuniapps2025@gmail.com"
```

No push credentials are mounted, by design — pushing is something you do from
the host after reviewing the diff.

## Tooling inside the sandbox

The image ships node 22, uv 0.12.6, CPython 3.12.8 (via uv, under `/opt`), git,
ripgrep, jq and `psql` — enough to run this project's toolchain. Because the
runtime user is unprivileged, the agent **cannot** `apt-get install` anything;
it can install into `/workspace` (uv, local npm) or into its own home
(`npm install -g` resolves to `/home/node/.npm-global`). Anything that needs to
be part of the image belongs in `infra/claude-sandbox/Dockerfile`, followed by
`scripts/claude-sandbox.sh build`.

## Files

- `compose.claude.yaml` — service definition and the entire isolation policy.
  Its own compose project, so `docker compose up` for the app never starts an
  agent.
- `infra/claude-sandbox/Dockerfile` — the image.
- `infra/claude-sandbox/entrypoint.sh` — startup checks, git identity, banner.
- `scripts/claude-sandbox.sh` — the wrapper you actually run.
