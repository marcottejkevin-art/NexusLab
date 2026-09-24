# Troubleshooting

These are the problems most likely to come up, and how to fix each one.

## The hub won't start: "port is already allocated"

Another program is already using the hub's port. Pick a free one in `hub/docker-compose.yml` (`--port 8484`), then `docker compose up -d`.

## The hub's own machine shows "Offline: Timed out"

The hub can't reach the agent on its own machine. This happens when the hub runs on Docker's default bridge network and the host firewall blocks it. The included `docker-compose.yml` uses `network_mode: host`, which avoids this. If you changed that, change it back.

## A machine shows "Invalid or missing token"

The token in `config.yaml` doesn't match the agent's. On that machine, run `sudo grep TOKEN /etc/nexuslab-agent.env`, copy the value exactly, then `docker compose restart` on the hub.

## A machine shows "Connection refused or host unreachable"

- Is it on? Check with `ping <ip>`.
- Is the agent running? Run `sudo systemctl status nexuslab-agent`, and see the logs with `journalctl -u nexuslab-agent -n 50`.
- Is a firewall blocking port 9101? From the hub machine, `curl http://<ip>:9101/health` should answer.
- Desktop Linux machines sometimes suspend when idle. To keep a server awake: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`

## Pi-hole: "401 Unauthorized"

The Pi-hole password in `config.yaml` is wrong. Check that it works on the Pi-hole web page. If it contains `"` or `\`, wrap it in single quotes: `password: 'my"pass'`. For v6 you can use an app password instead. Restart the hub after changing it.

## CPU temperature shows "n/a"

The agent didn't recognize the machine's sensor. Run `sudo apt install lm-sensors && sensors`, find the CPU's sensor name (like `k10temp` or `coretemp`), set `NEXUSLAB_TEMP_SENSOR=<name>` in `/etc/nexuslab-agent.env`, and restart the agent.

## No GPU section, or "No NVIDIA GPU detected"

Make sure `nvidia-smi` works on that machine and that the device has `gpu: true` in `config.yaml`. The agent reads the GPU through NVML, which comes with the NVIDIA driver.

## Chat: "Couldn't get an answer"

- **"No machine with Ollama is online":** on the Ollama machine, `curl http://localhost:11434/api/version` must answer. If Ollama runs in Docker, publish port 11434. Otherwise set `NEXUSLAB_OLLAMA_URL`.
- **Slow first answer:** the model is loading onto the GPU. Later answers are faster, or use Load in the Ollama panel beforehand.
- **The answer ignores machines:** raise `chat.num_ctx` (for example to 16384) so the whole status fits.
- **"Thinking" models** such as qwen3 or deepseek-r1 pause before answering. Their reasoning is hidden automatically.

## Start doesn't wake a machine ("didn't turn on")

1. It must be on **Ethernet**, not Wi-Fi.
2. Install `ethtool` and re-run `install-agent.sh`. The tab should show **Wake-on-LAN ready** while the machine is on.
3. Enable Wake-on-LAN in the BIOS ("Wake on LAN", "Power On by PCI-E" or "Resume by LAN") and disable "ErP".
4. The machine must have been online at least once since the updated agent was installed, so the hub could learn its MAC address. You can also set `mac:` in `config.yaml`.

If a machine loses power at the wall, Wake-on-LAN can't help. Use a smart plug together with the BIOS setting "Restore on AC Power Loss: Power On".

## Discord messages don't arrive

`docker logs nexuslab` should include `Discord alerts on`. If it says `off`, `alerts.discord_webhook` is missing or doesn't start with `https://`. If it logs `Discord answered 404`, the webhook was deleted, so create a new one.

## The phone app keeps asking me to sign in

A home-screen app keeps its own login, separate from Safari. Sign in once inside the app and it lasts 30 days. Changing `auth.password` signs everyone out.

## Unpacking a download fails with "No such file or directory"

Browsers add ` (1)` to repeated downloads. List the folder with `ls ~/Downloads` and quote the exact name: `python3 -m zipfile -e "nexuslab (1).zip" ~/`. Better still, use `git pull` for updates.
