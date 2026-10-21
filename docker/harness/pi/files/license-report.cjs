// 列出 node_modules 中每个包的名称、版本与 license 字段（轻量许可清单；公开发布前另需 SBOM）。
const fs = require("fs");
const path = require("path");
const root = process.argv[2];
const out = [];
function visit(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name === ".bin") continue;
    const full = path.join(dir, entry.name);
    if (entry.name.startsWith("@")) { visit(full); continue; }
    const pkg = path.join(full, "package.json");
    if (fs.existsSync(pkg)) {
      try {
        const p = JSON.parse(fs.readFileSync(pkg, "utf8"));
        out.push({ name: p.name, version: p.version, license: p.license || p.licenses || null,
                   path: path.relative(root, full) });
      } catch (e) { /* 跳过损坏的 package.json */ }
    }
    const nested = path.join(full, "node_modules");
    if (fs.existsSync(nested)) visit(nested);
  }
}
visit(root);
out.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
process.stdout.write(JSON.stringify(out, null, 1) + "\n");
