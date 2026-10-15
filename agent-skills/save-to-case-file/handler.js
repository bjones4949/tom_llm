// Save to case file: AnythingLLM custom agent skill.
//
// Writes the draft to /outbox/@<workspace-slug>/Drafts/<file>. The sync service
// maps the workspace slug to its SharePoint case folder and uploads it.
// The case is taken from the current workspace, never from the model, so the
// agent can only write into the case it is working on. Drafts are never embedded.

const fs = require("fs");
const path = require("path");

const OUTBOX = process.env.CASE_OUTBOX_DIR || "/outbox";
const SUBFOLDER = "Drafts";
const ALLOWED_EXT = [".md", ".txt"];
const MAX_CHARS = 200000;

function cleanName(raw) {
  let name = path.basename(String(raw || "").trim()).replace(/[^\w .()-]/g, "_");
  name = name.replace(/^[.\s]+/, "").slice(0, 100).trim(); // no hidden files
  if (!name) name = "draft";
  if (!ALLOWED_EXT.includes(path.extname(name).toLowerCase())) name += ".md";
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

module.exports.runtime = {
  handler: async function ({ filename, content }) {
    try {
      const ws = this.super?.handlerProps?.invocation?.workspace;
      if (!ws?.slug) {
        return "FAILED: could not tell which workspace this is, so nothing was saved. Tell the user the save did not happen.";
      }
      if (typeof content !== "string" || !content.trim()) {
        return "FAILED: no content was provided, so nothing was saved.";
      }
      if (content.length > MAX_CHARS) {
        return `FAILED: content is ${content.length} characters, over the ${MAX_CHARS} limit. Nothing was saved.`;
      }
      if (!fs.existsSync(OUTBOX)) {
        return "FAILED: the case outbox is not mounted on this server, so nothing was saved. Tell the user to contact IT support.";
      }

      const name = cleanName(filename);

      if (typeof this.requestToolApproval === "function") {
        const approval = await this.requestToolApproval({
          description: `Save "${name}" (${content.length} characters) to the Drafts folder of case "${ws.name}" in SharePoint?`,
          payload: { case: ws.name, folder: SUBFOLDER, filename: name, preview: content.slice(0, 500) },
        });
        if (!approval?.approved) {
          return `NOT SAVED: the user declined. ${approval?.message || ""}`.trim();
        }
      }

      const dir = path.join(OUTBOX, `@${ws.slug}`, SUBFOLDER);
      fs.mkdirSync(dir, { recursive: true });
      const finalPath = uniquePath(dir, name);
      const tmpPath = path.join(dir, `.${path.basename(finalPath)}.tmp`);
      fs.writeFileSync(tmpPath, content, { encoding: "utf8" });
      fs.renameSync(tmpPath, finalPath); // sync ignores dotfiles, so it only sees the finished file

      this.introspect(`Queued ${path.basename(finalPath)} for the case Drafts folder.`);
      return (
        `SUCCESS: "${path.basename(finalPath)}" is queued for the Drafts folder of case "${ws.name}". ` +
        "The sync service uploads it to SharePoint within a few minutes. It is not in SharePoint yet, " +
        "and drafts are not added to this workspace's documents until someone reviews them and moves them out of Drafts."
      );
    } catch (e) {
      return `FAILED: ${e.message}. Nothing was saved. Tell the user the save did not happen.`;
    }
  },
};