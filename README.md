# Teams Repost Graph POC

FastAPI proof-of-concept for reconstructing a Microsoft Teams channel message into another Teams channel using Microsoft Graph. It creates a new post with an audit-friendly header, preserves the original HTML body where possible, recreates Teams hosted-content inline images, and reposts file attachments as native Teams attachment cards.

This does not recreate the native Teams "Share to channel" forwarded-message UI.

## Current Graph Shapes Used

These payloads and endpoints were checked against Microsoft Graph documentation on 2026-06-02:

- Send a channel message: `POST /teams/{team-id}/channels/{channel-id}/messages`
- Get a channel message or reply: `GET /teams/{team-id}/channels/{channel-id}/messages/{message-id}` and `/replies/{reply-id}`
- Hosted content: body image references use `../hostedContents/{temporaryId}/$value`, and `hostedContents` entries use `@microsoft.graph.temporaryId`, `contentBytes`, and `contentType`
- File attachments: source `reference` attachments are attached to the repost as native Teams `reference` attachment cards using their original `contentUrl`.
- List source channel messages: `GET /teams/{team-id}/channels/{channel-id}/messages`

Inline images are recreated when Microsoft Graph can accept them in the channel-message payload. Oversized inline media, GIFs, and other unsupported inline hosted-content types are omitted with an in-message source link placeholder and a saved warning so automation can continue without retrying the same post forever. Non-portable Teams connector and tab cards are omitted with a saved warning because the repost already links to the original message. File attachments are still required to attach natively as Teams attachment cards; unsupported file attachment types block the repost instead of becoming text links.

## Setup

1. Create and activate a virtual environment.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in your Microsoft Entra app values.

```powershell
Copy-Item .env.example .env
```

Runtime files default to `.data/` under the app folder. `TEMP_FOLDER` can be any writable temporary folder, and relative `TEMP_FOLDER`, `REPOST_HISTORY_PATH`, `POST_CACHE_PATH`, `EXCEPTION_LIST_PATH`, `REVERSE_EXCEPTION_LIST_PATH`, `MSAL_TOKEN_CACHE_PATH`, and `AUTOMATION_LOCK_PATH` values are resolved from the app folder.

3. Configure the Entra app registration as a delegated web or public-client flow.

- Redirect URI: `http://localhost:8000/auth/callback`
- Delegated permissions to grant/admin-consent as needed:
  - `ChannelMessage.Read.All`
  - `ChannelMessage.Send`

The configured flow uses explicit team/channel IDs from environment variables, so it does not request team/channel discovery scopes. Native `reference` attachment cards are reposted by reference and do not require file copy scopes.

`GRAPH_SCOPES` controls the scopes requested during Microsoft sign-in. It accepts whitespace-separated or comma-separated values, for example:

```env
GRAPH_SCOPES=offline_access ChannelMessage.Read.All ChannelMessage.Send
```

`offline_access` lets MSAL keep refreshing delegated Microsoft Graph tokens after the initial browser sign-in. The serialized MSAL cache is stored at `MSAL_TOKEN_CACHE_PATH`; treat this file like a backend secret. It must never be committed, logged, exposed under `/static`, or made readable by other server users.

`POST_CACHE_PATH` stores pulled source-channel posts separately from repost history. `POST_CACHE_MAX_REFRESH_PAGES` limits how many Microsoft Graph pages a refresh checks while looking for posts newer than the latest cached post.

`EXCEPTION_LIST_PATH` stores English-side email addresses that should be skipped. `REVERSE_EXCEPTION_LIST_PATH` stores the separate Chinese-side list; when it is omitted, the app uses a sibling file named like `exception-list-reverse.json`. Each manager UI can add or remove addresses for its own side, and the matching posts API excludes cached or newly pulled posts whose available sender email matches that side's list.

`OPENAI_API_KEY` enables the per-post translation button in the manager UI. The default page translates the configured English source channel to `OPENAI_TRANSLATION_TARGET` and reposts to the configured destination channel. The reverse page at `/reverse` reads the configured destination channel, translates posts to English (`en`), and reposts to the configured source channel. Translations are generated with `OPENAI_TRANSLATION_MODEL` and saved under each post in `POST_CACHE_PATH`, so already translated posts can be toggled without another OpenAI call. The manager Repost button posts the cached translation, not the original body; if the post is not translated yet, the UI translates it first.

Both manager pages automatically skip posts whose body text starts with `原文作者：`. This prevents reposted English-to-Chinese messages from being picked up by the Chinese-to-English flow, and reposted Chinese-to-English messages from being picked up by the English-to-Chinese flow.

`AUTOMATION_ENABLED=false` keeps the unattended worker paused. Set it to `true` only after signing in once and confirming the manager UI can read, translate, and repost. `AUTOMATION_FLOWS=forward,reverse` runs both directions, and `AUTOMATION_MAX_POSTS_PER_FLOW` caps how many cached posts each worker pass checks per flow.

## Translated reply synchronization

The isolated reply manager is available at `/reply-sync`. It reads successful top-level mappings from `REPOST_HISTORY_PATH`, but stores its registry, reply cache, reply history, temporary files, and automation lock separately under `.data/reply-sync/`. The automation lock is managed by the operating system, so crashes, container stops, and computer shutdowns release it automatically; the harmless lock filename may remain on disk. Existing post cache and repost history files are never rewritten by the reply module.

Reply automation is disabled by default. Discovery creates preview entries only; each translated thread must be activated as either `backfill_all` or `future_only`. The worker fetches every Microsoft Graph reply page, sorts replies oldest-first, requires two identical complete scans, and blocks later replies in a thread when an earlier reply fails. Supported JPEG/PNG inline images and `reference` attachments are recreated; unsupported content requires an explicit degraded send from the reply manager.

Set `REPLY_SYNC_AUTO_ENROLL_NEW_THREADS=true` to activate newly discovered mappings with `backfill_all`. Enabling it also promotes existing `preview` mappings on the next discovery pass, while threads that were explicitly paused remain paused.

Reciprocal reply synchronization is separately protected by `REPLY_SYNC_RETURN_ENABLED=false`. When enabled, each fully linked mapping gets a return thread whose source is the translated post and whose destination is the original post. Replies created by either paired thread are excluded from the other thread using reply history, preventing translation loops. Return threads are independent, so a return-side failure does not pause the existing original-to-translated thread.

All translated replies in both directions use one persistent queue at `REPLY_SYNC_QUEUE_PATH`. Complete source threads are scanned into the queue without creating Teams notifications. A global dispatcher selects the oldest eligible thread head, translates it when its turn arrives, and posts at most one Teams reply every `REPLY_SYNC_SEND_INTERVAL_MINUTES` (1 minute by default). Ordering is preserved within each thread, and both the backlog and last-send timestamp survive container and computer restarts.

For a controlled historical rollout, set `REPLY_SYNC_RETURN_BACKFILL_EXISTING_THREADS=true`. This promotes existing return previews to `backfill_all` but does not reactivate explicitly paused or superseded threads. Set `REPLY_SYNC_RETURN_AUTO_ENROLL_NEW_THREADS=true` to enroll future mappings as well. Historical and live replies in either direction enter the same ordered queue and remain subject to the global send interval. Setting `REPLY_SYNC_RETURN_ENABLED=false` makes retained return threads dormant without deleting their registry, cache, queue, or history state; primary threads remain queued and rate-limited.

Run one pass only after testing the manager and setting `REPLY_SYNC_ENABLED=true`:

```text
python -m reply_sync.worker --once
```

The optional `reply-sync-automation` Compose service is behind the `reply-sync` profile, and the separate `teams-repost-reply-sync.timer` systemd template schedules the same one-shot worker every minute, matching the one-minute global send interval.

4. Run the app.

```powershell
uvicorn main:app --reload
```

5. Sign in locally and open the manager UI.

Open `http://localhost:8000/`, sign in, then repost translated Chinese versions from the recent source-channel posts list. Open `http://localhost:8000/reverse` to manage Chinese-to-English reposts.

The browser sign-in also seeds the backend MSAL token cache used by automation. If the cache expires or is revoked, sign in again through the manager UI.

## Docker on Windows

Docker Desktop can run the app without `systemd`. The Compose setup runs two services from the same image:

- `web` serves the manager UI and API on `http://localhost:8000/`.
- `automation` runs `python -m automation_worker --once`, sleeps for 1 minute, and repeats.

Copy `.env.example` to `.env`, fill in the settings, and keep:

```env
REDIRECT_URI=http://localhost:8000/auth/callback
```

Start both services with:

```powershell
docker compose up --build
```

Both services mount `.data/` into the container, so the MSAL token cache, post cache, repost history, exception lists, and automation lock persist across container restarts. Keep `AUTOMATION_ENABLED=false` until you have signed in through the manager UI and confirmed manual read, translate, and repost behavior. To enable unattended reposting, set `AUTOMATION_ENABLED=true` in `.env` and restart Compose:

```powershell
docker compose up -d
```

The `systemd/` units remain for Linux server deployments. Docker uses the `automation` service loop instead of a `systemd` timer.

## Automation Worker

Run one unattended automation pass with:

```powershell
python -m automation_worker --once
```

When `AUTOMATION_ENABLED=false`, the command exits without calling Microsoft Graph. When enabled, it loads the backend MSAL cache, refreshes each configured flow, translates missing translations, reposts pending translated messages, and records successful reposts in `REPOST_HISTORY_PATH` to avoid duplicates.

To pause automation, set:

```env
AUTOMATION_ENABLED=false
```

Then restart the timer or service environment. Logging output reports counts and message IDs for failures, but never token or cache contents.

## systemd

Template units are in `systemd/` and assume the repo is deployed to `/opt/teams-repost` with a dedicated `teams-repost` OS user:

```bash
sudo cp systemd/teams-repost.service /etc/systemd/system/
sudo cp systemd/teams-repost-automation.service /etc/systemd/system/
sudo cp systemd/teams-repost-automation.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teams-repost.service
sudo systemctl enable --now teams-repost-automation.timer
```

Use `systemctl status teams-repost-automation.timer` and `journalctl -u teams-repost-automation.service` to inspect scheduled runs.

## Request

```json
{
  "source_message_url": "https://teams.microsoft.com/l/message/...",
  "destination_team_id": "00000000-0000-0000-0000-000000000000",
  "destination_channel_id": "19:destination-channel@thread.tacv2",
  "mode": "dry_run"
}
```

Use `"mode": "post"` to actually create the destination message. If destination IDs are omitted, `DESTINATION_TEAM_ID` and `DESTINATION_CHANNEL_ID` are used.

## Dry Run Behavior

Dry run never posts. It reads Graph metadata and returns:

- parsed source identifiers
- resolved destination team/channel
- original subject and author
- inline image count
- attachment count and names
- attachment links for the manager UI
- warnings
- whether inline image recreation will be attempted

## Tests

```powershell
python -m unittest discover -s tests
```

The test suite covers Teams URL parsing, hosted-content HTML replacement, filename sanitization, and mock-based Graph client behavior including retries and safe multiple-match failures.
