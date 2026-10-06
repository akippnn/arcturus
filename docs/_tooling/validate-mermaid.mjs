import fs from "node:fs";
import path from "node:path";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
const { default: createDOMPurify } = await import("dompurify");
globalThis.DOMPurify = createDOMPurify(dom.window);
const { default: mermaid } = await import("mermaid");

const repoArg = process.argv.indexOf("--repo");
const repo = path.resolve(repoArg >= 0 ? process.argv[repoArg + 1] : path.resolve(import.meta.dirname, "../.."));
const skipped = new Set([".git", ".agents", ".codex", "node_modules", "target"]);

function markdownFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    if (skipped.has(entry.name)) return [];
    const candidate = path.join(directory, entry.name);
    if (entry.isDirectory()) return markdownFiles(candidate);
    return entry.isFile() && entry.name.endsWith(".md") ? [candidate] : [];
  });
}

const failures = [];
let count = 0;
for (const file of markdownFiles(repo)) {
  const blocks = [...fs.readFileSync(file, "utf8").matchAll(/```mermaid\s*\n([\s\S]*?)```/g)];
  for (const [index, match] of blocks.entries()) {
    count += 1;
    try {
      await mermaid.parse(match[1]);
    } catch (error) {
      failures.push(`${path.relative(repo, file)} block ${index + 1}: ${error.message}`);
    }
  }
}
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`validated ${count} Mermaid diagrams`);
