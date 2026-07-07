# Deployment — Vercel + Neon + Oracle Cloud (auto-deploy on push to main)

## Architecture

```
 push to main ──► GitHub Actions ──► Vercel        (frontend: Next.js)
 push to main ──► GitHub Actions ──► Oracle VM     (backend: FastAPI, Docker)
                                        │
                          ┌─────────────┼───────────────┐
                          ▼             ▼               ▼
                     Neon Postgres   Llama (Cloudflare)  Bisk Farm MySQL
                     (+ pgvector)     over HTTPS         (via SSH tunnel)
```

- **Frontend** → Vercel, auto-deployed by GitHub Actions.
- **Backend** → Oracle Cloud Always-Free VM (Docker), auto-deployed by GitHub Actions over SSH.
- **App DB** → Neon (Postgres + pgvector).
- **Analytics DB** → your Bisk Farm MySQL, reached from the VM via the SSH tunnel.

The two workflow files are already in the repos:
- Frontend: `botree_chat/.github/workflows/deploy-frontend.yml`
- Backend:  `botree_chat_backend/.github/workflows/deploy-backend.yml`

---

## Part A — Neon (app database)

1. Create a project at https://neon.com → it gives you a Postgres database.
2. In the Neon SQL editor, enable pgvector once:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
   (The Alembic migration also does this, but enabling it up front avoids a permissions surprise.)
3. Copy the connection string and convert it to the **asyncpg** form for this app:
   - Neon gives: `postgresql://USER:PASS@ep-xxx-pooler.REGION.aws.neon.tech/DBNAME?sslmode=require`
   - Use instead (note `+asyncpg` and `ssl=require`, **not** `sslmode`):
     ```
     PG_DSN=postgresql+asyncpg://USER:PASS@ep-xxx-pooler.REGION.aws.neon.tech/DBNAME?ssl=require
     ```
   > asyncpg does not understand `sslmode`; it uses `ssl`. Getting this wrong is the #1 Neon gotcha.
4. Put that `PG_DSN` in the VM's `.env` (Part B).

---

## Part B — Oracle Cloud Always-Free VM (backend)

1. Create an **Always Free** VM (Ampere ARM, e.g. VM.Standard.A1.Flex, ~1–2 OCPU / 6–12 GB RAM is plenty).
   Ubuntu 22.04 is easiest.
2. **Open ports** in both the OCI Security List AND the VM firewall:
   - `22` (SSH), `443`/`80` (HTTPS via Caddy — recommended), or `8000` if exposing the API directly.
   ```bash
   sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT   # (or 80/443 for Caddy)
   ```
3. Install Docker + compose plugin:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER && newgrp docker
   ```
4. Clone the backend repo and add secrets (NOT in git):
   ```bash
   git clone https://github.com/girish-raju/botree_chat_backend.git ~/botree_chat_backend
   cd ~/botree_chat_backend
   cp .env.example .env          # then edit .env (next step)
   mkdir -p ~/.ssh
   # copy the Bisk Farm private key onto the VM:
   #   scp aasim.niazi  <vm-user>@<vm-ip>:~/.ssh/aasim.niazi
   chmod 600 ~/.ssh/aasim.niazi
   ```
5. Edit `~/botree_chat_backend/.env`:
   ```
   PG_DSN=postgresql+asyncpg://USER:PASS@ep-xxx-pooler.REGION.aws.neon.tech/DBNAME?ssl=require
   JWT_SECRET=<a long random string>
   LLM_PROVIDER=cloudflare
   CLOUDFLARE_ACCOUNT_ID=...
   CLOUDFLARE_API_TOKEN=...
   # Bisk Farm MySQL via SSH tunnel:
   MYSQL_HOST=10.164.143.20
   MYSQL_PORT=3308
   MYSQL_USER=aasim_niazi
   MYSQL_PASSWORD=...
   MYSQL_DATABASE=biskfarm_report_pp3
   SSH_TUNNEL_ENABLED=true
   SSH_HOST=3.109.63.248
   SSH_USER=aasim.niazi
   SSH_KEY_PATH=/keys/aasim.niazi          # <-- matches the mount in docker-compose.prod.yml
   SSH_KEY_PASSWORD=...
   # Allow the Vercel frontend origin (browser CORS; the proxy is server-side so this is a safety net):
   CORS_ORIGINS=["https://your-app.vercel.app"]
   ```
6. First run + migrate + seed:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
   docker compose -f docker-compose.prod.yml run --rm api python scripts/seed_users.py
   curl http://localhost:8000/readyz     # expect postgres:true, mysql:true
   ```
7. **HTTPS (recommended).** Put Caddy in front for automatic TLS with a domain
   (a free `*.duckdns.org` works). Minimal `Caddyfile`:
   ```
   api.yourdomain.com {
       reverse_proxy localhost:8000
   }
   ```
   Then your backend URL is `https://api.yourdomain.com`.
   (Without a domain you can use `http://<VM-IP>:8000` — the Vercel frontend calls the
   backend **server-side**, so http works, but https is cleaner and safer.)

---

## Part C — Vercel (frontend)

1. Create a Vercel project from the `botree_chat` repo (or `vercel link` locally once to
   generate the project — you need its IDs for the GitHub Action).
2. Set an **Environment Variable** in the Vercel project (Production):
   - `BACKEND_URL = https://api.yourdomain.com`  (your Oracle backend URL from Part B)
3. Get the three values for GitHub Actions:
   - `VERCEL_TOKEN` — Vercel → Account Settings → Tokens → create.
   - `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` — from `.vercel/project.json` after `vercel link`,
     or the project's Settings page.

---

## Part D — GitHub secrets (enables auto-deploy on push to main)

**In the `botree_chat` (frontend) repo** → Settings → Secrets and variables → Actions:
| Secret | Value |
|--------|-------|
| `VERCEL_TOKEN` | your Vercel token |
| `VERCEL_ORG_ID` | Vercel org id |
| `VERCEL_PROJECT_ID` | Vercel project id |

**In the `botree_chat_backend` repo:**
| Secret | Value |
|--------|-------|
| `DEPLOY_HOST` | the VM's public IP / hostname |
| `DEPLOY_USER` | SSH user (e.g. `ubuntu` or `opc`) |
| `DEPLOY_SSH_KEY` | a **deploy** SSH private key; put its public half in the VM's `~/.ssh/authorized_keys` |
| `DEPLOY_PORT` | (optional) SSH port, default 22 |

> Generate a dedicated deploy key: `ssh-keygen -t ed25519 -f deploy_key -N ""`, add
> `deploy_key.pub` to the VM's authorized_keys, and paste `deploy_key` (private) as
> `DEPLOY_SSH_KEY`.

---

## Part E — Go live

1. Commit the workflow files (`.github/workflows/*.yml`) and `docker-compose.prod.yml` to
   `main` in both repos.
2. Every push to `main` now:
   - Frontend repo → GitHub Actions builds & deploys to Vercel.
   - Backend repo → GitHub Actions SSHes to the VM, `git reset --hard origin/main`,
     `docker compose up -d --build`, runs `alembic upgrade head`.
3. Verify:
   - `https://your-app.vercel.app` loads the login page.
   - Log in (seeded user `vp` / `botree123`), ask a question, see the answer.
   - `https://api.yourdomain.com/readyz` → `{"status":"ready","checks":{"postgres":true,"mysql":true}}`.

---

## Gotchas checklist
- **Neon DSN uses `?ssl=require`** (asyncpg), not `?sslmode=require`.
- **`SSH_KEY_PATH=/keys/aasim.niazi`** in `.env` must match the volume mount in `docker-compose.prod.yml`.
- **`BACKEND_URL`** on Vercel must be the backend's public URL (with `https://` if using Caddy).
- **`CORS_ORIGINS`** in the backend `.env` should include your Vercel domain.
- **Free-tier note:** Oracle Always-Free is always-on (no cold starts) — good for the SSH tunnel.
- **Secrets never in git:** `.env`, the SSH key, and all tokens live only on the VM / in GitHub Secrets / in Vercel env — never committed.
