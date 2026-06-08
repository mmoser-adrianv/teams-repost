# Teams Repost Graph POC

FastAPI proof-of-concept for reconstructing a Microsoft Teams channel message into another Teams channel using Microsoft Graph. It creates a new post with an audit-friendly header, preserves the original HTML body where possible, attempts Teams hosted-content inline images, and keeps original attachment links in a local manager UI for manual handling.

This does not recreate the native Teams "Share to channel" forwarded-message UI.

## Current Graph Shapes Used

These payloads and endpoints were checked against Microsoft Graph documentation on 2026-06-02:

- Send a channel message: `POST /teams/{team-id}/channels/{channel-id}/messages`
- Get a channel message or reply: `GET /teams/{team-id}/channels/{channel-id}/messages/{message-id}` and `/replies/{reply-id}`
- Hosted content: body image references use `../hostedContents/{temporaryId}/$value`, and `hostedContents` entries use `@microsoft.graph.temporaryId`, `contentBytes`, and `contentType`
- List source channel messages: `GET /teams/{team-id}/channels/{channel-id}/messages`

Inline images are attempted when `TRY_INLINE_HOSTED_CONTENTS=true`. If Graph rejects the hosted-content payload in post mode, the app reposts without embedded images and keeps image download links in the manager UI. File attachments are not copied because the default permission model avoids SharePoint file scopes.

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

`TEMP_FOLDER` can be any writable temporary folder. Relative `TEMP_FOLDER`, `REPOST_HISTORY_PATH`, and `POST_CACHE_PATH` values are resolved from the app folder, and the sample config keeps runtime files under `.data/`.

3. Configure the Entra app registration as a delegated web or public-client flow.

- Redirect URI: `http://localhost:8000/auth/callback`
- Delegated permissions to grant/admin-consent as needed:
  - `ChannelMessage.Read.All`
  - `ChannelMessage.Send`

The configured flow uses explicit team/channel IDs from environment variables, so it does not request team/channel discovery scopes or SharePoint file scopes.

`GRAPH_SCOPES` controls the scopes requested during Microsoft sign-in. It accepts whitespace-separated or comma-separated values, for example:

```env
GRAPH_SCOPES=ChannelMessage.Read.All ChannelMessage.Send
```

`POST_CACHE_PATH` stores pulled source-channel posts separately from repost history. `POST_CACHE_MAX_REFRESH_PAGES` limits how many Microsoft Graph pages a refresh checks while looking for posts newer than the latest cached post.

`EXCEPTION_LIST_PATH` stores email addresses that should be skipped. The manager UI can add or remove addresses, and `/api/posts` excludes cached or newly pulled posts whose available sender email matches the list.

`OPENAI_API_KEY` enables the per-post Chinese translation button in the manager UI. Translations are generated with `OPENAI_TRANSLATION_MODEL` for `OPENAI_TRANSLATION_TARGET` and saved under each post in `POST_CACHE_PATH`, so already translated posts can be toggled without another OpenAI call. The manager Repost button posts the cached translation, not the original English body; if the post is not translated yet, the UI translates it first.

4. Run the app.

```powershell
uvicorn main:app --reload
```

5. Sign in locally and open the manager UI.

Open `http://localhost:8000/`, sign in, then repost translated Chinese versions from the recent source-channel posts list.

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
