# Installation

This guide sets up NexusLab from scratch. The examples use four machines. Use as many or as few as you like.

| Example machine | Role | IP used below |
|---|---|---|
| Home server | runs the **hub** and an agent, controls its containers | `192.168.1.10` |
| AI PC | agent with NVIDIA GPU and Ollama | `192.168.1.11` |
| Raspberry Pi | agent plus Pi-hole | `192.168.1.12` |
| Mini PC | agent, can be started remotely | `192.168.1.13` |

## Before you start

- **Linux with systemd** on every machine (Ubuntu, Debian, Raspberry Pi OS, Kali, Arch and Fedora all work).
- **Docker with Compose** on the machine that will run the hub. Check with `docker compose version`.
- **A fixed IP for each machine.** Set a DHCP reservation in your router so addresses never change. Find each machine's IP with `hostname -I`; the first address is usually the one you want.
- **For NVIDIA GPUs:** a working driver. If `nvidia-smi` shows your card, NexusLab can read it.

## 1. Get the code onto each machine

```bash
git clone https://github.com/YOUR_USERNAME/nexuslab.git
```

Or download the ZIP from GitHub (Code > Download ZIP) and unpack it with `python3 -m zipfile -e nexuslab-main.zip ~/`.

## 2. Install the agent on every machine

```bash
cd nexuslab/agent
chmod +x install-agent.sh
sudo DOCKER_MODE=control ./install-agent.sh
```

Choose `DOCKER_MODE` per machine:

| Value | What the dashboard can do with this machine's containers |
|---|---|
| `off` | nothing (default; use for machines without Docker) |
| `monitor` | see status, CPU, memory and logs |
| `control` | all of the above, plus start, stop and restart |

The installer:
- creates `/opt/nexuslab-agent` with its own Python environment,
- writes settings to `/etc/nexuslab-agent.env` (kept on later re-installs),
- starts the `nexuslab-agent` service on port 9101,
- prints a **token**. Copy it; the hub needs it.

Lost a token? `sudo grep TOKEN /etc/nexuslab-agent.env`

**Test it** on the same machine:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:9101/metrics
```

You should get a line of JSON with CPU, RAM and disk figures. On a GPU machine, `"gpus"` lists your card.

**Optional:** to show only some containers (and hide the hub's own), edit `/etc/nexuslab-agent.env`:

```bash
NEXUSLAB_CONTAINERS=jellyfin,portainer,uptime-kuma
```

Then run `sudo systemctl restart nexuslab-agent`.

## 3. Check the hub machine can reach every agent

On the hub machine:

```bash
curl http://192.168.1.11:9101/health
curl http://192.168.1.12:9101/health
curl http://192.168.1.13:9101/health
```

Each should print `{"ok":true}`. If one hangs, a firewall on that machine is blocking port 9101 (for example `sudo ufw allow 9101/tcp`).

## 4. Configure and start the hub

```bash
cd nexuslab/hub
cp config.example.yaml config.yaml
nano config.yaml
```

For each machine, set `url` (its IP with `:9101`) and `token`. Set `auth.password` to protect the dashboard. Every option is explained in [CONFIGURATION.md](CONFIGURATION.md).

```bash
docker compose up -d --build
docker logs nexuslab
```

The log should show `Login on` and `Uvicorn running on http://0.0.0.0:8484`.

Open **`http://192.168.1.10:8484`** and sign in with `admin` and your password.

The hub uses host networking so it can reach an agent on its own machine and send Wake-on-LAN packets. If port 8484 is taken, change `--port 8484` in `docker-compose.yml`.

## 5. Pi-hole (optional)

Add a `pihole:` block under the Pi's device in `config.yaml`:

```yaml
    pihole:
      url: http://192.168.1.12
      version: 6
      password: "your Pi-hole web password"
```

For Pi-hole v6 you can use an **app password** instead (Settings > Web interface / API, in Expert mode). For v5, set `version: 5` and `api_token` (Settings > API > Show API token).

Restart the hub after any config change: `docker compose restart`.

## 6. Discord alerts and daily summary (optional)

1. In Discord, open a channel's settings, then **Integrations > Webhooks > New Webhook**, and copy the URL.
2. Add it to `config.yaml`:

   ```yaml
   alerts:
     discord_webhook: "https://discord.com/api/webhooks/..."
     timezone: America/Los_Angeles
   ```

3. `docker compose restart`. A "NexusLab is running" message confirms it works.

To see a daily summary right away, open `http://<hub-ip>:8484/api/summary/send` while signed in.

Treat the webhook URL like a password. Anyone with it can post in your channel.

## 7. Ollama and the Chat tab (optional)

The agent finds Ollama automatically at `http://127.0.0.1:11434` on its own machine. Check it's reachable:

```bash
curl http://localhost:11434/api/version
```

If Ollama runs in Docker, publish its port (`-p 11434:11434`). If it listens elsewhere, set `NEXUSLAB_OLLAMA_URL` in `/etc/nexuslab-agent.env` and restart the agent.

The Ollama panel then appears on that machine's tab, and **Chat** at the top of the dashboard uses its models. Set `chat.owner` in `config.yaml` to your name so the assistant uses it.

## 8. Start machines remotely (Wake-on-LAN)

The **Start** button wakes a machine that was shut down. It needs:

1. **A wired Ethernet connection.** Wi-Fi cards usually power off when the PC is off.
2. **`ethtool`** on the machine: `sudo apt install ethtool` (or `pacman -S ethtool`, `dnf install ethtool`). The agent uses it to switch Wake-on-LAN on at every boot.
3. **Wake-on-LAN enabled in the BIOS.** Look for "Wake on LAN", "Power On by PCI-E" or "Resume by LAN", and disable "ErP" if present.

Re-run `install-agent.sh` after installing `ethtool`. When everything is set up, the machine's tab shows **Wake-on-LAN ready** next to its name. Test it with Shut down, wait for **Off**, then Start.

A Raspberry Pi can't be woken this way, so its Start button stays greyed out. The hub's own machine can't start itself either, so keep that one running.

## 9. Add it to your phone

**iPhone:** open the dashboard in **Safari**, tap Share, then **Add to Home Screen**.
**Android:** open it in Chrome, then use the menu and **Install app** or **Add to Home screen**.

The app keeps its own login, so sign in once inside it. To use it away from home, install [Tailscale](https://tailscale.com) on the hub machine and your phone, and open the hub's Tailscale address (`http://100.x.y.z:8484`).

## Updating

```bash
cd nexuslab && git pull
cd agent && sudo ./install-agent.sh            # on every machine
cd ../hub && docker compose up -d --build      # on the hub machine
```

Your `config.yaml`, agent tokens and the hub's `data/` folder are kept.

## Uninstalling an agent

```bash
sudo systemctl disable --now nexuslab-agent
sudo rm -rf /opt/nexuslab-agent /etc/nexuslab-agent.env /etc/systemd/system/nexuslab-agent.service
sudo systemctl daemon-reload
```
