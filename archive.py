#!/usr/bin/env python3
"""
Download authorized VIDEO/PDF entries from Maths_VOD_all_links.csv and upload them to Google Drive through rclone.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path
import requests

DEFAULT_CSV = "Maths_VOD_all_links.csv"
DEFAULT_REMOTE = os.getenv("GDRIVE_REMOTE", "gdrive1")
DEFAULT_DRIVE_ROOT = os.getenv("GDRIVE_ROOT", "Telegram_Archive/Maths/Maths_Special_VOD_Batch-4.0")
STATE_FILE = Path(os.getenv("STATE_FILE", "archive_state.json"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
RETRIES = int(os.getenv("RETRIES", "4"))
session = requests.Session()
session.headers.update({"User-Agent": "Maths-VOD-Archiver/1.0"})

def safe_name(name: str, max_len: int = 180) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:max_len] or "untitled"

def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"completed": {}, "failed": {}}

def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def run(cmd: list[str], check: bool = True):
    print("$", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=check)

def rclone_copy(local_file: Path, drive_path: str) -> None:
    run(["rclone", "copyto", str(local_file), f"{DEFAULT_REMOTE}:{drive_path}", "--retries", "5", "--low-level-retries", "10", "--transfers", os.getenv("RCLONE_TRANSFERS", "2"), "--checkers", os.getenv("RCLONE_CHECKERS", "4"), "--stats", "30s"])

def download_pdf(url: str, out: Path) -> None:
    for attempt in range(1, RETRIES + 1):
        try:
            with session.get(url, stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                tmp = out.with_suffix(out.suffix + ".part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk: f.write(chunk)
                tmp.replace(out); return
        except Exception as e:
            print(f"PDF download failed ({attempt}/{RETRIES}): {e}", flush=True)
            if attempt == RETRIES: raise
            time.sleep(min(5 * attempt, 20))

def download_hls(url: str, out: Path) -> None:
    tmp = out.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", url, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc", str(tmp)]
    for attempt in range(1, RETRIES + 1):
        try:
            run(cmd)
            if not tmp.exists() or tmp.stat().st_size == 0: raise RuntimeError("ffmpeg produced an empty file")
            tmp.replace(out); return
        except Exception as e:
            print(f"HLS download failed ({attempt}/{RETRIES}): {e}", flush=True)
            tmp.unlink(missing_ok=True)
            if attempt == RETRIES: raise
            time.sleep(min(5 * attempt, 20))

def fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

def load_rows(csv_path: Path) -> list[dict]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f: rows = list(csv.DictReader(f))
    required = {"Category", "Type", "Title", "Decoded URL"}
    if not rows or required - set(rows[0].keys()): raise ValueError("CSV is missing required columns")
    return rows

def process_row(row: dict, state: dict, dry_run=False) -> bool:
    category, item_type, title, url = safe_name(row["Category"]), row["Type"].strip().upper(), safe_name(row["Title"]), row["Decoded URL"].strip()
    if item_type not in {"VIDEO", "PDF"}: return True
    ext, subfolder = ((".mp4", "Videos") if item_type == "VIDEO" else (".pdf", "PDFs"))
    key = f"{row.get('#', 'unknown')}|{url}"
    if state["completed"].get(key): print(f"[DONE] row {row.get('#')}: {title}"); return True
    filename = f"{title} [{fingerprint(url)}]{ext}"
    local_file = DOWNLOAD_DIR / category / subfolder / filename
    local_file.parent.mkdir(parents=True, exist_ok=True)
    drive_path = f"{DEFAULT_DRIVE_ROOT}/{category}/{subfolder}/{filename}"
    print(f"\n[{row.get('#')}] {item_type} | {category} | {title}\nURL: {url}\nDrive: {drive_path}")
    if dry_run: return True
    try:
        if not local_file.exists() or local_file.stat().st_size == 0:
            (download_hls if item_type == "VIDEO" else download_pdf)(url, local_file)
        rclone_copy(local_file, drive_path)
        state["completed"][key] = {"title": row["Title"], "type": item_type, "category": row["Category"], "url": url, "drive_path": drive_path}
        state["failed"].pop(key, None); save_state(state); print(f"[OK] uploaded: {drive_path}"); return True
    except Exception as e:
        state["failed"][key] = {"title": row["Title"], "type": item_type, "category": row["Category"], "url": url, "error": str(e)}
        save_state(state); print(f"[FAIL] row {row.get('#')}: {e}", file=sys.stderr); return False

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=DEFAULT_CSV); p.add_argument("--only", choices=["VIDEO", "PDF", "ALL"], default="ALL")
    p.add_argument("--category"); p.add_argument("--limit", type=int, default=0); p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if shutil.which("ffmpeg") is None or shutil.which("rclone") is None:
        print("ERROR: ffmpeg and rclone are required", file=sys.stderr); return 2
    rows = load_rows(Path(args.csv)); state = load_state(); selected=[]
    for row in rows:
        typ=row["Type"].strip().upper()
        if args.only != "ALL" and typ != args.only: continue
        if args.category is not None and row["Category"] != args.category: continue
        selected.append(row)
    if args.limit > 0: selected=selected[:args.limit]
    print(f"Rows selected: {len(selected)} | remote={DEFAULT_REMOTE} | root={DEFAULT_DRIVE_ROOT}")
    ok=failed=0
    for row in selected:
        if process_row(row,state,args.dry_run): ok+=1
        else: failed+=1
    print(f"SUMMARY: selected={len(selected)} success={ok} failed={failed}")
    return 1 if failed else 0

if __name__ == "__main__": raise SystemExit(main())
