# Jira2Zammad

Migrates a Jira Service Desk project into [Zammad](https://zammad.org/), preserving
tickets, comments, attachments, assignees, reporters, and original timestamps.

## What it does

- **`jira_to_zammad.py`** — the main migration. For every issue in a Jira project it:
  - creates a Zammad ticket with the issue's title/description as the first article
  - resolves the Jira reporter to a Zammad customer, and the assignee to a Zammad agent (creating users as needed)
  - migrates every comment as a separate ticket article, attributed to the right user
  - uploads attachments inline
  - preserves the original Jira `created` timestamp on the ticket and every article/comment
  - assigns all Zammad users to an organisation by email domain, as a final step

- **`extract_org_domains.py`** / **`create_zammad_orgs.py`** — optional pair of scripts to
  pre-create and name organisations (by reviewing a CSV) before running the main migration,
  if you want more control over organisation names than the automatic domain-based assignment gives you.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your values
```

Required environment variables (see `.env.example`):

| Variable | Description |
|---|---|
| `JIRA_BASE_URL` | e.g. `https://your-domain.atlassian.net` |
| `JIRA_EMAIL` | Jira account email (or username, for Jira Server) |
| `JIRA_API_TOKEN` | Jira API token / password |
| `JIRA_PROJECT_KEY` | The project to migrate, e.g. `PROJ` |
| `ZAMMAD_BASE_URL` | e.g. `https://zammad.example.com` |
| `ZAMMAD_API_TOKEN` | Zammad API token — the token's user needs admin rights (see below) |
| `ZAMMAD_GROUP` | Zammad group new tickets are created in (default: `Users`) |
| `MAX_ATTACHMENT_MB` | Attachments larger than this are skipped with a warning (default: `10`) |

### Zammad-side prerequisites

- The API token's user needs admin rights: creating/updating users, roles, groups,
  organizations, and tickets.
- **To preserve original Jira timestamps**, enable Zammad's `import_mode` system setting
  before running (`PUT /api/v1/settings/152` with `{"state_current": {"value": true}}`),
  and disable it again afterward. Without this, Zammad overwrites `created_at` with the
  actual import time regardless of what's sent in the payload. Import mode also suppresses
  outgoing notifications during the run — another reason to turn it back off when done.
- If ticket creation fails with `422 Invalid value for field 'owner_id'`, the Zammad user
  being set as owner needs both the **Agent** role and explicit group access
  (`group_ids`) to `ZAMMAD_GROUP` — the script handles this automatically via
  `ensure_agent_role()`, but it's worth knowing if you're debugging a related issue by hand.
- Large attachments can trip a reverse proxy's body-size limit in front of Zammad (e.g.
  nginx `client_max_body_size`), returning `413 Request Entity Too Large`. The script
  skips attachments over `MAX_ATTACHMENT_MB` client-side to avoid this failing the whole
  ticket; raise the proxy's limit if you'd rather migrate the large ones too.

## Usage

```bash
# Preview what would happen without touching Zammad
python3 jira_to_zammad.py --dry-run -v

# Test against a handful of issues first
python3 jira_to_zammad.py --limit 5

# Full run
python3 jira_to_zammad.py
```

Other flags:

- `--resume-from KEY` — skip issues before this Jira key (in created-ascending order)
- `--project-key KEY` — override `JIRA_PROJECT_KEY` for this run
- `-v` / `--verbose` — debug-level logging

### Resuming after a failure or interruption

Each successfully migrated issue is recorded in `migration_state.json`
(`jira_key → zammad_ticket_id`) as it happens. If the script is interrupted or crashes,
just re-run it — already-migrated issues are detected via this file and skipped, so you
won't get duplicate tickets. Delete `migration_state.json` to force a full re-migration
(only makes sense against a fresh/empty Zammad instance, since re-running otherwise
recreates every ticket).

Issues that fail outright (not interrupted, but a real per-issue error) are **not** added
to the state file, are logged to `migration_failed.txt` / `migration_failed_detail.txt`
at the end of the run, and will be retried automatically on the next run.

## Known limitations

- One Jira project per run (`JIRA_PROJECT_KEY`).
- Only plain-text is extracted from Jira's rich-text (ADF) fields — formatting, tables,
  and inline images in the description/comments are not preserved, only their text content.
- Designed for a one-time migration into a **fresh/empty** Zammad instance. Re-running
  against a Zammad instance that already has unrelated tickets is safe (state-file resume
  only prevents re-migrating what *this script* already created), but the script doesn't
  attempt to detect or merge against manually-created Zammad tickets.

## License

MIT — see [LICENSE](LICENSE).
