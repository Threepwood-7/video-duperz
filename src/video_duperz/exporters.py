"""Export helpers for serializing scan results to CSV and JSON."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database


def export_scan(db: Database, scan_id: int, out_dir: str | Path) -> tuple[Path, Path]:
    """Write the selected scan's duplicate groups to CSV and JSON files."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    groups = db.load_duplicate_groups(scan_id)
    scan = db.get_scan_info(scan_id)

    csv_path = out / "duplicates.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "group_id",
                "file_path",
                "duration",
                "width",
                "height",
                "bitrate",
                "similarity_score",
                "is_keep_default",
                "selected_action",
            ]
        )
        for group in groups:
            for item in group.items:
                writer.writerow(
                    [
                        group.group_id,
                        item.path,
                        item.duration_s,
                        item.width,
                        item.height,
                        item.bitrate,
                        round(item.similarity_score, 6),
                        "1" if item.keep_default else "0",
                        item.selected_action,
                    ]
                )

    json_path = out / "duplicates.json"
    payload = {
        "scan_id": scan_id,
        "created_at": scan["created_at"],
        "profile": scan["profile"],
        "groups": [
            {
                "group_id": group.group_id,
                "total_size_bytes": group.total_size_bytes,
                "average_confidence": group.average_confidence,
                "reclaimable_bytes": group.reclaimable_bytes,
                "items": [
                    {
                        "file_id": item.file_id,
                        "file_path": item.path,
                        "duration": item.duration_s,
                        "width": item.width,
                        "height": item.height,
                        "bitrate": item.bitrate,
                        "codec": item.codec,
                        "similarity_score": item.similarity_score,
                        "is_keep_default": item.keep_default,
                        "selected_action": item.selected_action,
                    }
                    for item in group.items
                ],
            }
            for group in groups
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return csv_path, json_path
