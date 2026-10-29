"""Pull Power Bottom Fantasies data from the Yahoo Fantasy API and write the dashboard's JSON files.

Runs in GitHub Actions. Needs env: YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REDIRECT_URI.
Usage:
  python scripts/yahoo_update.py connect <authorization code>   # first-time link; stores the encrypted refresh token
  python scripts/yahoo_update.py update                         # refresh the data files
"""
import base64
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

LEAGUE_ID = "1282888"
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TOKEN_FILE = ROOT / ".yahoo" / "token.enc"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2/"

CLIENT_ID = os.environ.get("YAHOO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("YAHOO_CLIENT_SECRET", "")
REDIRECT_FILE = ROOT / ".yahoo" / "redirect_uri"
REDIRECT_URI = os.environ.get("YAHOO_REDIRECT_URI") or (REDIRECT_FILE.read_text().strip() if REDIRECT_FILE.exists() else "") or "oob"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status(ok, message):
    DATA.mkdir(exist_ok=True)
    (DATA / "run_status.json").write_text(json.dumps({"at": now_iso(), "ok": ok, "message": message}, indent=1))


# ---------- token storage (encrypted with the client secret, so it is safe in a public repo) ----------
def save_refresh_token(token):
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    out = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-a", "-A", "-pass", "env:YAHOO_CLIENT_SECRET"],
        input=token.encode(), capture_output=True, check=True,
    )
    TOKEN_FILE.write_bytes(out.stdout + b"\n")


def load_refresh_token():
    out = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-a", "-A", "-pass", "env:YAHOO_CLIENT_SECRET"],
        input=TOKEN_FILE.read_bytes().strip(), capture_output=True, check=True,
    )
    return out.stdout.decode().strip()


def token_request(fields):
    body = urllib.parse.urlencode({"redirect_uri": REDIRECT_URI, **fields}).encode()
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Yahoo sign-in refused ({e.code}): {detail}")


# ---------- Yahoo API ----------
def strip_ns(el):
    for e in el.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return el


def api(token, path):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return strip_ns(ET.fromstring(r.read()))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        detail = body
        try:
            root = strip_ns(ET.fromstring(body))
            detail = " ".join(x.text.strip() for x in root.iter() if x.text and x.text.strip()) or body
        except ET.ParseError:
            pass
        www = e.headers.get("WWW-Authenticate", "")
        raise RuntimeError(f"Yahoo API {path} failed ({e.code}): {detail[:400]}" + (f" [{www[:200]}]" if www else ""))


def txt(el, path, default=""):
    f = el.find(path)
    return f.text.strip() if f is not None and f.text else default


def num(el, path):
    try:
        return round(float(txt(el, path, "0")), 2)
    except ValueError:
        return 0.0


def first_name(nick):
    w = (nick or "").strip().split()
    if not w:
        return ""
    return w[0][:1].upper() + w[0][1:]


def pull(token):
    game_key = txt(api(token, "game/nfl"), "game/game_key")
    key = f"{game_key}.l.{LEAGUE_ID}"
    settings = api(token, f"league/{key}/settings")
    playoff_start = int(txt(settings, "league/settings/playoff_start_week", "0") or 0)
    end_week = int(txt(settings, "league/end_week", "17") or 17)
    reg_end = playoff_start - 1 if playoff_start else end_week

    st = api(token, f"league/{key}/standings")
    managers, standings = {}, []
    for t in st.findall("league/standings/teams/team"):
        name = txt(t, "name")
        managers[name] = first_name(txt(t, "managers/manager/nickname"))
        s_type, s_val = txt(t, "team_standings/streak/type"), txt(t, "team_standings/streak/value")
        standings.append({
            "rank": int(txt(t, "team_standings/rank", "0") or 0),
            "team": name,
            "manager": managers[name],
            "w": int(txt(t, "team_standings/outcome_totals/wins", "0") or 0),
            "l": int(txt(t, "team_standings/outcome_totals/losses", "0") or 0),
            "pf": num(t, "team_standings/points_for"),
            "pa": num(t, "team_standings/points_against"),
            "streak": (("W" if s_type == "win" else "L" if s_type == "loss" else "T") + "-" + s_val) if s_val else "",
            "moves": int(txt(t, "number_of_moves", "0") or 0),
        })
    standings.sort(key=lambda r: r["rank"] or 99)

    games, projections, live, schedule = [], [], [], []
    live_week, live_status, proj_weeks = None, "", 0
    for w in range(1, reg_end + 1):
        sb = api(token, f"league/{key}/scoreboard;week={w}")
        matchups = sb.findall("league/scoreboard/matchups/matchup")
        if not matchups:
            break
        final = all(txt(m, "status") == "postevent" for m in matchups)
        for m in matchups:
            ts = m.findall("teams/team")
            if len(ts) != 2:
                continue
            side = [{"team": txt(t, "name"), "pts": num(t, "team_points/total"), "proj": num(t, "team_projected_points/total")} for t in ts]
            for a, b in ((side[0], side[1]), (side[1], side[0])):
                schedule.append({"week": w, "team": a["team"], "opp": b["team"]})
                if final:
                    games.append({"week": w, "team": a["team"], "manager": managers.get(a["team"], ""), "opp": b["team"],
                                  "pts": a["pts"], "opp_pts": b["pts"], "proj": a["proj"],
                                  "result": "W" if a["pts"] > b["pts"] else "L"})
                elif proj_weeks < 3:
                    projections.append({"week": w, "team": a["team"], "opp": b["team"], "proj": a["proj"], "opp_proj": b["proj"]})
                    if live_week is None or live_week == w:
                        live.append({"week": w, "team": a["team"], "pts": a["pts"]})
        if not final:
            if live_week is None:
                live_week = w
                live_status = ", ".join(sorted({txt(m, "status") for m in matchups}))
            proj_weeks += 1

    return {"standings": standings, "games": games, "projections": projections, "live": live, "schedule": schedule,
            "current_week": live_week, "live_status": live_status}


def check(d):
    n = len(d["standings"])
    if n < 4:
        raise RuntimeError(f"only {n} teams in standings")
    if len(d["games"]) % n:
        raise RuntimeError("games are not a whole number of weeks")
    if d["live"] and len(d["live"]) != n:
        raise RuntimeError(f"live week has {len(d['live'])} rows, expected {n}")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        status(False, "YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET secrets are missing")
        sys.exit(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "update"
    try:
        if cmd == "connect":
            code = sys.argv[2].strip()
            tok = token_request({"grant_type": "authorization_code", "code": code})
            save_refresh_token(tok["refresh_token"])
            REDIRECT_FILE.write_text(REDIRECT_URI + "\n")
            access = tok["access_token"]
        else:
            if not TOKEN_FILE.exists():
                raise RuntimeError("Yahoo is not connected yet: run the 'Connect Yahoo' workflow first")
            refresh = load_refresh_token()
            tok = token_request({"grant_type": "refresh_token", "refresh_token": refresh})
            if tok.get("refresh_token") and tok["refresh_token"] != refresh:
                save_refresh_token(tok["refresh_token"])
            access = tok["access_token"]
        d = pull(access)
        check(d)
        for name in ("standings", "games", "projections", "live", "schedule"):
            (DATA / f"{name}.json").write_text(json.dumps(d[name], ensure_ascii=False))
        (DATA / "meta.json").write_text(json.dumps({"updated": now_iso()}))
        lead = d["standings"][0]["team"] if d["standings"] else "?"
        status(True, f"week {d['current_week']} ({d['live_status']}); {len(d['games']) // max(1, len(d['standings']))} weeks final; leader {lead}")
        print("ok")
    except Exception as e:  # report without leaking secrets
        msg = str(e).replace(CLIENT_SECRET, "***").replace(CLIENT_ID, "***")
        status(False, msg)
        print("failed:", msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
