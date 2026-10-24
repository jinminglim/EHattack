# Findings log — Atelier (18.217.39.191)

Authorized SIT lab. Live results as they come in, for the report.

## Recon — confirmed 2026-10-24

**Stack:** `Werkzeug/3.0.4 Python/3.11.16` (Flask dev server, no reverse proxy).
`/health` = `{"app":"up","database":"up","llm_backend":"ollama","llm_primary":"up","status":"ok"}`

**Route map:**

| Route | Code | Note |
|---|---|---|
| `/products`, `/about` | 200 | public |
| `/register`, `/login` | 200 | open self-registration; login by username |
| `/cart` | 302→/login | needs auth |
| `/support`, `/support/tickets` | 200 (authed) / 302 (anon) | chatbot + tickets |
| `/account` | 200 (authed) | profile |
| **`/admin`** | **308 → /admin/** | **admin area EXISTS — new, untested** |
| `/api/chat` | 405 | exists, POST-only |
| `/api/tickets` | 405 | exists, POST-only |
| `/api/account` | 405 | exists, PUT-only |
| `/console` | 404 | Werkzeug debugger OFF |
| `/metrics`, `/debug` | 404 | absent |

**Source leak via web root: NONE.** `.git/config`, `.env`, `app.py`, `config.py`,
`requirements.txt`, `docker-compose.yml`, `db.sqlite3` all return the standard 404
page (identical 6101B body). Chain 6 (git-dumper / direct source pull) is dead —
the leaked build artifacts live on the container FS, reachable only via code-exec
or a file-read primitive.

## Session + admin structure — confirmed 2026-10-24

**Session cookie** = `{"user_id": 15}` (pentest1), Flask-signed. NO role field in
the cookie → the server resolves role from `user_id` against the DB. Consequence:
forging `is_admin`/role flags is useless; forging **`user_id` to an admin's id**
(typically 1) is the attack. Requires cracking `SECRET_KEY` (flask-unsign).

**Admin area** (`/admin/`): 403 to a Customer, 302→/login anon. Existing children
(403 = present, forbidden): `/admin/tickets`, `/admin/products`. Others (users,
orders, dashboard, settings, logs, export, report, kb, customers, staff) = 404.
The admin product/ticket management views are the likely exec sink once reached.

## Open leads, priority order

1. **`/admin/`** — enumerate it. If it gates on a role/session flag, it's the
   target for session forgery (Chain 1). An admin file/report feature = exec sink.
2. **Session forgery (Chain 1)** — decode the Flask cookie, crack SECRET_KEY,
   forge an admin session, hit `/admin/`. Now the top standalone path (source
   leak being dead).
3. **Upload traversal → templates/ SSTI (Chain 5)** — still the RCE path; needs
   the upload endpoint characterised first.
4. **LLM injection (Phase A)** — parallel, slow.

## Session forgery status
Flask session signing reproduced and validated (tools/flask_session.py). Target
cookie decodes to `{"user_id": 15}`. SECRET_KEY **not** recovered from rockyou
(all 14M exhausted) — key is not a common password (long/random or app-specific).
Session forgery is blocked unless a targeted key list or a leaked key (via another
chain) recovers it. Pivot to key-free RCE: ticket-photo upload traversal (Chain 5)
and LLM injection (Phase A).

## Ticket photo upload — characterised (RCE vector ruled out)
- `POST /api/tickets` (JSON subject/body) → 201 `{"status":"open","ticket_ref":"CUST-NNNNN"}`.
- Photo route is `POST /api/tickets/{numeric_id}/photo`, field `file`. The id is the
  DB integer from `data-ticket-id` in /support/tickets (our tickets = 17–20), NOT
  the ref. Low sequential ids ⇒ ids 1–16 are others'/seeded → IDOR surface.
- No server-side extension filter (`.php/.py/.html/.svg/.jpeg.php` all 201).
- BUT stored under a random UUID name (`send_file` serves it back via
  `/api/tickets/{id}/photo`, `Content-Disposition filename=<32hex>.jpg`) → filename
  traversal into templates/ is defeated. Bytes returned verbatim (22==22) → no
  Pillow/image processing → no decode/EXIF/ImageMagick RCE on upload. **Upload is
  storage only; not a code-exec sink.**

## Ruled out
- Werkzeug debug console (`/console` 404)
- Mass assignment of role on `/api/account`
- Source/VCS exposure on the web root
- SECRET_KEY via rockyou (exhausted, no hit)
- Ticket upload → RCE (random UUID filename; raw passthrough, no processing)
