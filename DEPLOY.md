# Deploying the harvester on the VM (145.100.135.123)

The app is a single FastAPI process. Harvest jobs run in **background threads inside
that one process** and their state lives in memory, so it must run as
**one uvicorn worker** (`--workers 1`). More than one worker breaks job polling.

Layout on the VM: `~/harvester` (code) + `~/harvester/.venv` + `~/harvester/.env`.
It listens on `0.0.0.0:8000`; port 8000 is already open (the old version used it).

---

## 1. Push the updated code from your laptop

`.env` on the VM holds real secrets — the `--exclude .env` keeps it untouched.

```bash
rsync -avz -e "ssh -i ~/.ssh/id_rsa" \
  --exclude '.venv' --exclude '.git' --exclude '.env' \
  --exclude 'jsonld' --exclude 'jsonld_filtered' --exclude '__pycache__' \
  --exclude '*.zip' --exclude '.DS_Store' --exclude '.idea' \
  ./ nafiseh@145.100.135.123:~/harvester/
```

Add `--delete` if you also want files removed on the VM when they're gone from
your laptop (a clean mirror). Safe with the excludes above, but double-check you
don't keep VM-only scripts in `~/harvester`.

(Alternative, if you prefer git: `git add -A && git commit && git push`, then
`git pull` on the VM. Pick one method and stick to it — don't mix rsync and pull.)

## 2. On the VM: update deps and restart

```bash
ssh -i ~/.ssh/id_rsa nafiseh@145.100.135.123
```

```bash
cd ~/harvester
python3 --version                      # expect 3.10+
python3 -m venv .venv                   # no-op if it already exists
source .venv/bin/activate
pip install -r requirements.txt         # new: owslib (CSW), python-multipart
```

Check `.env` has the LLM service pointed at localhost (the service runs on this VM):

```bash
grep LLM_HARVESTER .env
# LLM_HARVESTER_BASE_URL=http://127.0.0.1:8010
# LLM_HARVESTER_API_KEY=<your key>
# LLM_HARVESTER_MODEL=surf-default-text-large
```

Sanity check it imports:

```bash
python -c "import api; print('ok')"
```

## 3. Run it so it survives logout

Foreground `uvicorn ...` dies when you close SSH. Use **one** of:

### A. systemd (recommended)

```bash
sudo cp ~/harvester/deploy/harvester.service /etc/systemd/system/harvester.service
sudo systemctl daemon-reload
sudo systemctl enable --now harvester
systemctl status harvester --no-pager
journalctl -u harvester -f          # live logs; Ctrl-C to stop tailing
```

To ship a new version after step 1+2:
```bash
sudo systemctl restart harvester
```

### B. tmux (quick, manual)

```bash
tmux new -s harvester
cd ~/harvester && source .venv/bin/activate
uvicorn api:app --host 0.0.0.0 --port 8000
# detach: Ctrl-b then d      reattach later: tmux attach -t harvester
```

## 4. Verify from the internet

Open **http://145.100.135.123:8000/** in a browser, or:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://145.100.135.123:8000/
```

Pick a source (e.g. Zenodo), set Max records = 20, Run. Then try **Enrich with LLM**
(needs the LLM service up on `127.0.0.1:8010` — see its own start steps).

## 5. Public domain + HTTPS  (https://lter-life-harvester.qcdis.org)

### 5a. DNS — free subdomain of qcdis.org

Ask whoever manages **qcdis.org** DNS (the people running
`lter-life-catalogue.qcdis.org`) to add one record:

| Name | Type | Value | TTL |
|---|---|---|---|
| `lter-life-harvester.qcdis.org` | A | `145.100.135.123` | 3600 |

Cost: €0, nothing to renew. Wait until it resolves:
```bash
dig +short lter-life-harvester.qcdis.org      # -> 145.100.135.123
```

(Fallback if you can't get qcdis.org access: make a free `*.duckdns.org` name
yourself at https://www.duckdns.org and point it at `145.100.135.123`, then use
that name everywhere below instead.)

### 5b. Check the VM isn't already using 80/443 for something else

Other services on this VM are fine — nginx multiplexes by domain — but only if
**nginx** owns 80/443. Confirm nothing else (Apache, another container) has them:

```bash
sudo ss -ltnp | grep -E ':80 |:443 '
```

- Only `nginx` (or nothing) → good, proceed.
- Something else bound directly to 80/443 → that has to move behind nginx too, or
  onto another port, before continuing.

### 5c. nginx vhost + Let's Encrypt (on the VM)

```bash
sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp ~/harvester/deploy/nginx-harvester.conf /etc/nginx/sites-available/harvester
sudo ln -s /etc/nginx/sites-available/harvester /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx        # do NOT remove other sites-enabled/* vhosts
```

Open 80/443 (ufw **and** the SURF dashboard security group):

```bash
sudo ufw allow 'Nginx Full'
```

Get the certificate (needs 5a done and port 80 reachable from outside):

```bash
sudo certbot --nginx -d lter-life-harvester.qcdis.org --redirect -m <your-email> --agree-tos
```

certbot edits only this vhost to add the `443` block + auto-renew timer; other
vhosts are untouched.

### 5d. Take the app off the public port

Now only nginx should face the internet. Bind uvicorn to localhost and add
proxy-header handling:

```bash
sudo sed -i 's#--host 0.0.0.0#--host 127.0.0.1 --proxy-headers --forwarded-allow-ips=*#' \
  /etc/systemd/system/harvester.service
sudo systemctl daemon-reload && sudo systemctl restart harvester
sudo ufw delete allow 8000/tcp        # and remove 8000 from the SURF security group
```

Verify: **https://lter-life-harvester.qcdis.org** loads with a valid padlock;
`http://` redirects to `https://`; `http://145.100.135.123:8000` no longer responds.

Renewals are automatic (`systemctl list-timers | grep certbot`).

## Notes

- Keep uvicorn at **one worker** — job state is in-process memory.
- `jsonld/`, `jsonld_records.zip` etc. are regenerated each run and are excluded
  from rsync on purpose.
- The "Enrich with LLM" button needs the separate LLM harvester service running on
  `127.0.0.1:8010`; it is not managed by `harvester.service`.
