"""Optional: stitch a run's frames into an MP4 (needs ffmpeg: brew install ffmpeg).
Usage: python scripts/export_video.py runs/<run_id> [fps]
Playwright also records a .webm in runs/<run_id>/video/ (disable with FORMPILOT_RECORD_VIDEO=0 around sensitive demos).
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if len(sys.argv) < 2:
    sys.exit(__doc__)
run = Path(sys.argv[1])
fps = sys.argv[2] if len(sys.argv) > 2 else "1"
if not shutil.which("ffmpeg"):
    sys.exit("ffmpeg not found. Install with: brew install ffmpeg (or use the .webm in the run's video/ folder)")
frames = sorted((run / "frames").glob("*_after.jpg"))
if not frames:
    sys.exit("no frames found")
with tempfile.TemporaryDirectory() as td:
    for i, f in enumerate(frames):
        (Path(td) / f"{i:04d}.jpg").symlink_to(f.resolve())
    out = run / "demo.mp4"
    subprocess.run(["ffmpeg", "-y", "-framerate", fps, "-i", f"{td}/%04d.jpg", "-vf", "scale=1280:-2,format=yuv420p", str(out)], check=True)
print("wrote", out)
