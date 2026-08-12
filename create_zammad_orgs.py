#!/usr/bin/env python3
"""
Step 2: Read organizations.csv and create organizations in Zammad with domain mapping.
Run extract_org_domains.py first and review the CSV before running this.
"""

import csv
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

ZAMMAD_BASE_URL  = os.environ.get("ZAMMAD_BASE_URL", "")
ZAMMAD_API_TOKEN = os.environ.get("ZAMMAD_API_TOKEN", "")

INPUT_FILE = "organizations.csv"


def validate_config():
    missing = [name for name in ("ZAMMAD_BASE_URL", "ZAMMAD_API_TOKEN") if not globals()[name]]
    if missing:
        print(f"ERROR: missing required configuration: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your values, or export them as environment variables.")
        sys.exit(1)


def zammad_headers():
    return {
        "Authorization": f"Token token={ZAMMAD_API_TOKEN}",
        "Content-Type": "application/json",
    }


def fetch_existing_orgs():
    """Return a set of already-existing organization names (lowercase) to avoid duplicates."""
    existing = {}
    page = 1
    per_page = 100
    while True:
        r = requests.get(
            f"{ZAMMAD_BASE_URL}/api/v1/organizations",
            headers=zammad_headers(),
            params={"page": page, "per_page": per_page},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for org in batch:
            existing[org["name"].lower()] = org["id"]
        if len(batch) < per_page:
            break
        page += 1
    return existing


def create_org(name, domain):
    r = requests.post(
        f"{ZAMMAD_BASE_URL}/api/v1/organizations",
        headers=zammad_headers(),
        json={
            "name": name,
            "domain": domain,
            "domain_assignment": True,   # auto-assign users with matching email domain
            "active": True,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main():
    validate_config()
    try:
        with open(INPUT_FILE, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"ERROR: {INPUT_FILE} not found. Run extract_org_domains.py first.")
        sys.exit(1)

    print(f"Loaded {len(rows)} entries from {INPUT_FILE}")
    print("Fetching existing Zammad organizations...")
    existing = fetch_existing_orgs()
    print(f"  {len(existing)} organizations already exist")

    created, skipped, failed = 0, 0, []

    for row in rows:
        name = row["organization_name"].strip()
        domain = row["domain"].strip()
        if not name or not domain:
            continue

        if name.lower() in existing:
            print(f"  SKIP (exists): {name}")
            skipped += 1
            continue

        try:
            org = create_org(name, domain)
            print(f"  ✓ Created: {name} ({domain}) → id={org['id']}")
            created += 1
        except requests.HTTPError as e:
            print(f"  ✗ FAILED: {name} ({domain}): {e}")
            failed.append(name)

    print(f"\nDone. Created: {created}  Skipped: {skipped}  Failed: {len(failed)}")
    if failed:
        print(f"Failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
