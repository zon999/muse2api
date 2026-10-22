# Studio patches

Changes on the `studio` branch on top of upstream muse2api v1.6.3 (`e2b8936`), made so the engine can
run as a local backend for Muse Studio. `main` tracks upstream; `studio` is rebased onto it.

Run the offline suite with `tests/run_all.sh` (needs `uv` and `node`; uses local Chrome for the
browser-based media test). No test touches a real Muse account.

## Patches

| Area | Change | Commit | Live status |
|---|---|---|---|
| Tests | `tests/run_all.sh` runs pytest modules, script-style engine checks and the admin JS test | `47f6fa6` | — |
| Cookie helper | `tools/get_muse_cookie.py` no longer waits for `hatch_gw` (Muse stopped issuing it) | `302417f` | Offline only (login flow not rerun) |
| Local safety | Host defaults to `127.0.0.1`; CORS uses an allowlist; optional key for `/v1/media/*`; OpenAPI version from `version.json` | `c403006` | Offline only |
| Concurrency | Several jobs per account (`MUSE2API_ACCOUNT_CONCURRENCY`, default 2, max 5); idle accounts first | `f3731e1` | Pending |
| Stop detection | Wait loop reads the thread's status from the page's `sessions.list` every ~8s and ends on `interrupted`/`failed` | `c1c5864` | Pending |
| Cancel | `POST /v1/tasks/{id}/cancel`; queued tasks never reach Muse, running ones click Muse's stop button | `e2f0c99`, `140e8bf` | Pending |
| Task lookup | `GET /v1/tasks/{id}` for image and video tasks | `fe0e69a` | Offline only |
| Encryption | `accounts.json` encrypted with Fernet when `MUSE2API_DATA_KEY` is set | `18321c0` | Offline only |
| Reference images | `reference_images` (≤ 6, ≤ 8MB total) for image and video tasks, uploaded one by one | `ca24d3f` | Pending (checked against a fake composer in Chrome) |
| Resume | Muse thread id + account stored on the task; on startup the thread is reopened to fetch the result | `8f4ce03` | Pending |
| Readiness | `/readyz` reports `version` | `74b0526` | Offline only |

Quota stays on upstream's UI scraping: the server action HaoHao used no longer exists on Muse.

## New settings

| Variable | Default | Meaning |
|---|---|---|
| `MUSE2API_HOST` | `127.0.0.1` | Bind address shown in the admin page (Docker/systemd still pass `0.0.0.0`) |
| `MUSE2API_CORS_ORIGINS` | `https://muse.ai,https://www.muse.ai` | Allowed browser origins, comma separated; `*` allows all |
| `MUSE2API_MEDIA_AUTH` | `0` | `1` requires the API key for `/v1/media/*` |
| `MUSE2API_ACCOUNT_CONCURRENCY` | `2` | Jobs that may run on one account at the same time (1–5) |
| `MUSE2API_DATA_KEY` | empty | Fernet key; when set, `accounts.json` is stored encrypted. Losing it means re-importing accounts |

## New or changed endpoints

- `POST /v1/tasks/{id}/cancel` → `{"status": "cancelling"}`; 404 unknown task, 409 already finished.
  The task then ends as `cancelled`.
- `GET /v1/tasks/{id}` → the same body as `GET /v1/images/tasks/{id}` or `GET /v1/videos/{id}`.
  Running tasks carry `account` and `muse_session` once Muse has created the thread.
- `POST /v1/images/tasks`, `POST /v1/images/generations`, `POST /v1/videos` accept
  `reference_images: [dataURL | url | {url} | {b64_json} | {image_url}]`, merged with the older
  single-image fields; more than 6 images or more than 8MB inline → 400.
- `/readyz` adds `version`.
- New task status `cancelled`.

## Waiting for a live check

These rely on Muse behaviour that the offline tests can only fake. They need one session with a real
account (and the user's OK, since they use quota):

1. The `/thread/<id>` path segment equals `session_id` in `sessions.list` (stop detection and resume
   depend on it; if not, both quietly do nothing).
2. Two jobs on one account from two isolated browser contexts both finish, and cookie syncing after
   each job does not log the other out.
3. Cancel from the engine's own page moves the Muse session to `interrupted` and frees the account.
4. Three reference images are all accepted by the real composer.
5. Killing the engine mid-video and starting it again completes the task from the same thread.
6. `tools/get_muse_cookie.py` finishes a login without waiting for `hatch_gw` (only if that helper is still used;
   Muse Studio captures logins itself).
