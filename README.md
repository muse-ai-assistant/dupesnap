# dupesnap 📸

Find **duplicate and similar photos**, preview them side-by-side, and clean up — safely.

> Tens of thousands of photos pile up on every phone and computer: burst shots, multiple screenshot versions, the same meme saved from WeChat three times. Commercial cleaners cost money; online tools make you upload your private photos. dupesnap is **fully offline**, a single file, and **never deletes permanently** — everything goes to the system trash.

> 手机/电脑里几万张照片：连拍、截图多版本、微信表情包存了三遍。商用清理软件要钱，在线工具不敢传原图。dupesnap **纯离线**、单文件，**绝不直接删除**——全部进系统回收站。

## How it works

Two-stage detection, fast first:

1. **Exact duplicates** — SHA256 of file bytes. Instant, zero false positives.
2. **Similar photos** — perceptual hash (dHash, self-implemented, no extra deps). Finds resized / recompressed / slightly cropped / brightness-adjusted copies. Hamming distance ≤ 5 counts as similar (tunable with `--threshold`).

```
scan ~/Photos
  │
  ├─ Stage 1: SHA256 ──► [EXACT] photo.jpg = photo(1).jpg = photo-copy.jpg
  │
  └─ Stage 2: dHash ──► [SIMILAR d≤3] IMG_2048.jpg ~ IMG_2048_small.jpg ~ IMG_2048_crop.jpg
                                                              (resized)          (cropped)
```

Per group, keep the **largest** (default), **newest**, or **sharpest** (Laplacian variance) photo; the rest go to trash.

## Install

```bash
pip install -r requirements.txt
```

```bash
# or
pip install .
```

Requires Python 3.8+, only dependency is Pillow.

## Usage

```bash
# 1. Scan (recursive)
dupesnap scan ~/Photos --html report.html

# 2. Preview what would be removed (moves nothing)
dupesnap clean --dry-run

# 3. Clean — duplicates go to system trash, never deleted permanently
dupesnap clean --keep sharpest
```

Example output:

```
Scanned 12480 images in /home/user/Photos
Found 312 duplicate group(s)

[1] EXACT - 3 files, 24.6 MB total, can free 16.4 MB
    KEEP  /home/user/Photos/IMG_2048.jpg (8.2 MB, 2024-05-01)
    del   /home/user/Photos/IMG_2048 (1).jpg (8.2 MB, 2024-05-01)
    del   /home/user/Photos/vacation/IMG_2048.jpg (8.2 MB, 2024-06-12)

[2] SIMILAR(d<=3) - 2 files, 9.1 MB total, can free 1.2 MB
    KEEP  /home/user/Photos/screenshot.png (7.9 MB, 2025-01-20)
    del   /home/user/Photos/screenshot_small.png (1.2 MB, 2025-01-20)

Total reclaimable: 3.2 GB
```

The `--html` report shows every group with thumbnails side-by-side, KEEP/DELETE badges, checkboxes to adjust, and a one-click copy of the deletion list — review visually before you clean.

## Options

| | |
|---|---|
| `scan --threshold N` | dHash hamming distance for "similar" (default 5; lower = stricter) |
| `scan --min-size KB` | skip images smaller than KB kilobytes |
| `scan --html PATH` | visual report with thumbnails |
| `clean --keep` | `largest` (default) / `newest` / `sharpest` |
| `clean --dry-run` | preview only |

## Safety

- Deletions go to the **system trash** (freedesktop Trash on Linux, ~/.Trash on macOS) — recoverable, never `rm`.
- `--dry-run` first. The HTML report lets you eyeball every group.
- A scan database (`~/.cache/dupesnap/last_scan.json`) records exactly what was found; `clean` only touches files from that scan.

## License

MIT
