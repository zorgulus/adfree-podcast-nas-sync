# adfree-podcast-nas-sync

A small pipeline that sits on top of [MinusPod](https://github.com/ttlequals0/MinusPod)
(self-hosted ad removal for podcasts) and:

- merges multi-part episodes (e.g. a show split into "Part 1/2", "Part 2/2")
  into a single file,
- publishes a spec-compliant RSS feed and a self-contained HTML web player to
  your own NAS / home server,
- optionally remembers playback position and "listened" state **across every
  device** you use (phone, PC, tablet...), via a tiny companion API.

**MinusPod does the actual ad detection and removal.** This repo does not
touch that logic - it's a processing→storage handoff and a couple of quality
fixes discovered the hard way (see "Known gotchas" below).

## Why split processing and storage?

The intended setup is: a beefier machine (a always-on PC, a home server) runs
MinusPod and does the heavy lifting (Whisper transcription, LLM ad detection),
then hands the finished files off to a NAS or always-on server that's
responsible for actually serving them to your devices. Two machines doing two
different jobs, so the processing box doesn't need to also be your reliable
storage/serving layer.

```
┌─────────────────┐    LAN (never over VPN/Tailscale -    ┌──────────────────┐
│  processing PC   │    too slow for bulk file transfer)   │   NAS / server   │
│  - MinusPod       │ ─────────────────────────────────►   │  - RSS feed      │
│  - merge_multipart│         scp                            │  - web player    │
│  - sync_to_nas    │                                        │  - podcasts-api  │
└─────────────────┘                                        └──────────────────┘
                                                                     │
                                                          Tailscale HTTPS (or
                                                          any reverse proxy)
                                                                     │
                                                              your devices
```

## Setup

1. **Run MinusPod** via `docker-compose.cpu.yml` (CPU-only; adjust if you have
   a GPU worth using for Whisper). Copy `.env.example` to `.env` and fill it
   in - see "Choosing an LLM" below for the ad-detection model.
2. **Add your podcast(s)** to MinusPod (its own UI/API), set `maxEpisodes` to
   however much of the back-catalog you actually want processed (MinusPod
   defaults can pull way more than you need).
3. **Set up your NAS**: a static file server (nginx or similar) serving
   `NAS_PODCASTS_PATH` as static files, reachable at `PUBLIC_BASE_ROOT`
   (a Tailscale HTTPS hostname is a good, free, zero-config option - see
   "Security" below).
4. **(Optional) Deploy `podcasts-api/server.py`** on the NAS for cross-device
   resume - it's a single-file, dependency-free Python HTTP server. Point your
   web server to reverse-proxy `PUBLIC_BASE_ROOT/api/` to it. If you skip
   this, the web player still works, it just won't remember playback position
   across devices.
5. **Run the pipeline**: `merge_multipart.py` then `sync_to_nas.py`. Schedule
   both (`nightly_start.ps1` / `nightly_sync.ps1` are a Windows Task Scheduler
   example - adapt for cron/systemd on Linux/macOS).

### New to Docker/NAS setups? Use Claude Code

If you're not comfortable with Docker, SSH, or editing `.env` files by hand,
you can point [Claude Code](https://claude.com/claude-code) at this repo and
ask it to walk you through the setup on your own machine and NAS - reading
the `.env.example` comments, wiring up the reverse proxy, and debugging
whatever your specific NAS's quirks turn out to be. This project was in fact
built and iterated on entirely that way.

## Choosing an LLM

Ad detection is just a text-classification call to any OpenAI-compatible
endpoint - it doesn't need a huge model, and running it locally (via
[LM Studio](https://lmstudio.ai) or [Ollama](https://ollama.com)) means zero
per-episode API cost.

| Hardware | Suggested model |
|---|---|
| Limited (≤8GB VRAM, or CPU-only) | A ~7-8B instruct model (e.g. Qwen2.5-7B-Instruct) |
| Comfortable (12-24GB VRAM) | `qwen/qwen3-30b-a3b-2507` (what this project uses - a mixture-of-experts model, ~3B active params, so it's fast even though it's nominally "30B") |
| Plenty of VRAM to spare | A larger general instruct model of your choice |

Avoid code-specialized models (e.g. Qwen3-Coder) for this - ad detection is a
plain-language classification task, and a code-tuned model measurably
under-performs a general instruct model here.

## Security

`PUBLIC_BASE_ROOT` should point at something only *your* devices can reach -
a [Tailscale](https://tailscale.com) HTTPS hostname (`tailscale serve`) is a
good default: no port forwarding, no public exposure, free for personal use.

**Trade-off to know about**: some podcast apps validate a feed URL from
*their own servers*, not from your device - and a Tailscale-only URL is by
definition unreachable from outside your tailnet. We hit this with one
mainstream app (confirmed via [castfeedvalidator.com](https://castfeedvalidator.com),
which the app uses internally - it returned "Invalid URL" without ever
reading the feed content). If your podcast app does the same, the bundled
web player (`index.html`, generated by `sync_to_nas.py`) is the fallback -
it's just a page your own browser opens directly, so it works over Tailscale
fine.

## Known gotchas

Things that cost real debugging time - documenting them here so you don't
have to rediscover them:

- **MinusPod can trim real speech off the end of an episode.** If Whisper
  fails to transcribe the last few seconds cleanly (common - trailing audio
  is often quieter or trails off), MinusPod's `VAD_GAP_TAIL_MIN_SECONDS`
  setting (default: **3 seconds**) treats any untranscribed tail at or above
  that length as a cut candidate - and once the ad-detection LLM flags
  anything adjacent to it, the cut extends through to the true end of the
  file. We measured a real case: ~15 seconds removed from an episode's end,
  of which less than 1 second was actually promotional content. Raising this
  to 15-20s (see `.env.example`) fixes it for typical sign-offs/outros
  without meaningfully weakening ad detection.
- **MinusPod's `reprocess-all` batch endpoint can silently get stuck.**
  Episodes queue into `pending` and just sit there indefinitely, even after
  restarting the container or raising `maxEpisodes`. The reliable workaround:
  call the per-episode endpoint (`POST /episodes/{slug}/{episode_id}/reprocess`)
  individually for each episode instead of the batch one - it triggers
  immediately and queues correctly behind whichever episode is already
  processing.
- **A shared Docker network namespace (`network_mode: container:X`) breaks
  silently on recreation.** Not a MinusPod-specific issue, but relevant if you
  run this alongside other containers sharing a VPN/network container: if you
  ever recreate that shared container, *every* dependent container must be
  force-recreated with it, or they keep running against a network namespace
  that no longer exists - symptoms show up as DNS failures or "connection
  refused" that only starts hours later, not immediately.

## Files

| File | Purpose |
|---|---|
| `docker-compose.cpu.yml` | MinusPod, CPU-only |
| `.env.example` | Copy to `.env` and fill in |
| `merge_multipart.py` | Merges `[N/M]`-titled multi-part episodes |
| `sync_to_nas.py` | Uploads to the NAS, generates the RSS feed + web player |
| `podcasts-api/server.py` | Optional: cross-device playback position/completed sync |
| `nightly_start.ps1` / `nightly_sync.ps1` | Windows Task Scheduler example (23:00 refresh, 04:00 merge+sync) |

## License

MIT - see `LICENSE`.
