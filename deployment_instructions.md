# 🚀 Deploy Voice‑Habit‑Tracker Bot for Free (no laptop needed)

## Why you need a cloud host
The bot needs to stay **always‑on** to:
- Send the scheduled morning/afternoon/evening reminders.
- Listen for voice messages from Telegram.
- Keep the observation‑phase logic running.

Running it on your laptop means you must keep the computer powered on and the script active. A free‑tier cloud service can run the Docker container 24/7 without any cost.

---
## ✅ Recommended free‑tier providers
| Provider | Free tier features | No credit‑card needed? | Easy GitHub integration |
|----------|-------------------|------------------------|------------------------|
| **Render.com** – Background Worker | 750 hrs/month (≈ 31 days) of free CPU, 0 GB RAM limit (sufficient for this bot) | ✔️ (sign‑in with GitHub) | ✔️ (connect public repo) |
| **Railway.app** – Free tier | 500 hrs/month, 1 GB RAM, 1 CPU | ✔️ (GitHub sign‑in) | ✔️ |
| **Fly.io** – Free shared VM | 3 GB RAM‑hours/month, 1 CPU‑share, can run Docker | ✔️ (GitHub sign‑in) | ✔️ |

All three can clone a **public** GitHub repository and run the `Dockerfile` you already have.

---
## 📦 Preparing the repo (already done)
1. **Repository URL**: `https://github.com/Eveyrn/voice-habit-tracker`
2. The repo is **public** (required for free tier services to access it without auth).
3. It contains:
   - `Dockerfile` – builds a Python 3.12‑slim image, installs `requirements.txt`.
   - `Procfile` – `worker: python main.py`
   - `requirements.txt` – now includes `apscheduler>=3.10.0`.
   - `.env.example` (optional) – lists needed environment variables.

---
## 🔧 Environment variables you must set on the host
| Variable | Value (example) |
|----------|-----------------|
| `TELEGRAM_BOT_TOKEN` | *(your bot token from BotFather)* |
| `ALLOWED_USER_IDS` | *(your Telegram user ID)* |
| `GROQ_API_KEY` | *(your Groq API key)* |
| `TIMEZONE` | `Asia/Bishkek` |
| (optional) `DATABASE_URL` – you can keep the SQLite file inside the container; no extra variable needed.

---
## 🛠️ Deployment steps for **Render.com** (fastest & simplest)
1. **Log in** to <https://dashboard.render.com> using your GitHub account.
2. Click **New** → **Background Worker**.
3. **Connect repository** – choose `Eveyrn/voice-habit-tracker`.
4. **Name** the service, e.g., `voice‑habit‑tracker‑bot`.
5. **Environment**: select **Docker** (Render will use the `Dockerfile`).
6. **Instance Type**: choose **Free** (default). No credit‑card required.
7. **Add Environment Variables** – copy the table above.
8. Click **Create Background Worker**.
9. Render will build the image, start the container, and show logs.
   - Look for lines like `INFO:apscheduler.scheduler:Scheduler started` and `INFO:aiogram:Start polling`.
10. Once the logs show `Bot started` the bot is live – it will keep running even when your laptop is off.

---
## 🛠️ Deployment steps for **Railway.app** (alternative)
1. Sign in at <https://railway.app> with GitHub.
2. Click **New Project** → **Deploy from GitHub** → select the repo.
3. Railway auto‑detects the `Dockerfile` and creates a service.
4. Under **Settings → Variables**, add the environment variables from the table.
5. Deploy – Railway will spin up a free container (≈ 500 hrs/month).
6. Check the **Logs** tab for the same startup messages.

---
## 🛠️ Deployment steps for **Fly.io** (if you prefer Fly)
1. Install Fly CLI (`brew install superfly/tap/flyctl` on macOS, or use the Windows installer).
2. Run `fly auth login` – it opens a browser and logs you in via GitHub.
3. Inside the repo folder, execute:
   ```bash
   fly launch --name voice-habit-tracker-bot --copy-config
   ```
   - Accept the auto‑generated `fly.toml` (it uses the Dockerfile).
4. In the Fly dashboard, go to **Settings → Secrets** and add the environment variables.
5. Deploy with `fly deploy`.
6. The service will start and stay up 24/7 on the free shared VM.

---
## 📈 Monitoring & Restarting
- All three platforms provide a **Logs** view – watch for errors.
- If the bot crashes, the platform automatically restarts it.
- You can manually trigger a restart from the dashboard.

---
## 📬 How to verify it works
1. Send a voice message to your Telegram bot (`@MHabitT_bot`).
2. You should receive a reply from the bot.
3. Wait for the next scheduled reminder (e.g., 11:00 Asia/Bishkek). You’ll get a message even though your laptop is off.
4. Use `/patterns` to confirm the observation‑phase status.

---
## 🎉 Done!
Your bot will now run **continuously** on a free cloud host, eliminating the need to keep your laptop powered on.
If you run into any trouble (build failures, missing env vars, etc.) let me know – I can help troubleshoot the specific logs.
