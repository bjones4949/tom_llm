# SharePoint sync: test setup

Replaces the OneDrive container. The `sync` service reads one SharePoint library through Microsoft Graph, embeds each case folder into its own AnythingLLM workspace, and uploads files from a local outbox back into that case's folder.

Use dummy documents on the test site. No real case data until the production box is built.

## How it behaves

| Where the file is in SharePoint | What happens |
|---|---|
| `Cases/<Case>/...` | Embedded into the workspace for `<Case>` (created automatically) |
| `Cases/<Case>/Drafts/...` | Ignored (unreviewed AI output) |
| Anywhere outside `Cases/` | Ignored |
| File changed | Old version removed from AnythingLLM, new version embedded |
| File deleted or moved out | Removed from AnythingLLM |
| Case folder renamed | Nothing changes, the workspace is tracked by folder ID, not name |

Write-back: put a file at `/srv/llm/outbox/<Case>/<Transcripts|Drafts|Billing>/<file>`. Within one sync interval it's uploaded to that case's subfolder in SharePoint. It never overwrites a file (a clash creates `name 1.ext`). Uploaded files move to `outbox/.sent/`, and anything it can't place goes to `outbox/.rejected/`.

The service never deletes anything in SharePoint.

## 1. Prepare the test site

In your test site's Documents library create:

```
Cases/
  Test Case 001/
    referral.pdf
    Drafts/
      draft.docx
  Test Case 002/
    notes.txt
```

## 2. Create a certificate (on the server)

```bash
cd ~/local_llm            # your repo
mkdir -p sync/certs && cd sync/certs
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 730 -nodes -subj "/CN=spark-sync-test"
chmod 600 key.pem && sudo chown -R 1000:1000 .     # match PUID/PGID
```

`sync/certs/` is in `.gitignore`. Never commit the key. Note the expiry date (2 years) somewhere you'll see it.

## 3. Register the app in Entra

1. Entra admin centre > App registrations > New registration. Name it `Spark Sync (test)`, single tenant, no redirect URI.
2. Certificates & secrets > Certificates > Upload `cert.pem` (the public cert only).
3. API permissions > Add > Microsoft Graph > **Application permissions** > `Sites.Selected` > Add, then **Grant admin consent**.
4. Copy the Directory (tenant) ID and Application (client) ID.

`Sites.Selected` on its own gives access to nothing. Step 4 grants the one site.

## 4. Grant the app access to the test site

Easiest with Graph Explorer (https://developer.microsoft.com/graph/graph-explorer), signed in as a SharePoint or Global admin. Consent to `Sites.FullControl.All` for Graph Explorer when prompted.

Get the site ID:

```
GET https://graph.microsoft.com/v1.0/sites/yourtenant.sharepoint.com:/sites/CaseFilesTest?$select=id
```

Grant write on that site to the app:

```
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
{
  "roles": ["write"],
  "grantedToIdentities": [
    { "application": { "id": "<client-id>", "displayName": "Spark Sync (test)" } }
  ]
}
```

Check it with `GET .../sites/{site-id}/permissions`. Afterwards you can remove Graph Explorer's consent in Entra > Enterprise applications if you don't want it lingering.

## 5. Host folders

```bash
sudo mkdir -p /srv/llm/sync-state /srv/llm/outbox
sudo chown -R 1000:1000 /srv/llm
```

In production these go on the encrypted data volume.

## 6. Remove the OneDrive container

```bash
git pull
docker compose up -d --remove-orphans      # drops the old onedrive container
```

Then clean up what it left behind:

- `./onedrive/conf` holds a refresh token for the account it used. Delete it, and revoke that session (Entra > Users > the account > Revoke sessions, or remove the OneDrive client's consent under Enterprise applications).
- Delete `/srv/onedrive` once you're sure nothing there is needed.
- In AnythingLLM, delete documents that came in through the old `anythingllm-fs` mount, so they don't mix with the synced ones.

## 7. Configure

```bash
cp .env.example .env     # if you don't already have one, otherwise merge the new SP_* lines
```

Fill in `SP_TENANT_ID`, `SP_CLIENT_ID`, `SP_HOSTNAME`, `SP_SITE_PATH`.

Get the AnythingLLM API key: open AnythingLLM > Settings > Developer API > Generate new API key. Put it in `ANYTHINGLLM_API_KEY`.

## 8. Test the connection

```bash
docker compose build sync
docker compose run --rm sync check
```

Expected:

```
OK  Graph auth and site: https://yourtenant.sharepoint.com/sites/CaseFilesTest
OK  Library 'Documents' drive id: b!...
OK  Cases root 'Cases' has 2 case folder(s)
OK  AnythingLLM API: {'authenticated': True}
```

## 9. Run it

```bash
docker compose up -d sync
docker compose logs -f sync
docker compose exec sync python sync.py status
```

On the test box, set `SYNC_LOG_NAMES=true` in `.env` to see file names in the logs. Keep it `false` in production, because names often contain patient names.

## 10. Test checklist

Run through these on the test site and confirm each in AnythingLLM:

- [ ] Both test cases have a workspace, `referral.pdf` and `notes.txt` are embedded, `draft.docx` is not
- [ ] Edit `referral.pdf` in SharePoint: after the next pass there's still exactly one copy in the workspace
- [ ] Add a new case folder with a file: a new workspace appears
- [ ] Move a file into `Drafts`: it disappears from the workspace
- [ ] Delete a file: it disappears from the workspace
- [ ] Rename a case folder: no re-upload, the workspace keeps working
- [ ] Drop `/srv/llm/outbox/Test Case 001/Transcripts/test.txt`: it appears in SharePoint, then gets embedded on the following pass
- [ ] Drop the same file name again: SharePoint gets `test 1.txt`, the original is untouched
- [ ] `docker compose restart sync`: no files are re-uploaded
- [ ] `docker compose ps` shows `sync` as healthy

To force a full re-scan: `docker compose exec sync python sync.py resync`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Token error: invalid_client` | Wrong client ID, or `cert.pem` not uploaded to the app registration |
| `Graph GET 403` on the site | Step 4 grant missing or made against the wrong app ID |
| `Graph GET 404` on the site | `SP_HOSTNAME` or `SP_SITE_PATH` wrong (path must start with `/sites/`) |
| `Library 'Documents' not found` | The error lists the available library names. Set `SP_DRIVE_NAME` |
| `AnythingLLM 403` | API key missing or wrong |
| `Document processing API is not online` | AnythingLLM's collector hasn't finished starting. It retries next pass |
| File shows `skipped / extension-not-allowed` in `status` | Add the extension to `ALLOWED_EXTENSIONS` if AnythingLLM can parse it |
| Errors keep repeating | `status` shows the detail. Files are retried 5 times, then left until they change |

## Running the tests

Offline tests against fake Graph and AnythingLLM APIs:

```bash
pip install msal requests responses
python sync/tests/test_sync.py
```

## Not in this version yet

- Audio transcription (Whisper container): audio files are currently skipped
- Photo descriptions via the vision model
- The `save_draft` agent tool that writes to the outbox
- Billing spreadsheet generation
