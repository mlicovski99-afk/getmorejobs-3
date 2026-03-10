#!/usr/bin/env python3
"""
GetMoreJobs.au — Fully Automated Tradie Lead Gen + SMS Outreach
================================================================
Scrapes Google Maps → scores leads → sends personalised SMS via Twilio
→ tracks replies → auto follow-up → logs everything

DEPLOY ON: railway.app (free) or render.com (free)

REQUIRED ENV VARS (set in Railway/Render dashboard):
  TWILIO_ACCOUNT_SID   = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  TWILIO_AUTH_TOKEN    = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  TWILIO_FROM_NUMBER   = "+61400000000"
  OUTSCRAPER_API_KEY   = "your-outscraper-key"
  WEBHOOK_BASE_URL     = "https://your-app.railway.app"  (your deployed URL)

INSTALL: pip install -r requirements.txt
RUN:     python outreach.py
"""

import os, json, time, logging, sqlite3, hashlib
from datetime import datetime, timedelta
from pathlib import Path

import requests
import schedule
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from flask import Flask, request, Response

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("getmorejobs.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("GMJ")

# ── CONFIG ────────────────────────────────────────────────────────────────────
class Config:
    TWILIO_SID      = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM     = os.environ.get("TWILIO_FROM_NUMBER", "")
    OUTSCRAPER_KEY  = os.environ.get("OUTSCRAPER_API_KEY", "")
    WEBHOOK_URL     = os.environ.get("WEBHOOK_BASE_URL", "http://localhost:5000")
    PORT            = int(os.environ.get("PORT", 5000))

    # Outreach rules
    MIN_SCORE       = 6          # Only SMS leads scoring >= this
    DAILY_SMS_CAP   = 50         # Max SMS per day (stay under Twilio limits)
    FOLLOWUP_DAYS   = [3, 7]     # Days after initial SMS to follow up
    BIZ_HOURS_START = 8          # AEST
    BIZ_HOURS_END   = 17         # AEST
    BIZ_DAYS        = [0,1,2,3,4] # Mon–Fri

    # Scrape targets — add/remove suburbs freely
    SEARCH_QUERIES = [
        # Sydney
        "plumber Parramatta NSW",
        "plumber Blacktown NSW",
        "plumber Campbelltown NSW",
        "plumber Penrith NSW",
        "electrician Parramatta NSW",
        "electrician Liverpool NSW",
        "electrician Castle Hill NSW",
        "roofer Blacktown NSW",
        "roofer Penrith NSW",
        "painter Parramatta NSW",
        "builder Campbelltown NSW",
        "builder Bankstown NSW",
        # Melbourne
        "plumber Dandenong VIC",
        "plumber Frankston VIC",
        "plumber Werribee VIC",
        "electrician Dandenong VIC",
        "electrician Ringwood VIC",
        "roofer Frankston VIC",
        "painter Footscray VIC",
        "builder Cranbourne VIC",
        "builder Pakenham VIC",
        # Perth
        "plumber Joondalup WA",
        "plumber Rockingham WA",
        "electrician Joondalup WA",
        "electrician Armadale WA",
        "roofer Mandurah WA",
        "painter Wanneroo WA",
        "builder Baldivis WA",
        # Adelaide
        "plumber Salisbury SA",
        "electrician Morphett Vale SA",
        "roofer Port Adelaide SA",
        "builder Tea Tree Gully SA",
    ]

C = Config()

# ── DATABASE ──────────────────────────────────────────────────────────────────
DB_PATH = Path("leads.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id          TEXT PRIMARY KEY,
            name        TEXT,
            trade       TEXT,
            suburb      TEXT,
            city        TEXT,
            phone       TEXT UNIQUE,
            email       TEXT,
            website     TEXT,
            rating      REAL,
            reviews     INTEGER,
            score       INTEGER,
            status      TEXT DEFAULT 'new',
            created_at  TEXT,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     TEXT,
            direction   TEXT,    -- 'out' or 'in'
            body        TEXT,
            seq_step    INTEGER,
            sent_at     TEXT,
            twilio_sid  TEXT,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS optouts (
            phone       TEXT PRIMARY KEY,
            opted_out_at TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_counts (
            date        TEXT PRIMARY KEY,
            sent        INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    log.info("Database ready")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def lead_id(phone):
    return hashlib.md5(phone.encode()).hexdigest()[:12]

def is_opted_out(phone):
    with get_db() as db:
        row = db.execute("SELECT 1 FROM optouts WHERE phone=?", (phone,)).fetchone()
        return row is not None

def record_optout(phone):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO optouts(phone, opted_out_at) VALUES(?,?)",
            (phone, datetime.now().isoformat())
        )
        db.execute("UPDATE leads SET status='opted_out' WHERE phone=?", (phone,))
        db.commit()
    log.info(f"Opt-out recorded: {phone}")

def daily_sent_count():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as db:
        row = db.execute("SELECT sent FROM daily_counts WHERE date=?", (today,)).fetchone()
        return row["sent"] if row else 0

def increment_daily_count():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as db:
        db.execute("""
            INSERT INTO daily_counts(date, sent) VALUES(?, 1)
            ON CONFLICT(date) DO UPDATE SET sent = sent + 1
        """, (today,))
        db.commit()

# ── SCORING ───────────────────────────────────────────────────────────────────
def score_lead(data: dict) -> int:
    """
    Max score = 8. Higher = worse online presence = hotter lead.
      No website   → +3
      Rating < 3.5 → +3  |  Rating < 4.0 → +2
      Reviews < 10 → +2  |  Reviews < 20 → +1
    """
    score = 0
    website = str(data.get("site") or "").strip()
    if not website or website.lower() in ("none", "n/a", ""):
        score += 3

    try:
        rating = float(data.get("rating") or 5.0)
    except:
        rating = 5.0

    if rating < 3.5:
        score += 3
    elif rating < 4.0:
        score += 2

    try:
        reviews = int(data.get("reviews") or 99)
    except:
        reviews = 99

    if reviews < 10:
        score += 2
    elif reviews < 20:
        score += 1

    return score

# ── SMS TEMPLATES ─────────────────────────────────────────────────────────────
TEMPLATES = {
    "Plumber": {
        1: "Hey, noticed there's no website coming up when people search plumbers in {suburb}. We set up landing pages + Google profiles for tradies — most get their first inquiry within 2 weeks. Free 20 min chat? Reply STOP to opt out — GetMoreJobs.au",
        2: "Hey, following up from last week. Helped a plumber in {suburb} go from 3 inquiries/week to 14 last month. Happy to show you what we'd do — 15 mins? Reply STOP to opt out — GetMoreJobs.au",
        3: "Last one from me. If you ever want more plumbing jobs without the hipages fees, give us a shout. Reply STOP to unsubscribe — GetMoreJobs.au",
    },
    "Electrician": {
        1: "Hey, searched electricians in {suburb} and you weren't showing up. We sort the full setup — landing page + Google profile — for sparkies. Most start getting calls within 2 weeks. Free look? Reply STOP to opt out — GetMoreJobs.au",
        2: "Hey, following up. Helped a sparky in {suburb} ditch hipages and start getting direct leads. Happy to show you what we built — 15 mins? Reply STOP to opt out — GetMoreJobs.au",
        3: "Last one from me. If you want more electrical work coming in directly, we can sort that. Reply STOP to unsubscribe — GetMoreJobs.au",
    },
    "Roofer": {
        1: "Hey, we help roofers get found on Google without paying for leads. Landing page + Google Business fully set up — most clients see their first call within 2 weeks. Free chat? Reply STOP to opt out — GetMoreJobs.au",
        2: "Hey, following up from last week. Helped a roofer in {suburb} go from no online presence to booked out 6 weeks. Quick 15 min call to show you? Reply STOP to opt out — GetMoreJobs.au",
        3: "Last one from me. If you want more roofing jobs without the commission fees, we're here. Reply STOP to unsubscribe — GetMoreJobs.au",
    },
    "Painter": {
        1: "Hey, searched painters in {suburb} and you didn't come up on Google. We set up landing pages + Google profiles for painters — first leads usually within 2 weeks. Free 20 min look? Reply STOP to opt out — GetMoreJobs.au",
        2: "Hey, following up. Helped a painter in {suburb} stop paying $50/lead on hipages and start getting direct bookings. Worth 15 mins? Reply STOP to opt out — GetMoreJobs.au",
        3: "Last one from me. More painting work without the platform fees — that's what we do. Reply STOP to unsubscribe — GetMoreJobs.au",
    },
    "Builder": {
        1: "Hey, noticed {name} doesn't have a website coming up in {suburb}. We help builders get found online — landing page, Google, social sorted. One renovation client pays for a year. Free 20 min chat? Reply STOP to opt out — GetMoreJobs.au",
        2: "Hey, following up. Builder in {suburb} we worked with went from all-referral to 4 new project inquiries in a month. Happy to show you what we did — 15 mins? Reply STOP to opt out — GetMoreJobs.au",
        3: "Last one from me. If you want your building business showing up when clients search in {suburb}, we can make that happen. Reply STOP to unsubscribe — GetMoreJobs.au",
    },
}

def get_message(trade: str, step: int, lead: dict) -> str:
    trade_key = trade.capitalize()
    if trade_key not in TEMPLATES:
        trade_key = "Plumber"
    template = TEMPLATES[trade_key].get(step, TEMPLATES[trade_key][1])
    return template.format(
        name=lead.get("name", ""),
        suburb=lead.get("suburb", "your area"),
        trade=trade.lower(),
    )

# ── SCRAPER ───────────────────────────────────────────────────────────────────
def scrape_google_maps(query: str) -> list:
    """Call Outscraper API for a single query. Returns list of raw business dicts."""
    url = "https://api.app.outscraper.com/maps/search-v3"
    params = {
        "query": query,
        "limit": 40,
        "async": False,
        "apiKey": C.OUTSCRAPER_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        # Outscraper returns nested list
        results = []
        for item in data.get("data", []):
            if isinstance(item, list):
                results.extend(item)
            elif isinstance(item, dict):
                results.append(item)
        return results
    except Exception as e:
        log.error(f"Outscraper error for '{query}': {e}")
        return []

def extract_suburb_city(query: str):
    """Parse 'plumber Parramatta NSW' → suburb='Parramatta', city='Sydney'"""
    STATE_TO_CITY = {
        "NSW": "Sydney", "VIC": "Melbourne",
        "WA": "Perth", "SA": "Adelaide",
        "QLD": "Brisbane", "TAS": "Hobart",
    }
    parts = query.split()
    state = parts[-1].upper() if parts else "NSW"
    suburb = parts[-2] if len(parts) >= 3 else "Unknown"
    city = STATE_TO_CITY.get(state, state)
    return suburb, city

def run_scrape():
    """Scrape all configured queries and upsert new leads into DB."""
    log.info(f"Starting scrape — {len(C.SEARCH_QUERIES)} queries")
    new_count = 0

    for query in C.SEARCH_QUERIES:
        log.info(f"  Scraping: {query}")
        suburb, city = extract_suburb_city(query)
        trade = query.split()[0].capitalize()
        results = scrape_google_maps(query)

        for biz in results:
            phone = str(biz.get("phone") or biz.get("phone_1") or "").strip()
            if not phone or len(phone) < 8:
                continue

            # Normalise AU mobile numbers
            phone = phone.replace(" ", "").replace("-", "")
            if phone.startswith("0"):
                phone = "+61" + phone[1:]
            if not phone.startswith("+61"):
                continue

            lid = lead_id(phone)
            sc = score_lead(biz)

            with get_db() as db:
                existing = db.execute(
                    "SELECT id FROM leads WHERE id=?", (lid,)
                ).fetchone()

                if not existing:
                    db.execute("""
                        INSERT INTO leads
                          (id,name,trade,suburb,city,phone,email,website,
                           rating,reviews,score,status,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        lid,
                        biz.get("name", "Unknown"),
                        trade, suburb, city, phone,
                        biz.get("email_1", ""),
                        biz.get("site", ""),
                        biz.get("rating", 0),
                        biz.get("reviews", 0),
                        sc, "new",
                        datetime.now().isoformat(),
                        datetime.now().isoformat(),
                    ))
                    db.commit()
                    new_count += 1
                    log.info(f"    New lead: {biz.get('name')} ({phone}) score={sc}")

        time.sleep(1.5)  # Rate limit between queries

    log.info(f"Scrape complete — {new_count} new leads added")
    return new_count

# ── TWILIO SMS ─────────────────────────────────────────────────────────────────
twilio_client = None

def get_twilio():
    global twilio_client
    if not twilio_client and C.TWILIO_SID and C.TWILIO_TOKEN:
        twilio_client = Client(C.TWILIO_SID, C.TWILIO_TOKEN)
    return twilio_client

def is_business_hours() -> bool:
    now = datetime.now()
    return (
        now.weekday() in C.BIZ_DAYS and
        C.BIZ_HOURS_START <= now.hour < C.BIZ_HOURS_END
    )

def send_sms(lead: dict, step: int) -> bool:
    """Send an SMS to a lead. Returns True if sent."""
    phone = lead["phone"]

    # Safety checks
    if is_opted_out(phone):
        log.info(f"Skipping opted-out: {phone}")
        return False

    if not is_business_hours():
        log.info(f"Outside business hours, skipping {phone}")
        return False

    if daily_sent_count() >= C.DAILY_SMS_CAP:
        log.warning(f"Daily cap ({C.DAILY_SMS_CAP}) reached, stopping")
        return False

    tc = get_twilio()
    if not tc:
        log.error("Twilio not configured — set env vars")
        return False

    body = get_message(lead["trade"], step, dict(lead))

    try:
        msg = tc.messages.create(
            body=body,
            from_=C.TWILIO_FROM,
            to=phone,
            # Point Twilio webhook to our Flask handler
            status_callback=f"{C.WEBHOOK_URL}/sms/status",
        )
        # Record in DB
        with get_db() as db:
            db.execute("""
                INSERT INTO messages(lead_id,direction,body,seq_step,sent_at,twilio_sid)
                VALUES(?,?,?,?,?,?)
            """, (lead["id"], "out", body, step, datetime.now().isoformat(), msg.sid))
            db.execute(
                "UPDATE leads SET status=?, updated_at=? WHERE id=?",
                ("sms_sent", datetime.now().isoformat(), lead["id"])
            )
            db.commit()

        increment_daily_count()
        log.info(f"SMS sent → {lead['name']} ({phone}) step={step} SID={msg.sid}")
        return True

    except Exception as e:
        log.error(f"SMS failed to {phone}: {e}")
        return False

# ── OUTREACH JOBS ─────────────────────────────────────────────────────────────
def job_send_initial():
    """Send SMS step 1 to all new leads with score >= MIN_SCORE."""
    if not is_business_hours():
        log.info("Outside business hours, skipping initial send")
        return

    log.info("Running initial outreach job...")
    with get_db() as db:
        leads = db.execute("""
            SELECT * FROM leads
            WHERE status = 'new' AND score >= ?
            ORDER BY score DESC, created_at ASC
        """, (C.MIN_SCORE,)).fetchall()

    sent = 0
    for lead in leads:
        if daily_sent_count() >= C.DAILY_SMS_CAP:
            break
        if send_sms(lead, step=1):
            sent += 1
        time.sleep(3)  # Space out messages

    log.info(f"Initial outreach: {sent} sent")

def job_send_followups():
    """Send follow-up SMS to leads who haven't replied."""
    if not is_business_hours():
        return

    log.info("Running follow-up job...")
    now = datetime.now()
    sent = 0

    for step, days_after in enumerate(C.FOLLOWUP_DAYS, start=2):
        cutoff = (now - timedelta(days=days_after)).isoformat()
        cutoff_upper = (now - timedelta(days=days_after-1)).isoformat()

        with get_db() as db:
            # Leads that got step (step-1) sent ~days_after days ago and haven't replied
            leads = db.execute("""
                SELECT l.* FROM leads l
                WHERE l.status = 'sms_sent'
                AND l.updated_at BETWEEN ? AND ?
                AND NOT EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.lead_id = l.id AND m.direction = 'in'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.lead_id = l.id AND m.seq_step = ?
                )
            """, (cutoff, cutoff_upper, step)).fetchall()

        for lead in leads:
            if daily_sent_count() >= C.DAILY_SMS_CAP:
                break
            if send_sms(lead, step=step):
                sent += 1
            time.sleep(3)

    log.info(f"Follow-up job: {sent} sent")

def job_scrape_and_send():
    """Weekly job: scrape new leads then immediately send to qualifying ones."""
    log.info("=== Weekly scrape + send job starting ===")
    new_count = run_scrape()
    log.info(f"Scrape done: {new_count} new leads")
    time.sleep(5)
    job_send_initial()
    log.info("=== Weekly job complete ===")

# ── FLASK WEBHOOK (receives Twilio replies) ────────────────────────────────────
app = Flask(__name__)

@app.route("/sms/receive", methods=["POST"])
def receive_sms():
    """Twilio calls this when someone replies to your SMS."""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    log.info(f"REPLY from {from_number}: {body}")

    lid = lead_id(from_number)

    # Check for opt-out keywords
    OPT_OUT_KEYWORDS = ["stop", "unsubscribe", "optout", "opt out", "remove", "cancel"]
    if any(kw in body.lower() for kw in OPT_OUT_KEYWORDS):
        record_optout(from_number)
        reply = "You've been removed from our list. No more messages — GetMoreJobs.au"
        resp = MessagingResponse()
        resp.message(reply)
        return Response(str(resp), mimetype="text/xml")

    # Record inbound message
    with get_db() as db:
        db.execute("""
            INSERT INTO messages(lead_id,direction,body,seq_step,sent_at)
            VALUES(?,?,?,?,?)
        """, (lid, "in", body, 0, datetime.now().isoformat()))
        # Update lead status → replied
        db.execute(
            "UPDATE leads SET status='replied', updated_at=? WHERE id=?",
            (datetime.now().isoformat(), lid)
        )
        db.commit()

    # Auto-reply
    auto_reply = (
        "Thanks for getting back to me! I'll give you a call shortly to chat about "
        "getting your business found online. What's a good time? — GetMoreJobs.au"
    )
    resp = MessagingResponse()
    resp.message(auto_reply)
    return Response(str(resp), mimetype="text/xml")

@app.route("/sms/status", methods=["POST"])
def sms_status():
    """Twilio status callback — logs delivery confirmations."""
    sid = request.form.get("MessageSid", "")
    status = request.form.get("MessageStatus", "")
    log.info(f"SMS status update: SID={sid} status={status}")
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    return {"status": "running", "daily_sent": daily_sent_count()}, 200

@app.route("/stats", methods=["GET"])
def stats():
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) as c FROM leads").fetchone()["c"]
        new   = db.execute("SELECT COUNT(*) as c FROM leads WHERE status='new'").fetchone()["c"]
        sent  = db.execute("SELECT COUNT(*) as c FROM leads WHERE status='sms_sent'").fetchone()["c"]
        rep   = db.execute("SELECT COUNT(*) as c FROM leads WHERE status='replied'").fetchone()["c"]
        won   = db.execute("SELECT COUNT(*) as c FROM leads WHERE status='won'").fetchone()["c"]
        optout= db.execute("SELECT COUNT(*) as c FROM optouts").fetchone()["c"]
    return {
        "total_leads": total,
        "new": new,
        "sms_sent": sent,
        "replied": rep,
        "won": won,
        "opted_out": optout,
        "daily_sent_today": daily_sent_count(),
        "daily_cap": C.DAILY_SMS_CAP,
    }, 200

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
def setup_schedule():
    # Main scrape + send → every Monday 8:30am
    schedule.every().monday.at("08:30").do(job_scrape_and_send)

    # Follow-ups → every day at 9am (only sends if leads qualify)
    schedule.every().day.at("09:00").do(job_send_followups)

    # Catch any new qualifying leads mid-week
    schedule.every().wednesday.at("09:00").do(job_send_initial)
    schedule.every().friday.at("09:00").do(job_send_initial)

    log.info("Schedule set:")
    log.info("  Mon 08:30 — Scrape + initial SMS")
    log.info("  Wed 09:00 — Send to new qualifying leads")
    log.info("  Fri 09:00 — Send to new qualifying leads")
    log.info("  Daily 09:00 — Follow-ups (Day 3 and Day 7)")

def run_scheduler():
    """Runs in background thread alongside Flask."""
    import threading
    def _loop():
        while True:
            schedule.run_pending()
            time.sleep(30)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info("Scheduler running in background")

# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("GetMoreJobs.au — Outreach Automation")
    log.info("=" * 60)

    # Validate config
    missing = [k for k in ["TWILIO_SID","TWILIO_TOKEN","TWILIO_FROM","OUTSCRAPER_KEY"]
               if not getattr(C, k)]
    if missing:
        log.warning(f"Missing env vars: {missing} — running in DRY RUN mode")
    else:
        log.info("All API keys configured ✓")

    init_db()
    setup_schedule()
    run_scheduler()

    log.info(f"Starting Flask webhook server on port {C.PORT}")
    log.info(f"Twilio webhook URL: {C.WEBHOOK_URL}/sms/receive")
    log.info("=" * 60)

    app.run(host="0.0.0.0", port=C.PORT)
