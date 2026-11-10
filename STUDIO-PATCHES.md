# Studio patches

Changes on the `studio` branch on top of upstream muse2api, made so the engine can run as a local backend for
Muse Studio. `main` tracks upstream. `studio` started on v1.6.3 (`e2b8936`) and has upstream v1.6.9 (`8a3dab9`)
merged in (a merge, not a rebase: the two sides changed the same proxy, reference and account-pool code).

## Upstream v1.6.9 merge (2026-11-08)

Taken from upstream: composer detection on the new muse.ai page (no placeholder overlay, no `<form>`), references
uploaded with `DOM.setFileInputFiles` (images, video, audio, PDF/DOC; 10 files, 40 MiB each, 100 MiB total), a
generated result that is byte-identical to an uploaded reference is never delivered, per-context download folders,
model-refusal detection, fresh disposable context per quota read, login sessions accepted with only `hatch_sess`,
reference-source safety switches (`MUSE2API_REF_IMAGE_LOCAL_FILES`, `MUSE2API_REF_IMAGE_PRIVATE_FETCH`) and the
full-width admin page.

Kept from studio instead of upstream:

- **Proxies.** Upstream's per-account proxy (`proxy.py`, a `proxy` object on the account, `/admin/accounts/{id}/proxy/test`,
  test receipts) is dropped. Studio's `proxy_url`, `/admin/proxy/test`, `/admin/proxy/tunnel` and `proxy_support.py`
  stay, because Muse Studio calls them. The admin page's proxy button is removed since its dialog needs the dropped
  routes. Upstream's proxy tests went with it.
- **Account leases.** Upstream's `_account_operation` locks an account against tasks. Here it only hands out a copy
  and closes the page: Studio runs several jobs per account and polls keepalive while they run.
- **Reference field.** `reference_images` is still accepted and joins `images` (Studio sends it). The old 6-file / 8 MB
  limits and base64 injection are replaced by upstream's limits and upload path.
- Cancel, resume (`on_session`), session-stop detection and the load gate are carried into upstream's new
  `generate` / `_generate_attached` / `_wait_attachment`.

`tests/run_all.sh` also runs `test_reference_upload_browser.py` (real Chrome, no Muse login). `test_admin_layout.py`
is not run on macOS: Chrome there keeps a 15px scrollbar and the English page at 320px overflows (upstream too).

Run the offline suite with `tests/run_all.sh` (needs `uv` and `node`; uses local Chrome for the
browser-based media test). No test touches a real Muse account.

## Patches

| Area | Change | Commit | Live status |
|---|---|---|---|
| Tests | `tests/run_all.sh` runs pytest modules, script-style engine checks and the admin JS test | `47f6fa6` | — |
| Cookie helper | `tools/get_muse_cookie.py` no longer waits for `hatch_gw` (Muse stopped issuing it) | `302417f` | Offline only (login flow not rerun) |
| Local safety | Host defaults to `127.0.0.1`; CORS uses an allowlist; optional key for `/v1/media/*`; OpenAPI version from `version.json` | `c403006` | Offline only |
| Concurrency | Several jobs per account (`MUSE2API_ACCOUNT_CONCURRENCY`, default 2, max 5); idle accounts first | `f3731e1` | Verified 2026-10-23: 2 images ran at once on one account |
| Stop detection | Wait loop reads the thread's status from the page's `sessions.list` every ~8s and ends on `interrupted`/`failed` | `c1c5864` | Verified: `/thread/<id>` equals `session_id` |
| Cancel | `POST /v1/tasks/{id}/cancel`; queued tasks never reach Muse, running ones click Muse's stop button | `e2f0c99`, `140e8bf` | Verified: video cancelled in 3s, Muse session `interrupted` |
| Task lookup | `GET /v1/tasks/{id}` for image and video tasks | `fe0e69a` | Offline only |
| Encryption | `accounts.json` encrypted with Fernet when `MUSE2API_DATA_KEY` is set | `18321c0` | Offline only |
| Reference images | `reference_images` for image and video tasks; since the v1.6.9 merge it joins upstream's `images` and uses upstream's limits and upload | `ca24d3f` | Verified before the merge: 3 references accepted, all used in the output |
| Resume | Muse thread id + account stored on the task; on startup the thread is reopened to fetch the result | `8f4ce03` | Verified: engine stopped mid-video, restarted, same thread's video fetched |
| Readiness | `/readyz` reports `version` | `74b0526` | Offline only |
| Browser lifecycle | One Chromium even when jobs start concurrently; a reattached browser is closed on stop | `cb7fa8d` | Verified: no Chrome left after shutdown |
| Readiness | `/readyz` reports `concurrency` and `account_concurrency` | `baa1097` | Offline only |
| Account proxy | Optional `proxy_url` per account: `requests` calls and the account's BrowserContext (`proxyServer`) use it; a proxied account never falls back to a plain tab; proxies with a password go through a loopback forwarder (`proxy_support.py`) | (this commit) | Chrome verified offline against a fake authenticating proxy; not run against a real proxy |
| Video idempotency | `POST /v1/videos` honours `Idempotency-Key` like image tasks (same key → same task; different body → 409) | (this commit) | Offline only |
| Chat streaming | `chat_stream` emits only text past what it already sent and holds back a trailing markdown marker Muse is still rendering ("-" plus a zero-width space), so a re-rendered bubble no longer repeats the whole reply; a new agent bubble flushes the previous one first. While the Stop button shows (the agent is running tools), the 40s first-token limit gives way to the request timeout. Not fixed upstream as of `8213b3d` | `4cf7527` | Verified 2026-11-10: list replies on both accounts come back once |
| Account pin | `/v1/chat/completions` and `/v1/videos` accept `account_id`: the request runs on that account only and never fails over, because each account has its own Muse workspace (`/home/hatch`). With a `[raw]` video prompt (`f10272f`) this also fetches an existing workspace file ("attach ~/…/final.mp4") through the normal media download | (this commit) | Verified 2026-11-10: status check answered from the pinned account; two workspace videos fetched byte-identical |
| Chat detach | `/v1/chat/completions` accepts `detach: true`: the call returns once Muse has accepted the message (the page's thread is listed by the gateway with a live status, or the first text arrived), frees the chat lock and answers with `muse_session` (the thread id), so long agent jobs run in parallel in Muse. Returning on the Stop button alone lost the message: the button also shows before the gateway acknowledges it. The chat lock is still one for the whole engine (one browser) | (this commit) | Verified 2026-11-10: two detached shell jobs sent 16s apart on one account both ran to the end in parallel, and a status chat during them did not disturb them |

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
  `reference_images: [dataURL | url | {url} | {b64_json} | {image_url}]`, appended after upstream's `images`;
  limits are upstream's (10 files, 40 MiB each, 100 MiB total) → 400 beyond them.
- `PATCH /admin/accounts/{id}` accepts `proxy_url` (`http://`, `https://` or `socks5://`, no password for socks5;
  `""` clears it). Account responses never contain the URL, only `has_proxy` and `proxy_host`.
- `POST /admin/proxy/test` `{proxy_url, quick?}` → `{ok, ip, latency_ms, muse_reachable, muse_status, error}`.
  With `quick: true` the proxy alone is probed (it is asked to open a tunnel to a neutral address, nothing is sent to
  Muse or any web service); Studio's periodic health check uses it.
- `POST /admin/proxy/tunnel` `{proxy_url}` → `{server}`: the address Chromium should use for that proxy.
- Proxy failures raise `MuseProxyError`, whose text starts with `PROXY_ERROR:` and never contains the URL.
- `/readyz` adds `version`.
- New task status `cancelled`.

## Live check (2026-10-23)

All five checks passed with one real account: thread id equals `session_id`; two jobs on one
account ran in parallel (35s and 162s); a running video was cancelled in 3s and its Muse session
became `interrupted`; three reference images were all used; a video survived an engine restart and
was fetched from the same thread. The run also exposed concurrent `start()` launching two Chromium
processes (fixed in `cb7fa8d`). Not rerun: `tools/get_muse_cookie.py` login (Muse Studio captures
logins itself).
