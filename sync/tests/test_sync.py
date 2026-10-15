"""
End-to-end test of sync.py against in-memory fakes of Graph and AnythingLLM.
Run: python -m pytest -q sync/tests   (or: python sync/tests/test_sync.py)
"""
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync  # noqa: E402

G = "https://graph.test/v1.0"
A = "http://allm.test/api"
D = "drive1"


class FakeDrive:
    def __init__(self):
        self.items, self.log, self.seq, self.n = {}, [], 0, 0
        self.root = self.add(None, "", folder=True, root=True)
        self.broken_token = False

    def _touch(self, iid):
        self.seq += 1
        self.log.append((self.seq, iid))

    def add(self, parent, name, folder=False, root=False, content=b"x"):
        self.n += 1
        iid = f"i{self.n}"
        self.items[iid] = dict(id=iid, name=name, parent=parent, folder=folder, root=root,
                               content=content, ctag=f"c{self.n}-0", deleted=False)
        self._touch(iid)
        return iid

    def modify(self, iid, content):
        it = self.items[iid]
        it["content"] = content
        it["ctag"] = it["ctag"].split("-")[0] + f"-{self.seq}"
        self._touch(iid)

    def move(self, iid, parent=None, name=None):
        it = self.items[iid]
        if parent:
            it["parent"] = parent
        if name:
            it["name"] = name
        self._touch(iid)  # note: descendants deliberately NOT touched, like real delta

    def delete(self, iid):
        self.items[iid]["deleted"] = True
        self._touch(iid)

    def json(self, iid):
        it = self.items[iid]
        if it["deleted"]:
            return {"id": iid, "deleted": {}}
        j = {"id": iid, "name": it["name"], "cTag": it["ctag"], "size": len(it["content"])}
        if it["parent"]:
            j["parentReference"] = {"id": it["parent"]}  # no path, like real delta
        if it["root"]:
            j["root"] = {}
        if it["folder"]:
            j["folder"] = {}
        else:
            j["file"] = {}
        return j

    def child(self, parent, name):
        for it in self.items.values():
            if it["parent"] == parent and it["name"].lower() == name.lower() and not it["deleted"]:
                return it["id"]
        return None


class FakeAllm:
    def __init__(self):
        self.workspaces = {}   # slug -> set(locations)
        self.names = {}        # slug -> display name
        self.docs = {}         # location -> title
        self.k = 0

    def ws_docs(self, name_prefix):
        out = {}
        for slug, locs in self.workspaces.items():
            if slug.startswith(name_prefix):
                out[slug] = sorted(self.docs[l] for l in locs)
        return out


def install(rs, drive, allm):
    # ---------------- Graph
    def delta(req):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.url).query)
        token = int(q.get("token", ["-1"])[0])
        if token >= 0 and drive.broken_token:
            drive.broken_token = False
            return (410, {"Location": f"{G}/drives/{D}/root/delta"}, "{}")
        if token < 0:
            ids = [i for i, it in drive.items.items() if not it["deleted"]]
        else:
            ids = list(dict.fromkeys(i for s, i in drive.log if s > token))
        page = int(q.get("page", ["0"])[0])
        chunk = ids[page * 3:(page + 1) * 3]
        body = {"value": [drive.json(i) for i in chunk]}
        if (page + 1) * 3 < len(ids):
            tok = f"&token={token}" if token >= 0 else ""
            body["@odata.nextLink"] = f"{G}/drives/{D}/root/delta?page={page + 1}{tok}"
        else:
            body["@odata.deltaLink"] = f"{G}/drives/{D}/root/delta?token={drive.seq}"
        return (200, {}, json.dumps(body))

    def get_item(req):
        iid = req.url.split("/items/")[1].split("?")[0]
        if iid not in drive.items or drive.items[iid]["deleted"]:
            return (404, {}, "{}")
        return (200, {}, json.dumps(drive.json(iid)))

    def content(req):
        iid = req.url.split("/items/")[1].split("/")[0]
        return (200, {}, drive.items[iid]["content"])

    def create_folder(req):
        pid = req.url.split("/items/")[1].split("/")[0]
        body = json.loads(req.body)
        if drive.child(pid, body["name"]):
            return (409, {}, "{}")
        iid = drive.add(pid, body["name"], folder=True)
        return (201, {}, json.dumps(drive.json(iid)))

    def by_path(req):
        m = re.search(r"/items/([^:/]+):/([^:?]+)", req.url)
        pid, name = m.group(1), urllib.parse.unquote(m.group(2))
        iid = drive.child(pid, name)
        return (200, {}, json.dumps(drive.json(iid))) if iid else (404, {}, "{}")

    def put_content(req):
        m = re.search(r"/items/([^:/]+):/([^:]+):/content", req.url)
        pid, name = m.group(1), urllib.parse.unquote(m.group(2))
        assert "conflictBehavior=rename" in req.url
        base, ext = os.path.splitext(name)
        k = 1
        while drive.child(pid, name):
            name = f"{base} {k}{ext}"
            k += 1
        iid = drive.add(pid, name, content=req.body)
        return (201, {}, json.dumps(drive.json(iid)))

    rs.add_callback(responses.GET, re.compile(rf"{G}/drives/{D}/root/delta.*"), callback=delta)
    rs.add_callback(responses.GET, re.compile(rf"{G}/drives/{D}/items/[^/:]+/content"), callback=content)
    rs.add_callback(responses.GET, re.compile(rf"{G}/drives/{D}/items/[^/:]+:/.+"), callback=by_path)
    rs.add_callback(responses.GET, re.compile(rf"{G}/drives/{D}/items/[^/:]+$"), callback=get_item)
    rs.add_callback(responses.POST, re.compile(rf"{G}/drives/{D}/items/[^/:]+/children"), callback=create_folder)
    rs.add_callback(responses.PUT, re.compile(rf"{G}/drives/{D}/items/.+:/content.*"), callback=put_content)

    # ---------------- AnythingLLM
    def ws_new(req):
        name = json.loads(req.body)["name"]
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        base, k = slug, 1
        while slug in allm.workspaces:
            k += 1
            slug = f"{base}-{k}"
        allm.workspaces[slug] = set()
        allm.names[slug] = name
        return (200, {}, json.dumps({"workspace": {"slug": slug}}))

    def upload(req):
        folder = req.url.rsplit("/", 1)[1]
        body = req.body if isinstance(req.body, bytes) else req.body.read()
        m = re.search(rb'filename="([^"]+)"', body)
        title = m.group(1).decode()
        allm.k += 1
        loc = f"{folder}/{title}-{allm.k}.json"
        allm.docs[loc] = title
        return (200, {}, json.dumps({"success": True, "documents": [{"location": loc}]}))

    def embed(req):
        slug = req.url.split("/workspace/")[1].split("/")[0]
        if slug not in allm.workspaces:
            return (400, {}, "")
        b = json.loads(req.body)
        allm.workspaces[slug] -= set(b["deletes"])
        allm.workspaces[slug] |= set(b["adds"])
        return (200, {}, json.dumps({"workspace": {}}))

    def remove(req):
        for name in json.loads(req.body)["names"]:
            allm.docs.pop(name, None)
            for s in allm.workspaces.values():
                s.discard(name)
        return (200, {}, json.dumps({"success": True}))

    def ws_list(req):
        return (200, {}, json.dumps({"workspaces": [{"slug": k, "name": allm.names.get(k, k)}
                                                    for k in allm.workspaces]}))

    def ws_update(req):
        slug = req.url.split("/workspace/")[1].split("/")[0]
        if slug not in allm.workspaces:
            return (400, {}, "")
        b = json.loads(req.body)
        if "name" in b:
            allm.names[slug] = b["name"]
        return (200, {}, json.dumps({"workspace": {"slug": slug}}))

    rs.add_callback(responses.POST, f"{A}/v1/workspace/new", callback=ws_new)
    rs.add_callback(responses.GET, f"{A}/v1/workspaces", callback=ws_list)
    rs.add_callback(responses.POST, re.compile(rf"{A}/v1/workspace/[^/]+/update$"), callback=ws_update)
    rs.add_callback(responses.POST, re.compile(rf"{A}/v1/document/upload/.+"), callback=upload)
    rs.add_callback(responses.POST, re.compile(rf"{A}/v1/workspace/.+/update-embeddings"), callback=embed)
    rs.add_callback(responses.DELETE, f"{A}/v1/system/remove-documents", callback=remove)


def make_sync(tmp):
    os.environ.update(GRAPH_BASE=G, ANYTHINGLLM_URL=A[:-4], ANYTHINGLLM_API_KEY="k",
                      STATE_DIR=str(tmp / "state"), OUTBOX_DIR=str(tmp / "outbox"),
                      SP_CASES_ROOT="Cases", LOG_NAMES="true")
    sync.CFG = sync.Config()
    g = sync.Graph(sync.CFG)
    g._token = lambda: "fake"
    s = sync.Sync(sync.CFG, g, sync.AnythingLLM(sync.CFG), sync.State(sync.CFG.state_dir / "state.db"))
    s.st.set("drive_id", D)
    return s


def drop_outbox(tmp, rel, data=b"transcript"):
    p = tmp / "outbox" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    old = time.time() - 60
    os.utime(p, (old, old))
    return p


def test_end_to_end():
    tmp = Path(tempfile.mkdtemp())
    drive, allm = FakeDrive(), FakeAllm()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rs:
        install(rs, drive, allm)
        s = make_sync(tmp)

        cases = drive.add(drive.root, "Cases", folder=True)
        a = drive.add(cases, "Case A", folder=True)
        drafts = drive.add(a, "Drafts", folder=True)
        ref = drive.add(a, "referral.pdf")
        drive.add(drafts, "draft.docx")
        drive.add(a, "photo.heic")
        drive.add(drive.root, "outside.pdf")

        # 1. initial full enumeration
        s.run_once()
        assert allm.ws_docs("") == {"case-a": ["referral.pdf"]}, allm.ws_docs("")

        # 2. content change -> old doc purged, new one embedded
        drive.modify(ref, b"v2")
        s.run_once()
        assert allm.ws_docs("") == {"case-a": ["referral.pdf"]}
        assert len(allm.docs) == 1

        # 3. new case folder -> new workspace
        b = drive.add(cases, "Case B", folder=True)
        notes = drive.add(b, "notes.txt")
        s.run_once()
        assert allm.ws_docs("") == {"case-a": ["referral.pdf"], "case-b": ["notes.txt"]}

        # 4. move file into Drafts -> purged
        drive.move(notes, parent=drafts)
        s.run_once()
        assert allm.ws_docs("") == {"case-a": ["referral.pdf"], "case-b": []}

        # 5. rename case folder -> nothing re-embedded, same workspace
        uploads_before = allm.k
        drive.move(a, name="Case A renamed")
        s.run_once()
        assert allm.k == uploads_before
        assert allm.ws_docs("") == {"case-a": ["referral.pdf"], "case-b": []}

        # 6. outbox write-back -> uploaded to SharePoint, then embedded next pass
        drop_outbox(tmp, "Case B/Transcripts/call-01.txt")
        drop_outbox(tmp, "Case B/Secret/nope.txt")          # not an allowed subfolder
        drop_outbox(tmp, "Case Z/Transcripts/x.txt")         # unknown case
        s.run_once()
        tdir = drive.child(b, "Transcripts")
        assert tdir and drive.child(tdir, "call-01.txt")
        assert (tmp / "outbox/.rejected/Case B/Secret/nope.txt").exists()
        assert (tmp / "outbox/.rejected/Case Z/Transcripts/x.txt").exists()
        assert list((tmp / "outbox/.sent").rglob("call-01.txt"))
        s.run_once()
        assert allm.ws_docs("")["case-b"] == ["call-01.txt"]

        # 7. same name again -> renamed copy, never overwritten
        drop_outbox(tmp, "Case B/Transcripts/call-01.txt", b"second")
        s.run_once()
        assert drive.child(tdir, "call-01 1.txt")

        # 7b. agent skill path: @<workspace-slug> routes to that workspace's case,
        #     even after the case folder was renamed; unknown slug is rejected
        drop_outbox(tmp, "@case-a/Drafts/agent-note.md", b"hello")
        drop_outbox(tmp, "@no-such-ws/Drafts/x.md", b"nope")
        s.run_once()
        a_drafts = drive.child(a, "Drafts")
        assert drive.child(a_drafts, "agent-note.md")
        assert (tmp / "outbox/.rejected/@no-such-ws/Drafts/x.md").exists()
        s.run_once()
        assert "agent-note.md" not in allm.ws_docs("")["case-a"]   # Drafts never embedded

        # 8. move whole case folder out of Cases -> its files are purged
        drive.move(a, parent=drive.root)
        s.run_once()
        assert allm.ws_docs("")["case-a"] == []

        # 9. delete a file
        drive.move(a, parent=cases)
        s.run_once()
        assert allm.ws_docs("")["case-a"] == ["referral.pdf"]
        drive.delete(ref)
        s.run_once()
        assert allm.ws_docs("")["case-a"] == []

        # 10. expired delta token -> full resync reconciles deletions
        extra = drive.add(b, "letter.pdf")
        s.run_once()
        assert "letter.pdf" in allm.ws_docs("")["case-b"]
        drive.items[extra]["deleted"] = True   # deleted without a delta entry
        drive.broken_token = True
        s.run_once()
        assert "letter.pdf" not in allm.ws_docs("")["case-b"]

        # 11. workspace deleted by hand in AnythingLLM -> rebuilt with ALL its docs, no file change needed
        before = sorted(allm.ws_docs("")["case-b"])
        assert before, "case-b should have docs before the test"
        del allm.workspaces["case-b"]
        s.run_once()
        rebuilt = [k for k in allm.workspaces if k.startswith("case-b")]
        assert len(rebuilt) == 1, rebuilt
        assert sorted(allm.ws_docs("")[rebuilt[0]]) == before, (allm.ws_docs(""), before)
        assert allm.names[rebuilt[0]] == "Case B"

        # 11b. deleted while a new file arrives in the same pass -> new file AND old ones end up together
        cb = rebuilt[0]
        del allm.workspaces[cb]
        drive.add(b, "fresh.pdf")
        s.run_once()
        cb2 = [k for k in allm.workspaces if k.startswith("case-b")][0]
        assert sorted(allm.ws_docs("")[cb2]) == sorted(before + ["fresh.pdf"]), allm.ws_docs("")

        # 12. case folder renamed in SharePoint -> workspace name follows, slug unchanged
        drive.move(b, name="Case B (Smith)")
        s.run_once()
        assert allm.names[cb2] == "Case B (Smith)"

        # 13. case folder deleted in SharePoint -> docs removed, workspace kept and marked DELETED
        for iid, it in list(drive.items.items()):
            if it["parent"] == b and not it["folder"]:
                drive.delete(iid)
        drive.delete(b)
        s.run_once()
        assert allm.ws_docs("")[cb2] == []
        assert allm.names[cb2] == "DELETED - Case B (Smith)", allm.names[cb2]
        s.run_once()                                   # stable, no double prefix
        assert allm.names[cb2] == "DELETED - Case B (Smith)"
        # outbox can no longer write to a deleted case
        drop_outbox(tmp, f"@{cb2}/Drafts/late.md", b"x")
        s.run_once()
        assert (tmp / f"outbox/.rejected/@{cb2}/Drafts/late.md").exists()

        # 14. DELETED workspace removed in AnythingLLM -> forgotten, not recreated
        del allm.workspaces[cb2]
        s.run_once()
        assert not [k for k in allm.workspaces if k.startswith("case-b")]
        assert s.st.db.execute("SELECT count(*) FROM workspaces WHERE case_id=?", (b,)).fetchone()[0] == 0

        # 15. case folder deleted then restored (recycle bin keeps IDs) -> name back, docs back
        ca = [k for k in allm.workspaces if k.startswith("case-a")][0]
        a_files = [iid for iid, it in drive.items.items() if it["parent"] == a and not it["folder"] and not it["deleted"]]
        for iid in a_files:
            drive.delete(iid)
        drive.delete(a)
        s.run_once()
        assert allm.names[ca].startswith("DELETED - ")
        drive.items[a]["deleted"] = False; drive._touch(a)
        for iid in a_files:
            drive.items[iid]["deleted"] = False; drive._touch(iid)
        s.run_once()
        assert allm.names[ca] == drive.items[a]["name"], allm.names[ca]
        assert len(allm.ws_docs("")[ca]) == len([i for i in a_files if drive.items[i]["name"].endswith((".pdf", ".txt"))])

        assert (tmp / "state/heartbeat").exists()
    print("all scenarios passed")


if __name__ == "__main__":
    test_end_to_end()