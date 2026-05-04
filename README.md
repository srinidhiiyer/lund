# 📡 Telegram Toss Message Forwarder

A production-ready Telethon userbot that monitors a public Telegram channel and
automatically copies **only** cricket toss-result messages into your own channel.

---

## 📁 Project Structure

```
toss_forwarder/
├── toss_forwarder.py        # Main script
├── test_filter.py           # Unit tests for the message filter
├── requirements.txt         # Python dependencies
├── .env.example             # Config template  →  copy to .env
├── toss-forwarder.service   # systemd unit (for VPS 24/7 deployment)
├── logs/                    # Auto-created; forwarder.log written here
└── seen_hashes.txt          # Auto-created; prevents duplicate forwards
```

---

## ✅ Message Format Matched

The script forwards **only** messages that satisfy ALL three rules:

| Rule | Pattern |
|------|---------|
| Contains | `WON THE TOSS AND DECIDED TO` |
| Contains | `BAT` or `BOWL` (whole word) |
| Ends with | `✔`, `✔✔`, `✅`, `✅✅` (one–four check emojis) |

**Valid examples:**
```
🇵🇹 PORTUGAL 🇵🇹 WON THE TOSS AND DECIDED TO BAT ✔✔
🏴 KOLKATA { KKR } 🏴 WON THE TOSS AND DECIDED TO BOWL ✔✔
```

**Ignored examples:**
```
🔥 BEST TIPS — JOIN @xyz              ← ad, no toss phrase
INDIA WON THE MATCH BY 6 WICKETS ✔✔  ← match winner, not toss
```

---

## 🔑 Step 1 — Get Your API Credentials

1. Go to **https://my.telegram.org** and sign in with your phone number.
2. Click **"API development tools"**.
3. Fill in **App title** (any name) and **Short name** (any name).
4. Click **Create application**.
5. Copy the **`api_id`** (a number) and **`api_hash`** (a long hex string).

> ⚠️ Keep these secret — they identify your Telegram account.

---

## ⚙️ Step 2 — Configure `.env`

```bash
cp .env.example .env
nano .env          # or use any text editor
```

Fill in:

```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
PHONE=+919876543210

SOURCE_CHANNEL=@source_public_channel   # or -100xxxxxxxxxx
TARGET_CHANNEL=@my_target_channel       # must be a channel you admin
SESSION_NAME=toss_session
```

**How to find a channel's numeric ID:**
- Forward any message from that channel to **@userinfobot** on Telegram.
- It will reply with the channel's ID (e.g. `-1001234567890`).

> Make sure your account has **"Post Messages"** admin rights in the target channel.

---

## 📦 Step 3 — Install Dependencies

```bash
# Create and activate a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install packages
pip install -r requirements.txt
```

---

## ▶️ Step 4 — First Run (Authentication)

```bash
python toss_forwarder.py
```

On the **first run**, Telethon will:
1. Ask for your phone number (already in `.env`, will be used automatically).
2. Send a login code to your Telegram app.
3. Ask you to enter that code in the terminal.
4. Ask for your 2FA password if you have one enabled.

This creates a `toss_session.session` file. **Guard this file** — it represents a
logged-in session to your Telegram account.

After login the bot starts listening. You should see:
```
Logged in successfully. Listening for messages…
```

---

## 🧪 Running the Tests

```bash
pip install pytest
pytest test_filter.py -v
```

All filter logic is tested independently — no Telegram connection needed.

---

## 🛰️ Step 5 — 24/7 Deployment

### Option A — VPS with systemd (recommended)

Tested on Ubuntu 22.04 / 24.04 (DigitalOcean, Hetzner, AWS EC2, etc.).

```bash
# 1. Upload your project to the VPS
scp -r toss_forwarder/ ubuntu@YOUR_VPS_IP:/home/ubuntu/

# 2. SSH in and set up
ssh ubuntu@YOUR_VPS_IP
cd /home/ubuntu/toss_forwarder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Run ONCE interactively to complete login and create the session file
python toss_forwarder.py
# Enter the Telegram code when prompted, then Ctrl-C once logged in

# 4. Install the systemd service
sudo cp toss-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable toss-forwarder   # auto-start on reboot
sudo systemctl start  toss-forwarder

# 5. Verify it's running
sudo systemctl status toss-forwarder
sudo journalctl -u toss-forwarder -f   # live logs
```

---

### Option B — Railway.app (free tier, no VPS needed)

1. Push the project to a **private** GitHub repo.
2. Go to **https://railway.app** → New Project → Deploy from GitHub.
3. Set all `.env` variables in Railway's **Environment** tab.
4. Railway auto-runs `python toss_forwarder.py`.

> ⚠️ You still need to generate the `.session` file locally first and upload it,
> or use Railway's volume mount. The session file must exist before the service
> starts in a non-interactive environment.

---

### Option C — Render.com Background Worker

Same approach as Railway. Create a **Background Worker** service, set environment
variables, and deploy.

---

## 🔒 Security Tips

| Concern | Recommendation |
|---------|---------------|
| `.env` file | Never commit to git. Add to `.gitignore`. |
| `.session` file | Same — it's equivalent to your password. |
| VPS access | Use SSH keys, disable password login. |
| API credentials | Rotate if you suspect a leak (my.telegram.org → Revoke). |

---

## 📝 Logs

- **Console** — real-time output.
- **`logs/forwarder.log`** — persistent log file (UTF-8, append mode).
- **`seen_hashes.txt`** — SHA-256 hashes of forwarded messages (prevents duplicates across restarts).

---

## 🐛 Troubleshooting

| Symptom | Fix |
|---------|-----|
| `FloodWaitError` | Telegram rate-limited you. The client will retry automatically. |
| `ChannelPrivateError` | Your account isn't a member of the source channel. Join it first. |
| `ChatAdminRequiredError` | Your account isn't an admin of the target channel. |
| Session file missing on server | Re-run interactively once locally, then copy the `.session` file to the server. |
| Messages not being forwarded | Run `pytest test_filter.py -v` and check if your sample messages pass. |
