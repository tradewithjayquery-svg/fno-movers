#!/usr/bin/env python3
"""
FNO MOVERS
Watchlist (FnOin.csv) ke top 5 gainers aur top 5 losers + Nifty ka update
Telegram par bhejta hai: 09:20, 09:25, ... 09:50 (kul 7 alerts).

Sirf Python standard library use hoti hai (koi pip install nahi).

Environment variables (GitHub Secrets se aate hain):
  UPSTOX_TOKEN        Upstox analytics / access token
  TELEGRAM_BOT_TOKEN  Telegram bot token
  TELEGRAM_CHAT_ID    Channel ka chat id (-100xxxxxxxxxx) ya @channelusername
  MODE                0 = live (slots ke hisaab se), 1 = test (abhi ek alert)
"""

import csv
import gzip
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ----------------------------- SETTINGS -----------------------------
CSV_FILE = os.environ.get("WATCHLIST_CSV", "FnOin.csv")
TOP_N = 5
SLOTS = ["09:20", "09:25", "09:30", "09:35", "09:40", "09:45", "09:50"]
LATE_GRACE_MIN = 2          # slot ke itne minute baad tak bhi bhej denge
BATCH_SIZE = 100            # ek API call mein kitne instruments
NIFTY_KEY = "NSE_INDEX|Nifty 50"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
# --------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

UPSTOX_TOKEN = os.environ.get("UPSTOX_TOKEN", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
MODE = os.environ.get("MODE", "0").strip()


def now():
    return datetime.now(IST)


def log(msg):
    print(f"[{now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http(url, headers=None, data=None, tries=3, timeout=30):
    last = None
    for i in range(tries):
        try:
            hdrs = {"User-Agent": UA}
            hdrs.update(headers or {})
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            last = f"HTTP {e.code}: {body}"
            if e.code in (400, 401, 403):
                break
        except Exception as e:  # network error
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(last)


# ----------------------------- WATCHLIST -----------------------------
def load_symbols(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header = [h.strip().upper() for h in rows[0]]
    col = header.index("SYMBOL") if "SYMBOL" in header else 0
    start = 1 if "SYMBOL" in header else 0
    seen, out = set(), []
    for r in rows[start:]:
        if len(r) <= col:
            continue
        s = r[col].strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def resolve_keys(symbols):
    """Symbol -> Upstox instrument_key (NSE equity)."""
    log("Upstox instrument list download ho rahi hai...")
    raw = http(MASTER_URL, timeout=90)
    items = json.loads(gzip.decompress(raw))
    table = {}
    for it in items:
        if it.get("segment") == "NSE_EQ" and it.get("instrument_type") == "EQ":
            table[str(it.get("trading_symbol", "")).upper()] = it["instrument_key"]
    key_to_sym, missing = {}, []
    for s in symbols:
        k = table.get(s)
        if k:
            key_to_sym[k] = s
        else:
            missing.append(s)
    return key_to_sym, missing


# ----------------------------- QUOTES -----------------------------
def fetch_quotes(keys):
    """Returns {instrument_key: (ltp, prev_close, change, pct)}."""
    headers = {
        "Authorization": f"Bearer {UPSTOX_TOKEN}",
        "Accept": "application/json",
    }
    result = {}
    for i in range(0, len(keys), BATCH_SIZE):
        batch = keys[i:i + BATCH_SIZE]
        qs = "instrument_key=" + urllib.parse.quote(",".join(batch), safe=",")
        raw = http(f"{QUOTE_URL}?{qs}", headers=headers)
        data = json.loads(raw).get("data", {}) or {}
        for v in data.values():
            tok = v.get("instrument_token")
            ltp = v.get("last_price")
            if not tok or not ltp:
                continue
            nc = v.get("net_change")
            if nc is not None:
                prev = ltp - nc
            else:
                prev = (v.get("ohlc") or {}).get("close")
            if not prev or prev <= 0:
                continue
            chg = ltp - prev
            result[tok] = (ltp, prev, chg, chg / prev * 100.0)
    return result


# ----------------------------- MESSAGE -----------------------------
def rank(quotes, key_to_sym):
    rows = []
    for k, sym in key_to_sym.items():
        q = quotes.get(k)
        if q:
            rows.append((sym, q[0], q[3]))
    rows.sort(key=lambda r: r[2], reverse=True)
    gainers = rows[:TOP_N]
    losers = list(reversed(rows[-TOP_N:])) if len(rows) >= TOP_N else []
    return gainers, losers, len(rows)


def fmt_rows(rows):
    lines = []
    for i, (sym, ltp, pct) in enumerate(rows, 1):
        lines.append(f"{i}. {sym:<12}{ltp:>10,.2f}  {pct:+6.2f}%")
    return html.escape("\n".join(lines))


def build_message(label, nifty, gainers, losers, counted, total, missing_note=""):
    if nifty:
        n_ltp, _, n_chg, n_pct = nifty
        arrow = "🟢" if n_chg >= 0 else "🔴"
        nifty_line = f"{arrow} <b>NIFTY 50</b>  {n_ltp:,.2f}  {n_pct:+.2f}%  ({n_chg:+.2f})"
    else:
        nifty_line = "NIFTY 50: data nahi mila"
    msg = (
        f"📊 <b>FNO MOVERS</b>  |  {label} IST\n"
        f"{nifty_line}\n\n"
        f"🟢 <b>Top {TOP_N} Gainers</b>\n<pre>{fmt_rows(gainers)}</pre>\n"
        f"🔴 <b>Top {TOP_N} Losers</b>\n<pre>{fmt_rows(losers)}</pre>\n"
        f"<i>{counted}/{total} stocks ka data</i>"
    )
    if missing_note:
        msg += "\n" + missing_note
    return msg


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    http(url, data=payload)


# ----------------------------- RUN -----------------------------
def run_once(label, key_to_sym, missing_note=""):
    keys = list(key_to_sym.keys())
    quotes = fetch_quotes([NIFTY_KEY] + keys)
    gainers, losers, counted = rank(quotes, key_to_sym)
    if not gainers:
        raise RuntimeError("Koi quote nahi mila (token expire ya market band?)")
    msg = build_message(label, quotes.get(NIFTY_KEY), gainers, losers,
                        counted, len(keys), missing_note)
    send_telegram(msg)
    log(f"Alert bheja: {label} ({counted}/{len(keys)} stocks)")


def slot_dt(hhmm):
    h, m = map(int, hhmm.split(":"))
    return now().replace(hour=h, minute=m, second=0, microsecond=0)


def sleep_until(t):
    while True:
        left = (t - now()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 20))


def main():
    for name, val in (("UPSTOX_TOKEN", UPSTOX_TOKEN),
                      ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
                      ("TELEGRAM_CHAT_ID", CHAT_ID)):
        if not val:
            log(f"ERROR: secret {name} set nahi hai")
            sys.exit(1)

    symbols = load_symbols(CSV_FILE)
    log(f"Watchlist: {len(symbols)} symbols")
    key_to_sym, missing = resolve_keys(symbols)
    log(f"Matched: {len(key_to_sym)}, not found: {len(missing)} {missing}")
    if not key_to_sym:
        log("ERROR: koi symbol match nahi hua")
        sys.exit(1)

    missing_note = ""
    if missing:
        shown = ", ".join(missing[:15]) + (" ..." if len(missing) > 15 else "")
        missing_note = f"⚠️ Nahi mile ({len(missing)}): {html.escape(shown)}"

    if MODE == "1":
        log("TEST mode: abhi ek alert bhej raha hu")
        run_once(now().strftime("%H:%M") + " (TEST)", key_to_sym, missing_note)
        return

    sent = 0
    for s in SLOTS:
        t = slot_dt(s)
        if now() > t + timedelta(minutes=LATE_GRACE_MIN):
            log(f"Slot {s} nikal chuka, skip")
            continue
        sleep_until(t)
        try:
            run_once(s, key_to_sym, missing_note if sent == 0 else "")
            sent += 1
        except Exception as e:
            log(f"ERROR slot {s}: {e}")
            try:
                send_telegram(f"⚠️ FNO MOVERS {s}: update fail hua\n{html.escape(str(e)[:200])}")
            except Exception as e2:
                log(f"Error alert bhi fail: {e2}")

    if sent == 0:
        log("Koi alert nahi gaya (late start ya errors)")
        try:
            send_telegram("⚠️ FNO MOVERS: koi update nahi ja saka. Workflow 9:20 se pehle start karo.")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
