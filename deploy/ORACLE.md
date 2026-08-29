# Putting the agent on an always-on box (Oracle Cloud Always Free)

Why: the agent is a process. Alpaca executes the orders it sends and Featherless
answers the questions it asks, but neither of them *runs* it. A laptop that
sleeps takes the 15:15 cutoff and the 4 Sep submission flatten down with it.

Time: ~30 minutes for the account, ~10 for the deploy. Cost: nothing, forever.

---

## 1. The account

<https://signup.oraclecloud.com> — pick the home region closest to you and
**leave it alone afterwards**; Always Free resources only exist in the home
region and it cannot be changed later.

A card is required for identity verification. It is not charged. At the end of
the trial Oracle offers to upgrade to Pay As You Go: **decline it.** The Always
Free resources keep running either way.

## 2. The machine

Compute -> Instances -> Create instance.

| field | value |
|---|---|
| Image | Ubuntu 24.04 |
| Shape | **VM.Standard.A1.Flex**, 2 OCPU / 12 GB (Always Free) |
| SSH keys | "Generate a key pair" and **download the private key** |

If A1 capacity is refused - common in busy regions, and it says "Out of host
capacity" - take **VM.Standard.E2.1.Micro** instead. It is also Always Free but
it is 1 OCPU / 1 GB, which is not enough to *build* the image. See the note at
the end.

Save the instance's **public IP**.

## 3. Open the ports you actually need (which is none)

Skip this. Do **not** open 8501 to the internet: the Control tab has no login,
and anyone who found it could switch trading books on the judged account. Reach
the dashboards over an SSH tunnel instead - step 6.

The only inbound port you need is 22, which is open by default.

## 4. Get on the box

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<PUBLIC_IP>
```

Then, on the box:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu && newgrp docker

# Oracle images ship a restrictive iptables that blocks container traffic.
# This is the single most common reason a deploy "works" and answers nothing.
sudo iptables -I INPUT -i docker0 -j ACCEPT
sudo iptables -I FORWARD -i docker0 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || sudo apt install -y iptables-persistent
```

## 5. The code and the keys

```bash
git clone https://github.com/hivan04/alpaca-hackathon.git
cd alpaca-hackathon
```

The keys are **not** in git and must never be. Copy them across from your Mac,
in a second terminal:

```bash
scp -i ~/Downloads/ssh-key-*.key .env ubuntu@<PUBLIC_IP>:~/alpaca-hackathon/.env
```

Then, back on the box, prove the keys reach the right accounts **before**
anything trades:

```bash
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml run --rm agent oaa doctor --profile judged
docker compose -f deploy/docker-compose.yml run --rm agent oaa doctor --profile dev
```

Look for the `account identity` row: it must say the keys open the account the
profile expects. Do not continue past a red one.

## 6. Start it

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f agent
```

Four containers: `agent` (judged), `agent-dev` (backtesting), `control` (the
Streamlit dashboard - Positions and Control tabs), `dashboard` (the public
FastAPI page for the submission).

To see the dashboards, tunnel from your Mac rather than exposing them:

```bash
ssh -i ~/Downloads/ssh-key-*.key -L 8501:localhost:8501 -L 8080:localhost:8080 ubuntu@<PUBLIC_IP>
```

Then open <http://localhost:8501>. The tunnel is only needed while you are
looking; the agent keeps trading when you close it.

**The public FastAPI page is the one the submission links to.** When you are
ready to publish it, open 8080 in the Oracle security list and in `iptables` -
that page is read-only. Never 8501.

## 7. Check it survives a reboot

```bash
sudo reboot
# wait a minute, then
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<PUBLIC_IP> \
  'cd alpaca-hackathon && docker compose -f deploy/docker-compose.yml ps'
```

`restart: unless-stopped` is set on every service, so they should already be
back. If they are not, Docker is not enabled at boot: `sudo systemctl enable docker`.

---

## Day to day

```bash
# what has it been doing
docker compose -f deploy/docker-compose.yml logs --tail 100 agent

# which books are switched on, per account
docker compose -f deploy/docker-compose.yml exec agent oaa switchboard --profile judged

# ship a code change
git pull && docker compose -f deploy/docker-compose.yml up -d --build
```

Switching books on and off does **not** need any of this - that is the Control
tab, and it takes effect at the agent's next cycle.

## Notes

**Keys.** `.env` lives only on the box and on your Mac. It is gitignored, and
`git clone` on a public repo would expose it if it ever were not.

**Clock.** Every container pins `TZ=America/New_York` because every firewall
boundary is an ET time. A box defaulting to UTC fires the 15:15 cutoff at 11:15.

**If you ended up on the 1 GB E2.1.Micro**, the image build will run out of
memory. Add swap first:

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

and consider running only `agent` and `agent-dev` there, keeping the dashboard
on your Mac pointed at the same `runs/` over `scp`. The trading loop is the part
that has to be always-on; the dashboard is only a viewer.

**Do not run the agent in two places at once.** Two loops on the same account
will both scan, both size against the same equity and both submit. If the box is
running, stop the local one.
