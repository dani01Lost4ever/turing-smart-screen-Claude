"""Diagnostic: print the REAL /usage percentages via Claude Code's OAuth credentials.

Same data source the widget's gauges use (see claude_screen.py _oauth_fetch_once).
Refreshes the token if expired and persists it back to ~/.claude/.credentials.json.
Run it when the gauges look wrong to see exactly what Anthropic is reporting.
"""
import json, time, urllib.request, urllib.error
from pathlib import Path

CRED_FILE = Path.home() / ".claude" / ".credentials.json"
TOKEN_URLS = [
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
    "https://api.anthropic.com/v1/oauth/token",
]
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code's public OAuth client id
UA = "claude-cli/2.1.170 (external, cli)"


def load_creds():
    return json.loads(CRED_FILE.read_text(encoding="utf-8"))


def save_creds(creds):
    tmp = CRED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(creds), encoding="utf-8")
    tmp.replace(CRED_FILE)


def try_refresh(creds, url):
    oauth = creds["claudeAiOauth"]
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  {url} -> HTTP {e.code}: {e.read()[:300]!r}")
        return None
    except Exception as e:
        print(f"  {url} -> {e}")
        return None


def fetch_usage(token):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


creds = load_creds()
oauth = creds["claudeAiOauth"]
token = oauth["accessToken"]
if oauth.get("expiresAt", 0) <= time.time() * 1000 + 60_000:
    print("token expired -> refreshing...")
    for url in TOKEN_URLS:
        tok = try_refresh(creds, url)
        if tok:
            oauth["accessToken"] = tok["access_token"]
            if tok.get("refresh_token"):
                oauth["refreshToken"] = tok["refresh_token"]
            oauth["expiresAt"] = int(time.time() * 1000) + int(tok.get("expires_in", 28800)) * 1000
            save_creds(creds)
            print(f"refreshed via {url}, expires_in={tok.get('expires_in')}s, "
                  f"rotated={bool(tok.get('refresh_token'))}")
            token = tok["access_token"]
            break
    else:
        raise SystemExit("all refresh endpoints failed")
print(json.dumps(fetch_usage(token), indent=2))
