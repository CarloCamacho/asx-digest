# ASXDigest — Setup Guide

Twice-daily ASX stock tip aggregator. Fetches 5 sources, extracts picks via Claude, emails a tiered digest.

## Sources

| Source | Type | URL |
|--------|------|-----|
| Wealth Within | YouTube RSS | Auto-configured |
| Stocks Down Under | RSS | Auto-configured |
| Stockhead | RSS | Auto-configured |
| r/ASX_Bets | Reddit JSON | Auto-configured |
| HotCopper | HTML (disabled) | Enable in config if desired |

---

## Setup Steps

### Step 1 — Configure Email

Edit `config.json` and fill in the email section:

#### Option A: Gmail (simplest)

1. Enable 2-Step Verification on your Google account
2. Go to: https://myaccount.google.com/apppasswords
3. Create an App Password for "Mail" → copy the 16-char password
4. In `config.json`, set:
   ```json
   "method": "gmail",
   "from_address": "your.email@gmail.com",
   "app_password": "xxxx xxxx xxxx xxxx",
   "recipients": ["your.email@gmail.com"]
   ```

#### Option B: AgentMail (https://agentmail.to)

1. Sign up at agentmail.to and get an API key
2. Create an inbox — note the inbox_id
3. In `config.json`, set:
   ```json
   "method": "agentmail"
   ```
   And fill in the `agentmail` section:
   ```json
   "api_key": "your-api-key",
   "from_address": "asx-digest@agentmail.to",
   "inbox_id": "your-inbox-id"
   ```

### Step 2 — Test it

```bash
python3 ~/Documents/Claude/Scheduled/ASXDigest/asx_digest.py --dry-run
```

This runs the full pipeline but prints the digest instead of emailing it.

### Step 3 — Scheduling

Scheduled via macOS LaunchAgent at 8am and 5pm daily. To verify:

```bash
launchctl list | grep asxdigest
```

The plist is at `~/Library/LaunchAgents/com.ianf.asxdigest.plist`. To reload after editing:

```bash
launchctl unload ~/Library/LaunchAgents/com.ianf.asxdigest.plist
launchctl load ~/Library/LaunchAgents/com.ianf.asxdigest.plist
```

---

## Adding Kelly

In `config.json`, add her email to the recipients list:

```json
"recipients": [
  "your.email@gmail.com",
  "kelly@example.com"
]
```

---

## How It Works

1. **Fetch** — pulls latest content from each enabled source
2. **Deduplicate** — skips anything already seen in previous runs (stored in `state.json`)
3. **Analyze** — sends each new item to Claude Haiku, asks for ASX stock picks + reasoning
4. **Aggregate** — groups by ticker, counts sources
5. **Tier** — stocks in 2+ sources → HIGH CONVICTION 🔥; single source → standard pick 📌
6. **Email** — sends tiered HTML digest only if new picks found (no empty emails)

---

## Files

| File | Purpose |
|------|---------|
| `config.json` | All settings — edit this |
| `state.json` | Auto-managed — tracks seen items |
| `asx_digest.py` | Main script |
| `logs/asx_digest.log` | Run history |

---

## Troubleshooting

**No email received:** Check `logs/asx_digest.log` for errors. Most common cause: Gmail App Password not configured.

**Email marked as spam:** Use your real Gmail address as `from_address`. Consider switching to AgentMail for better deliverability.

**Claude not found:** Verify `claude_cli_path` in config.json. Check with: `which claude`

**Reddit 403 errors:** The script includes the required User-Agent header automatically.

---

## Claude Code Scheduling Note

`CronCreate` in Claude Code is **session-only** (disappears when Claude Code exits, max 7 days).
This script uses a **macOS LaunchAgent** instead — persistent across reboots, no Claude Code dependency.
