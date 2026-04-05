# chute

Internal merge-train bot for a single monorepo.

## What It Is

Chute is an internal merge-train controller built around GitHub pull requests and labels.

The intended model is:

- GitHub is the control plane
- labels drive behavior
- webhooks mark pull requests dirty
- a reconciler recomputes desired state from GitHub truth
- SQLite stores local queue state and action history

The long-term train design is a stacked PR queue:

- first queued PR targets `main`
- second queued PR targets the first PR branch
- third queued PR targets the second PR branch
- and so on

The core bet is that downstream PRs may stay green when GitHub retargets them after the head merges, as long as the effective patch is unchanged.

## Current Status

Implemented today:

- `uv`-managed Python project
- FastAPI app
- Swagger UI at `/`
- SQLite schema and repositories
- GitHub webhook receiver with signature verification
- startup bootstrap stub
- reconciler loop
- pure planner for admission and queue/head state
- read-only state endpoints
- planner unit tests

Not implemented yet:

- real GitHub App authentication
- real GitHub read model
- PR base retargeting
- merge execution
- notification delivery
- branch/restack repair logic

Treat the current codebase as a working controller skeleton, not a production-ready merge bot.

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

Chute currently recognizes these labels:

- `Automerge`
  Means the PR is armed, but should not enter the queue until it is eligible.
- `Automerge-queue-now`
  Means the PR should enter the queue immediately, even if checks are still pending.

If both labels are present, `Automerge-queue-now` wins.

## Current Queue Semantics

The current planner behavior is:

- `Automerge` stays `armed` until checks, reviews, and mergeability are acceptable
- `Automerge-queue-now` enters the queue immediately
- existing queue order is preserved
- new queue candidates are appended
- the front PR becomes `ready` if eligible
- the front PR becomes `waiting_checks` if still pending or blocked
- a failed head is marked `failed` and ejected from the active queue

This is local planning behavior only. GitHub writes are not wired yet.

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
CHUTE_RECONCILE_INTERVAL_SECONDS=10

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
- `Checks`: Read-only
- `Commit statuses`: Read-only

Event subscriptions:

- `Pull request`
- `Pull request review`
- `Check run`
- `Check suite`
- `Status`

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

Create these labels in the repository:

- `Automerge`
- `Automerge-queue-now`

Use the exact spelling above for now. The code currently treats these label names as fixed constants.

## Local Bring-Up

1. Create `.env`
2. Run `uv sync --extra dev`
3. Start the service with `uv run chute-dev`
4. Open `http://localhost:8000/`
5. Confirm the Swagger UI loads
6. Send a health check to `GET /healthz`

## Endpoints

The current HTTP surface is:

- `GET /` Swagger UI
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

Tables:

- `pull_requests`
- `queue_entries`
- `events`
- `actions`
- `notifications`

GitHub is still intended to be the source of truth for live PR state. SQLite is there for:

- restart safety
- queue state
- action history
- debugging

## Testing

Run the current unit tests:

```bash
uv run pytest
```

## Next Implementation Steps

The next milestones in code are:

1. Real GitHub App authentication and API client
2. Startup scrape of open relevant PRs from GitHub
3. Webhook-driven PR refresh against GitHub truth
4. Richer planner outputs for desired base chaining
5. GitHub writes for retargeting and merge execution

## Notes

- `uv run chute` is the non-reloading mode
- `uv run chute-dev` is the reloading development mode
- if hot reload does not reflect recent code changes, rerun `uv sync` first and then start `uv run chute-dev`
