#!/usr/bin/env python3
"""Fetches a MinusPod (ad-cleaned) feed, finds multi-part episodes
(titles ending in "[N/M]"), merges each complete group into a single
audio file via ffmpeg, and writes a merged RSS feed pointing at them.
"""
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from urllib.request import urlretrieve, urlopen

MINUSPOD_URL = os.environ.get("MINUSPOD_URL", "http://localhost:8000")
FEED_SLUG = os.environ.get("FEED_SLUG", "my-podcast")
OUTPUT_DIR = os.environ.get("MERGE_OUTPUT_DIR", "./merged")

PART_RE = re.compile(r"^(.*?)\s*\[(\d+)/(\d+)\]\s*$")

NS = {"": "http://www.w3.org/2005/Atom"}


def fetch_feed_items():
    url = f"{MINUSPOD_URL}/{FEED_SLUG}"
    with urlopen(url, timeout=30) as r:
        xml_bytes = r.read()
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    items = []
    for item in channel.findall("item"):
        title = item.findtext("title", "")
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None
        guid = item.findtext("guid", title)
        pubdate = item.findtext("pubDate", "")
        items.append({"title": title, "audio_url": audio_url, "guid": guid, "pubdate": pubdate})
    return items


def group_multipart(items):
    groups = {}
    singles = []
    for it in items:
        m = PART_RE.match(it["title"])
        if not m:
            singles.append(it)
            continue
        base, n, total = m.group(1), int(m.group(2)), int(m.group(3))
        if total <= 1:
            singles.append(it)
            continue
        groups.setdefault((base, total), {})[n] = it
    complete = []
    incomplete = []
    for (base, total), parts in groups.items():
        if len(parts) == total and all(parts[i]["audio_url"] for i in range(1, total + 1)):
            complete.append((base, total, [parts[i] for i in range(1, total + 1)]))
        else:
            incomplete.append((base, total, sorted(parts.keys())))
    return complete, incomplete, singles


def merge_group(base_title, parts, workdir):
    local_files = []
    for i, p in enumerate(parts):
        dest = os.path.join(workdir, f"part_{i}.mp3")
        print(f"  Telechargement partie {i+1}/{len(parts)}: {p['title']}")
        urlretrieve(p["audio_url"], dest)
        local_files.append(dest)

    concat_list = os.path.join(workdir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for lf in local_files:
            f.write(f"file '{lf}'\n")

    safe_name = re.sub(r"[^\w\-]+", "_", base_title).strip("_")[:100]
    out_path = os.path.join(OUTPUT_DIR, f"{safe_name}.mp3")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", out_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("  ECHEC ffmpeg (concat copy), nouvelle tentative en reencodage...")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c:a", "libmp3lame", "-q:a", "2", out_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("  ECHEC definitif:", result.stderr[-500:])
            return None
    print(f"  OK -> {out_path}")
    return out_path


def main():
    print(f"Recuperation du flux nettoye: {FEED_SLUG}")
    items = fetch_feed_items()
    print(f"{len(items)} episodes dans le flux")

    complete, incomplete, singles = group_multipart(items)
    print(f"{len(complete)} groupes multi-parties complets, {len(incomplete)} incomplets (en attente), {len(singles)} episodes simples")

    for base, total, parts in incomplete:
        have = [p for p in parts]
        print(f"  EN ATTENTE: '{base}' [{total} parties] - a maintenant: {have}")

    for base, total, parts in complete:
        out_name = re.sub(r"[^\w\-]+", "_", base).strip("_")[:100] + ".mp3"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        if os.path.exists(out_path):
            continue
        print(f"Fusion: '{base}' ({total} parties)")
        try:
            with tempfile.TemporaryDirectory() as workdir:
                merge_group(base, parts, workdir)
        except Exception as ex:
            print(f"  IGNORE (probablement encore en cours de traitement cote MinusPod): {ex}")


if __name__ == "__main__":
    main()
