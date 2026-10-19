#!/usr/bin/env python3
"""
dupesnap - find and clean duplicate / similar photos.

Offline, single-file, trash-safe: nothing is ever deleted permanently.
Only dependency: Pillow.

Usage:
    dupesnap scan ~/Photos [--threshold 5] [--html report.html] [--db scan.json]
    dupesnap clean [--db scan.json] [--keep largest|newest|sharpest] [--dry-run]
"""
import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageFilter
except ImportError:
    sys.exit("dupesnap requires Pillow: pip install -r requirements.txt")

VERSION = "1.0.0"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif", ".heic", ".heif"}
DEFAULT_DB = Path.home() / ".cache" / "dupesnap" / "last_scan.json"


# ---------------------------------------------------------------- perceptual hash
def dhash(path, hash_size=8):
    """Difference hash: 64-bit fingerprint, robust to resize/recompress."""
    with Image.open(path) as im:
        im = im.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
        px = list(im.getdata())
    h = 0
    for y in range(hash_size):
        row = y * (hash_size + 1)
        for x in range(hash_size):
            h = (h << 1) | (1 if px[row + x] > px[row + x + 1] else 0)
    return h


def hamming(a, b):
    return bin(a ^ b).count("1")


def sharpness(path):
    """Laplacian variance: higher = sharper. Used by --keep sharpest."""
    with Image.open(path) as im:
        g = im.convert("L").resize((320, 320), Image.LANCZOS)
    lap = g.filter(ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1))
    px = list(lap.getdata())
    mean = sum(px) / len(px)
    return sum((p - mean) ** 2 for p in px) / len(px)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- trash (never delete permanently)
def _unique(p):
    p = Path(p)
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f"{stem}.{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def move_to_trash(path):
    """Move a file to the system trash. Returns the trash location."""
    path = Path(path)
    if sys.platform == "darwin":
        dest = _unique(Path.home() / ".Trash" / path.name)
        shutil.move(str(path), str(dest))
        return dest
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    trash = base / "Trash"
    files_dir = trash / "files"
    info_dir = trash / "info"
    files_dir.mkdir(parents=True, exist_ok=True)
    info_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique(files_dir / path.name)
    shutil.move(str(path), str(dest))
    (info_dir / (dest.name + ".trashinfo")).write_text(
        "[Trash Info]\nPath=%s\nDeletionDate=%s\n"
        % (path.resolve(), datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
        encoding="utf-8",
    )
    return dest


# ---------------------------------------------------------------- scan
def iter_images(root):
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            yield p


def scan_dir(root, threshold=5, min_size_kb=0):
    files = []
    for p in iter_images(root):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < min_size_kb * 1024:
            continue
        files.append({"path": str(p.resolve()), "size": st.st_size, "mtime": st.st_mtime})

    # Stage 1: exact duplicates by SHA256 (fast)
    by_hash = {}
    for f in files:
        try:
            digest = file_sha256(f["path"])
        except OSError:
            continue
        f["sha256"] = digest
        by_hash.setdefault(digest, []).append(f)

    groups = []
    reps = []  # one representative per exact-group + all singletons, for stage 2
    for digest, members in by_hash.items():
        if len(members) > 1:
            groups.append({"kind": "exact", "files": members, "max_distance": 0})
            reps.append(members[0])
        else:
            reps.append(members[0])

    # Stage 2: similar images by dHash (union-find on hamming distance)
    hashes = {}
    for f in reps:
        try:
            hashes[f["path"]] = dhash(f["path"])
        except Exception:
            continue
    parent = {p: p for p in hashes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    paths = list(hashes)
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            if hamming(hashes[paths[i]], hashes[paths[j]]) <= threshold:
                union(paths[i], paths[j])

    buckets = {}
    for p in paths:
        buckets.setdefault(find(p), []).append(p)
    path2file = {f["path"]: f for f in reps}
    for members in buckets.values():
        if len(members) < 2:
            continue
        dists = [hamming(hashes[members[i]], hashes[members[j]])
                 for i in range(len(members)) for j in range(i + 1, len(members))]
        groups.append({
            "kind": "similar",
            "files": [path2file[m] for m in members],
            "max_distance": max(dists) if dists else 0,
        })

    groups.sort(key=lambda g: -sum(f["size"] for f in g["files"]))
    return {
        "version": 1,
        "scanned_dir": str(Path(root).resolve()),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "threshold": threshold,
        "image_count": len(files),
        "groups": groups,
    }


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def print_report(data):
    groups = data["groups"]
    total_free = 0
    print(f"Scanned {data['image_count']} images in {data['scanned_dir']}")
    print(f"Found {len(groups)} duplicate group(s) (similarity threshold: {data['threshold']})\n")
    for i, g in enumerate(groups, 1):
        files = g["files"]
        total = sum(f["size"] for f in files)
        keep = pick_keep(files, "largest")
        free = total - keep["size"]
        total_free += free
        tag = "EXACT" if g["kind"] == "exact" else f"SIMILAR(d<={g['max_distance']})"
        print(f"[{i}] {tag} - {len(files)} files, {fmt_size(total)} total, can free {fmt_size(free)}")
        for f in sorted(files, key=lambda x: -x["size"]):
            mark = "KEEP" if f["path"] == keep["path"] else "del "
            dt = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")
            print(f"    {mark}  {f['path']}  ({fmt_size(f['size'])}, {dt})")
        print()
    print(f"Total reclaimable: {fmt_size(total_free)}")


def pick_keep(files, rule):
    if rule == "newest":
        return max(files, key=lambda f: f["mtime"])
    if rule == "sharpest":
        scored = []
        for f in files:
            try:
                scored.append((sharpness(f["path"]), f))
            except Exception:
                scored.append((-1, f))
        scored.sort(key=lambda x: -x[0])
        return scored[0][1]
    return max(files, key=lambda f: f["size"])  # largest


# ---------------------------------------------------------------- HTML report
def thumbnail_b64(path, size=240):
    with Image.open(path) as im:
        im.thumbnail((size, size), Image.LANCZOS)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
            im = bg
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def write_html(data, out_path, keep_rule="largest"):
    groups = data["groups"]
    total_free = 0
    cards = []
    for i, g in enumerate(groups, 1):
        files = sorted(g["files"], key=lambda x: -x["size"])
        keep = pick_keep(files, keep_rule)
        free = sum(f["size"] for f in files) - keep["size"]
        total_free += free
        tag = "EXACT DUPLICATE" if g["kind"] == "exact" else f"SIMILAR (dHash\u2264{g['max_distance']})"
        items = []
        for f in files:
            is_keep = f["path"] == keep["path"]
            try:
                thumb = thumbnail_b64(f["path"])
            except Exception:
                thumb = ""
            dt = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M")
            badge = ("<span class='keep'>KEEP</span>" if is_keep
                     else "<span class='del'>DELETE</span>")
            checked = "" if is_keep else "checked"
            img = f"<img src='data:image/jpeg;base64,{thumb}'>" if thumb else "<div class='noimg'>?</div>"
            items.append(
                f"<label class='card'><input type='checkbox' data-path={json.dumps(f['path'])} {checked}>"
                f"{img}{badge}"
                f"<div class='meta'>{Path(f['path']).name}<br>{fmt_size(f['size'])} · {dt}</div></label>")
        cards.append(
            f"<section><h2>Group {i} <small>{tag} · {len(files)} files · frees {fmt_size(free)}</small></h2>"
            f"<div class='row'>{''.join(items)}</div></section>")

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dupesnap report - {Path(data['scanned_dir']).name}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px;background:#f6f7f9}}
h1 small{{color:#666;font-weight:normal}} section{{background:#fff;border-radius:12px;padding:16px;margin:16px 0;box-shadow:0 1px 4px #0001}}
h2 small{{color:#666;font-weight:normal;font-size:.7em}} .row{{display:flex;flex-wrap:wrap;gap:12px}}
.card{{border:2px solid #e2e5ea;border-radius:10px;padding:8px;width:190px;cursor:pointer;position:relative}}
.card img{{width:100%;border-radius:6px;display:block}} .noimg{{height:120px;display:flex;align-items:center;justify-content:center;background:#eee;border-radius:6px}}
.meta{{font-size:12px;color:#555;margin-top:6px;word-break:break-all}}
.keep,.del{{position:absolute;top:12px;left:12px;font-size:11px;font-weight:bold;padding:2px 8px;border-radius:20px;color:#fff}}
.keep{{background:#16a34a}} .del{{background:#dc2626}}
.card input{{position:absolute;top:10px;right:10px;width:18px;height:18px}}
.toolbar{{position:sticky;top:0;background:#fff;padding:12px;border-radius:12px;box-shadow:0 1px 4px #0002;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
button{{padding:8px 16px;border-radius:8px;border:1px solid #ccc;background:#111;color:#fff;cursor:pointer}}
#out{{width:100%;height:80px;margin-top:8px;font-size:12px}}
</style></head><body>
<h1>📸 dupesnap report <small>{data['image_count']} images · {len(groups)} groups · reclaimable {fmt_size(total_free)}</small></h1>
<div class="toolbar">
<button onclick="copyList()">📋 Copy deletion list (checked)</button>
<button onclick="toggleAll(true)">Select all</button>
<button onclick="toggleAll(false)">Select none</button>
<span style="font-size:13px;color:#666">Uncheck any photo to keep it. Checked photos are the ones <b>dupesnap clean</b> would move to trash.</span>
</div>
<textarea id="out" placeholder="Deletion list appears here…" readonly></textarea>
{''.join(cards) if cards else '<p>No duplicates found. 🎉</p>'}
<script>
function checkedPaths(){{return [...document.querySelectorAll('.card input:checked')].map(c=>c.dataset.path)}}
function copyList(){{const l=checkedPaths();document.getElementById('out').value=l.join('\\n');navigator.clipboard&&navigator.clipboard.writeText(l.join('\\n'))}}
function toggleAll(v){{document.querySelectorAll('.card input').forEach(c=>c.checked=v)}}
</script></body></html>"""
    Path(out_path).write_text(html, encoding="utf-8")
    return out_path

# ---------------------------------------------------------------- clean
def cmd_clean(db_path, keep_rule, dry_run):
    db_path = Path(db_path)
    if not db_path.exists():
        sys.exit(f"No scan database at {db_path}. Run `dupesnap scan <dir>` first.")
    data = json.loads(db_path.read_text(encoding="utf-8"))
    groups = data["groups"]
    if not groups:
        print("Nothing to clean. 🎉")
        return

    plan = []  # (keep, [to_delete])
    for g in groups:
        files = [f for f in g["files"] if Path(f["path"]).exists()]
        if len(files) < 2:
            continue
        keep = pick_keep(files, keep_rule)
        dels = [f for f in files if f["path"] != keep["path"]]
        plan.append((keep, dels))

    total = sum(f["size"] for _, dels in plan for f in dels)
    n = sum(len(dels) for _, dels in plan)
    if n == 0:
        print("Nothing to clean (files already gone?). 🎉")
        return

    print(f"Keep rule: {keep_rule}. {n} file(s), {fmt_size(total)} -> trash.\n")
    for keep, dels in plan:
        print(f"  KEEP  {keep['path']}")
        for f in dels:
            print(f"  {'WOULD DELETE' if dry_run else 'DELETE'}  {f['path']} ({fmt_size(f['size'])})")
        print()

    if dry_run:
        print(f"Dry run: {n} file(s), {fmt_size(total)} would move to trash. Nothing was touched.")
        return

    moved, failed = 0, []
    for _, dels in plan:
        for f in dels:
            try:
                dest = move_to_trash(f["path"])
                moved += 1
                print(f"  trashed -> {dest}")
            except Exception as e:
                failed.append((f["path"], str(e)))
    print(f"\nDone: {moved} file(s), {fmt_size(total)} moved to trash. Nothing was permanently deleted.")
    if failed:
        print("Failed:")
        for p, e in failed:
            print(f"  {p}: {e}")


# ---------------------------------------------------------------- CLI
def cmd_scan(args):
    t0 = time.time()
    data = scan_dir(args.directory, threshold=args.threshold, min_size_kb=args.min_size)
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print_report(data)
    print(f"\nScan database saved to {db} ({time.time()-t0:.1f}s)")
    if args.html:
        write_html(data, args.html, keep_rule="largest")
        print(f"Visual report: {args.html}")


def main():
    ap = argparse.ArgumentParser(
        prog="dupesnap",
        description="Find and clean duplicate / similar photos. Offline, trash-safe.",
    )
    ap.add_argument("--version", action="version", version=f"dupesnap {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="Scan a directory for duplicates")
    s.add_argument("directory", help="Directory to scan (recursive)")
    s.add_argument("--db", default=str(DEFAULT_DB), help="Scan database path")
    s.add_argument("--threshold", type=int, default=5,
                   help="Max dHash hamming distance to count as similar (default 5)")
    s.add_argument("--min-size", type=int, default=0, metavar="KB",
                   help="Skip images smaller than KB kilobytes")
    s.add_argument("--html", metavar="PATH", help="Write visual HTML report")

    c = sub.add_parser("clean", help="Move duplicates to trash (from last scan)")
    c.add_argument("--db", default=str(DEFAULT_DB), help="Scan database path")
    c.add_argument("--keep", choices=["largest", "newest", "sharpest"], default="largest",
                   help="Which file to keep per group (default largest)")
    c.add_argument("--dry-run", action="store_true", help="Preview only, move nothing")

    args = ap.parse_args()
    if args.cmd == "scan":
        if not Path(args.directory).is_dir():
            sys.exit(f"Not a directory: {args.directory}")
        cmd_scan(args)
    else:
        cmd_clean(args.db, args.keep, args.dry_run)


if __name__ == "__main__":
    main()
