# Deploying Argus to a QNAP NAS

This guide runs the argus container on a QNAP NAS via **Container Station** (QNAP's
Docker frontend). It assumes the generic Docker flow from the
[README § Deploy with Docker](../README.md#deploy-with-docker) already works, and
adds only the QNAP-specific layer: architecture detection, build strategy,
Container Station compose import, reverse proxy for HTTPS, autostart, and the
troubleshooting cases that actually bite on low-power NAS hardware.

Argus is **stateless** — the Camoufox browser binary is baked into the image at
build time and the service stores nothing on disk — so there are no volumes to
provision and no persistent data to back up. The only state is the bearer token
in an env var.

---

## 1. Before you start: check your NAS can run it

### 1a. CPU architecture — the one hard requirement

The image is `python:3.12-slim` (multi-arch) plus a Camoufox Firefox build that
`camoufox fetch` downloads at **build** time. That fetch will fail loudly if
Camoufox does not ship a build for your NAS CPU, so check first.

Find your arch from the NAS (SSH in, or via Container Station → Info):

```bash
uname -m
```

| `uname -m` | QNAP models | Camoufox support |
|---|---|---|
| `x86_64` | TS-x51/53/64/65/69 series, most `x64`/`x70+`, N-series (N5095…) | ✅ primary target — the provided Dockerfile is tested here |
| `aarch64` | TS-x28A, TS-x31B, TS-x32, some `arm-based` models | ⚠️ requires an `arm64` Camoufox build — verify at https://camoufox.com/ before building |
| `armv7l` | older TS-x31 (Annapurna ARMv7) | ❌ not supported by Camoufox |

> If your NAS is `aarch64`, try the build below — `camoufox fetch` is the
> authority. If it 404s during the build, your arch has no Camoufox build and you
> must run argus on an x86_64 NAS (or a small x86_64 VM/N100 mini-PC) instead.

### 1b. Memory and CPU budget

A resident Camoufox Firefox process tree is **~350–500 MB** while a fetch is in
flight; the browser tears down to nothing after `ARGUS_IDLE_TIMEOUT_SECONDS`
(default 300s) of inactivity. Add ~150 MB for the Python/uvicorn process. So:

- **Minimum**: 2 GB RAM NAS with nothing else running.
- **Recommended**: 4 GB+ RAM, or 2 GB if the NAS is otherwise idle and you keep
  `ARGUS_CONCURRENCY` low (1–2). Each concurrent fetch holds its own
  `BrowserContext` (cheaper than a full browser, but not free).
- **CPU**: any Intel Celeron (J3455/J4125/N5095/…) is fine. ARM cores are slower
  per fetch; expect longer render times and lower concurrency headroom.

### 1c. Storage

The built image is ~1.2–1.5 GB (Firefox binary + GTK/NSS/X11 libs + pruned font
sets). Make sure the Container Station storage pool (usually `/Container` or the
`container-station-*` volume) has ≥3 GB free.

---

## 2. Get the source onto the NAS

Argus ships a `Dockerfile` and `docker-compose.yml` that build from source, so
the repo must be present wherever the image is built. Two paths:

### Path A — build on the NAS (simplest, slower)

```bash
# on the NAS (SSH in)
cd /share/Container            # or wherever you keep app sources
git clone <your-argus-repo-url> argus
cd argus
# generate a strong bearer token now; you'll reuse it
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Path B — build on your dev machine, ship the image (recommended for weak NAS CPUs)

The image build runs `apt-get` + `pip install` + a ~200–900 MB browser
download. On a J3455-class Celeron that is 10–20 min; on ARM it is worse. Build
once on your laptop and load the finished image:

```bash
# on your dev machine (must be the SAME arch as the NAS)
cd ~/Sources/argus
docker build -t argus:latest .

# for aarch64 NAS from an x86_64 laptop, set the platform explicitly:
#   docker build --platform linux/arm64 -t argus:latest --provenance=false .

# ship the image to the NAS
docker save argus:latest | gzip > argus.tar.gz
scp argus.tar.gz howard@nas:/tmp/

# on the NAS
gunzip -c /tmp/argus.tar.gz | docker load
```

> `docker save/load` preserves the image verbatim with the browser binary already
> downloaded — the NAS needs **no internet access at deploy or run time**. This
> is the only path that works on an air-gapped NAS.

---

## 3. Create a compose file for the NAS

The repo's `docker-compose.yml` works as-is, but a QNAP-tuned copy makes
resource limits and autostart explicit. On the NAS create
`/share/Container/argus/docker-compose.yml`:

```yaml
services:
  argus:
    image: argus:latest        # use the image loaded in Path B; for Path A use `build: .`
    container_name: argus
    restart: unless-stopped    # survives NAS reboots — Container Station auto-starts on boot
    volumes:
      - ./.env:/app/.env:ro    # ALL ARGUS_* config flows from here — the single source of truth
    ports:
      - "8000:8000"
    deploy:
      resources:
        limits:
          memory: 2G           # raise to 3-4G if you raise ARGUS_CONCURRENCY above 2
          cpus: "2.0"
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:8000/health >/dev/null || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 60s        # longer on slow NAS CPUs
```

All configuration lives in a sibling `.env` (same dir), **not** inline in the
compose file. The file is bind-mounted read-only at `/app/.env`, where
pydantic-settings reads it at startup (`src/argus/config.py`); anything you
leave out falls back to the app's built-in defaults. Create it **before** the
first `up` — a missing mount source makes Docker create an empty *directory*
at that path, which crashes the app with a confusing error:

```bash
# /share/Container/argus/.env  (chmod 600; never commit)
cp .env.example .env
# then edit .env — the two lines that matter on a NAS:
#   ARGUS_API_TOKENS=<the secrets.token_urlsafe(32) output from step 2>
#   ARGUS_CONCURRENCY=1              # low-RAM NAS: one fetch at a time
#   ARGUS_IDLE_TIMEOUT_SECONDS=120   # release the browser's ~500 MB sooner than the 300 default
# AI fallback stays off unless you set all three ARGUS_AI_* vars.
```

> **Why a bind-mount and not an `environment:` block or `env_file`?** One
> mechanism, three benefits: the bearer token stays out of `docker inspect`
> (env-injected vars don't), config edits need only `docker compose restart`
> (no recreate — env vars are frozen at container create), and `.env` stays the
> single source of truth with no interpolation precedence surprises. See the
> [README compose section](../README.md#docker-compose-recommended) for the
> same pattern.

See the [README config table](../README.md#configuration-env-vars) for every
`ARGUS_*` var.

---

## 4. Deploy via Container Station

### 4a. Container Station 3 (current) — import the compose file

1. Open **Container Station** (App Center → Container Station).
2. **Applications** → **Create** → switch to the **YAML** editor.
3. Paste the `docker-compose.yml` from §3. Set the **Application name** to `argus`.
4. Click **Create**. Container Station pulls/uses the `argus:latest` image and
   starts the container.
5. Watch the **Logs** tab — on first boot the browser is absent
   (`/health` returns `{"browser":"absent"}`). That is correct: the browser
   launches lazily on the first `/v1/fetch`.

### 4b. Plain docker CLI (if you prefer SSH)

```bash
cd /share/Container/argus
docker compose up -d        # reads .env via the bind mount — no extra flags needed
docker compose logs -f argus
# after editing .env:
docker compose restart argus
```

One Container Station caveat: when deploying via the CS UI (**4a**), the YAML
editor may not pick up the sibling `.env`. If `/v1/*` calls reject with 403,
deploy this application over SSH instead (`docker compose up -d`), or paste the
resolved env values into CS's per-application environment settings.

### 4c. Verify

```bash
# from any LAN host
TOKEN=$(grep ARGUS_API_TOKENS /share/Container/argus/.env | cut -d= -f2)
curl -s http://<nas-ip>:8000/health
# -> {"status":"ok","browser":"absent"}

curl -s -X POST http://<nas-ip>:8000/v1/fetch \
  -H "content-type: application/json" \
  -H "authorization: Bearer $TOKEN" \
  -d '{"url":"https://example.com"}' | head -c 200
# -> {"ok":true,"html":"<!DOCTYPE html>...","url":"https://example.com/"}
# (first fetch takes 5–15s as the browser launches)
```

---

## 5. HTTPS / remote access (optional but recommended)

Bare bearer tokens over plain HTTP are fine on a trusted LAN. If callers reach
argus from outside the LAN, terminate TLS in front of it. Two QNAP-native ways:

### 5a. QNAP Reverse Proxy (no extra container)

**Control Panel → Applications → Reverse Proxy** → Create:

| Field | Value |
|---|---|
| Source protocol | `HTTPS` |
| Source hostname | your NAS hostname / DDNS |
| Source port | a free port, e.g. `18443` |
| Destination protocol | `HTTP` |
| Destination hostname | `127.0.0.1` (or the container's bridge IP) |
| Destination port | `8000` |

Use the NAS's built-in Let's Encrypt cert (Control Panel → Security &
Certificates → Certificate). Callers then hit `https://nas.example.com:18443`.

### 5b. Caddy in a sidecar container

If you want automatic HTTPS with a custom domain via Caddy, run a second
container on the same Docker network:

```yaml
services:
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on: [argus]

volumes:
  caddy_data:
```

```caddyfile
argus.example.com {
    reverse_proxy argus:8000
}
```

Put both services in the same compose file so `argus` resolves by container name.

---

## 6. Autostart and restart behavior

`restart: unless-stopped` is all you need. Container Station honors Docker's
restart policy: on NAS reboot or a container crash, argus comes back up. A manual
`docker stop argus` clears the "auto-restart" intent until you explicitly start
it again — that's the `unless-stopped` semantics.

To disable autostart without deleting the container: stop it from the
Container Station UI (not `docker kill`, which the policy treats as a crash).

---

## 7. Updating

### If you built on the NAS (Path A)

```bash
cd /share/Container/argus
git pull
docker compose build --pull
docker compose up -d
```

### If you loaded an image (Path B)

```bash
# dev machine
docker build -t argus:latest .
docker save argus:latest | gzip > argus.tar.gz
scp argus.tar.gz howard@nas:/tmp/

# NAS
gunzip -c /tmp/argus.tar.gz | docker load
docker compose -f /share/Container/argus/docker-compose.yml up -d   # recreate picks up new image
```

Old containers/images: `docker image prune -f` to reclaim the ~1.5 GB.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build fails at `camoufox fetch` (404 / no matching build) | No Camoufox build for your CPU arch | Confirm `uname -m`; switch to an x86_64 NAS or an x86_64 side host. See §1a. |
| Container starts, `/health` ok, but every `/v1/fetch` returns `{ok:false,reason:"fetch_failed"}` | Browser launch failing — usually OOM or missing GL libs | Check `docker logs argus` for the launch error; raise the `memory:` limit; verify the image wasn't stripped of `libgbm1`/`libnss3` (use the provided Dockerfile unmodified). |
| Container exits with code 137 (OOMKilled) | NAS ran out of RAM under concurrency | Lower `ARGUS_CONCURRENCY` to 1; raise the `memory:` limit; check what else is running on the NAS. |
| `wget` healthcheck fails forever, `start_period` too short | Slow NAS CPU launching the browser | It's normal for `/health` to pass immediately (it doesn't launch the browser); if it fails, the container itself isn't up — check `docker logs`. |
| First fetch after boot is slow (5–15s) | Lazy browser launch — by design | Expected. Subsequent fetches within the idle window reuse the warm browser. |
| Antidetect sites still 403 | `ARGUS_DEFAULT_FINGERPRINT_OS` set to something other than `linux` | Keep it `linux`: the image prunes macos/windows fonts, so a non-linux fingerprint that names missing fonts is itself a tell. |
| `curl: (56) Recv failure` from outside the LAN | Port 8000 not exposed or firewall blocking | Confirm the port mapping in §3 and the NAS firewall (Network & File Services → Telnet/SSH/HTTP). Use the reverse proxy in §5 for external access. |
| Logs show `Address already in use` | Another service (or the old argus) holds 8000 | `docker ps -a \| grep 8000`; stop the conflict or remap `"8001:8000"`. |

### Reading the logs

```bash
docker logs -f argus                        # follow
docker logs --since 30m argus | grep -i err # last 30 min, errors only
```

Container Station 3 also surfaces the same output in **Containers → argus →
Logs**.

---

## 9. Quick reference — the whole flow

```bash
# 1. on the NAS: check arch + grab source
uname -m                                      # expect x86_64 (or aarch64, see §1a)
cd /share/Container && git clone <repo> argus && cd argus

# 2. bearer token into .env
printf 'ARGUS_API_TOKENS=%s\n' \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
chmod 600 .env

# 3. drop in the QNAP compose from §3 (edit image: build: . for on-NAS build)
# 4. Container Station → Applications → Create from YAML
# 5. verify
curl http://localhost:8000/health
```
