# chute

Internal merge-train bot for a single monorepo.

## What It Is

Chute is an internal merge-train controller built around GitHub pull requests and labels.

The intended model is:

- GitHub is the control plane
- labels drive behavior
- webhooks schedule targeted work against fresh GitHub truth
- a reconciler repairs the train and recovers missed events
- SQLite stores local queue state and action history

The train is a stacked PR queue:

- first queued PR targets `main`
- second queued PR targets the first PR branch
- third queued PR targets the second PR branch
- and so on

The core bet is that downstream PRs may stay green when GitHub retargets them after the head merges, as long as the effective patch is unchanged.

## Current Status

The controller implements GitHub App authentication, two-stage CI admission,
real Git rebases, PR retargeting, rebase-and-merge landing, and descendant repair.
SQLite journals expected pushes and merge intent for recovery after restarts.
Webhooks wake a serialized controller immediately. Reconciliation runs at startup,
with a 10-minute safety sweep for missed events and a **Reconcile now** dashboard button.
CI/status and review events refresh only the affected tracked PRs. A green head
attempts to land immediately, without listing all PRs or fetching Git. Label,
branch, base, and queue changes schedule train repair; repair traverses the active
stack and affected watchers, without repository-wide PR discovery. Unrelated
events do not query GitHub. Full discovery is reserved for startup, manual requests,
the safety sweep, and retries of an interrupted full pass.

Event routing and pending work are journaled together, with an in-flight batch
retained across crashes. API failures retry with capped backoff. Completion and
review events get up to five short, PR-scoped retries for GitHub's eventual
consistency; the safety sweep remains a fallback. Logs distinguish `mode=targeted`
from `mode=full` and include affected PR numbers and whether train repair ran.

This is a prototype intended for live validation on a disposable repository.
Targeted single-head landing has been exercised on `merge-bot-experiments`:
PR #30 landed after required CI without another full sweep or a force-push.
The journal-only backend also landed PRs #31-#33 as a stack, including follower
two-stage CI, SHA-preserving promotion, and a restart during an in-flight CI
batch. Conflict/ejection paths and every remote-write crash window have not
been re-exercised after this refactor.
`GET /train` reports queue entries, first-stage watchers, required checks, and
configuration errors. `GET /readyz` returns 503 while configuration is blocked.

## Development

Create the environment and install dependencies:

```bash
uv sync
```

Run the service in normal mode:

```bash
uv run chute
```

Run the service with hot reload:

```bash
uv run chute-dev
```

If you add or change script entries in `pyproject.toml`, run `uv sync` again before invoking them.

## Runtime Model

Right now Chute runs as a single process with:

- FastAPI for HTTP
- one in-process reconciler task
- one SQLite database on local disk

That is intentional. It keeps local development, Fly.io, and k8s all viable while the controller semantics are still being built.

## Labels

`automerge` arms a PR for the train (case-insensitive). Queue-now is deferred
and does not bypass admission. Chute removes `automerge` on ejection and adds:

- `chute-failed` for failed required CI or rebase conflicts.
- `chute-usererror` for a user push after labeling or an unsupported PR source.

After fixing an ejected PR, add `automerge` again to start a new admission attempt.

## Current Queue Semantics

1. PRs start against `main`. Adding `automerge` freezes their observed head SHA.
2. On an empty train, the first non-draft labeled PR takes the head position
   immediately. If it already includes current `main`, its SHA and existing CI
   are preserved; it lands once required CI and review/merge requirements pass.
3. Followers wait for green initial CI before appending. Chute rebases each onto
   the train tail, retargets it, and pushes with an explicit force-with-lease.
4. Any rewritten SHA must pass required CI again. This also applies to a head
   that needs rebasing because `main` advanced. Only the head can land.
5. Landing uses GitHub's rebase-and-merge API with the expected head SHA.
6. Promotion preserves the tested SHA when the old boundary and new base have
   identical trees. Other base changes require another rebase and fresh CI.
7. Ejection removes the PR from the train, restores its target to `main`, and
   restacks descendants. If cleanup conflicts, its branch is retained for manual repair.

Required checks come from classic branch protection and active rulesets on
`CHUTE_TARGET_BRANCH` (default `main`). No required checks, unreadable policies,
or disabled rebase merging block all branch mutations. The App must not be a
bypass actor for repository protection. GitHub remains the final merge gate;
concurrent changes to `main` can require another reconciliation.

Only same-repository branches are supported. Keep `.data/` across restarts:
it contains both the database journal and Git objects used as stack boundaries.
Run one controller process per database; a local lock prevents duplicate workers.
Git credentials are passed in subprocess environment variables, never clone URLs.

## Configuration

Configuration is driven by environment variables with the `CHUTE_` prefix.

Key settings:

- `CHUTE_DATABASE_PATH`
- `CHUTE_GITHUB_OWNER`
- `CHUTE_GITHUB_REPO`
- `CHUTE_GITHUB_WEBHOOK_SECRET`
- `CHUTE_GITHUB_APP_ID`
- `CHUTE_GITHUB_INSTALLATION_ID`
- `CHUTE_GITHUB_PRIVATE_KEY_PATH`
- `CHUTE_RECONCILE_INTERVAL_SECONDS`

Example local `.env`:

```env
CHUTE_ENV=development
CHUTE_HOST=0.0.0.0
CHUTE_PORT=8000
CHUTE_LOG_LEVEL=INFO

CHUTE_DATABASE_PATH=.data/chute.sqlite3
CHUTE_RECONCILE_INTERVAL_SECONDS=600
CHUTE_GIT_WORKDIR=.data/train-git
CHUTE_TARGET_BRANCH=main

CHUTE_GITHUB_OWNER=your-org
CHUTE_GITHUB_REPO=your-monorepo
CHUTE_GITHUB_WEBHOOK_SECRET=replace-me

CHUTE_GITHUB_APP_ID=123456
CHUTE_GITHUB_INSTALLATION_ID=78901234
CHUTE_GITHUB_PRIVATE_KEY_PATH=.secrets/github-app-private-key.pem
```

## GitHub Setup

You can start configuring the GitHub side now even though the full integration is not finished.

### 1. Create a GitHub App

Create a GitHub App scoped to the single repository.

Recommended baseline permissions:

- Repository permissions:
- `Pull requests`: Read and write
- `Contents`: Read and write
- `Metadata`: Read-only
- `Administration`: Read-only (required to read branch protection)
- `Checks`: Read-only
- `Commit statuses`: Read-only

Event subscriptions:

- `Pull request`
- `Pull request review`
- `Check run`
- `Check suite`
- `Status`
- `Push`

You may later need additional permissions depending on how merge execution is implemented, but this is the right starting shape.

### 2. Install the App

Install the GitHub App into the target organization or user account, restricted to the single monorepo Chute will manage.

Record:

- App ID
- Installation ID
- webhook secret
- private key file path

These map directly to the `CHUTE_GITHUB_*` environment variables above.

### 3. Configure the Webhook URL

Point the GitHub App webhook to:

```text
https://your-chute-host/webhooks/github
```

If you are running locally, use a tunnel such as `ngrok`, `cloudflared`, or an equivalent internal ingress path.

### 4. Create the Labels

Create `automerge` in the repository. Chute creates `chute-failed` and
`chute-usererror` when needed.

Label matching is case-insensitive: `automerge` and `Automerge` are equivalent.

## Local Bring-Up

1. Create `.env`
2. Run `uv sync --extra dev`
3. Start the service with `uv run chute-dev`
4. Open `http://localhost:8000/`
5. Confirm the live train board loads; Swagger is at `/docs`
6. Send a health check to `GET /healthz`

## Endpoints

The current HTTP surface is:

- `GET /` live train board (Alpine.js and Tailwind via CDN)
- `GET /stream` server-sent state updates; reconnects automatically
- `POST /reconcile` request a full reconciliation (dashboard action)
- `GET /docs` Swagger UI
- `GET /openapi.json` OpenAPI spec
- `GET /healthz`
- `GET /readyz`
- `GET /train` active queue summary
- `GET /prs` tracked pull requests
- `GET /prs/{number}` one tracked pull request
- `GET /events` recently received webhook events
- `GET /actions` recent controller actions
- `POST /webhooks/github` GitHub webhook receiver

## Storage

SQLite is currently the only state store.

Active tables:

- `train_journal`: the sole train/PR state, including display metadata and pending work.
- `events`: the durable, deduplicated webhook inbox.
- `actions`: controller action history.

The first startup of the journal-only backend backfills titles/authors from the
old PR table and normalizes action names/statuses in one transaction under the
controller lock. Retired tables remain as history but are no longer written or
used for live state. `/prs` and `/prs/{number}` now return journal entries, not
the retired projection schema. There is no compatibility projection.

GitHub is still intended to be the source of truth for live PR state. SQLite is there for:

- restart safety
- queue state
- action history
- debugging

## Backend Layout

- `controller.py`: event selection, shared PR refresh, train transitions and recovery.
- `store.py`: journal, inbox, action history and one-time metadata migration.
- `github.py`: App authentication and GitHub API/policy operations.
- `git.py`: isolated rebases and lease-protected pushes.
- `app.py`: HTTP endpoints and native FastAPI SSE.
- `config.py`, `main.py`, `logging.py`: configuration and process startup.

The retired planner/client/projection tests were removed with their implementation.
Use `uv run ruff check src/chute` and `uv run ty check` for static checks; behavior
is exercised live on the disposable `RmStorm/merge-bot-experiments` repository.

## Notes

- `uv run chute` is the non-reloading mode
- `uv run chute-dev` is the reloading development mode
- if hot reload does not reflect recent code changes, rerun `uv sync` first and then start `uv run chute-dev`
