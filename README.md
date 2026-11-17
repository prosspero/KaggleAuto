# KaggleAuto — 24/7 Qwen3-32B via Kaggle GPU Rotation

Serve **Qwen3-32B** around the clock using 6 free Kaggle accounts that rotate
automatically, fronted by a single **Cloudflare Tunnel** endpoint.

```
┌──────────────────────────────────────────────────────┐
│  Your App / Client                                   │
│  (sends OpenAI-compatible requests)                  │
└──────────────┬───────────────────────────────────────┘
               │ HTTPS
               ▼
┌──────────────────────────────────────────────────────┐
│  Cloudflare Tunnel  (qwen.yourdomain.com)            │
│  → routes to whichever Kaggle notebook is active     │
└──────────────┬───────────────────────────────────────┘
               │
   ┌───────────┴───────────┐
   ▼                       ▼            (round-robin)
┌────────────┐  ┌────────────┐  ...  ┌────────────┐
│ Account 1  │  │ Account 2  │       │ Account 6  │
│ 11.5 h run │  │ 11.5 h run │       │ 11.5 h run │
│ vLLM + CF  │  │ vLLM + CF  │       │ vLLM + CF  │
└────────────┘  └────────────┘       └────────────┘
```

## Quota Math

| Resource | Value |
|---|---|
| Kaggle free GPU quota | **30 h / week / account** |
| Accounts | **6** |
| Total weekly GPU time | **180 h** |
| Hours in a week | 168 h |
| **Headroom** | **12 h** (~7 %) |

Each session runs for **11.5 h** (leaving a 30-minute buffer before Kaggle's
hard 12-hour limit). The orchestrator starts the *next* account 5 minutes
before the current one ends, so the tunnel endpoint never goes dark.

---

## Prerequisites

| Requirement | Minimum | Install |
|---|---|---|
| Python | 3.8+ | <https://python.org> |
| `kaggle` CLI | latest | `pip install kaggle` |
| Cloudflare account | free tier | <https://dash.cloudflare.com/sign-up> |
| `cloudflared` (local) | latest | <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/> |
| 6 Kaggle accounts | each phone-verified for GPU | <https://kaggle.com> |

---

## 1 — Cloudflare Tunnel Setup

The tunnel lets every Kaggle notebook expose its local vLLM server
(`localhost:8000`) to the internet through a single, stable hostname.

### 1.1 Install `cloudflared`

```bash
# Windows (winget)
winget install --id Cloudflare.cloudflared

# macOS
brew install cloudflared

# Linux (Debian/Ubuntu)
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
```

### 1.2 Authenticate

```bash
cloudflared login
# A browser window opens — select the domain you want to use.
```

### 1.3 Create a Named Tunnel

```bash
cloudflared tunnel create kaggle-qwen
# Note the tunnel UUID printed — you'll need it below.
```

### 1.4 Configure DNS

Point a subdomain at the tunnel:

```bash
cloudflared tunnel route dns kaggle-qwen qwen.yourdomain.com
```

### 1.5 Get the Tunnel Token

```bash
cloudflared tunnel token kaggle-qwen
```

Copy the token (a long base64 string starting with `eyJ…`). You will:

1. Paste it into `config.json` → `cloudflare_tunnel_token`.
2. Add it as a **Kaggle Secret** named `CLOUDFLARE_TUNNEL_TOKEN` on every
   Kaggle account (see §2).

### 1.6 Configure the Tunnel Ingress

Create `~/.cloudflared/config.yml` (or the equivalent path shown by
`cloudflared tunnel info`):

```yaml
tunnel: <TUNNEL_UUID>
credentials-file: ~/.cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: qwen.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
```

> **Note:** The Kaggle notebook itself runs `cloudflared tunnel --token …`,
> which uses the *token-based* connector and does **not** need this config
> file on the notebook side. The config above is only needed if you want to
> test the tunnel locally.

---

## 2 — Kaggle Account Setup

Repeat these steps for **each** of the 6 accounts.

### 2.1 Enable GPU Quota

1. Go to **kaggle.com → Your Profile → Settings → Phone Verification** and
   verify a phone number (each account needs a unique number for GPU access).
2. Confirm GPU quota is active by creating a test notebook and selecting
   **Accelerator → GPU T4 ×2**.

### 2.2 Get API Credentials

1. Go to **kaggle.com → Your Profile → Settings → API → Create New Token**.
2. A `kaggle.json` file downloads containing `username` and `key`. Save these
   — you'll put them into `config.json`.

### 2.3 Add Cloudflare Tunnel Token as a Kaggle Secret

1. Go to **kaggle.com → Your Profile → Settings → Secrets**.
2. Click **Add a new secret**.
3. **Label:** `CLOUDFLARE_TUNNEL_TOKEN`
4. **Value:** paste the tunnel token from §1.5.
5. Make sure the toggle for "Attach to all notebooks" is **on**, or manually
   attach it to the kernel you will push.

### 2.4 Verify API Access

```bash
set KAGGLE_USERNAME=kaggle_user_1
set KAGGLE_KEY=hex_api_key_1
kaggle kernels list --mine
```

You should see an empty list or your existing kernels (no errors).

---

## 3 — Configuration

### 3.1 Copy the Example Config

```bash
copy config.example.json config.json
```

### 3.2 Fill in `config.json`

```jsonc
{
    "accounts": [
        {
            "username": "your_kaggle_user_1",
            "api_key": "your_hex_api_key_1",
            "kernel_slug": "your_kaggle_user_1/qwen3-32b-serve-1"
        }
        // ... repeat for all 6 accounts
    ],

    "cloudflare_tunnel_token": "eyJhIjoiYWJjZGVm...",  // from §1.5
    "health_check_url": "https://qwen.yourdomain.com/v1/models",

    "timing": {
        "session_duration_hours": 11.5,   // max safe runtime
        "overlap_minutes": 5,             // start next before stopping current
        "health_check_interval_seconds": 60,
        "status_poll_interval_seconds": 30,
        "max_start_wait_minutes": 15
    },

    "quota": {
        "weekly_limit_hours": 30,
        "quota_reset_day": "sunday"
    },

    "notebook_template_dir": "notebook",
    "log_max_bytes": 10485760,
    "log_backup_count": 5
}
```

> **Important:** `kernel_slug` must follow the pattern
> `<username>/<kernel-name>`. Each account needs its own unique slug.

### 3.3 Validate

```bash
python setup.py
```

The script checks Python, kaggle CLI, config format, API keys, and tunnel
connectivity. Fix any ❌ items before proceeding.

---

## 4 — Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 5 — Running the Orchestrator

```bash
python orchestrator.py
```

The orchestrator will:

1. Load `config.json` and `state.json` (if resuming).
2. Pick the next account in the rotation.
3. Push the notebook template to Kaggle via `kaggle kernels push`.
4. Poll until the kernel is running.
5. Begin health checks against the tunnel endpoint.
6. At **T − 5 min** (before the 11.5 h session ends), start the next account.
7. Once the new session is healthy, let the old one expire.
8. Repeat forever.

### Running in the Background (Windows)

```powershell
# PowerShell — run detached
Start-Process -NoNewWindow python -ArgumentList "orchestrator.py" `
    -RedirectStandardOutput "logs\orchestrator_stdout.log" `
    -RedirectStandardError  "logs\orchestrator_stderr.log"
```

Or use **Task Scheduler** to start `orchestrator.py` at login.

### Graceful Shutdown

Press `Ctrl+C`. The orchestrator saves state to `state.json` and exits.
The currently-running Kaggle notebook will continue until its own timer
expires or Kaggle's 12-hour limit kicks in.

---

## 6 — Monitoring

### Terminal Dashboard

```bash
python monitor.py
```

Shows a live view (refreshes every 30 s) with:

- **Active account** and time remaining
- **Quota usage** per account (hours used this week)
- **Health check** status (last result, latency)
- **Recent logs** (tail of the latest log file)

Press `Ctrl+C` to exit the monitor.

### Manual Health Check

```bash
curl https://qwen.yourdomain.com/v1/models
```

A successful response returns a JSON list of loaded models.

### Querying the Model

Once the tunnel is live, send OpenAI-compatible requests:

```bash
curl https://qwen.yourdomain.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-32B",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 256
  }'
```

---

## 7 — Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `setup.py` says kaggle CLI not found | Not installed or not on PATH | `pip install kaggle` and restart terminal |
| API credential test fails | Wrong key or username | Re-download `kaggle.json` from that account |
| Notebook stays in "queued" state | Kaggle GPU queue is busy | Wait; the orchestrator retries automatically |
| Health check fails after notebook starts | Tunnel not connected, or model still loading | Model loading takes 5-10 min on T4; wait for `max_start_wait_minutes` |
| Quota exceeded on an account | Used >30 h this week | That account is skipped until quota resets on the configured day |
| Tunnel returns 502 | vLLM process crashed inside notebook | Orchestrator will detect this and failover to the next account |
| `state.json` is corrupted | Crash during write | Delete `state.json`; the orchestrator rebuilds it from scratch |

---

## File Structure

```
KaggleAuto/
├── orchestrator.py          # Main rotation loop (runs on your machine)
├── config.json              # Your account/tunnel configuration
├── config.example.json      # Template for config.json
├── state.json               # Runtime state (auto-generated)
├── setup.py                 # Pre-flight validation script
├── monitor.py               # Terminal monitoring dashboard
├── requirements.txt         # Python dependencies
├── README.md                # This file
├── notebook/
│   ├── kaggle_notebook.py   # Notebook template pushed to Kaggle
│   └── kernel-metadata.json # Kaggle kernel metadata template
└── logs/
    └── orchestrator.log     # Rotating log files
```

---

## License

MIT
