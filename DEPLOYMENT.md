# Deployment Guide

## Server Info

- **Provider**: DigitalOcean
- **IP**: 198.211.102.7
- **User**: root
- **App directory**: `/home/ubuntu/Etrade-stock-monitor`

## SSH Access

```bash
ssh root@198.211.102.7
```

## Deploy / Restart

```bash
cd /home/ubuntu/Etrade-stock-monitor && git pull && ./restart_server.sh
```

## Key Config Notes

- **Email backend**: must be `EMAIL_BACKEND=gmail_api` (NOT `smtp`)
  - DigitalOcean blocks outbound SMTP ports (25/465/587) by default
  - Gmail API uses HTTPS (port 443) and is not affected
- **Gmail OAuth files** (not in git, must be kept on server):
  - `credentials.json` — Google OAuth app credentials
  - `gmail_token.json` — OAuth token (auto-refreshes; re-run locally with `--once` if expired)
- **`.env`** — not in git, must be maintained separately on server

## Uploading Secrets to Server

If `gmail_token.json` or `credentials.json` need to be updated:

```bash
# From local machine (inside project directory)
scp gmail_token.json root@198.211.102.7:/home/ubuntu/Etrade-stock-monitor/
scp credentials.json root@198.211.102.7:/home/ubuntu/Etrade-stock-monitor/
```

If `.env` needs to be updated:

```bash
scp .env root@198.211.102.7:/home/ubuntu/Etrade-stock-monitor/
```

## Troubleshooting

### Email not sending (`Network is unreachable`)
- Cause: `EMAIL_BACKEND=smtp` is set, but DO blocks port 587
- Fix: set `EMAIL_BACKEND=gmail_api` in `.env` on the server

### Gmail token expired
- Run locally: `python -m src.main --once --dry-run`
- This opens a browser OAuth flow and saves a new `gmail_token.json`
- Then scp the new token to the server (see above)

### Reports running late / MISSED warnings
- The "Mac was likely asleep" message in logs is a hardcoded string — ignore it on the server
- MISSED events happen when the scheduler restarts and catches up on skipped jobs
- Check if the app crashed: `systemctl status stockmonitor` or check the process

## Scheduler Jobs

| Report | Time (ET) | Days |
|--------|-----------|------|
| Pre-Market | 08:30 | Mon–Fri |
| Mid-Day | 12:00 | Mon–Fri |
| Post-Market | 16:30 | Mon–Fri |
| Portfolio Refresh | Sun 12:00 | Weekly |
