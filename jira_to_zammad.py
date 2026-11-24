#!/usr/bin/env python3
"""
Jira Service Desk → Zammad Migration Script
Migrates: tickets (title + description), comments, attachments, assignees & reporters
"""

import argparse
import base64
import functools
import itertools
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION — set these via .env or environment variables
# (see .env.example for a template)
# ─────────────────────────────────────────────

JIRA_BASE_URL    = os.environ.get("JIRA_BASE_URL", "")
JIRA_EMAIL       = os.environ.get("JIRA_EMAIL", "")       # Jira Server username (not necessarily an email)
JIRA_API_TOKEN   = os.environ.get("JIRA_API_TOKEN", "")   # Jira Server password (or Personal Access Token if enabled)
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
JIRA_AUTH_TYPE   = os.environ.get("JIRA_AUTH_TYPE", "basic").lower()   # "basic" (email/username + token) or "bearer" (Server/DC Personal Access Token)

ZAMMAD_BASE_URL  = os.environ.get("ZAMMAD_BASE_URL", "")
ZAMMAD_API_TOKEN = os.environ.get("ZAMMAD_API_TOKEN", "")
ZAMMAD_GROUP     = os.environ.get("ZAMMAD_GROUP", "Users")   # change to match your Zammad group name if needed

# Attachments larger than this are skipped (with a warning) rather than
# risking a destination-side upload limit (e.g. nginx client_max_body_size)
# failing the whole ticket. Tune to match your Zammad host's actual limit.
MAX_ATTACHMENT_MB = float(os.environ.get("MAX_ATTACHMENT_MB", "10"))
MAX_ATTACHMENT_BYTES = int(MAX_ATTACHMENT_MB * 1024 * 1024)

_REQUIRED = [
    "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
    "ZAMMAD_BASE_URL", "ZAMMAD_API_TOKEN",
]

def validate_config():
    missing = [name for name in _REQUIRED if not globals()[name]]
    if JIRA_AUTH_TYPE == "bearer" and "JIRA_EMAIL" in missing:
        missing.remove("JIRA_EMAIL")   # PATs don't need a username
    if missing:
        logger.error(f"missing required configuration: {', '.join(missing)}")
        logger.error("Copy .env.example to .env and fill in your values, or export them as environment variables.")
        sys.exit(1)

# Optional: map Jira status names → Zammad state names
# Zammad defaults: "new", "open", "pending reminder", "pending close", "closed"
STATUS_MAP = {
    "Open":        "open",
    "In Progress": "open",
    "Waiting":     "pending reminder",
    "Waiting for support":  "open",
    "Waiting for customer": "open",
    "Pending":     "open",
    "2:nd Line":   "open",
    "Resolved":    "closed",
    "Closed":      "closed",
    "Done":        "closed",
    "Canceled":    "closed",
    "Cancelled":   "closed",
}

# Jira statuses that mean "cancelled": migrated as closed, but tagged so they
# stay distinguishable from solved tickets (Zammad has no cancelled state).
CANCELED_STATUSES = {"Canceled", "Cancelled"}

# Optional: map Jira priority names → Zammad priority names
# Zammad defaults: "1 low", "2 normal", "3 high"
PRIORITY_MAP = {
    "Lowest":  "1 low",
    "Low":     "1 low",
    "Medium":  "2 normal",
    "High":    "3 high",
    "Highest": "3 high",
    "Critical":"3 high",
}

STATE_FILE = Path("migration_state.json")

DRY_RUN = False
_dry_run_ticket_ids = itertools.count(1)
_dry_run_user_ids = itertools.count(1)

logger = logging.getLogger("jira_to_zammad")

def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("migration_run.log"),
        ],
    )

# ─────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

def with_retries(max_retries=3, backoff_base=2.0):
    """Retry transient network errors and 5xx/429 responses with exponential
    backoff. Does not retry 4xx errors (e.g. 422, 413) since those won't
    succeed on retry."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else None
                    attempt += 1
                    if status not in RETRYABLE_STATUS or attempt > max_retries:
                        raise
                except (requests.ConnectionError, requests.Timeout) as e:
                    attempt += 1
                    if attempt > max_retries:
                        raise
                wait = backoff_base ** attempt
                logger.warning(f"    ...transient error in {func.__name__}, retrying in {wait:.0f}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
        return wrapper
    return decorator

def jira_headers():
    if JIRA_AUTH_TYPE == "bearer":
        auth = f"Bearer {JIRA_API_TOKEN}"
    else:
        auth = "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": auth,
        "Accept": "application/json",
    }

def zammad_headers():
    return {
        "Authorization": f"Token token={ZAMMAD_API_TOKEN}",
        "Content-Type": "application/json",
    }

@with_retries()
def jira_get(path, params=None):
    r = requests.get(f"{JIRA_BASE_URL}/rest/api/2{path}",
                     headers=jira_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

@with_retries()
def jira_raw_get(url):
    r = requests.get(url, headers=jira_headers(), timeout=60)
    r.raise_for_status()
    return r.content

@with_retries()
def zammad_post(path, data):
    # Long timeouts: a ticket with hundreds of MB of attachments can take minutes
    # to upload and store, and timing out here would retry and duplicate the ticket.
    # urllib3 applies the *connect* timeout to each socket write while sending the
    # body, so it must also cover stalls mid-upload (e.g. nginx buffering to disk).
    r = requests.post(f"{ZAMMAD_BASE_URL}/api/v1{path}",
                      headers=zammad_headers(), json=data, timeout=(300, 600))
    r.raise_for_status()
    return r.json()

@with_retries()
def zammad_get(path):
    r = requests.get(f"{ZAMMAD_BASE_URL}/api/v1{path}",
                     headers=zammad_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

@with_retries()
def zammad_put(path, data):
    r = requests.put(f"{ZAMMAD_BASE_URL}/api/v1{path}",
                     headers=zammad_headers(), json=data, timeout=30)
    r.raise_for_status()
    return r.json()

def error_detail(e):
    """Return a string with the HTTP response body (if any) alongside the exception."""
    response = getattr(e, "response", None)
    if response is not None:
        try:
            return f"{e} | response body: {response.text[:1000]}"
        except Exception:
            pass
    return str(e)

# ─────────────────────────────────────────────
# Jira fetchers
# ─────────────────────────────────────────────

def fetch_all_jira_issues():
    issues = []
    start = 0
    batch = 50
    while True:
        data = jira_get("/search", params={
            "jql": f"project={JIRA_PROJECT_KEY} ORDER BY created ASC",
            "startAt": start,
            "maxResults": batch,
            "fields": "summary,description,status,priority,assignee,reporter,comment,attachment,created,updated,resolution"
        })
        issues.extend(data["issues"])
        logger.info(f"  Fetched {len(issues)}/{data['total']} issues...")
        if start + batch >= data["total"]:
            break
        start += batch
        time.sleep(0.2)
    return issues

def extract_text(adf_node):
    """Recursively extract plain text from Atlassian Document Format."""
    if not adf_node:
        return ""
    if isinstance(adf_node, str):
        return adf_node
    ntype = adf_node.get("type", "")
    text = adf_node.get("text", "")
    content = adf_node.get("content", [])
    parts = [text] + [extract_text(c) for c in content]
    joined = "".join(parts)
    if ntype in ("paragraph", "heading", "bulletList", "listItem", "blockquote"):
        joined = joined.rstrip() + "\n"
    return joined

def download_attachment(attachment):
    """Download a Jira attachment and return (filename, base64_data, mime_type),
    or None if it exceeds MAX_ATTACHMENT_BYTES."""
    size = attachment.get("size")
    if size and size > MAX_ATTACHMENT_BYTES:
        logger.warning(
            f"    skipping attachment '{attachment['filename']}' "
            f"({size / 1024 / 1024:.1f} MB > {MAX_ATTACHMENT_MB:.0f} MB limit)"
        )
        return None
    content = jira_raw_get(attachment["content"])
    if len(content) > MAX_ATTACHMENT_BYTES:
        logger.warning(
            f"    skipping attachment '{attachment['filename']}' "
            f"({len(content) / 1024 / 1024:.1f} MB > {MAX_ATTACHMENT_MB:.0f} MB limit)"
        )
        return None
    return (
        attachment["filename"],
        base64.b64encode(content).decode(),
        attachment.get("mimeType", "application/octet-stream"),
    )

# ─────────────────────────────────────────────
# Zammad helpers
# ─────────────────────────────────────────────

_zammad_users_cache = {}
_zammad_orgs_cache = {}
_zammad_role_id_cache = {}
_zammad_group_id_cache = {}
_agent_role_ensured = set()

def get_role_id(role_name):
    if role_name in _zammad_role_id_cache:
        return _zammad_role_id_cache[role_name]
    roles = zammad_get("/roles")
    for r in roles:
        if r.get("name") == role_name:
            _zammad_role_id_cache[role_name] = r["id"]
            return r["id"]
    raise RuntimeError(f"Zammad role '{role_name}' not found")

def get_group_id(group_name):
    if group_name in _zammad_group_id_cache:
        return _zammad_group_id_cache[group_name]
    groups = zammad_get("/groups")
    for g in groups:
        if g.get("name") == group_name:
            _zammad_group_id_cache[group_name] = g["id"]
            return g["id"]
    raise RuntimeError(f"Zammad group '{group_name}' not found")

def ensure_agent_role(email, user_id):
    """Zammad's ticket owner_id must reference a user with the Agent role
    AND explicit access to the ticket's group."""
    if email in _agent_role_ensured:
        return
    agent_role_id = get_role_id("Agent")
    group_id = get_group_id(ZAMMAD_GROUP)
    user = zammad_get(f"/users/{user_id}")
    role_ids = set(user.get("role_ids") or [])
    group_ids = dict(user.get("group_ids") or {})
    changed = False
    if agent_role_id not in role_ids:
        role_ids.add(agent_role_id)
        changed = True
    if str(group_id) not in group_ids:
        group_ids[str(group_id)] = ["full"]
        changed = True
    if changed:
        zammad_put(f"/users/{user_id}", {"role_ids": list(role_ids), "group_ids": group_ids})
    _agent_role_ensured.add(email)

def find_or_create_zammad_user(email, display_name, as_agent=False):
    if not email:
        return None
    user_id = _zammad_users_cache.get(email)
    if user_id is None:
        if DRY_RUN:
            user_id = next(_dry_run_user_ids)
            logger.debug(f"    [dry-run] would find/create Zammad user for {email}")
        else:
            # Search for existing user
            try:
                results = zammad_get(f"/users/search?query={requests.utils.quote(email)}")
                for u in results:
                    if u.get("email", "").lower() == email.lower():
                        user_id = u["id"]
                        break
            except Exception:
                pass
            if user_id is None:
                # Create new user (default: customer role; ensure_agent_role() below
                # handles granting Agent role + group access when as_agent=True)
                parts = display_name.split(" ", 1)
                user = zammad_post("/users", {
                    "firstname": parts[0],
                    "lastname": parts[1] if len(parts) > 1 else "",
                    "email": email,
                    "role_ids": [],
                })
                user_id = user["id"]
        _zammad_users_cache[email] = user_id
    if as_agent and not DRY_RUN:
        ensure_agent_role(email, user_id)
    return user_id

def find_or_create_zammad_org(domain):
    if domain in _zammad_orgs_cache:
        return _zammad_orgs_cache[domain]
    if DRY_RUN:
        org_id = -len(_zammad_orgs_cache) - 1
        _zammad_orgs_cache[domain] = org_id
        return org_id
    try:
        results = zammad_get(f"/organizations/search?query={requests.utils.quote(domain)}")
        for org in (results or []):
            if org.get("name", "").lower() == domain.lower():
                _zammad_orgs_cache[domain] = org["id"]
                return org["id"]
    except Exception:
        pass
    org = zammad_post("/organizations", {"name": domain})
    _zammad_orgs_cache[domain] = org["id"]
    return org["id"]

def fetch_all_zammad_users():
    users = []
    page = 1
    per_page = 100
    while True:
        batch = zammad_get(f"/users?page={page}&per_page={per_page}")
        if not batch:
            break
        users.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return users

def assign_users_to_organisations():
    logger.info("=" * 60)
    logger.info("Assigning Zammad users to organisations by email domain")
    logger.info("=" * 60)

    logger.info("Fetching all Zammad users...")
    users = fetch_all_zammad_users()
    logger.info(f"  Total users: {len(users)}")

    assigned, skipped, failed = 0, 0, 0
    for user in users:
        email = user.get("email", "")
        user_id = user["id"]
        if not email or "@" not in email:
            skipped += 1
            continue
        if user.get("organization_id"):
            skipped += 1
            continue
        domain = email.split("@", 1)[1].lower()
        try:
            org_id = find_or_create_zammad_org(domain)
            zammad_put(f"/users/{user_id}", {"organization_id": org_id})
            logger.info(f"  {email} → {domain} (org #{org_id})")
            assigned += 1
        except Exception as e:
            logger.error(f"  {email}: {e}")
            failed += 1
        time.sleep(0.1)

    logger.info(f"Done: {assigned} assigned, {skipped} skipped, {failed} errors")

def map_status(jira_status):
    """Map a Jira status object to a Zammad state. Unmapped statuses fall back
    on Jira's status category: "Done" → closed, anything else → open."""
    name = jira_status["name"]
    if name in STATUS_MAP:
        return STATUS_MAP[name]
    category = (jira_status.get("statusCategory") or {}).get("key")
    state = "closed" if category == "done" else "open"
    logger.warning(f"    unmapped Jira status '{name}' (category: {category}) → '{state}'; add it to STATUS_MAP")
    return state

def jira_tags(fields):
    """Zammad tags recording a cancelled Jira issue and its resolution."""
    if fields["status"]["name"] not in CANCELED_STATUSES:
        return []
    tags = ["jira-canceled"]
    resolution = (fields.get("resolution") or {}).get("name")
    if resolution:
        tags.append(resolution.lower())
    return tags

def map_priority(jira_priority_name):
    return PRIORITY_MAP.get(jira_priority_name or "Medium", "2 normal")

# ─────────────────────────────────────────────
# Migration core
# ─────────────────────────────────────────────

def migrate_issue(issue):
    fields = issue["fields"]
    jira_key = issue["key"]

    # --- Reporter / customer ---
    reporter = fields.get("reporter") or {}
    reporter_email = reporter.get("emailAddress", "")
    reporter_name  = reporter.get("displayName", "Unknown")
    customer_id = find_or_create_zammad_user(reporter_email, reporter_name)

    # --- Assignee / owner ---
    assignee = fields.get("assignee") or {}
    assignee_email = assignee.get("emailAddress", "")
    assignee_name  = assignee.get("displayName", "")
    owner_id = find_or_create_zammad_user(assignee_email, assignee_name, as_agent=True) if assignee_email else None

    # --- Description ---
    desc_raw = fields.get("description") or {}
    description = extract_text(desc_raw).strip() or "(no description)"

    # --- Build first article (the ticket body) ---
    created_at = fields.get("created")
    article = {
        "subject":     fields.get("summary", "(no title)"),
        "body":        f"[Imported from Jira {jira_key}]\n\n{description}",
        "type":        "note",
        "internal":    False,
        "sender":      "Customer",
    }
    if created_at:
        article["created_at"] = created_at

    # --- Attachments on the issue itself ---
    attachments = []
    for att in fields.get("attachment") or []:
        try:
            result = download_attachment(att)
            if result is None:
                continue
            fname, b64data, mime = result
            attachments.append({
                "filename": fname,
                "data":     b64data,
                "mime-type": mime,
            })
            logger.info(f"    attachment: {fname}")
        except Exception as e:
            logger.warning(f"    could not download attachment '{att['filename']}': {e}")

    if attachments:
        article["attachments"] = attachments

    # --- Ticket payload ---
    ticket_payload = {
        "title":       fields.get("summary", "(no title)"),
        "state":       map_status(fields["status"]),
        "priority":    map_priority((fields.get("priority") or {}).get("name")),
        "group":       ZAMMAD_GROUP,
        "article":     article,
    }
    if customer_id:
        ticket_payload["customer_id"] = customer_id
    if owner_id:
        ticket_payload["owner_id"] = owner_id
    if created_at:
        ticket_payload["created_at"] = created_at
    tags = jira_tags(fields)
    if tags:
        ticket_payload["tags"] = ",".join(tags)

    # Create the ticket
    if DRY_RUN:
        ticket_id = next(_dry_run_ticket_ids)
        logger.info(f"  [dry-run] would create ticket ← {jira_key}: {fields['summary'][:60]}")
    else:
        ticket = zammad_post("/tickets", ticket_payload)
        ticket_id = ticket["id"]
        logger.info(f"  Created Zammad ticket #{ticket_id} ← {jira_key}: {fields['summary'][:60]}")

    # --- Comments ---
    comments = (fields.get("comment") or {}).get("comments", [])
    for comment in comments:
        author = comment.get("author") or {}
        author_email = author.get("emailAddress", "")
        author_name  = author.get("displayName", "Jira User")
        body_text = extract_text(comment.get("body", {})).strip() or "(empty comment)"

        comment_article = {
            "ticket_id": ticket_id,
            "subject":   "Comment",
            "body":      f"[{author_name}]:\n{body_text}",
            "type":      "note",
            "internal":  False,
            "sender":    "Agent",
        }
        author_id = find_or_create_zammad_user(author_email, author_name)
        if author_id:
            comment_article["created_by_id"] = author_id
        if comment.get("created"):
            comment_article["created_at"] = comment["created"]

        if DRY_RUN:
            logger.debug(f"    [dry-run] would add comment by {author_name}")
        else:
            zammad_post("/ticket_articles", comment_article)
            logger.debug(f"    comment by {author_name}")
            time.sleep(0.1)

    return ticket_id

# ─────────────────────────────────────────────
# State (resume support)
# ─────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            logger.warning(f"could not parse {STATE_FILE}, starting with empty state")
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Migrate a Jira Service Desk project to Zammad.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Fetch and process issues from Jira but don't create or modify anything in Zammad.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                         help="Only migrate the first N pending issues. Useful for testing.")
    parser.add_argument("--resume-from", metavar="KEY", default=None,
                         help="Skip issues before this Jira key (in created-ascending order). "
                              "Runs already resume automatically via %s; use this to skip further ahead." % STATE_FILE)
    parser.add_argument("--project-key", default=None,
                         help="Override JIRA_PROJECT_KEY from config.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()

def main():
    global JIRA_PROJECT_KEY, DRY_RUN

    args = parse_args()
    setup_logging(args.verbose)
    if args.project_key:
        JIRA_PROJECT_KEY = args.project_key
    DRY_RUN = args.dry_run

    logger.info("=" * 60)
    logger.info("Jira → Zammad Migration" + ("  [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 60)

    validate_config()

    logger.info(f"[1/3] Fetching issues from Jira project '{JIRA_PROJECT_KEY}'...")
    try:
        issues = fetch_all_jira_issues()
    except requests.HTTPError as e:
        logger.error(f"fetching Jira issues: {e}\nCheck JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY.")
        sys.exit(1)
    logger.info(f"  Total issues found: {len(issues)}")

    if args.resume_from:
        idx = next((i for i, iss in enumerate(issues) if iss["key"] == args.resume_from), None)
        if idx is not None:
            skipped_count = idx
            issues = issues[idx:]
            logger.info(f"  --resume-from {args.resume_from}: skipping {skipped_count} earlier issues")
        else:
            logger.warning(f"  --resume-from key {args.resume_from} not found among fetched issues; ignoring")

    state = {} if DRY_RUN else load_state()
    if state:
        logger.info(f"  {len(state)} issues already migrated per {STATE_FILE}, will skip")

    if args.limit:
        pending = [iss for iss in issues if iss["key"] not in state][:args.limit]
        pending_keys = {iss["key"] for iss in pending}
        issues = [iss for iss in issues if iss["key"] in pending_keys or iss["key"] in state]

    logger.info(f"[2/3] Migrating {len(issues)} issues to Zammad...")
    success, failed = 0, []
    for i, issue in enumerate(issues, 1):
        key = issue["key"]
        if key in state:
            logger.debug(f"[{i}/{len(issues)}] {key} — already migrated (ticket #{state[key]}), skipping")
            success += 1
            continue
        logger.info(f"[{i}/{len(issues)}] {key}")
        try:
            ticket_id = migrate_issue(issue)
            success += 1
            if not DRY_RUN:
                state[key] = ticket_id
                save_state(state)
        except Exception as e:
            detail = error_detail(e)
            logger.error(f"  FAILED: {detail}")
            failed.append((key, detail))
        time.sleep(0.3)   # be gentle on both APIs

    logger.info("[3/4] Done migrating.")
    logger.info(f"  Migrated: {success}/{len(issues)} tickets")
    if failed:
        logger.info(f"  Failed:   {len(failed)} tickets → {', '.join(k for k, _ in failed)}")
        Path("migration_failed.txt").write_text(
            "\n".join(key for key, _ in failed)
        )
        Path("migration_failed_detail.txt").write_text(
            "\n".join(f"{key}\t{detail}" for key, detail in failed)
        )
        logger.info("  Failed keys written to migration_failed.txt")
        logger.info("  Failure details written to migration_failed_detail.txt")

    if DRY_RUN:
        logger.info("[4/4] Skipping organisation assignment ([DRY RUN]).")
    else:
        logger.info("[4/4] Assigning users to organisations...")
        assign_users_to_organisations()

if __name__ == "__main__":
    main()
