"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(
  "src/qveris_proxy/admin_assets/admin.js",
  "utf8",
);
const start = source.indexOf("function copyTextWithSelection");
const end = source.indexOf("async function copyApiKey");
assert.ok(start >= 0 && end > start, "copy helpers must be present");
const helpers = source.slice(start, end);

function makeHarness({ secure, clipboardResult = "success" }) {
  const calls = [];
  const activeElement = {
    focus(options) {
      calls.push(["restore-focus", options]);
    },
  };
  const field = {
    style: {},
    value: "",
    setAttribute(name, value) {
      calls.push(["attribute", name, value]);
    },
    focus(options) {
      calls.push(["field-focus", options]);
    },
    select() {
      calls.push(["select"]);
    },
    setSelectionRange(startOffset, endOffset) {
      calls.push(["selection-range", startOffset, endOffset]);
    },
    remove() {
      calls.push(["remove"]);
    },
  };
  const clipboard = {
    async writeText(text) {
      calls.push(["clipboard", text]);
      if (clipboardResult === "failure") {
        throw new Error("clipboard denied");
      }
    },
  };
  const range = {
    selectNodeContents(element) {
      calls.push(["select-node", element]);
    },
  };
  const selection = {
    removeAllRanges() {
      calls.push(["remove-ranges"]);
    },
    addRange(value) {
      assert.equal(value, range);
      calls.push(["add-range"]);
    },
  };
  const context = vm.createContext({
    document: {
      activeElement,
      body: {
        append(node) {
          assert.equal(node, field);
          calls.push(["append"]);
        },
      },
      createElement(tagName) {
        assert.equal(tagName, "textarea");
        return field;
      },
      createRange() {
        calls.push(["create-range"]);
        return range;
      },
      execCommand(command) {
        calls.push(["exec", command, field.value]);
        return true;
      },
    },
    window: {
      isSecureContext: secure,
      navigator: { clipboard },
      getSelection() {
        calls.push(["get-selection"]);
        return selection;
      },
    },
  });
  vm.runInContext(
    `${helpers}\nglobalThis.copyText = copyText; globalThis.selectElementText = selectElementText;`,
    context,
  );
  return {
    calls,
    copyText: context.copyText,
    field,
    selectElementText: context.selectElementText,
  };
}

(async () => {
  const insecure = makeHarness({ secure: false });
  assert.equal(await insecure.copyText("lan-key"), true);
  assert.equal(insecure.calls.some(([name]) => name === "clipboard"), false);
  assert.deepEqual(
    insecure.calls.find(([name]) => name === "exec"),
    ["exec", "copy", "lan-key"],
  );
  assert.equal(insecure.calls.some(([name]) => name === "field-focus"), true);
  assert.equal(insecure.calls.some(([name]) => name === "remove"), true);
  assert.equal(insecure.calls.some(([name]) => name === "restore-focus"), true);

  const secure = makeHarness({ secure: true });
  assert.equal(await secure.copyText("secure-key"), true);
  assert.deepEqual(
    secure.calls.find(([name]) => name === "clipboard"),
    ["clipboard", "secure-key"],
  );
  assert.equal(secure.calls.some(([name]) => name === "exec"), false);

  const denied = makeHarness({ secure: true, clipboardResult: "failure" });
  assert.equal(await denied.copyText("fallback-key"), true);
  assert.equal(denied.calls.some(([name]) => name === "clipboard"), true);
  assert.deepEqual(
    denied.calls.find(([name]) => name === "exec"),
    ["exec", "copy", "fallback-key"],
  );

  const manual = makeHarness({ secure: false });
  assert.equal(manual.selectElementText(manual.field), true);
  assert.equal(
    manual.calls.some(
      ([name, element]) => name === "select-node" && element === manual.field,
    ),
    true,
  );
  assert.equal(manual.calls.some(([name]) => name === "add-range"), true);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
