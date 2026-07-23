#!/usr/bin/env python3
"""Runs after merge_multipart.py. Copies merged + single-episode clean audio
files to your NAS/home server over LAN (never over Tailscale/a VPN - measured
far too slow for bulk file transfer), and publishes:
  - a corrected, spec-compliant RSS feed (many podcast apps reject feeds
    missing an XML declaration, enclosure "length", or a real guid)
  - a small self-contained HTML player (useful if your podcast app's server
    can't reach a Tailscale-only URL - see README "Known gotchas")
Both point at your NAS's public-facing URL (e.g. a Tailscale HTTPS endpoint),
not at this machine, since this machine only does the processing.
"""
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.request import urlretrieve, urlopen, Request
from urllib.parse import quote
from email.utils import formatdate, parsedate_to_datetime

MINUSPOD_URL = os.environ.get("MINUSPOD_URL", "http://localhost:8000")
FEED_SLUG = os.environ.get("FEED_SLUG", "my-podcast")
MERGED_DIR = os.environ.get("MERGE_OUTPUT_DIR", "./merged")
SINGLES_DIR = os.environ.get("SINGLES_DIR", "./singles")

# --- NAS / home-server destination -----------------------------------------
NAS_HOST = os.environ["NAS_HOST"]  # direct LAN IP/hostname - do not route this over Tailscale/VPN, too slow for bulk transfer
NAS_PODCASTS_PATH = os.environ["NAS_PODCASTS_PATH"]  # base folder on the NAS that your web server serves as static files
# Public URL your listening devices will use (e.g. a Tailscale HTTPS hostname,
# or any URL that reaches NAS_PODCASTS_PATH/{FEED_SLUG}/ over HTTP(S)).
PUBLIC_BASE_URL = f"{os.environ['PUBLIC_BASE_ROOT']}/{FEED_SLUG}"
# Sibling of the per-feed folder, same origin as the page (no CORS needed) -
# proxied by your web server to the podcasts-api container, which persists
# playback position/completed state on the NAS so it's shared across every
# device (see podcasts-api/). Optional: leave PUBLIC_BASE_ROOT-only setup if
# you don't want cross-device resume - the player just falls back silently.
API_BASE_URL = f"{os.environ['PUBLIC_BASE_ROOT']}/api"

PART_RE = re.compile(r"^(.*?)\s*\[(\d+)/(\d+)\]\s*$")
# Example customization: this groups two-segment episodes (e.g. a French true
# crime show split into "... - Le récit" / "... - Le débrief") so they always
# stay adjacent in the player, regardless of publish order. Adjust the regex
# (or drop group_by_story entirely and just use publication order) to match
# your own show's naming convention.
STORY_SUFFIX_RE = re.compile(r"^(.*?)[\s,]*-?\s*Le (récit|débrief)\s*$", re.IGNORECASE)
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def parse_pubdate(s):
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def format_date(dt):
    return dt.strftime("%d/%m/%y")


def group_by_story(entries):
    """Groups entries so a story's "Le récit" is always immediately followed by
    its "Le débrief", with story groups ordered by real publication date
    (most recent first). See STORY_SUFFIX_RE above if your show doesn't use
    this two-segment convention - this still works fine as a no-op grouping."""
    story_best_date = {}
    parsed = []
    for e in entries:
        m = STORY_SUFFIX_RE.match(e["title"])
        if m:
            key, kind = m.group(1).strip().rstrip(","), m.group(2).lower()
        else:
            key, kind = e["title"], None
        parsed.append((key, kind, e))
        dt = e.get("pubdate_dt")
        if dt and dt > story_best_date.get(key, EPOCH):
            story_best_date[key] = dt
    kind_rank = {"récit": 0, "débrief": 1, None: 0}
    parsed.sort(key=lambda t: (-story_best_date.get(t[0], EPOCH).timestamp(), kind_rank[t[1]]))
    return [(kind, e) for (_, kind, e) in parsed]


def fetch_feed():
    with urlopen(f"{MINUSPOD_URL}/{FEED_SLUG}", timeout=30) as r:
        return ET.fromstring(r.read())


def scp_to_nas(local_path, remote_name):
    remote_dir = f"{NAS_PODCASTS_PATH}/{FEED_SLUG}"
    subprocess.run(["ssh", f"root@{NAS_HOST}", f"mkdir -p '{remote_dir}'"], check=True)
    subprocess.run(["scp", local_path, f"root@{NAS_HOST}:{remote_dir}/{remote_name}"], check=True)


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES_NS)


def extract_show_meta(channel):
    image_el = channel.find(f"{{{ITUNES_NS}}}image")
    image_href = image_el.get("href") if image_el is not None else channel.findtext("image/url")
    categories = [c.get("text") for c in channel.findall(f"{{{ITUNES_NS}}}category") if c.get("text")]
    return {
        "title": channel.findtext("title", FEED_SLUG),
        "link": channel.findtext("link", PUBLIC_BASE_URL),
        "description": channel.findtext("description", f"Ad-free feed (episodes merged) - {FEED_SLUG}"),
        "image_href": image_href,
        "categories": categories or ["Society & Culture"],
        "explicit": channel.findtext(f"{{{ITUNES_NS}}}explicit", "no"),
    }


def build_output_rss(entries, show_meta):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = show_meta["title"]
    ET.SubElement(channel, "link").text = PUBLIC_BASE_URL
    ET.SubElement(channel, "description").text = show_meta["description"]
    ET.SubElement(channel, "language").text = "fr"
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = show_meta["title"]
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = show_meta["explicit"]
    if show_meta["image_href"]:
        ET.SubElement(channel, f"{{{ITUNES_NS}}}image", href=show_meta["image_href"])
    for cat in show_meta["categories"]:
        ET.SubElement(channel, f"{{{ITUNES_NS}}}category", text=cat)
    for e in entries:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = e["title"]
        # isPermaLink="false" matters: our guid is a filename, not a real URL,
        # and some podcast apps flag a bare guid without this as invalid.
        ET.SubElement(item, "guid", isPermaLink="false").text = e["filename"]
        pub_dt = e.get("pubdate_dt")
        pubdate_str = formatdate(pub_dt.timestamp(), localtime=False) if pub_dt else formatdate()
        ET.SubElement(item, "pubDate").text = pubdate_str
        length = str(os.path.getsize(e["local_path"])) if os.path.exists(e["local_path"]) else "0"
        ET.SubElement(item, "enclosure", url=f"{PUBLIC_BASE_URL}/{quote(e['filename'])}", type="audio/mpeg", length=length)
        if show_meta["image_href"]:
            ET.SubElement(item, f"{{{ITUNES_NS}}}image", href=show_meta["image_href"])
    xml_body = ET.tostring(rss, encoding="unicode")
    # The declaration matters: several podcast apps reject a feed missing it,
    # even when the XML itself parses fine everywhere else.
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_body


def get_duration_str(local_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", local_path],
            capture_output=True, text=True, timeout=30,
        )
        seconds = float(result.stdout.strip())
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return None


def build_html_player(entries, show_meta):
    rows = []
    for kind, e in group_by_story(entries):
        css_class = f" {kind}" if kind else ""
        duration = get_duration_str(e["local_path"])
        duration_suffix = f' <span class="duration">({duration})</span>' if duration else ""
        pub_dt = e.get("pubdate_dt")
        date_suffix = f' <span class="pubdate">{format_date(pub_dt)}</span>' if pub_dt else ""
        rows.append(f"""    <div class="episode">
      <h2 class="title{css_class}" onclick="togglePlayback(this)">{e['title']}{duration_suffix}{date_suffix} <span class="remaining"></span></h2>
      <audio preload="metadata" data-filename="{e['filename']}" src="{PUBLIC_BASE_URL}/{quote(e['filename'])}"></audio>
    </div>""")
    episodes_html = "\n".join(rows)
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{show_meta['title']}</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; font-size: 14px; max-width: 700px; margin: 0 auto; padding: 1rem 1.2rem 3rem; background:#111; color:#eee; }}
  h1 {{ font-size: 1.1rem; margin-bottom: 0.2rem; }}
  .episode {{ padding: 0.2rem 0; }}
  .episode h2 {{ font-size: 0.85rem; margin: 0 0 0.15rem; font-weight: 600; cursor: pointer; }}
  .episode h2.récit {{ color: #4caf50; }}
  .episode h2.débrief {{ color: #e53935; }}
  .episode h2.completed {{ opacity: 0.4; }}
  .duration {{ font-weight: 400; color: #888; font-size: 0.85em; }}
  .pubdate {{ font-weight: 400; color: #888; font-size: 0.85em; }}
  .remaining {{ font-weight: 400; color: #6aa9ff; font-size: 0.85em; }}
  audio {{ width: 100%; display: none; }}
  audio.visible {{ display: block; }}
  @media (prefers-color-scheme: light) {{
    body {{ background:#fff; color:#111; }}
  }}
</style>
</head>
<body>
  <h1>{show_meta['title']}</h1>
{episodes_html}
<script>
function formatTime(sec) {{
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = h ? String(m).padStart(2, '0') : m;
  return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
}}

const FEED_SLUG = {FEED_SLUG!r};
const API_URL = {API_BASE_URL!r} + '/state?feed=' + FEED_SLUG;
let savedState = {{}};

function saveState(filename, patch) {{
  fetch(API_URL, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(Object.assign({{filename: filename}}, patch)),
  }}).catch(function() {{}});
}}

// Cross-device resume is optional: if podcasts-api isn't deployed, this fetch
// just fails silently and the player still works (no position memory).
fetch(API_URL).then(function(r) {{ return r.ok ? r.json() : {{}}; }}).catch(function() {{ return {{}}; }}).then(function(data) {{
  savedState = data || {{}};
  document.querySelectorAll('.episode').forEach(setupEpisode);
  const lastPlayed = (savedState._meta || {{}}).last_played;
  const lastEntry = lastPlayed ? savedState[lastPlayed] : null;
  if (lastPlayed && lastEntry && !lastEntry.completed) {{
    const audio = document.querySelector('audio[data-filename="' + CSS.escape(lastPlayed) + '"]');
    if (audio) {{
      audio.classList.add('visible');
      audio.setAttribute('controls', '');
    }}
  }}
}});

function setupEpisode(ep) {{
  const audio = ep.querySelector('audio');
  const remainingEl = ep.querySelector('.remaining');
  const titleEl = ep.querySelector('h2');
  const filename = audio.dataset.filename;
  const entry = savedState[filename] || {{}};

  if (entry.completed) {{
    titleEl.classList.add('completed');
    remainingEl.textContent = 'Terminé ✓';
  }}

  function updateRemaining() {{
    if (audio.duration && !isNaN(audio.duration)) {{
      const current = audio.currentTime || entry.position || 0;
      remainingEl.textContent = formatTime(audio.duration - current) + ' restant';
    }}
  }}

  function onMetadataReady() {{
    const saved = entry.position || 0;
    if (saved > 1 && saved < audio.duration - 2) {{
      audio.currentTime = saved;
    }}
    if (!entry.completed) updateRemaining();
  }}
  audio.addEventListener('loadedmetadata', onMetadataReady);
  if (audio.readyState >= 1) onMetadataReady();

  audio.addEventListener('timeupdate', function() {{
    if (!entry.completed) updateRemaining();
    if (Math.floor(audio.currentTime) % 10 === 0) {{
      saveState(filename, {{position: audio.currentTime}});
      saveState('_meta', {{last_played: filename}});
    }}
  }});
  audio.addEventListener('pause', function() {{
    saveState(filename, {{position: audio.currentTime}});
    saveState('_meta', {{last_played: filename}});
  }});
  audio.addEventListener('ended', function() {{
    entry.completed = true;
    titleEl.classList.add('completed');
    remainingEl.textContent = 'Terminé ✓';
    saveState(filename, {{position: 0, completed: true}});
    saveState('_meta', {{last_played: ''}});
  }});
}}

function togglePlayback(titleEl) {{
  const audio = titleEl.closest('.episode').querySelector('audio');
  const wasVisible = audio.classList.contains('visible');
  document.querySelectorAll('audio').forEach(function(other) {{
    if (other !== audio) {{
      other.pause();
      other.classList.remove('visible');
      other.removeAttribute('controls');
    }}
  }});
  if (wasVisible) {{
    audio.pause();
    audio.classList.remove('visible');
    audio.removeAttribute('controls');
  }} else {{
    audio.classList.add('visible');
    audio.setAttribute('controls', '');
    saveState('_meta', {{last_played: audio.dataset.filename}});
  }}
}}
</script>
</body>
</html>
"""


def verify_feed(rss_local_path, entries):
    """Basic post-publish health check: is the feed we just wrote well-formed,
    does it contain every episode we meant to publish, and is each audio file
    actually reachable on the NAS (not just referenced)? Prints a clear
    OK/problem report instead of silently trusting that everything worked."""
    problems = []

    try:
        with open(rss_local_path, "rb") as f:
            root = ET.fromstring(f.read())
    except ET.ParseError as e:
        print(f"HEALTH CHECK FAILED: feed XML is not well-formed: {e}")
        return False

    items = root.findall(".//item")
    if len(items) != len(entries):
        problems.append(f"expected {len(entries)} items, found {len(items)} in the feed")

    for e in entries:
        url = f"{PUBLIC_BASE_URL}/{quote(e['filename'])}"
        try:
            with urlopen(Request(url, method="HEAD"), timeout=15) as r:
                size = int(r.headers.get("Content-Length", 0))
                if size <= 0:
                    problems.append(f"{e['filename']}: reachable but reports 0 bytes")
        except Exception as ex:
            problems.append(f"{e['filename']}: not reachable ({ex})")

    if problems:
        print("HEALTH CHECK: problems found:")
        for p in problems:
            print(f"  - {p}")
        return False

    print(f"HEALTH CHECK OK: {len(entries)} episode(s) verified reachable on the NAS.")
    return True


def main():
    root = fetch_feed()
    channel = root.find("channel")
    show_meta = extract_show_meta(channel)
    items = channel.findall("item")

    grouped = {}
    singles = []
    for item in items:
        title = item.findtext("title", "")
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None
        pubdate_str = item.findtext("pubDate", "")
        m = PART_RE.match(title)
        if m and int(m.group(3)) > 1:
            base, n, total = m.group(1), int(m.group(2)), int(m.group(3))
            grouped.setdefault((base, total), {})[n] = (audio_url, pubdate_str)
        else:
            singles.append((title, audio_url, pubdate_str))

    # Map the exact merged filename (same formula as merge_multipart.py) back to
    # its real title (with punctuation intact) so we don't have to derive a
    # lossy title from the filename (underscores swallow apostrophes etc), and
    # to its representative publication date (latest part = when it completed).
    safe_name_to_title = {}
    safe_name_to_pubdate = {}
    for (base, total), parts in grouped.items():
        safe_name = re.sub(r"[^\w\-]+", "_", base).strip("_")[:100] + ".mp3"
        safe_name_to_title[safe_name] = base
        dts = [d for d in (parse_pubdate(p[1]) for p in parts.values()) if d]
        if dts:
            safe_name_to_pubdate[safe_name] = max(dts)

    entries = []

    # Merged multi-part episodes: use the already-merged local file if present.
    os.makedirs(MERGED_DIR, exist_ok=True)
    for fname in os.listdir(MERGED_DIR):
        if not fname.endswith(".mp3"):
            continue
        local_path = os.path.join(MERGED_DIR, fname)
        print(f"Uploading (merged) {fname}...")
        scp_to_nas(local_path, fname)
        title = safe_name_to_title.get(fname, fname[:-4].replace("_", " "))
        pubdate_dt = safe_name_to_pubdate.get(fname)
        entries.append({"title": title, "filename": fname, "local_path": local_path, "pubdate_dt": pubdate_dt})

    # Single episodes: download from MinusPod's clean feed, then push to NAS.
    os.makedirs(SINGLES_DIR, exist_ok=True)
    for title, audio_url, pubdate_str in singles:
        if not audio_url:
            continue
        safe_name = re.sub(r"[^\w\-]+", "_", title).strip("_")[:100] + ".mp3"
        local_path = os.path.join(SINGLES_DIR, safe_name)
        if not os.path.exists(local_path):
            print(f"Downloading (single) {title}...")
            urlretrieve(audio_url, local_path)
        print(f"Uploading (single) {safe_name}...")
        scp_to_nas(local_path, safe_name)
        pubdate_dt = parse_pubdate(pubdate_str)
        entries.append({"title": title, "filename": safe_name, "local_path": local_path, "pubdate_dt": pubdate_dt})

    rss_xml = build_output_rss(entries, show_meta)
    rss_local = os.path.join(".", f"{FEED_SLUG}.xml")
    with open(rss_local, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    scp_to_nas(rss_local, "feed.xml")

    html = build_html_player(entries, show_meta)
    html_local = os.path.join(".", f"{FEED_SLUG}.html")
    with open(html_local, "w", encoding="utf-8") as f:
        f.write(html)
    scp_to_nas(html_local, "index.html")

    print()
    verify_feed(rss_local, entries)

    print(f"\nDone. {len(entries)} episodes published.")
    print("RSS feed (add this to your podcast app):")
    print(f"  {PUBLIC_BASE_URL}/feed.xml")
    print("Web player (works even where the RSS feed can't be validated remotely):")
    print(f"  {PUBLIC_BASE_URL}/")


if __name__ == "__main__":
    main()
