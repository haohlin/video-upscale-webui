#!/usr/bin/env node

import { readFileSync } from "node:fs";

const lockPath = new URL("../frontend/package-lock.json", import.meta.url);
const lock = JSON.parse(readFileSync(lockPath, "utf8"));
const missing = Object.entries(lock.packages)
  .filter(([path, entry]) => path && !entry.link)
  .filter(([, entry]) => !entry.resolved || !entry.integrity)
  .map(([path]) => path);

if (missing.length) {
  console.error(`lock entries missing resolved/integrity: ${missing.join(", ")}`);
  process.exit(1);
}

console.log("ok: frontend dependency integrity");
