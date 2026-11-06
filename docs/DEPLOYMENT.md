# Deployment

## What the host has to provide

- **An always-on process.** The worker polls every 30 s, so a host that sleeps when there's no web traffic would silently stop ingestion. This rules out most "free web app" tiers.
- **PostgreSQL 16** with a few GB of disk: about 200k positions/day × 14 days of retention, plus derived tables (see PERFORMANCE.md).
- **Very little CPU.** On the laptop, the worker averages well under one core.

## Options compared (researched 2026-11-06; prices and free tiers change, so check before signing up)

| | **A. Oracle Cloud Always Free VM** (Ampere A1, ARM) | **B. Small paid VPS** (e.g. Hetzner Cloud, ~2 vCPU / 4 GB) |
|---|---|---|
| Cost | €0 within the Always Free limits | roughly €4-7/month in the EU; Singapore is reportedly 40-67% more |
| Resources | reportedly cut in 2026 to **2 OCPU / 12 GB** total (from 4 / 24) | 2 vCPU / 4 GB is plenty |
| Always-on | yes, **but** Oracle reclaims "idle" Always Free VMs (reported threshold: CPU p95 < ~20% over 7 days). This worker *is* that idle. Converting the account to **Pay As You Go** avoids reclaim while free resources stay free, but needs a card, so set a budget alert. | yes |
| Gotchas | "Out of host capacity" errors when creating A1 VMs in busy regions; an account idle for 30+ days may be closed; the 2026 limit change was barely announced | plan line-up and prices changed twice in 2026; some cost-optimised plans were reportedly not orderable in Sep/Oct 2026 |
| Deploy method | the steps below (Docker Compose) | the same steps |

**Rejected: Render free tier.** Free web services spin down after 15 min without traffic, background workers reportedly have no free type (from ~$7/month), and free Postgres reportedly expires after 30 days. Any of these breaks a 24/7 collector.

**Recommendation:** **A (Oracle Always Free) with the account converted to Pay As You Go**, plus a budget alert set to a few dollars. It's the only option that is genuinely €0 and always on, and 2 OCPU / 12 GB is far more than this needs. If you don't want a card on a pay-as-you-go cloud account, use **B**: about €5/month buys something simple and predictable. **Neither has been set up. Both involve a payment card, so this is your decision.**

Sources: [InfoQ on the 2026 A1 limit cut](https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/), [Oracle Always Free resources (official)](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm), [OCI reclamation rules summary](https://oracommit.blogspot.com/2026/02/understanding-oci-always-free-compute.html), [Render pricing 2026](https://temps.sh/blog/render-pricing-2026), [Hetzner price adjustment (official)](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/), [Hetzner plans and availability 2026](https://www.bitdoze.com/hetzner-cloud-cost-optimized-plans).

## Deploy (any Linux VM with Docker: Ubuntu 24.04 assumed)

The images are multi-arch (`python:3.12-slim`, `postgres:16`, uv), and psycopg ships ARM wheels, so this works on Oracle's ARM VMs too. (I haven't run it on ARM yet.)

```bash
# 1. Docker (official convenience script; or follow docs.docker.com/engine/install/ubuntu)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# 2. Code
git clone https://github.com/<you>/kl-transit-pulse.git && cd kl-transit-pulse

# 3. Config: copy, then set a strong POSTGRES_PASSWORD (and the matching DATABASE_URL)
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
nano .env

# 4. Start. The one-shot `migrate` service applies Alembic migrations before the worker
#    and API start, on every deploy.
docker compose up -d --build
curl -s localhost:8000/health
```

**Updating:** `git pull && docker compose up -d --build`. Migrations run automatically, and `worker` and `api` only start after `migrate` succeeds, so a failed migration stops the deploy instead of running new code on an old schema.

### Exposing the API safely

Postgres and the API are bound to `127.0.0.1` in `docker-compose.yml`, so nothing is public by default. To serve the API, put an HTTPS reverse proxy in front. Caddy gets certificates automatically:

```bash
sudo apt install -y caddy
echo 'transit.example.com {
    reverse_proxy 127.0.0.1:8000
}' | sudo tee /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

Then open only ports 80/443 in the cloud firewall (Oracle: the VCN security list **and** the VM's iptables; Hetzner: Cloud Firewall). **Never open 5432.**

### Backups

The raw positions are re-collectable going forward, but history is not. A nightly dump is enough at this size:

```bash
# crontab -e
15 3 * * * cd ~/kl-transit-pulse && docker compose exec -T db pg_dump -U transit -Fc transit > ~/backup-$(date +\%a).dump
```

That keeps 7 rolling daily dumps. Copy them off the VM if the history matters.

### Checking it's alive

`GET /health` returns **503** when ingestion has stopped or the feed is frozen. Point a free uptime monitor (e.g. UptimeRobot, Better Stack) at `https://<your-domain>/health` to get an email when it goes unhealthy.
