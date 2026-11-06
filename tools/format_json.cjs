// 使用 VS Code JSON 语言服务采用的 jsonc-parser 排版，保留注释和字段顺序。
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { format, applyEdits, parse, printParseErrorCode } = require("jsonc-parser");

const root = path.resolve(__dirname, "..");
const args = process.argv.slice(2);
const check = args.includes("--check");
const explicit = args.filter((arg) => arg !== "--check");
const gitFiles = (...args) => execFileSync("git", args, { cwd: root, encoding: "utf8" }).split("\0").filter(Boolean);
const files = explicit.length ? explicit : [
    ...gitFiles("diff", "--name-only", "--diff-filter=ACMR", "-z", "HEAD"),
    ...gitFiles("ls-files", "--others", "--exclude-standard", "-z"),
];
let failures = 0;
let count = 0;
for (const file of new Set(files)) {
    if (!/\.jsonc?$/i.test(file)) continue;
    const target = path.resolve(root, file);
    const source = fs.readFileSync(target, "utf8");
    const errors = [];
    parse(source, errors, { allowTrailingComma: true });
    if (errors.length) {
        console.error(`${file}: ${errors.map((e) => `${printParseErrorCode(e.error)} @ ${e.offset}`).join(", ")}`);
        failures++;
        continue;
    }
    const output = applyEdits(source, format(source, undefined, {
        insertSpaces: true,
        tabSize: 2,
        eol: source.includes("\r\n") ? "\r\n" : "\n",
    }));
    count++;
    if (output === source) continue;
    if (check) {
        console.error(`需要排版: ${file}`);
        failures++;
    } else {
        fs.writeFileSync(target, output, "utf8");
        console.log(`已排版: ${file}`);
    }
}
console.log(`检查 ${count} 个 JSON/JSONC 文件${failures ? `，${failures} 项未通过` : "，完成"}`);
process.exitCode = failures ? 1 : 0;
