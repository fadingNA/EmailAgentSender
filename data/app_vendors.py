#!/usr/bin/env python3
"""Collect unique application vendors from CSV files in the data directory."""

import argparse
import csv
import json
from pathlib import Path


DEFAULT_DATA_DIR = Path(__file__).resolve().parent
DEFAULT_VENDOR_COLUMN = "app_vendor"
DEFAULT_VENDOR_LIMIT = 50
DEFAULT_CACHE_PATH = DEFAULT_DATA_DIR / ".cache" / "app_vendors.json"


def normalize_vendor_name(value):
    return " ".join(str(value).strip().split())


def split_vendor_cell(value):
    if value is None:
        return []

    parts = [str(value)]
    for separator in (";", "|", "\n"):
        next_parts = []
        for part in parts:
            next_parts.extend(part.split(separator))
        parts = next_parts

    return [normalize_vendor_name(part) for part in parts if normalize_vendor_name(part)]


def csv_files(data_dir=DEFAULT_DATA_DIR):
    data_path = Path(data_dir)
    if not data_path.exists():
        return []
    return sorted(path for path in data_path.rglob("*.csv") if path.is_file())


def collect_app_vendors(
    data_dir=DEFAULT_DATA_DIR,
    column_name=DEFAULT_VENDOR_COLUMN,
    limit=DEFAULT_VENDOR_LIMIT,
):
    """Return de-duplicated app_vendor values from CSV files under data_dir."""
    seen = set()
    vendors = []

    for path in csv_files(data_dir):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                continue

            field_lookup = {
                field.strip().lower(): field
                for field in reader.fieldnames
                if field and field.strip()
            }
            vendor_field = field_lookup.get(column_name.lower())
            if not vendor_field:
                continue

            for row in reader:
                for vendor in split_vendor_cell(row.get(vendor_field)):
                    vendor_key = vendor.casefold()
                    if vendor_key in seen:
                        continue
                    seen.add(vendor_key)
                    vendors.append(vendor)
                    if limit and len(vendors) >= limit:
                        return vendors

    return vendors


def read_cached_app_vendors(cache_path=DEFAULT_CACHE_PATH):
    path = Path(cache_path)
    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    vendors = data.get("vendors")
    if not isinstance(vendors, list):
        return None
    return [normalize_vendor_name(vendor) for vendor in vendors if normalize_vendor_name(vendor)]


def write_cached_app_vendors(vendors, cache_path=DEFAULT_CACHE_PATH):
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"vendors": vendors}
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def collect_cached_app_vendors(
    data_dir=DEFAULT_DATA_DIR,
    column_name=DEFAULT_VENDOR_COLUMN,
    limit=DEFAULT_VENDOR_LIMIT,
    cache_path=DEFAULT_CACHE_PATH,
    refresh_cache=False,
):
    """Return cached vendors, rebuilding from CSV only when needed or requested."""
    if not refresh_cache:
        cached_vendors = read_cached_app_vendors(cache_path)
        if cached_vendors is not None:
            return cached_vendors[:limit] if limit else cached_vendors

    vendors = collect_app_vendors(data_dir, column_name, limit)
    write_cached_app_vendors(vendors, cache_path)
    return vendors


def format_vendor_context(vendors):
    if not vendors:
        return ""
    return ", ".join(vendors)


def main():
    parser = argparse.ArgumentParser(
        description="Print unique app_vendor values found in CSV files under data/."
    )
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="Directory containing CSV files.")
    parser.add_argument("--column", default=DEFAULT_VENDOR_COLUMN, help="Vendor column name.")
    parser.add_argument("--cache-path", default=DEFAULT_CACHE_PATH, help="Path to the vendor cache JSON file.")
    parser.add_argument("--refresh-cache", action="store_true", help="Rebuild the cache from CSV files.")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_VENDOR_LIMIT,
        help="Maximum vendors to print. Use 0 for no limit.",
    )
    args = parser.parse_args()

    vendors = collect_cached_app_vendors(
        args.data_dir,
        args.column,
        args.limit,
        args.cache_path,
        args.refresh_cache,
    )
    for vendor in vendors:
        print(vendor)


if __name__ == "__main__":
    main()
