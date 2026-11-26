# Pairing endpoint — module contract

`freetalon.pairing` is the server side of the M6 pairing-join protocol
(DESIGN.md §6, `/home/seeker/freetalon-workspace/provisioning/DESIGN.md`).
This file is the contract: the store, the HTTP layer, and the tests all
answer to what is written here. Where DESIGN.md §6 pins a behavior, it
wins; where it is silent, the choice made here is the only choice — do
not re-decide it in a test or an implementation detail.

## Roles

- **Node** — a freshly installed machine on the provisioning segment. It
  generated its pairing code itself (node console, `ensure_state`) and
  knows the code, its 128-bit nonce, and its machine-id. Node endpoints
  need no bearer token: on the isolated segment the code is a capability
  that was only ever visible on the node's physical console screen
  (DESIGN.md §6's physical-presence model).
- **Operator** — Pat / the control plane. Operator endpoints require
  `Authorization: Bearer <token>`, token from the `FREETALON_PAIRING_TOKEN`
  environment variable only, compared with `hmac.compare_digest`
  (`freetalon.security.authorize`). **Fail-closed:** if the env var is
  unset or empty, every operator request is refused — there is no
  default token, no file fallback, and the token is never logged and
  never stored in the repo.

## Status vocabulary (exact strings, no synonyms)

| status      | meaning                                                        |
|-------------|----------------------------------------------------------------|
| `pending`   | registered, waiting for operator confirmation                  |
| `confirmed` | operator confirmed; credential bundle attached, not yet claimed |
| `claimed`   | bundle delivered exactly once; the code is burned              |
| `expired`   | TTL passed while `pending` or `confirmed`; bundle wiped        |

No other status string may appear anywhere. (The loop's failed draft
used `confirmed: bool`, `"gone"`, and `"not-found"` in different modules;
that incoherence is what this contract exists to prevent.)

The four strings are the module constants `STATUS_PENDING = "pending"`,
`STATUS_CONFIRMED = "confirmed"`, `STATUS_CLAIMED = "claimed"`,
`STATUS_EXPIRED = "expired"` in `freetalon.pairing.store`, re-exported
from `freetalon.pairing`. Outside the store, code and tests name the
constants, never the literals.

## Record fields

Stored per code in one JSON file: `{"version": 1, "records": {...}}`.

| field         | type         | notes                                             |
|---------------|--------------|---------------------------------------------------|
| `code`        | str          | `WORD-NNNN`, see validation                       |
| `nonce`       | str          | 32 lowercase hex chars (the node's 128-bit nonce) |
| `machine_id`  | str          | node's `/etc/machine-id`                          |
| `hostname`    | str \| null  | optional, as registered                          |
| `mac`         | str \| null  | optional, as registered                          |
| `status`      | str          | one of the four statuses above                    |
| `credentials` | object\|null | the bundle; non-null **only** while `confirmed`   |
| `created_at`  | float        | epoch seconds, store clock                        |
| `expires_at`  | float        | `created_at + ttl_seconds`                        |
| `confirmed_at`| float\|null  |                                                   |
| `claimed_at`  | float\|null  |                                                   |

**Public view** (everything the HTTP layer returns about a record):
`code, status, machine_id, hostname, mac, created_at, expires_at,
confirmed_at, claimed_at`. The public view **never** contains `nonce`
or `credentials`. The only place credentials ever leave the server is
the single claiming poll.

## Validation

- `code` must match `^[A-Z]{2,}-\d{4}$` (console codes are an uppercase
  word + zero-padded 4 digits, e.g. `KESTREL-4821`). The server does not
  hardcode the node's word list.
- `nonce` must be 32 hex chars; stored lowercased.
- `machine_id` is required text (≤128 chars, no control characters);
  `hostname` / `mac` are optional text under the same rules. Text
  fields are stripped of surrounding whitespace before validation and
  storage: a required field that is empty after stripping is invalid
  (`""` and `"   "` are both rejected), and the length cap applies to
  the stripped text.
- A credential bundle must be a non-empty JSON object, JSON-serializable,
  ≤ 32768 bytes serialized. The server never inspects bundle contents
  (per §6: a Tailscale auth key + SSH management key material).

## Clock seam

`PairingStore(path, ttl_seconds=1800, now_fn=time.time)` — `path` may
be a `str` or a `pathlib.Path`. `ttl_seconds` must be positive; a
non-positive value raises `ValueError` from the constructor. **Every**
time read in the store goes through `now_fn` — nothing calls
`time.time` directly. Tests inject a fake clock; there is no other
time source to patch. Default TTL is 1800 s, overridable via
`FREETALON_PAIRING_TTL_SECONDS` (read by the server entry point, not
by the store).

## Expiry

Lazy: on every store operation, a `pending`/`confirmed` record whose
`expires_at` has passed becomes `expired` and its `credentials` are
wiped, persisted in the same write as the operation. `claimed` records
never expire (410 is forever). Re-registering an `expired` code with
the **same** nonce + machine-id starts a fresh `pending` record (the
node is still standing there with the same code on its screen; §6 step
5's next window). Re-registering a `claimed` code changes nothing.

## Store API (`store.py`) and exceptions

All errors derive from `PairingError`. `store.py` exports
`PairingStore`, `PollOutcome`, the four `STATUS_*` constants, and
every exception named in this section; `freetalon.pairing` re-exports
all of them from the package root.

- `register(code, nonce, machine_id, hostname=None, mac=None)` →
  public view. New code → `pending`. Same code + same nonce +
  machine_id → idempotent, returns the current view. Same code,
  different nonce or machine_id → `CodeConflict`. Bad input →
  `InvalidRegistration` (also a `ValueError`).
- `confirm(code, credentials)` → public view. Unknown → `CodeNotFound`;
  expired → `CodeExpired`; claimed → `CodeAlreadyClaimed`; already
  confirmed → `CodeAlreadyConfirmed`; bad bundle → `InvalidBundle`.
- `poll(code)` → `PollOutcome(status, credentials)` — a frozen
  dataclass; `.credentials` is the bundle dict or `None`. Unknown →
  `CodeNotFound` (raised, never a dict). `pending` → credentials None.
  `confirmed` → returns the bundle **and burns the record in the same
  locked operation** (status `claimed`, credentials wiped,
  `claimed_at` set); the outcome's status is `"confirmed"` because that
  is the state that was claimed. Every later poll → `"claimed"`, None.
  `expired` → `"expired"`, None.
- `list_records()` → list of public views.

## Persistence

Single JSON file, one writer process (the pairing server). Every
mutation is written atomically: temp file in the same directory opened
with mode `0600`, `fsync`, `os.replace` — the store file can hold live
credential bundles, so it is `0600` at all times. A missing file is an
empty store; a corrupt file raises `StoreCorruptError` (never silently
reset — a reset would resurrect burned codes). All operations hold one
lock; the claiming poll is atomic under it.

## HTTP API (`server.py`)

Stdlib `http.server`, threaded, mirroring `freetalon/api.py`'s shape.
Request bodies are capped at 65536 bytes (`413` above that); unparseable
JSON is `400`. Responses are JSON. The server logs method/path/status
only — never headers, never bodies, never the token.

| route                        | role     | success | errors |
|------------------------------|----------|---------|--------|
| `POST /pairing/register`     | node     | `201` new, `200` idempotent → public view | `400` invalid, `409` conflict |
| `GET /pairing/<code>`        | node     | see poll table below | — |
| `POST /pairing/<code>/confirm` | operator | `200` → public view | `400` invalid bundle, `401` unauthorized, `404` unknown, `409` already confirmed, `410` expired or claimed |
| `GET /pairing`               | operator | `200` `{"records": [public views]}` | `401` unauthorized |

Poll (`GET /pairing/<code>`, the §6 poll the node console performs):

| record state | HTTP | body |
|--------------|------|------|
| pending      | 404  | `{"code", "status": "pending"}` |
| unknown code | 404  | identical to pending — no existence oracle |
| confirmed    | 200  | `{"code", "status": "confirmed", "credentials": {...}}` — exactly once |
| claimed      | 410  | `{"code", "status": "claimed"}` |
| expired      | 410  | `{"code", "status": "expired"}` |

§6 mapping: 404 = "not confirmed yet, keep waiting", 200 = the one-time
bundle, 410 = gone (the console's own message for 410 is "expired or
already used", so expired → 410 matches the shipped client).

`401` is identical for missing header, wrong token, and no token
configured — an attacker cannot distinguish them. Its body is exactly
`{"error": "unauthorized"}` in every refusal case (including a bearer
token that cannot be compared, e.g. one carrying non-ASCII characters:
that is a refusal, never a dropped connection).

Unknown routes → `404 {"error": "not found"}`; a known path with the
wrong method → `405`. A confirm refused with `410` returns the
poll-style body `{"code", "status"}` (`"expired"` or `"claimed"`), so
a client reads the terminal state the same way it does from a poll.

## Static fallthrough (deployment option)

When a static root is configured (`static_root` on `make_server`, or
`FREETALON_PAIRING_STATIC_ROOT` for the standalone entry), `GET` and
`HEAD` requests whose path is **not** a pairing route serve files from
that directory, so one process can front both this API and the static
provisioning payloads. Pairing routes are matched first and are
unaffected. The request path is percent-decoded and resolved under the
root: a path that resolves outside the root, a missing file, and a
directory without an `index.html` all return the same
`404 {"error": "not found"}` as an unknown route, and there are no
directory listings. Other methods never fall through. With no static
root configured (the default) the fallthrough is off and every
non-route path is the plain `404` above.

## Server construction (`server.py`)

- `make_server(store, token, host="127.0.0.1", port=0, static_root=None)` → a threaded
  `http.server` instance, already bound to `(host, port)`. The caller
  runs `serve_forever()` (typically in a thread) and stops it with
  `shutdown()` + `server_close()`; `port=0` binds an ephemeral port,
  read back from `server_address`. `token` is this instance's operator
  bearer token: `token=None` (or empty) closes the operator API —
  every operator request is `401` — while the node endpoints keep
  working.
- `python -m freetalon.pairing` runs `server.main()`: the standalone
  entry. Configuration comes from the environment only (next section);
  it constructs the store at the configured path and serves via
  `make_server` until interrupted. The factory defaults above are the
  library defaults — the standalone entry's bind defaults are the
  Configuration table's.

## Configuration (env only)

Read by `PairingServerConfig.from_env`.

| var | default | meaning |
|-----|---------|---------|
| `FREETALON_PAIRING_TOKEN` | *(unset → operator API closed)* | operator bearer token |
| `FREETALON_PAIRING_STORE_PATH` | `/home/seeker/freetalon-workspace/pairing-store.json` | store file |
| `FREETALON_PAIRING_TTL_SECONDS` | `1800` | code lifetime |
| `FREETALON_PAIRING_HOST` | `0.0.0.0` | bind host |
| `FREETALON_PAIRING_PORT` | `8080` | bind port |
| `FREETALON_PAIRING_STATIC_ROOT` | *(unset → static fallthrough off)* | directory served for non-`/pairing` GET/HEAD (see "Static fallthrough") |

Production note: the node console's default base is
`http://10.40.0.1` (port 80), so in Pat's session the control plane
binds this server to `10.40.0.1:80` (privileged — root or
`CAP_NET_BIND_SERVICE`). The defaults above keep an unprivileged dev
instance off port 80; nothing in the boot chain starts this server.

## Deliberate deviation from DESIGN.md §6's letter

§6 narrates registration as Pat typing the code into the control plane.
Here the **node self-registers** (code + nonce + machine-id) and Pat's
control-plane action is the confirm. Reason: the identity binding
(nonce, machine-id) cannot be typed by a human, and approval must bind
to it; §6 leaves the server side unspecified ("a control-plane
component specified here but not part of this prep build") and its
security property — *no credentials move until a human confirms a code
they read off the physical screen* — is preserved exactly.
