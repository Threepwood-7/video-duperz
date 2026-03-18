"""Export helpers for serializing scan results to CSV and JSON."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database


@dataclass(slots=True)
class ScanExportPaths:
    """Output file paths produced by one scan export."""

    duplicates_csv: Path
    duplicates_json: Path
    links_csv: Path
    links_json: Path


def export_scan(db: Database, scan_id: int, out_dir: str | Path) -> ScanExportPaths:
    """Write the selected scan's duplicate groups and links to export files."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    groups = db.load_duplicate_groups(scan_id)
    links = db.load_scan_links(scan_id)
    scan = db.get_scan_info(scan_id)

    duplicates_csv = out / "duplicates.csv"
    with duplicates_csv.open("w", encoding="utf-8", newline="") as f:
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

    duplicates_json = out / "duplicates.json"
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
    duplicates_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    links_csv = out / "links.csv"
    with links_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "scan_id",
                "link_kind",
                "link_path",
                "target_original_path",
                "target_exists",
                "source_root",
            ]
        )
        for link in links:
            writer.writerow(
                [
                    link.scan_id,
                    link.link_kind,
                    link.link_path,
                    link.target_original_path,
                    "1" if link.target_exists else "0",
                    link.source_root,
                ]
            )

    links_json = out / "links.json"
    links_payload = {
        "scan_id": scan_id,
        "created_at": scan["created_at"],
        "profile": scan["profile"],
        "links": [
            {
                "scan_id": link.scan_id,
                "link_kind": link.link_kind,
                "link_path": link.link_path,
                "target_original_path": link.target_original_path,
                "target_exists": link.target_exists,
                "source_root": link.source_root,
            }
            for link in links
        ],
    }
    links_json.write_text(json.dumps(links_payload, indent=2), encoding="utf-8")
    return ScanExportPaths(
        duplicates_csv=duplicates_csv,
        duplicates_json=duplicates_json,
        links_csv=links_csv,
        links_json=links_json,
    )
