import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const directory = new URL("./", import.meta.url);
const files = readdirSync(directory).filter((file) => file.endsWith(".test.ts")).sort();
if (!files.length) throw new Error("No browser unit tests found");
for (const file of files) {
  const result = spawnSync(process.execPath,
    ["--import", "tsx", fileURLToPath(new URL(file, directory))],
    { stdio: "inherit", timeout: 60_000 });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
