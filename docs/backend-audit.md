# Backend size and deletion audit

## Implemented Result

The journal-only refactor now has **1,514 backend lines in nine Python files**,
down from the audited 2,544 lines in 27 files: **1,030 fewer lines (40.5%)**.
The UI remains 202 lines. The separate 184 lines of retired unit tests are not
included in backend savings.

| Current responsibility | Lines |
| --- | ---: |
| Controller | 703 |
| GitHub API and authentication | 283 |
| HTTP routes, dashboard and native SSE | 195 |
| Store, three models and migration | 136 |
| Isolated Git operations | 123 |
| Config, entrypoints, logging, package marker | 74 |

There are no PR/queue projection writes, old planner, bootstrap, repository
classes, container/dependency layer, or duplicate GitHub client. Targeted events
and full sweeps share PR refresh/validation; structural repair remains explicit
and CI-only updates do not fetch Git or list every open PR. An in-flight batch
is removed only after successful processing, rather than copied back to another
queue on errors. Merge guards still re-read GitHub truth before mutation.

The one-time schema-v2 migration backfilled all 26 existing journal entries with
titles/authors and normalized historical action enums under the controller lock.
It preserved train state, event history, action history and retired tables.
`/prs` and `/prs/{number}` now expose journal entries, deliberately without the
old projection response schema. Native SSE preserves the named heartbeat used
by the existing UI. A real webhook replay returned `duplicate`.

The old planner/client/mapper tests were removed with their implementation;
pytest and its unused dependencies were removed. No replacement automated tests
or mocks were added or run. Static checks and live service verification passed.

Live restart verification interrupted a PR #32 batch with queue `[32, 33]`.
Startup recovered that in-flight batch and preserved both branch SHAs and their
single recorded Chute pushes. PR #31 landed, #32 landed after its second-stage
CI, and #33 was promoted to main without another push and landed at 10:16:19 UTC.
Both disposable PRs completed two-stage CI with exactly one Chute push each;
GitHub confirmed all three merged, and the final train was empty without errors.
The native SSE stream delivered both its snapshot and named heartbeat.
This exercises restart
recovery during CI reads, not every possible crash window around remote writes.

## Original Assessment

2026-11-03. Baseline is the working tree before this dependency upgrade and
formatter cleanup. Counts are physical lines, including comments and blanks;
they exclude the lockfile, documentation, virtualenv, and Git checkout cache.

## Count

| Responsibility | Files | Lines |
| --- | --- | ---: |
| Live controller and journal | `reconcile/loop.py`, `reconcile/store.py` | 838 |
| GitHub API, authentication, Git operations | `github/{client,train,auth,git}.py` | 553 |
| ORM models, enums, repositories, SQLite setup | `core/{models,enums}.py`, `db/{repo,sqlite}.py` | 522 |
| HTTP routes, dashboard snapshot, SSE | `api/` excluding package marker | 290 |
| Older planner and bootstrap | `reconcile/planner.py`, `startup/bootstrap.py` | 173 |
| Application wiring, config, entrypoints, logging | `app.py`, `config.py`, `main.py`, `logging.py` | 156 |
| Package markers | Six `__init__.py` files | 12 |
| **Backend total** | **27 Python files** | **2,544** |
| UI | `static/index.html` | 202 |
| Existing tests | Three unit-test files | 184 |

Backend excluding blank lines: 2,257. Excluding comment-only lines as well:
2,236. These are not statement counts. Whitespace deletion is not an
architectural improvement and is not included in the savings estimates below.
After upgrade-related formatting and timezone cleanup, the backend is 2,540 lines;
the four-line reduction is not the proposed architectural deletion pass.

## Where the complexity lives

The application contains two architectural generations, not two running
controllers. The journal-based controller makes the actual train decisions.
The older normalized PR/queue representation is still maintained alongside it.

Current flow:

```text
signed webhook -> events table -> controller -> journal + GitHub/Git
                      |                 |
                      v                 v
                 PR records <- publish() -> queue records
                      |
                dashboard and /prs
```

`publish()` reconstructs `PullRequestRecord` objects with empty reviews and
checks, then overwrites selected fields with journal state. It also copies the
journal order into a second queue table. The dashboard joins both representations
mostly to obtain PR titles/authors. This is a larger simplification opportunity
than replacing FastAPI or SQLModel.

The 797-line controller roughly divides into setup/scheduling (152), watch/eject
and event routing (107), durable dispatch/targeted updates/retries (138), train
repair/admission (154), rebase/push/ejection/merge (201), and publication (45).
Scheduling and recovery account for real requirements, but publication is
duplicate state and the targeted/full paths have overlapping refresh logic.

## Deletion plan

These are estimates of net backend savings, not measured completed deletions.
The stages are incremental; do not count the same code twice.

| Stage | What goes | Estimated net saving |
| --- | --- | ---: |
| 1. Retired runtime code | Old planner except its label helper, unused bootstrap, legacy GitHub PR fetch chain, unused repository methods, notification model | 280-350 |
| 2. One authoritative state | PR/queue projections, mapping enums/helpers, `publish()`, webhook projection writes, redundant container wiring | 400-550 more |
| 3. Shared execution path | Duplicate refresh/admission decisions, client wrappers, HTTP/SSE boilerplate | 120-240 more |
| **Total opportunity** | Preserve current behavior and safety requirements | **800-1,140** |

Stage 1's identifiable blocks account for about 278 lines before import/package
cleanup: planner decision functions 133, bootstrap 24, legacy client methods 76,
unused repository methods 35, notification model 10. The old planner is still
imported for `determine_admission_mode`; do not simply delete the module without
moving/replacing that helper. Existing planner/client tests reference the retired
code; they are not evidence that those paths run in the service.

Stage 2 must first put title/author metadata into journal entries and backfill
existing entries from PR records. Serve `/prs` from the journal or deliberately
retire the old response fields. Do not build a permanent compatibility projection.
Stop writing retired tables; dropping their historical contents is unnecessary.

Stage 3 should make events select work and full sweeps discover/select all work.
Both should call the same per-PR refresh and explicit train-transition operations.
Keep the dependency ordering needed when a head disappears or a follower fails.
Do not replace readable branches with a generic workflow framework or compressed
one-liners just to hit a line count.

## Proposed shape

```text
app.py         routes, lifespan, dashboard/SSE, config wiring
store.py       journal, durable inbox, action log, transactions
controller.py  work selection, transitions, retries/recovery
github.py      app auth, HTTP, policy, CI, review and merge operations
git.py         isolated checkout, rebase, lease-protected push
config.py      settings
```

Keep one journal, one inbox, one action log. These are distinct responsibilities,
not redundant state. Typed journal entries could improve readability and remove
scattered defaults, but adding validation models may increase raw line count.
Keep SQLite and SQLModel initially; replacing them with handwritten SQL is not
the source of the big savings.

**Assessment:** roughly 1,400-1,700 readable physical backend lines is plausible.
1,000 is a stretch, not demonstrated. Reaching it would require further measured
design improvements or an explicit scope reduction, not just removing dead code.

Preserve durable event deduplication and work acknowledgements; serialized train
mutation; expected-SHA merge and force-with-lease pushes; own-push recognition;
required-check policy discovery and fail-closed behavior; synthetic-merge/head CI
validation; review gates; recovery after a remote write but before its local
acknowledgement; descendant repair; Git isolation and credential handling.
These are the distributed-system part of the application, not ORM boilerplate.

## Dependencies and useful library support

The old `sqlmodel>=0.0.24` declaration was a lower bound. The lock and environment
actually used 0.0.37. Python dependencies and development tools were upgraded,
including transitive dependencies, and direct minimum versions were refreshed.

| Direct dependency | Installed before | Upgraded to |
| --- | --- | --- |
| cryptography | 46.0.6 | 50.0.2 |
| FastAPI | 0.135.2 | 0.162.0 |
| HTTPX | 0.28.1 | 0.28.1 |
| PyJWT | 2.12.1 | 2.15.1 |
| pydantic-settings | 2.13.1 | 2.16.2 |
| SQLModel | 0.0.37 | 0.0.48 |
| Uvicorn | 0.42.0 | 0.54.0 |
| pytest | 9.0.2 | 9.2.0 |
| Ruff | 0.15.8 | 0.17.3 |
| ty | 0.0.27 | 0.0.96 |

SQLAlchemy moved from 2.0.48 to 2.1.6; Pydantic from 2.12.5 to 2.14.2.
UI CDN pins are outside this backend upgrade and remain unchanged.

- SQLModel 0.0.45 introduced UTC-aware database datetimes, including SQLite;
  0.0.48 adds SQLAlchemy 2.1 support. Existing Chute producers already write UTC.
  Old stored rows were read successfully with the new types. Removed redundant
  timezone attachment and UI timestamp suffix handling. This saves very little
  code; it does not replace the controller. [Release notes](https://sqlmodel.tiangolo.com/release-notes/)
- FastAPI has native `EventSourceResponse`/`ServerSentEvent`, including serialization,
  keepalive comments and proxy/cache headers. This was already available in our
  previous installed version. It can replace manual SSE framing, but the UI's
  named heartbeat listener must be considered: ping comments do not invoke it.
  Snapshot selection and notification still belong to Chute. [SSE documentation](https://fastapi.tiangolo.com/tutorial/server-sent-events/)
- `sqlmodel_update()` can replace repetitive attribute assignment if a projection
  is retained. It existed before this upgrade. Prefer deleting the projection
  altogether. [Update documentation](https://sqlmodel.tiangolo.com/tutorial/fastapi/update/)
- SQLAlchemy's SQLite conflict-aware inserts can make webhook deduplication atomic
  instead of read-then-insert. This is also an existing capability, not a new
  justification for a framework migration. [SQLite upserts](https://docs.sqlalchemy.org/en/21/dialects/sqlite.html#insert-on-conflict-upsert)

The dependency upgrade is worthwhile, but hundreds of removable lines come from
our duplicated design, not missing library features. Architectural deletions are
deliberately deferred until this assessment is agreed upon. No automated tests
were added or run for this audit/upgrade.

Verification completed: Ruff, ty, dependency compatibility (`uv pip check`), and
`git diff --check` passed. The restarted service completed startup reconciliation
against GitHub with the existing SQLite database. Readiness, train/PR/event/action
endpoints, OpenAPI, dashboard HTML, and an SSE snapshot responded successfully.
No new merge-train exercise was performed for the dependency upgrade.
