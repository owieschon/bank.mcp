#!/usr/bin/env python3
"""plaid_link.py — mint a Plaid access_token for a NEW bank Item via Plaid Link.

bank-mcp's `init` (production) asks you to PASTE an access_token but does NOT run
Plaid Link itself. This local helper runs Link: it reads your Plaid app credentials
from ~/.bank-mcp/config.json (the secret is never printed), opens a local Plaid Link
page, the person logs into THEIR bank, and it prints the access_token to paste into
`npx @bank-mcp/server init`.

Banks that use OAuth redirect to the bank and back, so ONE dashboard step is
required first (see below).

PREREQUISITE (one time):
  In the Plaid dashboard -> Team Settings -> API -> "Allowed redirect URIs", add:
      http://localhost:8765/oauth
  (https://dashboard.plaid.com/developers/api)

USAGE:
  python3 plaid_link.py
  -> a browser opens; log into the target bank account; approve
  -> the access_token is printed here. Copy it.
  -> run `npx @bank-mcp/server init` -> Production -> reuse keys -> paste the token
     -> label the connection for the account holder.
"""

import json
import os
import sys
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from finance_mcp.ingest import safehttp

CFG = os.path.expanduser("~/.bank-mcp/config.json")
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/oauth"
ENVS = {"sandbox": "https://sandbox.plaid.com",
        "development": "https://development.plaid.com",
        "production": "https://production.plaid.com"}


def _creds():
    try:
        d = json.load(open(CFG))
    except Exception as e:
        sys.exit(f"Can't read {CFG}: {e}")
    for c in d.get("connections", []):
        cf = c.get("config", {})
        if cf.get("clientId") and cf.get("secret"):
            return cf["clientId"], cf["secret"], cf.get("environment", "production")
    sys.exit("No Plaid client_id/secret found in ~/.bank-mcp/config.json")


def plaid(base, client, secret, path, body):
    # Routed through safehttp (certifi-verified TLS, host-pinned) — this is a
    # credential exchange, so verified transport matters.
    data = json.dumps({**body, "client_id": client, "secret": secret}).encode()
    with safehttp.fetch(base + path, data=data, method="POST",
                        headers={"Content-Type": "application/json"}, timeout=30,
                        allowed_hosts={"production.plaid.com", "sandbox.plaid.com",
                                       "development.plaid.com"}) as r:
        return json.load(r)


def _page(link_token, received_redirect=False):
    extra = "receivedRedirectUri: window.location.href," if received_redirect else ""
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Connect bank</title></head><body style="font-family:sans-serif;padding:40px">
<h2>Plaid Link</h2><p>A popup will open — log into the bank and approve.</p>
<pre id=out style="white-space:pre-wrap;background:#f4f4f4;padding:16px;border-radius:8px"></pre>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
var h = Plaid.create({{
  token: "{link_token}",
  {extra}
  onSuccess: function(pt){{
    fetch('/exchange', {{method:'POST', body: pt}})
      .then(function(r){{return r.text();}})
      .then(function(t){{document.getElementById('out').textContent = t;}});
  }},
  onExit: function(err){{
    if (err) document.getElementById('out').textContent = 'Exited: ' + JSON.stringify(err);
  }}
}});
h.open();
</script></body></html>"""


def make_handler(base, client, secret, link_token):
    class H(BaseHTTPRequestHandler):
        def _html(self, body):
            self.send_response(200); self.send_header("Content-Type", "text/html")
            self.end_headers(); self.wfile.write(body.encode())

        def do_GET(self):
            # "/" starts Link; "/oauth" resumes after the bank's OAuth redirect.
            self._html(_page(link_token, received_redirect=self.path.startswith("/oauth")))

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            pt = self.rfile.read(n).decode()
            try:
                at = plaid(base, client, secret,
                           "/item/public_token/exchange", {"public_token": pt})["access_token"]
                msg = "✅ ACCESS TOKEN (copy this into `npx @bank-mcp/server init`):\n\n" + at
                print("\n" + "=" * 60 + "\n" + msg + "\n" + "=" * 60 + "\n")
            except Exception as e:
                msg = "exchange failed: " + str(e)
                print(msg)
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.end_headers(); self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass

    return H


def main():
    client, secret, env = _creds()
    base = ENVS.get(env, ENVS["production"])

    # Create the link_token (transactions, US, OAuth-capable via redirect_uri).
    try:
        link_token = plaid(base, client, secret, "/link/token/create", {
            "user": {"client_user_id": "finance-mcp-user"},
            "client_name": "finance.mcp",
            "products": ["transactions"],
            "country_codes": ["US"],
            "language": "en",
            "redirect_uri": REDIRECT_URI,
        })["link_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"link/token/create failed: {e.read().decode()[:300]}\n"
                 f"(Did you add {REDIRECT_URI} to Allowed redirect URIs in the Plaid dashboard?)")

    print(f"Plaid Link helper ready (env={env}). Opening http://localhost:{PORT} ...")
    print("Log into the target bank account in the popup. The access token prints here when done.")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        HTTPServer(("127.0.0.1", PORT), make_handler(base, client, secret, link_token)).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
