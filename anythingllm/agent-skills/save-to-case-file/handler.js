// Save to case file: AnythingLLM custom agent skill.
//
// Saves into /outbox/@<workspace-slug>/Drafts/. The sync service maps the
// workspace slug to its SharePoint case folder and uploads it.
// The case is taken from the current workspace, never from the model, so the
// agent can only write into the case it is working on. Drafts are never embedded.
//
// Two modes:
//  - text:  `content` is written as a .md or .txt file.
//  - file:  `file` names a Word/Excel/PowerPoint/PDF file that AnythingLLM's
//           built-in "Create files" skill made earlier in this same request.
//           It is copied from AnythingLLM's generated-files store.

const fs = require("fs");
const path = require("path");

const OUTBOX = process.env.CASE_OUTBOX_DIR || "/outbox";
const GENERATED_DIR = path.join(process.env.STORAGE_DIR || "/app/server/storage", "generated-files");
const SUBFOLDER = "Drafts";
const TEXT_EXT = [".md", ".txt"];
const FILE_EXT = [".docx", ".xlsx", ".pptx", ".pdf"];
const MAX_CHARS = 200000;

function cleanName(raw, allowed, fallbackExt) {
  let name = path.basename(String(raw || "").trim()).replace(/[^\w .()-]/g, "_");
  name = name.replace(/^[.\s]+/, "").slice(0, 100).trim(); // no hidden files
  if (!name) name = "draft";
  if (!allowed.includes(path.extname(name).toLowerCase())) name += fallbackExt;
  return name;
}

function uniquePath(dir, name) {
  const ext = path.extname(name);
  const stem = name.slice(0, name.length - ext.length);
  let candidate = path.join(dir, name);
  for (let i = 1; fs.existsSync(candidate); i++) {
    candidate = path.join(dir, `${stem} (${i})${ext}`);
  }
  return candidate;
}

// Files made by built-in skills during this agent request.
function generatedFiles(aibitat) {
  const outputs = Array.isArray(aibitat?._pendingOutputs) ? aibitat._pendingOutputs : [];
  return outputs
    .map((o) => o?.payload)
    .filter((p) => p && typeof p.storageFilename === "string" && typeof p.filename === "string")
    .filter((p) => FILE_EXT.includes(path.extname(p.filename).toLowerCase()));
}

// Files made by built-in skills in earlier replies of this same chat (workspace + thread).
// Uses AnythingLLM's own chat model; if its location changes in a future version,
// this quietly returns nothing and only same-request files can be saved.
async function generatedFilesFromHistory(invocation) {
  try {
    const { WorkspaceChats } = require(path.resolve(__dirname, "../../../../models/workspaceChats"));
    const clause = {
      workspaceId: Number(invocation.workspace_id),
      thread_id: invocation.thread_id ? Number(invocation.thread_id) : null,
      response: { contains: "storageFilename" },
    };
    if (invocation.user_id) clause.user_id = Number(invocation.user_id);
    const chats = await WorkspaceChats.where(clause, 20, { id: "desc" });
    const found = [];
    for (const chat of chats) {
      let outputs = [];
      try { outputs = JSON.parse(chat.response)?.outputs || []; } catch { outputs = []; }
      for (const o of outputs) found.push(o);
    }
    return generatedFiles({ _pendingOutputs: found.reverse() }); // oldest..newest
  } catch {
    return [];
  }
}

function pickGenerated(list, wanted) {
  const want = path.basename(String(wanted || "")).toLowerCase();
  const exact = list.filter((p) => p.filename.toLowerCase() === want);
  if (exact.length) return exact[exact.length - 1];
  return list.length === 1 ? list[0] : null;
}

async function approve(ctx, description, payload) {
  if (typeof ctx.requestToolApproval !== "function") return { approved: true };
  return ctx.requestToolApproval({ description, payload });
}

module.exports.runtime = {
  handler: async function ({ filename, content, file }) {
    try {
      const ws = this.super?.handlerProps?.invocation?.workspace;
      if (!ws?.slug) {
        return "FAILED: could not tell which workspace this is, so nothing was saved. Tell the user the save did not happen.";
      }
      if (!fs.existsSync(OUTBOX)) {
        return "FAILED: the case outbox is not mounted on this server, so nothing was saved. Tell the user to contact IT support.";
      }
      const dir = path.join(OUTBOX, `@${ws.slug}`, SUBFOLDER);

      // ---- file mode: save a file the built-in Create files skill just made
      if (file) {
        // Prefer files from this request; otherwise look back through this chat.
        const current = generatedFiles(this.super);
        let picked = pickGenerated(current, file);
        let earlier = [];
        if (!picked) {
          earlier = await generatedFilesFromHistory(this.super?.handlerProps?.invocation || {});
          const want = path.basename(String(file)).toLowerCase();
          const matches = earlier.filter((p) => p.filename.toLowerCase() === want);
          picked = matches.length ? matches[matches.length - 1] : null; // newest with that name
        }
        if (!picked) {
          const names = [...new Set([...current, ...earlier].map((p) => p.filename))];
          if (!names.length) {
            return (
              "FAILED: no Word, Excel, PowerPoint or PDF file has been created in this chat, so nothing was saved. " +
              "Create the file with the Create files skill first."
            );
          }
          return `FAILED: "${file}" was not found. Files created in this chat: ${names.join(", ")}. Nothing was saved. Call this tool again with one of those exact names.`;
        }
        const src = path.join(GENERATED_DIR, path.basename(picked.storageFilename));
        if (!fs.existsSync(src)) {
          return `FAILED: the generated file "${picked.filename}" could not be found on the server. Nothing was saved.`;
        }
        const name = cleanName(filename || picked.filename, FILE_EXT, path.extname(picked.filename));
        const sizeKB = Math.max(1, Math.round(fs.statSync(src).size / 1024));
        const approval = await approve(
          this,
          `Save "${name}" (${sizeKB} KB) to the Drafts folder of case "${ws.name}" in SharePoint?`,
          { case: ws.name, folder: SUBFOLDER, filename: name, source: picked.filename }
        );
        if (!approval?.approved) return `NOT SAVED: the user declined. ${approval?.message || ""}`.trim();

        fs.mkdirSync(dir, { recursive: true });
        const finalPath = uniquePath(dir, name);
        const tmpPath = path.join(dir, `.${path.basename(finalPath)}.tmp`);
        fs.copyFileSync(src, tmpPath);
        fs.renameSync(tmpPath, finalPath);
        this.introspect(`Queued ${path.basename(finalPath)} for the case Drafts folder.`);
        return (
          `SUCCESS: "${path.basename(finalPath)}" is queued for the Drafts folder of case "${ws.name}". ` +
          "The sync service uploads it to SharePoint within about 15 seconds. It is a draft for review, " +
          "and is not added to this workspace's documents until someone moves it out of Drafts."
        );
      }

      // ---- text mode
      if (typeof content !== "string" || !content.trim()) {
        return "FAILED: no content was provided, so nothing was saved.";
      }
      if (content.length > MAX_CHARS) {
        return `FAILED: content is ${content.length} characters, over the ${MAX_CHARS} limit. Nothing was saved.`;
      }
      const name = cleanName(filename, TEXT_EXT, ".md");
      const approval = await approve(
        this,
        `Save "${name}" (${content.length} characters) to the Drafts folder of case "${ws.name}" in SharePoint?`,
        { case: ws.name, folder: SUBFOLDER, filename: name, preview: content.slice(0, 500) }
      );
      if (!approval?.approved) return `NOT SAVED: the user declined. ${approval?.message || ""}`.trim();

      fs.mkdirSync(dir, { recursive: true });
      const finalPath = uniquePath(dir, name);
      const tmpPath = path.join(dir, `.${path.basename(finalPath)}.tmp`);
      fs.writeFileSync(tmpPath, content, { encoding: "utf8" });
      fs.renameSync(tmpPath, finalPath); // sync ignores dotfiles, so it only sees the finished file
      this.introspect(`Queued ${path.basename(finalPath)} for the case Drafts folder.`);
      return (
        `SUCCESS: "${path.basename(finalPath)}" is queued for the Drafts folder of case "${ws.name}". ` +
        "The sync service uploads it to SharePoint within about 15 seconds. It is a draft for review, " +
        "and is not added to this workspace's documents until someone moves it out of Drafts."
      );
    } catch (e) {
      return `FAILED: ${e.message}. Nothing was saved. Tell the user the save did not happen.`;
    }
  },
};