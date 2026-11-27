"""Claim at most one queued download without installing FFmpeg or spotDL."""
from __future__ import annotations
import os
from pathlib import Path
from process import api

def main() -> None:
    pending = api("GET", "/api/processor/next").get("job")
    path = Path(os.environ["GITHUB_OUTPUT"])
    with path.open("a", encoding="utf-8") as out:
        if pending:
            out.write("has_job=true\n")
            out.write("job_id=" + pending["id"] + "\n")
            out.write("playlist_url=" + pending.get("url", "") + "\n")
            out.write("kind=" + pending.get("kind", "spotify") + "\n")
            print("Queued playlist claimed:", pending["id"])
        else:
            out.write("has_job=false\n")
            print("No playlist queued. Skipping FFmpeg and spotDL.")

if __name__ == "__main__":
    main()
