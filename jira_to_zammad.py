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
from datetime import datetime, timezone
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
    for comment in fetch_comments(issue):
        add_comment(ticket_id, comment)

    return ticket_id

def fetch_comments(issue):
    """All comments of an issue; the search API may truncate the embedded list."""
    embedded = issue["fields"].get("comment") or {}
    comments = embedded.get("comments", [])
    if embedded.get("total", 0) > len(comments):
        comments = jira_get(f"/issue/{issue['key']}/comment", params={"maxResults": 1000})["comments"]
    return comments

def add_comment(ticket_id, comment):
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

# ─────────────────────────────────────────────
# Sync (bring already-migrated tickets up to date)
# ─────────────────────────────────────────────

_zammad_state_names = {}

def zammad_state_name(state_id):
    if not _zammad_state_names:
        _zammad_state_names.update({s["id"]: s["name"] for s in zammad_get("/ticket_states")})
    return _zammad_state_names[state_id]

def parse_ts(value):
    """Jira ("...+0100") or Zammad ("...Z") timestamp → aware UTC datetime."""
    return datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)

def sync_issue(issue, ticket_id, jira_wins=False):
    """Add Jira comments and attachments missing from the Zammad ticket and update its state.

    Comments are matched on their creation timestamp, which the migration
    preserves. A state mismatch is resolved in Jira's favour only when Jira
    changed after the Zammad ticket was last updated (or jira_wins is set);
    otherwise the ticket is left alone and reported as a conflict.
    Returns (comments_added, attachments_added, state_changed, conflict_message_or_None)."""
    fields = issue["fields"]
    ticket = zammad_get(f"/tickets/{ticket_id}")
    current = zammad_state_name(ticket["state_id"])
    target = map_status(fields["status"])
    ticket_updated = parse_ts(ticket["updated_at"])

    articles = zammad_get(f"/ticket_articles/by_ticket/{ticket_id}")
    have = {parse_ts(a["created_at"]).replace(microsecond=0) for a in articles}
    missing = [c for c in fetch_comments(issue)
               if c.get("created") and parse_ts(c["created"]).replace(microsecond=0) not in have]
    for comment in missing:
        add_comment(ticket_id, comment)
    if missing:
        logger.info(f"    {'[dry-run] would add' if DRY_RUN else 'added'} {len(missing)} comment(s)")

    # Attachments added in Jira after migration go on one extra article,
    # dated like the newest of them so the ticket timeline stays in order.
    have_files = {att["filename"] for a in articles for att in a.get("attachments", [])}
    late = [a for a in fields.get("attachment") or [] if a["filename"] not in have_files]
    uploads = []
    for att in late:
        result = download_attachment(att)   # None if over MAX_ATTACHMENT_MB (already logged)
        if result:
            fname, b64data, mime = result
            uploads.append({"filename": fname, "data": b64data, "mime-type": mime})
    if uploads:
        names = [u["filename"] for u in uploads]
        article = {
            "ticket_id":   ticket_id,
            "subject":     "Attachments",
            "body":        "[Attachments added in Jira]\n" + "\n".join(names),
            "type":        "note",
            "internal":    False,
            "sender":      "Agent",
            "attachments": uploads,
            "created_at":  max((a["created"] for a in late), key=parse_ts),
        }
        if DRY_RUN:
            logger.info(f"    [dry-run] would add {len(uploads)} attachment(s): {', '.join(names)}")
        else:
            zammad_post("/ticket_articles", article)
            logger.info(f"    added {len(uploads)} attachment(s): {', '.join(names)}")

    # State last: adding articles to a closed ticket could otherwise reopen it.
    conflict, state_changed = None, False
    if current != target:
        if jira_wins or parse_ts(fields["updated"]) > ticket_updated:
            state_changed = True
            if DRY_RUN:
                logger.info(f"    [dry-run] would change state {current} → {target}")
            else:
                zammad_put(f"/tickets/{ticket_id}", {"state": target})
                logger.info(f"    state {current} → {target}")
        else:
            conflict = (f"Zammad '{current}' vs Jira '{fields['status']['name']}' → '{target}': "
                        f"changed in Zammad after Jira; left as is (use --jira-wins {issue['key']})")

    return len(missing), len(uploads), state_changed, conflict

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
    parser.add_argument("--sync", action="store_true",
                         help="Also update already-migrated tickets: add Jira comments and attachments missing in Zammad and "
                              "update the state when Jira changed more recently than the Zammad ticket.")
    parser.add_argument("--jira-wins", metavar="KEYS", default="",
                         help="Comma-separated Jira keys whose Zammad state is overwritten from Jira during --sync "
                              "even if the ticket was changed in Zammad more recently.")
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

    state = load_state()
    if state:
        action = "will sync" if args.sync else "will skip"
        logger.info(f"  {len(state)} issues already migrated per {STATE_FILE}, {action}")
    jira_wins = {k.strip() for k in args.jira_wins.split(",") if k.strip()}
    synced_comments, synced_files, synced_states, conflicts = 0, 0, 0, []

    if args.limit:
        pending = [iss for iss in issues if iss["key"] not in state][:args.limit]
        pending_keys = {iss["key"] for iss in pending}
        issues = [iss for iss in issues if iss["key"] in pending_keys or iss["key"] in state]

    logger.info(f"[2/3] Migrating {len(issues)} issues to Zammad...")
    success, failed = 0, []
    for i, issue in enumerate(issues, 1):
        key = issue["key"]
        if key in state:
            if not args.sync:
                logger.debug(f"[{i}/{len(issues)}] {key} — already migrated (ticket #{state[key]}), skipping")
                success += 1
                continue
            try:
                added, files, changed, conflict = sync_issue(issue, state[key], jira_wins=key in jira_wins)
                if added or files or changed or conflict:
                    logger.info(f"[{i}/{len(issues)}] {key} → ticket #{state[key]} synced")
                if conflict:
                    logger.warning(f"    CONFLICT: {conflict}")
                    conflicts.append((key, state[key], conflict))
                synced_comments += added
                synced_files += files
                synced_states += changed
                success += 1
            except Exception as e:
                detail = error_detail(e)
                logger.error(f"  {key} sync FAILED: {detail}")
                failed.append((key, detail))
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
    if args.sync:
        verb = "would be " if DRY_RUN else ""
        logger.info(f"  Sync: {synced_states} state change(s), {synced_comments} comment(s) and "
                    f"{synced_files} attachment(s) {verb}applied")
        for key, tid, msg in conflicts:
            logger.info(f"  Conflict {key} (#{tid}): {msg}")
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
