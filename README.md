# SportsFan360 — Sentiment Engine

AI-powered fan sentiment automation for FIFA World Cup 2026 and WT20 Women's World Cup.

**Deployed Service URL:** [https://sportsfan360-sentiment.onrender.com](https://sportsfan360-sentiment.onrender.com)

---

## What It Does

Runs automatically on a configurable schedule, searches the real internet for the latest sports fan sentiment, and stores structured reports in Firebase Firestore — ready to feed into FanZone and AskAI.

**Flow:**
```
Scheduler (2x daily, configurable)
    ↓
Agent: Vertex AI (Gemini 2.5 Flash) — uses built-in Google Search grounding
    ↓
Firebase Firestore — stores as /fifaSentiments/{timestamp} or /wt20wSentiments/{timestamp}
    ↓
FanZone + AskAI read from Firebase
```

---

## Report Structure (9 Sections)

Each run generates a JSON report with:

1. **trending_ads** — Top 3 trending ads with links
2. **misfired_ads** — Top 2 ads that backfired
3. **trending_statements** — Top 5 quotes from analysts, players, commentators
4. **top_searches** — Top 5 global searches
5. **fan_emotions** — Top 5 fan emotions with scores
6. **footballer_emotions** — Top 5 footballer/cricketer emotions
7. **national_emotions** — Top 5 national emotions across participating countries
8. **trending_memes** — Top 3 trending memes
9. **top_signals** — Top 5 intelligence signals with SportsFan360 plays

Every report includes:
- `timestamp` — doc ID (e.g. `2026-06-01_14-32-02`) — never overwrites
- `disclaimer` — "Answers are AI-generated. SportsFan360 does not claim accuracy of this content."

---

## Files

| File | Purpose |
|---|---|
| `sentiment_engine.py` | Core logic — Gemini 2.5 Flash client and prompting |
| `firebase_store.py` | Saves reports to Firestore with timestamp |
| `scheduler.py` | Runs pipeline on configurable schedule |
| `main.py` | FastAPI wrapper — `/health`, `/run-now`, `/latest` |
| `requirements.txt` | Dependencies |
| `.env` | Configuration variables (never commit — see below) |

---

## Setup

### 1. Install dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create `.env` file
```env
GCP_PROJECT_ID=fleet-gift-498306-p7
GCP_LOCATION=us-central1
FIREBASE_PROJECT_ID=sportsfan360-new
FIREBASE_CLIENT_EMAIL=firebase-adminsdk-xxx@sportsfan360-new.iam.gserviceaccount.com
FIREBASE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
RUNS_PER_DAY=2
SPORT=FIFA_WC_2026
```

### 3. GCP Authentication (For Local Development)
To run the sentiment engine locally, developers must authenticate using Google's **Application Default Credentials (ADC)**. 

1. Ask your GCP Project Admin to grant your work email access to the project `fleet-gift-498306-p7` with the role **`Vertex AI User`**.
2. Install the Google Cloud SDK on your machine:
   ```bash
   brew install --cask google-cloud-sdk
   ```
3. Run the login command and log in via your browser:
   ```bash
   gcloud auth application-default login
   ```
4. Configure the default project and quota project in your CLI:
   ```bash
   gcloud config set project fleet-gift-498306-p7
   gcloud auth application-default set-quota-project fleet-gift-498306-p7
   ```

No API key files or service account JSON credentials need to be shared or downloaded!

---

### 4. Run scheduler
```bash
python scheduler.py
```

### 5. Run as API server
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## API Endpoints

| Endpoint | Method | Params | Purpose |
|---|---|---|---|
| `/health` | GET | None | Health check |
| `/run-now` | POST | `sport` (optional) | Trigger one run manually |
| `/latest` | GET | `sport` (optional) | Get latest report from Firebase |
| `/list-reports` | GET | `sport` (optional), `limit` (optional) | List available report timestamps |
| `/get-report` | GET | `sport`, `timestamp` | Get a specific report by timestamp |

---

## Configuration

Change `RUNS_PER_DAY` in `.env` to run more or less frequently:

```
RUNS_PER_DAY=2    # runs every 12 hours
RUNS_PER_DAY=4    # runs every 6 hours
RUNS_PER_DAY=10   # runs every 2.4 hours
```

---

## Firebase Structure

```
fifaSentiments/ (or wt20wSentiments/)
  {YYYY-MM-DD_HH-MM-SS}/
    timestamp: "2026-06-01_14-32-02"
    sport: "FIFA_WC_2026"
    generated_at: Timestamp
    disclaimer: "Answers are AI-generated..."
    report: { ...all 9 sections... }
```

---

## Prompt Details

* **FIFA System Instructions:** You are SportsFan360, the daily app for loyal fans of FIFA and football sport.
* **WT20W System Instructions:** You are SportsFan360, the daily app for loyal fans of cricket and women's sports.
* **Constraints:** No made-up information. No repeats from last report. Only use real URLs found via search.

---

## Deployment

Deploy to Render or Railway — connect this GitHub repo, set env vars, set start command:

```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

---

## Tech Stack

- **GCP Vertex AI** — Gemini 2.5 Flash model
- **Google Search Grounding** — Real-time web search
- **Firebase Firestore** — Storage
- **FastAPI** — API wrapper
- **APScheduler** — Scheduling