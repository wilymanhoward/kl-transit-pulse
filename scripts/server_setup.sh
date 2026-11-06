#!/usr/bin/env bash
# One-time (and re-runnable) setup of a fresh Ubuntu 22.04/24.04 server, e.g. an Oracle
# Cloud Always Free ARM VM. Installs Docker, fetches the code, creates a .env with a
# random database password, starts the stack and schedules nightly backups.
#
# Run on the server:
#   curl -fsSL https://raw.githubusercontent.com/wilymanhoward/kl-transit-pulse/main/scripts/server_setup.sh | bash
#
# Re-running is safe: it updates the code and restarts, and never replaces an existing .env.
set -euo pipefail

REPO=https://github.com/wilymanhoward/kl-transit-pulse.git
DIR="$HOME/kl-transit-pulse"

echo "== 1/5 Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER" # takes effect at next login; this script uses sudo meanwhile

echo "== 2/5 Code"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

echo "== 3/5 Config"
if [ ! -f .env ]; then
  cp .env.example .env
  pw=$(openssl rand -hex 24)
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$pw/; s#transit:transit@#transit:$pw@#g" .env
  chmod 600 .env # the password is readable by this user only
  echo "   created .env with a random database password"
else
  echo "   keeping existing .env"
fi

echo "== 4/5 Start (migrations run first, automatically)"
sudo docker compose up -d --build

echo "== 5/5 Nightly backup at 03:15 (keeps 7 rolling daily dumps in ~/backups)"
mkdir -p "$HOME/backups"
job="15 3 * * * cd $DIR && sudo docker compose exec -T db pg_dump -U transit -Fc transit > $HOME/backups/transit-\$(date +\\%a).dump"
( crontab -l 2>/dev/null | grep -v 'pg_dump -U transit' ; echo "$job" ) | crontab -

echo "== Waiting for the API..."
for _ in $(seq 1 30); do
  if curl -fsS 127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
  sleep 5
done
curl -sS 127.0.0.1:8000/health || true
echo
echo "Done. The API listens on 127.0.0.1:8000 (not public). From your PC:"
echo "  ssh -i ~/.ssh/oracle_kl_transit -L 8000:127.0.0.1:8000 ubuntu@<server-ip>"
echo "then open http://127.0.0.1:8000/docs"
