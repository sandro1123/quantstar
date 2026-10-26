"""Preview mode — save reports to output/ without sending"""
import json
import os


def save_preview(content: dict, content_type: str, date: str):
    """Save report to output/preview/ for review"""
    output_dir = os.path.join("output", "preview")
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{content_type}_{date}.json"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    return filepath
