# API

## Hub (port 8484)

When `auth.password` is set, every endpoint except the login and static icons needs a signed-in session cookie. Without one, API calls return `401` and pages redirect to `/login`.

| Method | Path | Does |
|---|---|---|
| `GET` | `/` | Dashboard (`/?demo` shows simulated data) |
| `GET` | `/login` | Login page |
| `POST` | `/api/login` | `{"username", "password"}` → sets the session cookie |
| `GET` | `/logout` | Signs out |
| `GET` | `/api/state` | Everything the dashboard shows: every device's metrics, history, containers, Pi-hole, Ollama and power state |
| `POST` | `/api/devices/{id}/containers/{name}/{action}` | `start`, `stop` or `restart` a container (device must be `docker: control`) |
| `GET` | `/api/devices/{id}/containers/{name}/logs?tail=300` | Container logs as plain text |
| `POST` | `/api/devices/{id}/power/{action}` | `on` (Wake-on-LAN), `reboot` or `shutdown` |
| `POST` | `/api/devices/{id}/ollama/{action}` | `load` or `unload`, body `{"model": "llama3.1:8b"}` |
| `POST` | `/api/chat` | `{"model", "messages": [{role, content}]}` → streams Ollama's reply as NDJSON, with live status included |
| `GET` | `/api/summary/send` | Posts a daily summary to Discord now (for testing) |
| `GET` | `/manifest.webmanifest` | Web app manifest for "Add to Home Screen" |

## Agent (port 9101)

Every endpoint except `/health` needs the header `Authorization: Bearer <token>`.

| Method | Path | Does |
|---|---|---|
| `GET` | `/health` | `{"ok": true}` |
| `GET` | `/metrics` | CPU, memory, disk, temperatures, GPUs, network card, Wake-on-LAN status, and whether power and Ollama are available |
| `GET` | `/containers` | Containers with status, health, CPU and memory |
| `POST` | `/containers/{name}/{action}` | `start`, `stop`, `restart` (only in `control` mode) |
| `GET` | `/containers/{name}/logs?tail=200` | Logs as plain text |
| `POST` | `/power/{action}` | `reboot` or `shutdown` (unless `NEXUSLAB_POWER=off`) |
| `GET` | `/ollama` | Ollama version, installed models and loaded models |
| `POST` | `/ollama/{action}` | `load` or `unload`, body `{"model": "..."}` |
| `POST` | `/ollama/chat` | Passes a chat request to Ollama and streams the reply |

### Example

```bash
TOKEN=$(sudo grep TOKEN /etc/nexuslab-agent.env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:9101/metrics | python3 -m json.tool
```
