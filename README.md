# GetMoreJobs.au — Outreach Automation
### Fully automated tradie lead gen + SMS via Twilio

---

## What this does

1. **Every Monday 8:30am** — Scrapes Google Maps for tradie businesses across your target suburbs
2. **Immediately** — Scores each lead (no website = hot, low reviews = hot)
3. **Auto-sends SMS Step 1** to every lead scoring 6+
4. **Day 3** — Auto follow-up to anyone who hasn't replied
5. **Day 7** — Final follow-up, then stops
6. **On reply** — Stops sequence, logs reply, sends auto-acknowledgement, notifies you
7. **On STOP** — Immediately removes from list (Spam Act compliant)

---

## Setup — 3 steps, ~20 minutes total

### Step 1: Get Twilio (Australian number)

1. Go to **twilio.com** → Sign up free
2. Console → Phone Numbers → Buy Number → Australia → Mobile
3. Cost: ~$3 AUD/month for the number + $0.08 per SMS sent
4. Note your:
   - Account SID (starts with `AC`)
   - Auth Token
   - Phone number (format: `+614XXXXXXXX`)

### Step 2: Get Outscraper API key

1. Go to **outscraper.com** → Sign up
2. Top right → API Key → Create
3. $10 credit = ~500 business leads scraped
4. Note your API key

### Step 3: Deploy to Railway (free hosting)

1. Go to **railway.app** → Sign up with GitHub
2. New Project → Deploy from GitHub repo
   - (Upload this folder to a GitHub repo first — takes 2 min)
   - OR: New Project → Empty → Add Service → drag & drop this folder
3. Go to your service → Variables → Add all these:

```
TWILIO_ACCOUNT_SID   = ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN    = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER   = +61400000000
OUTSCRAPER_API_KEY   = your-outscraper-key
WEBHOOK_BASE_URL     = https://your-app-name.up.railway.app
PORT                 = 5000
```

4. Deploy → wait ~2 minutes → it's live

### Step 4: Point Twilio to your webhook

1. Twilio Console → Phone Numbers → your AU number → Configure
2. **Messaging** → When a message comes in:
   - URL: `https://your-app-name.up.railway.app/sms/receive`
   - Method: `HTTP POST`
3. Save

That's it. The system runs forever.

---

## Customisation

### Add more suburbs (outreach.py → SEARCH_QUERIES)
```python
SEARCH_QUERIES = [
    "plumber Geelong VIC",
    "electrician Gold Coast QLD",
    # ... add as many as you want
]
```

### Change scoring threshold (only SMS leads above this score)
```python
MIN_SCORE = 6   # 6 = no website + low reviews. Lower to cast wider net.
```

### Change daily SMS cap
```python
DAILY_SMS_CAP = 50   # Stay conservative. Twilio limits new accounts to 200/day.
```

### Edit SMS templates
Find `TEMPLATES` dict in `outreach.py`. Edit the strings for each trade and step.

---

## Monitoring

- **Live stats**: `https://your-app.railway.app/stats`
- **Health check**: `https://your-app.railway.app/health`
- **Logs**: Railway dashboard → your service → Logs tab

---

## Costs (monthly)

| Item | Cost |
|------|------|
| Railway hosting | Free |
| Twilio AU number | ~$3/mo |
| SMS (200/week × 4) | ~$64/mo |
| Outscraper (500 leads) | ~$10 one-off |
| **Total** | **~$77/mo** |

One client at $1,500/month = 19x ROI on running costs.

---

## Legal (AU Spam Act 2003)

All templates include:
- Business identification (GetMoreJobs.au)
- Clear opt-out instruction (Reply STOP)
- Opt-out is honoured immediately and permanently

Sending to business numbers (tradies) = commercial communication = compliant with reasonable identification + opt-out included.

---

## Support

Built by GetMoreJobs.au. Questions → update the config and redeploy.
