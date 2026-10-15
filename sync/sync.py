#!/usr/bin/env python3
"""
SharePoint -> AnythingLLM sync service.

- Reads changes from one SharePoint document library using Graph delta queries.
- Each first-level folder under SP_CASES_ROOT is a case and maps to one
  AnythingLLM workspace (created automatically).
- New/changed files are uploaded to AnythingLLM and embedded into the case
  workspace. Deleted or moved-out files are purged from AnythingLLM.
- Files under skipped subfolders (default: Drafts) are never embedded.
- Files dropped into OUTBOX_DIR/<Case>/<Subfolder>/<file> are uploaded to that
  case's subfolder in SharePoint (only allowed subfolders, never overwrites).
- The service never deletes or overwrites anything in SharePoint.

Commands:
  python sync.py check        test Graph + AnythingLLM connectivity
  python sync.py once         run one sync pass
  python sync.py run          loop forever (default)
  python sync.py status       print state counts
  python sync.py resync       forget the delta token, do a full re-enumeration
"""
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import ssl
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import msal
import requests

log = logging.getLogger("sync")

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
CHUNK = 32 * 327680  # 10 MiB, must be a multiple of 320 KiB
MAX_RETRY_ATTEMPTS = 5


def _list(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Config:
    def __init__(self):
        e = os.environ.get
        self.tenant_id = e("TENANT_ID", "")
        self.client_id = e("CLIENT_ID", "")
        self.cert_path = e("CERT_PATH", "/certs/cert.pem")
        self.key_path = e("KEY_PATH", "/certs/key.pem")
        self.graph_base = e("GRAPH_BASE", "https://graph.microsoft.com/v1.0").rstrip("/")
        self.sp_hostname = e("SP_HOSTNAME", "")            # contoso.sharepoint.com
        self.sp_site_path = e("SP_SITE_PATH", "")          # /sites/CaseFiles
        self.sp_drive_name = e("SP_DRIVE_NAME", "Documents")
        self.cases_root = [p for p in e("SP_CASES_ROOT", "Cases").split("/") if p]
        self.allm_url = e("ANYTHINGLLM_URL", "http://anythingllm:3001").rstrip("/")
        self.allm_key = e("ANYTHINGLLM_API_KEY", "")
        self.interval = int(e("SYNC_INTERVAL", "300"))
        self.skip_folders = {s.lower() for s in _list(e("SKIP_SUBFOLDERS", "Drafts"))}
        self.outbox_subfolders = _list(e("OUTBOX_SUBFOLDERS", "Drafts,Transcripts,Billing"))
        self.extensions = {x.lower() if x.startswith(".") else "." + x.lower()
                           for x in _list(e("ALLOWED_EXTENSIONS",
                                            ".pdf,.docx,.txt,.md,.csv,.xlsx,.pptx,.odt,.html,.json,.epub"))}
        self.max_bytes = int(float(e("MAX_FILE_MB", "100")) * 1024 * 1024)
        self.state_dir = Path(e("STATE_DIR", "/state"))
        self.outbox_dir = Path(e("OUTBOX_DIR", "/outbox"))
        self.keep_sent = e("OUTBOX_KEEP_SENT", "true").lower() == "true"
        self.log_names = e("LOG_NAMES", "false").lower() == "true"
        prompt_file = e("WORKSPACE_PROMPT_FILE", "")
        self.workspace_prompt = Path(prompt_file).read_text() if prompt_file and Path(prompt_file).exists() else None

    def require(self, *names):
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(f"Missing required settings: {', '.join(missing)}")


CFG = None


def n(name):
    """File/folder names can contain patient names. Only log them if allowed."""
    if CFG and CFG.log_names:
        return repr(name)
    return "#" + hashlib.sha1((name or "").encode()).hexdigest()[:8]


# --------------------------------------------------------------------------- Graph

class GraphError(Exception):
    def __init__(self, resp):
        self.status = resp.status_code
        super().__init__(f"Graph {resp.request.method} {resp.status_code}: {resp.text[:300]}")


class Graph:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = requests.Session()
        self._app = None

    def _token(self):
        if self._app is None:
            cert_pem = Path(self.cfg.cert_path).read_text()
            key_pem = Path(self.cfg.key_path).read_text()
            thumb = hashlib.sha1(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest().upper()
            self._app = msal.ConfidentialClientApplication(
                self.cfg.client_id,
                authority=f"https://login.microsoftonline.com/{self.cfg.tenant_id}",
                client_credential={"private_key": key_pem, "thumbprint": thumb},
            )
        result = self._app.acquire_token_for_client(scopes=GRAPH_SCOPE)  # cached by msal
        if "access_token" not in result:
            raise RuntimeError(f"Token error: {result.get('error')}: {result.get('error_description')}")
        return result["access_token"]

    def request(self, method, url, auth=True, **kw):
        if not url.startswith("http"):
            url = self.cfg.graph_base + url
        base_headers = kw.pop("headers", {})
        resp = None
        for attempt in range(6):
            headers = dict(base_headers)
            if auth:
                headers["Authorization"] = "Bearer " + self._token()
            resp = self.s.request(method, url, headers=headers, timeout=300, **kw)
            if resp.status_code in (429, 503, 504):
                wait = int(resp.headers.get("Retry-After", 5 * (attempt + 1)))
                log.warning("Graph throttled (%s), waiting %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            return resp
        return resp

    def json(self, method, url, ok=(200, 201), **kw):
        r = self.request(method, url, **kw)
        if r.status_code not in ok:
            raise GraphError(r)
        return r.json() if r.content else {}

    # --- site / drive
    def resolve_drive(self):
        site = self.json("GET", f"/sites/{self.cfg.sp_hostname}:{self.cfg.sp_site_path}")
        drives = self.json("GET", f"/sites/{site['id']}/drives").get("value", [])
        for d in drives:
            if d.get("name", "").lower() == self.cfg.sp_drive_name.lower():
                return site, d
        names = ", ".join(d.get("name", "?") for d in drives)
        raise SystemExit(f"Library '{self.cfg.sp_drive_name}' not found. Available: {names}")

    # --- delta
    def delta(self, drive_id, delta_link):
        """Returns (items, new_delta_link, was_full_enumeration)."""
        url = delta_link or f"/drives/{drive_id}/root/delta"
        full = delta_link is None
        items, new_link = {}, None
        while url:
            r = self.request("GET", url)
            if r.status_code == 410:
                log.warning("Delta token no longer valid, starting full re-enumeration")
                items, full, new_link = {}, True, None
                url = r.headers.get("Location") or f"/drives/{drive_id}/root/delta"
                continue
            if r.status_code != 200:
                raise GraphError(r)
            data = r.json()
            for it in data.get("value", []):
                items[it["id"]] = it  # last occurrence wins
            url = data.get("@odata.nextLink")
            new_link = data.get("@odata.deltaLink", new_link)
        return list(items.values()), new_link, full

    def get_item(self, drive_id, item_id):
        r = self.request("GET", f"/drives/{drive_id}/items/{item_id}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GraphError(r)
        return r.json()

    def download(self, drive_id, item_id, dest):
        r = self.request("GET", f"/drives/{drive_id}/items/{item_id}/content", stream=True)
        if r.status_code != 200:
            raise GraphError(r)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1024 * 1024):
                fh.write(chunk)

    def ensure_child_folder(self, drive_id, parent_id, name):
        body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        r = self.request("POST", f"/drives/{drive_id}/items/{parent_id}/children", json=body)
        if r.status_code == 201:
            return r.json()
        if r.status_code == 409:
            return self.json("GET", f"/drives/{drive_id}/items/{parent_id}:/{urllib.parse.quote(name)}")
        raise GraphError(r)

    def upload(self, drive_id, parent_id, name, path):
        """Upload a file. Never overwrites: a name clash creates a renamed copy."""
        size = path.stat().st_size
        qname = urllib.parse.quote(name)
        if size <= SIMPLE_UPLOAD_MAX:
            return self.json(
                "PUT",
                f"/drives/{drive_id}/items/{parent_id}:/{qname}:/content"
                "?@microsoft.graph.conflictBehavior=rename",
                data=path.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
        session = self.json(
            "POST",
            f"/drives/{drive_id}/items/{parent_id}:/{qname}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
        )
        upload_url = session["uploadUrl"]
        with open(path, "rb") as fh:
            start = 0
            while start < size:
                chunk = fh.read(CHUNK)
                end = start + len(chunk) - 1
                r = self.request(
                    "PUT", upload_url, auth=False, data=chunk,
                    headers={"Content-Length": str(len(chunk)),
                             "Content-Range": f"bytes {start}-{end}/{size}"},
                )
                if r.status_code not in (200, 201, 202):
                    raise GraphError(r)
                start = end + 1
        return r.json()


# --------------------------------------------------------------------------- AnythingLLM

class AllmError(Exception):
    def __init__(self, status, text):
        self.status = status
        super().__init__(f"AnythingLLM {status}: {text[:300]}")


class AnythingLLM:
    def __init__(self, cfg):
        self.base = cfg.allm_url + "/api"
        self.s = requests.Session()
        self.s.headers["Authorization"] = "Bearer " + cfg.allm_key

    def _req(self, method, path, timeout=60, **kw):
        r = self.s.request(method, self.base + path, timeout=timeout, **kw)
        if r.status_code >= 400:
            raise AllmError(r.status_code, r.text)
        return r.json() if r.content else {}

    def check(self):
        return self._req("GET", "/v1/auth")

    def create_workspace(self, name, prompt=None):
        body = {"name": name}
        if prompt:
            body["openAiPrompt"] = prompt
        ws = self._req("POST", "/v1/workspace/new", json=body).get("workspace")
        if not ws:
            raise AllmError(400, "workspace not created")
        return ws["slug"]

    def upload(self, path, folder, metadata):
        with open(path, "rb") as fh:
            data = self._req(
                "POST", f"/v1/document/upload/{urllib.parse.quote(folder)}", timeout=1800,
                files={"file": (path.name, fh)},
                data={"metadata": json.dumps(metadata)},
            )
        if not data.get("success"):
            raise AllmError(500, data.get("error") or "upload failed")
        return [d["location"] for d in data.get("documents", [])]

    def update_embeddings(self, slug, adds=(), deletes=()):
        return self._req("POST", f"/v1/workspace/{slug}/update-embeddings", timeout=1800,
                         json={"adds": list(adds), "deletes": list(deletes)})

    def remove_documents(self, locations):
        if locations:
            self._req("DELETE", "/v1/system/remove-documents", json={"names": list(locations)})


# --------------------------------------------------------------------------- State

class State:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY, name TEXT, parent_id TEXT);
        CREATE TABLE IF NOT EXISTS files(
            id TEXT PRIMARY KEY, name TEXT, parent_id TEXT, ctag TEXT, case_id TEXT,
            locations TEXT DEFAULT '[]', status TEXT, detail TEXT,
            attempts INTEGER DEFAULT 0, updated REAL);
        CREATE TABLE IF NOT EXISTS workspaces(case_id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        """)
        self.db.commit()

    def get(self, k):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
        self.db.commit()

    def folder(self, fid):
        return self.db.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()

    def upsert_folder(self, fid, name, parent_id):
        self.db.execute("INSERT OR REPLACE INTO folders(id,name,parent_id) VALUES(?,?,?)",
                        (fid, name, parent_id))

    def child_folder(self, parent_id, name):
        return self.db.execute(
            "SELECT * FROM folders WHERE parent_id=? AND lower(name)=lower(?)",
            (parent_id, name)).fetchone()

    def file(self, fid):
        return self.db.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()

    def save_file(self, fid, **fields):
        fields["updated"] = time.time()
        if self.file(fid) is None:
            self.db.execute("INSERT INTO files(id) VALUES(?)", (fid,))
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE files SET {sets} WHERE id=?", (*fields.values(), fid))
        self.db.commit()


# --------------------------------------------------------------------------- Sync engine

class Sync:
    def __init__(self, cfg, graph, allm, state):
        self.cfg, self.graph, self.allm, self.st = cfg, graph, allm, state
        self.drive_id = None

    # ---- setup
    def init_drive(self):
        drive_id = self.st.get("drive_id")
        if drive_id is None:
            _, drive = self.graph.resolve_drive()
            drive_id = drive["id"]
            self.st.set("drive_id", drive_id)
        self.drive_id = drive_id

    # ---- folder tree helpers
    def chain(self, folder_id):
        """[(id, name), ...] from just below the library root down to folder_id, or None if unknown."""
        root_id = self.st.get("root_id")
        out, cur = [], folder_id
        for _ in range(64):
            if cur == root_id:
                return list(reversed(out))
            row = self.st.folder(cur)
            if row is None:
                return None
            out.append((row["id"], row["name"]))
            cur = row["parent_id"]
        return None

    def cases_root_id(self):
        cur = self.st.get("root_id")
        for name in self.cfg.cases_root:
            row = self.st.child_folder(cur, name)
            if row is None:
                return None
            cur = row["id"]
        return cur

    def classify(self, parent_id, name, size=None):
        """Returns (case_id, case_name, reason). case_id is None if the file should not be embedded."""
        ch = self.chain(parent_id)
        if ch is None:
            return None, None, "orphan"
        prefix = self.cfg.cases_root
        if [c[1].lower() for c in ch[:len(prefix)]] != [p.lower() for p in prefix]:
            return None, None, "outside-cases-root"
        rest = ch[len(prefix):]
        if not rest:
            return None, None, "not-in-a-case-folder"
        case_id, case_name = rest[0]
        if any(nm.lower() in self.cfg.skip_folders for _, nm in rest[1:]):
            return None, None, "skipped-subfolder"
        if Path(name).suffix.lower() not in self.cfg.extensions:
            return None, None, "extension-not-allowed"
        if size is not None and size > self.cfg.max_bytes:
            return None, None, "too-large"
        return case_id, case_name, "ok"

    # ---- AnythingLLM helpers
    def workspace_for(self, case_id, case_name, force_new=False):
        row = self.st.db.execute("SELECT slug FROM workspaces WHERE case_id=?", (case_id,)).fetchone()
        if row and not force_new:
            return row["slug"]
        slug = self.allm.create_workspace(case_name, self.cfg.workspace_prompt)
        self.st.db.execute("INSERT OR REPLACE INTO workspaces(case_id,slug,name) VALUES(?,?,?)",
                           (case_id, slug, case_name))
        self.st.db.commit()
        log.info("Created workspace %s for case %s", slug, n(case_name))
        return slug

    def purge(self, fid):
        row = self.st.file(fid)
        if row is None:
            return
        locs = json.loads(row["locations"] or "[]")
        if locs:
            self.allm.remove_documents(locs)
            log.info("Removed %s from AnythingLLM (%d doc(s))", n(row["name"]), len(locs))
        self.st.save_file(fid, locations="[]")

    def ingest(self, fid, name, case_id, case_name, ctag):
        tmp_dir = self.cfg.state_dir / "tmp" / fid
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = tmp_dir / name
            self.graph.download(self.drive_id, fid, path)
            slug = self.workspace_for(case_id, case_name)
            meta = {"title": name, "docSource": f"SharePoint: {case_name}"}
            locs = self.allm.upload(path, f"case-{slug}", meta)
            try:
                self.allm.update_embeddings(slug, adds=locs)
            except AllmError as e:
                if e.status != 400:
                    raise
                log.warning("Workspace %s missing, recreating", slug)
                slug = self.workspace_for(case_id, case_name, force_new=True)
                self.allm.update_embeddings(slug, adds=locs)
            self.st.save_file(fid, ctag=ctag, case_id=case_id, locations=json.dumps(locs),
                              status="embedded", detail=None, attempts=0)
            log.info("Embedded %s into %s", n(name), slug)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ---- per-file handling
    def handle_file(self, item):
        fid, name = item["id"], item["name"]
        parent_id = item.get("parentReference", {}).get("id")
        ctag = item.get("cTag")
        prev = self.st.file(fid)
        self.st.save_file(fid, name=name, parent_id=parent_id)
        case_id, case_name, reason = self.classify(parent_id, name, item.get("size"))

        if case_id is None:
            if prev is not None and json.loads(prev["locations"] or "[]"):
                self.purge(fid)
            self.st.save_file(fid, case_id=None, ctag=ctag, status="skipped", detail=reason)
            return

        if (prev is not None and prev["status"] == "embedded" and prev["ctag"] == ctag
                and prev["case_id"] == case_id):
            return  # metadata-only change

        try:
            self.purge(fid)
            self.ingest(fid, name, case_id, case_name, ctag)
        except Exception as e:  # noqa: BLE001 keep going with other files
            attempts = (prev["attempts"] if prev else 0) + 1
            self.st.save_file(fid, status="error", detail=str(e)[:500], attempts=attempts, ctag=None)
            log.error("Failed %s (attempt %d): %s", n(name), attempts, e)

    def handle_deleted(self, fid):
        if self.st.file(fid) is not None:
            self.purge(fid)
            self.st.db.execute("DELETE FROM files WHERE id=?", (fid,))
            self.st.db.commit()
            return "file"
        if self.st.folder(fid) is not None:
            self.st.db.execute("DELETE FROM folders WHERE id=?", (fid,))
            self.st.db.commit()
            return "folder"
        return None

    def recheck_all_files(self):
        """After folder renames/moves/deletes, files may have changed case without appearing in delta."""
        for row in self.st.db.execute("SELECT * FROM files").fetchall():
            case_id, _, reason = self.classify(row["parent_id"], row["name"])
            if case_id == row["case_id"] and (case_id is not None or row["status"] == "skipped"):
                continue
            item = self.graph.get_item(self.drive_id, row["id"])
            if item is None:
                self.handle_deleted(row["id"])
            else:
                self.st.save_file(row["id"], ctag=None)
                self.handle_file(item)

    def retry_errors(self):
        rows = self.st.db.execute(
            "SELECT id FROM files WHERE status='error' AND attempts < ?", (MAX_RETRY_ATTEMPTS,)).fetchall()
        for row in rows:
            item = self.graph.get_item(self.drive_id, row["id"])
            if item is None:
                self.handle_deleted(row["id"])
            else:
                self.handle_file(item)

    # ---- outbox (write-back)
    def _reject(self, path, rel, reason):
        dest = self.cfg.outbox_dir / ".rejected" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), dest)
        log.warning("Outbox: %s, moved %s to .rejected", reason, n(str(rel)))

    def process_outbox(self):
        root = self.cfg.outbox_dir
        if not root.exists():
            return
        cases_root = self.cases_root_id()
        allowed = {s.lower(): s for s in self.cfg.outbox_subfolders}
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(part.startswith(".") for part in rel.parts) or p.suffix in (".tmp", ".partial"):
                continue
            if time.time() - p.stat().st_mtime < 10:
                continue  # still being written
            if len(rel.parts) != 3:
                self._reject(p, rel, "expected <Case>/<Subfolder>/<file>")
                continue
            case_name, sub, fname = rel.parts
            if sub.lower() not in allowed:
                self._reject(p, rel, f"subfolder {n(sub)} not allowed")
                continue
            case = self.st.child_folder(cases_root, case_name) if cases_root else None
            if case is None:
                self._reject(p, rel, f"case folder {n(case_name)} not found in SharePoint")
                continue
            try:
                sub_folder = self.st.child_folder(case["id"], allowed[sub.lower()])
                if sub_folder is None:
                    created = self.graph.ensure_child_folder(self.drive_id, case["id"], allowed[sub.lower()])
                    self.st.upsert_folder(created["id"], created["name"], case["id"])
                    self.st.db.commit()
                    sub_id = created["id"]
                else:
                    sub_id = sub_folder["id"]
                result = self.graph.upload(self.drive_id, sub_id, fname, p)
                log.info("Uploaded outbox file %s to SharePoint as %s", n(fname), n(result.get("name")))
            except Exception as e:  # noqa: BLE001
                log.error("Outbox upload failed for %s: %s", n(fname), e)
                continue
            if self.cfg.keep_sent:
                dest = root / ".sent" / datetime.now().strftime("%Y%m%d-%H%M%S") / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), dest)
            else:
                p.unlink()

    # ---- main pass
    def run_once(self):
        self.init_drive()
        items, new_link, full = self.graph.delta(self.drive_id, self.st.get("delta_link"))
        folders_changed = False
        seen_files, seen_folders = set(), set()

        # 1. folders first (delta order isn't guaranteed)
        for it in items:
            if "deleted" in it:
                continue
            if "root" in it:
                self.st.set("root_id", it["id"])
                seen_folders.add(it["id"])
            elif "folder" in it:
                old = self.st.folder(it["id"])
                parent = it.get("parentReference", {}).get("id")
                if old is None or old["name"] != it["name"] or old["parent_id"] != parent:
                    folders_changed = True
                self.st.upsert_folder(it["id"], it["name"], parent)
                seen_folders.add(it["id"])
        self.st.db.commit()

        # 2. deletions
        for it in items:
            if "deleted" in it and self.handle_deleted(it["id"]) == "folder":
                folders_changed = True

        # 3. files
        for it in items:
            if "file" in it and "deleted" not in it:
                seen_files.add(it["id"])
                self.handle_file(it)

        # 4. full enumeration: anything we know about that wasn't returned is gone
        if full:
            for row in self.st.db.execute("SELECT id FROM files").fetchall():
                if row["id"] not in seen_files:
                    self.handle_deleted(row["id"])
            for row in self.st.db.execute("SELECT id FROM folders").fetchall():
                if row["id"] not in seen_folders:
                    self.st.db.execute("DELETE FROM folders WHERE id=?", (row["id"],))
                    folders_changed = True
            self.st.db.commit()

        if folders_changed:
            self.recheck_all_files()
        self.retry_errors()
        if new_link:
            self.st.set("delta_link", new_link)

        # 5. write-back after delta so the folder map is current
        self.process_outbox()
        (self.cfg.state_dir / "heartbeat").write_text(str(time.time()))


# --------------------------------------------------------------------------- CLI

def build():
    st = State(CFG.state_dir / "state.db")
    return Sync(CFG, Graph(CFG), AnythingLLM(CFG), st)


def cmd_check():
    CFG.require("tenant_id", "client_id", "sp_hostname", "sp_site_path", "allm_key")
    g = Graph(CFG)
    site, drive = g.resolve_drive()
    print(f"OK  Graph auth and site: {site.get('webUrl')}")
    print(f"OK  Library '{drive['name']}' drive id: {drive['id']}")
    path = "/".join(CFG.cases_root)
    url = f"/drives/{drive['id']}/root" + (f":/{urllib.parse.quote(path)}:" if path else "") + "/children"
    kids = g.json("GET", url).get("value", [])
    cases = [k for k in kids if "folder" in k]
    print(f"OK  Cases root '{path or '(library root)'}' has {len(cases)} case folder(s)")
    print(f"OK  AnythingLLM API: {AnythingLLM(CFG).check()}")


def cmd_status():
    st = State(CFG.state_dir / "state.db")
    for row in st.db.execute("SELECT status, detail, count(*) c FROM files GROUP BY status, detail"):
        print(f"{row['status'] or '-':10} {row['detail'] or '':25} {row['c']}")
    ws = st.db.execute("SELECT count(*) c FROM workspaces").fetchone()["c"]
    print(f"workspaces: {ws}   delta token: {'yes' if st.get('delta_link') else 'no'}")


def main():
    global CFG
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    CFG = Config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "check":
        return cmd_check()
    if cmd == "status":
        return cmd_status()
    if cmd == "resync":
        st = State(CFG.state_dir / "state.db")
        st.db.execute("DELETE FROM kv WHERE k='delta_link'")
        st.db.commit()
        print("Delta token cleared. Next pass does a full re-enumeration.")
        return

    CFG.require("tenant_id", "client_id", "sp_hostname", "sp_site_path", "allm_key")
    sync = build()
    if cmd == "once":
        return sync.run_once()
    if cmd != "run":
        raise SystemExit(__doc__)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Sync started, interval %ss", CFG.interval)
    while True:
        try:
            sync.run_once()
        except Exception:  # noqa: BLE001
            log.exception("Sync pass failed")
        time.sleep(CFG.interval)


if __name__ == "__main__":
    main()
