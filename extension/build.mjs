// Builds extension/dist: background (ES module service worker), content script and side panel
// (IIFE: content scripts can't be modules), then copies public/ (manifest, html, css).
import { cpSync, rmSync } from "node:fs";
import { build } from "vite";

rmSync("dist", { recursive: true, force: true });
const entries = [
  { entry: "src/background.ts", name: "background", format: "es" },
  { entry: "src/content.ts", name: "content", format: "iife" },
  { entry: "src/sidepanel.ts", name: "sidepanel", format: "iife" },
];
for (const e of entries) {
  await build({
    configFile: false,
    logLevel: "warn",
    publicDir: false,
    build: {
      outDir: "dist",
      emptyOutDir: false,
      minify: false, // keep the shipped code readable/auditable
      target: "chrome120",
      lib: { entry: e.entry, name: e.name, formats: [e.format], fileName: () => `${e.name}.js` },
    },
  });
}
cpSync("public", "dist", { recursive: true });
console.log("Built extension/dist. Load it with chrome://extensions -> Developer mode -> Load unpacked.");
