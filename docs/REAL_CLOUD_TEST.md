# Real cloud test: platform on your laptop, agents on cloud machines

```
 your laptop (platform + dashboard)  <---- HTTPS (outbound from the agents only) ----  EC2 in us-east-1  (agent "virginia")
        ^                                                                            \- EC2 in us-west-2  (agent "oregon")
   browser: dashboard
```
The agents only make **outbound** calls. Only the platform must be reachable from the internet.

## 0. What you can and cannot claim afterwards
- Proven: a decision made by your engine really started containers on machines in different cloud regions, through agents.
- Not proven: measured energy (VMs usually give only estimates) or a real-world saving. Savings are computed from grid data and an assumed power draw.

## 1. Check your sandbox first (5 minutes)
Rules differ per sandbox. For AWS Academy-style Learner Labs the usual limits are: only us-east-1 and us-west-2,
small instance sizes (nano to large), 4-hour sessions, and a fixed credit budget. Read your own lab page, then confirm you can:
1. launch an EC2 instance in **both** regions,
2. reach the internet from the instance (outbound HTTPS),
3. connect to it (SSH, or the browser terminal / Session Manager).

## 2. Prepare data for the two sandbox regions
1. `python data/make_regions_aws.py` creates `data/regions_aws.csv` (virginia, oregon) from your regions dataset.
2. Find the correct **Electricity Maps zone** for each location on the data portal and write it in the `grid_zone` column (it says CHECK_ME on purpose). US grids are split into several zones, so verify them.
3. Build `data/carbon_aws.csv` the same way as `carbon.csv` (columns `timestamp_utc, region, gco2_per_kwh`) with regions named `virginia` and `oregon`, 48 hours each.

## 3. Start the platform with keys (Command Prompt, backend folder)
```
set ADMIN_KEY=choose-a-long-random-string
set DEMO_API_KEY=choose-another-long-random-string
set REGIONS_PATH=..\data\regions_aws.csv
set CARBON_PATH=..\data\carbon_aws.csv
set DEFAULT_REGION=virginia
uvicorn main:app --host 127.0.0.1 --port 8001
```
Never leave the keys empty when the platform is reachable from the internet.

## 4. Make the platform reachable (pick one)
- **Tunnel (easiest):** run a tunnel tool in a second terminal, for example `cloudflared tunnel --url http://localhost:8001` or `ngrok http 8001`. It prints an `https://...` address. Check the tool's current instructions. Stop it when you finish.
- **Or** run the platform on a third small VM and open port 8001 in its security group.

## 5. Enroll one agent per region (PowerShell)
```
$h = @{ "X-Admin-Key" = "YOUR_ADMIN_KEY"; "Content-Type" = "application/json" }
Invoke-RestMethod -Method Post -Uri https://YOUR-PLATFORM-URL/admin/agents -Headers $h -Body '{"region":"virginia"}'
Invoke-RestMethod -Method Post -Uri https://YOUR-PLATFORM-URL/admin/agents -Headers $h -Body '{"region":"oregon"}'
```
Each call prints a **token once**. Copy it somewhere private (not the repo, not chat). Running the call again replaces the token.

## 6. Launch one EC2 instance per region
- Ubuntu LTS image, `t3.micro` or `t3.small` (whichever your lab allows), default storage.
- Security group: **no inbound rules** except SSH from your own IP (or use Session Manager instead). The agent needs no inbound access.
- Key pairs: a lab's default key may exist only in one region; create a new key pair in the other region and download it.
- Name them `greenops-virginia` and `greenops-oregon` so you remember what to delete.

## 7. Set up each VM (connect, then run)
```
sudo apt-get update && sudo apt-get install -y docker.io python3
sudo usermod -aG docker $USER      # then log out and back in
```
Copy the code from your laptop (works with a private repo, no tokens needed):
```
scp -i your-key.pem -r agent job ubuntu@VM-PUBLIC-IP:~/
```
Build the test image **on the VM** (the agent never pulls images):
```
cd ~/job && docker build -t demo-job:1 .
```
Check the connection (token only in this one command, never in a file in the repo):
```
cd ~/agent
PLATFORM_URL=https://YOUR-PLATFORM-URL AGENT_TOKEN=THE-TOKEN python3 agent.py --check
```
You want: `OK platform reachable, this agent serves region: virginia` and `OK docker server ...`.

Run it in the background (simple version):
```
PLATFORM_URL=https://YOUR-PLATFORM-URL AGENT_TOKEN=THE-TOKEN nohup python3 agent.py > agent.log 2>&1 &
```
For a version that restarts itself, use systemd:
```
# /etc/greenops-agent.env   (then: sudo chmod 600 /etc/greenops-agent.env)
PLATFORM_URL=https://YOUR-PLATFORM-URL
AGENT_TOKEN=THE-TOKEN
ALLOWED_IMAGES=demo-job:1

# /etc/systemd/system/greenops-agent.service
[Unit]
Description=GreenOps agent
After=network-online.target docker.service
Wants=network-online.target
[Service]
User=ubuntu
EnvironmentFile=/etc/greenops-agent.env
ExecStart=/usr/bin/python3 /home/ubuntu/agent/agent.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target

sudo systemctl daemon-reload && sudo systemctl enable --now greenops-agent
journalctl -u greenops-agent -f
```

## 8. Run the test (laptop, backend folder)
1. Open the dashboard: the header should say `Agents: oregon online | virginia online`.
2. Run the full check:
```
set GREENOPS_KEY=YOUR_DEMO_API_KEY
python smoke_test.py https://YOUR-PLATFORM-URL --agents
```
It resets the demo, loads the 4 samples, fast-forwards the clock, waits for the containers and prints which machine ran each job. `SMOKE TEST PASSED` means decision -> agent -> container -> report works end to end.
3. Look at one job's log on the dashboard (`/tasks/ID/log`): the first line names the VM that ran it.

## 9. Clean up (do this every time)
1. Revoke the agents: `Invoke-RestMethod -Method Delete -Uri https://YOUR-PLATFORM-URL/admin/agents/virginia -Headers $h` (and oregon).
2. Terminate both EC2 instances. Delete extra security groups and key pairs.
3. Stop the tunnel and the platform.
4. End the lab session and check the budget page.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| `--check` says cannot reach | Wrong URL, tunnel stopped, or the VM has no outbound internet |
| `--check` says 401 | Wrong or replaced token (enrolling again replaces the old one) |
| Job `Failed`, log says image not found | Image not built on **that** VM, or not in ALLOWED_IMAGES |
| Job `Failed`, log says permission denied | The agent's user is not in the docker group (log out/in or restart the service) |
| Tasks stay `Scheduled` after the start time | The region's agent is offline: tasks wait on purpose instead of running elsewhere |
| Task stays `Queued` | The agent is online but not collecting: check its log |
| Page shows "API key missing or wrong" | Open the page with `?key=YOUR_DEMO_API_KEY` |
| Page cannot reach the API from GitHub Pages | Use an https tunnel address; set `ALLOWED_ORIGINS` to your Pages address |

# Running the website from GitHub (GitHub Pages)
GitHub Pages hosts **static files only**, so it can host the dashboard (`frontend/`) but not the Python backend.
1. Make sure `frontend/chart.umd.js` is committed.
2. Repo Settings > Pages > Source: **GitHub Actions**. The workflow `.github/workflows/deploy-frontend.yml` publishes `frontend/` whenever it changes on `main`.
3. Pages is free for **public** repos; a private repo needs a paid GitHub plan, and the published site is public either way.
4. Open: `https://YOUR-USER.github.io/YOUR-REPO/?api=https://YOUR-PLATFORM-URL&key=YOUR_DEMO_API_KEY`
5. Restrict the platform to your page: `set ALLOWED_ORIGINS=https://YOUR-USER.github.io`.
6. The key is in the address, so use a throw-away key for tests and change it afterwards. Never commit keys or tokens.
