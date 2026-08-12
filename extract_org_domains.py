#!/usr/bin/env python3
"""
Step 1: Extract unique email domains from Zammad users and write organizations.csv.
Review and edit organizations.csv before running create_zammad_orgs.py.
"""

import csv
import os
import re
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

ZAMMAD_BASE_URL  = os.environ.get("ZAMMAD_BASE_URL", "")
ZAMMAD_API_TOKEN = os.environ.get("ZAMMAD_API_TOKEN", "")

OUTPUT_FILE = "organizations.csv"


def validate_config():
    missing = [name for name in ("ZAMMAD_BASE_URL", "ZAMMAD_API_TOKEN") if not globals()[name]]
    if missing:
        print(f"ERROR: missing required configuration: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your values, or export them as environment variables.")
        sys.exit(1)

# Known second-level TLDs where the org name sits one level higher
SECOND_LEVEL_TLDS = {
    "co.uk", "co.nz", "co.jp", "co.za", "co.in",
    "com.au", "com.br", "com.mx", "com.ar",
    "org.uk", "net.au", "ac.uk", "gov.uk",
}

SKIP_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}


def zammad_headers():
    return {
        "Authorization": f"Token token={ZAMMAD_API_TOKEN}",
        "Content-Type": "application/json",
    }


def fetch_all_users():
    users = []
    page = 1
    per_page = 100
    while True:
        r = requests.get(
            f"{ZAMMAD_BASE_URL}/api/v1/users",
            headers=zammad_headers(),
            params={"page": page, "per_page": per_page},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        users.extend(batch)
        print(f"  Fetched {len(users)} users...")
        if len(batch) < per_page:
            break
        page += 1
    return users


def domain_from_email(email):
    if "@" not in email:
        return None
    return email.strip().lower().split("@", 1)[1]


def org_name_from_domain(domain):
    """Derive a human-readable org name from a domain."""
    # Check for known two-part TLDs (e.g. co.uk)
    parts = domain.split(".")
    suffix2 = ".".join(parts[-2:]) if len(parts) >= 2 else ""
    if suffix2 in SECOND_LEVEL_TLDS and len(parts) >= 3:
        name_part = parts[-3]
    else:
        name_part = parts[-2] if len(parts) >= 2 else parts[0]

    # Replace hyphens/underscores with spaces and title-case
    name = re.sub(r"[-_]+", " ", name_part).title()
    return name


def main():
    validate_config()
    print("Fetching Zammad users...")
    users = fetch_all_users()
    print(f"Total users: {len(users)}")

    domain_map = {}  # domain -> suggested org name
    for user in users:
        email = user.get("email", "")
        if not email:
            continue
        domain = domain_from_email(email)
        if not domain or domain in SKIP_DOMAINS:
            continue
        if domain not in domain_map:
            domain_map[domain] = org_name_from_domain(domain)

    print(f"Unique domains found: {len(domain_map)}")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["organization_name", "domain"])
        for domain, name in sorted(domain_map.items()):
            writer.writerow([name, domain])

    print(f"Written to {OUTPUT_FILE} — review and edit before running create_zammad_orgs.py")


if __name__ == "__main__":
    main()
