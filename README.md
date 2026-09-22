# daily-sync

Small Cloudflare Pages + D1 app.

## Deploy

Pushes to `main` deploy automatically through GitHub Actions (`.github/workflows/deploy.yml`).

Repository secrets required:

| Secret | Purpose |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare token with Pages, D1 and R2 edit rights |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `SETUP_CODE` | One-time code used to create the first account |

No user data is stored in this repository.
