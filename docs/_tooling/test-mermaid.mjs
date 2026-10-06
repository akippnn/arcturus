import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
const { default: createDOMPurify } = await import("dompurify");
globalThis.DOMPurify = createDOMPurify(dom.window);
const { default: mermaid } = await import("mermaid");

test("accepts valid Mermaid", async () => {
  await assert.doesNotReject(() => mermaid.parse("flowchart TD\n  A --> B"));
});

test("rejects invalid Mermaid", async () => {
  await assert.rejects(() => mermaid.parse("flowchart TD\n  A["));
});
