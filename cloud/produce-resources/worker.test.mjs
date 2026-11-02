// 内存 R2 回归，不读取真实云端凭据。
import test from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.mjs";

const hash = "a".repeat(64);
const path = `/v1/objects/${hash}`;
function environment({ changed = false } = {}) {
    return {
        PRODUCE_RESOURCES: {
            async head() {
                return {
                    size: 6,
                    etag: "r2-etag",
                    customMetadata: { sha256: hash },
                };
            },
            async get(key, options) {
                assert.equal(options.onlyIf.etagMatches, "r2-etag");
                if (changed) return {};
                const start = options.range?.offset ?? 0;
                const end = options.range ? start + options.range.length : 6;
                return {
                    body: new TextEncoder().encode("abcdef".slice(start, end)),
                };
            },
        },
    };
}

test("完整下载和 HEAD", async () => {
    const response = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`),
        environment(),
    );
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "abcdef");
    assert.equal(response.headers.get("ETag"), `"${hash}"`);
    const head = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`, { method: "HEAD" }),
        environment(),
    );
    assert.equal(head.headers.get("Content-Length"), "6");
    assert.equal(await head.text(), "");
});

test("断点、后缀范围与 If-Range", async () => {
    for (const [range, expected] of [
        ["bytes=2-", "cdef"],
        ["bytes=-2", "ef"],
        ["bytes=1-3", "bcd"],
    ]) {
        const response = await worker.fetch(
            new Request(`https://mag.2j.hk${path}`, {
                headers: { Range: range, "If-Range": `"${hash}"` },
            }),
            environment(),
        );
        assert.equal(response.status, 206);
        assert.equal(await response.text(), expected);
    }
    const response = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`, {
            headers: { Range: "bytes=2-", "If-Range": "old" },
        }),
        environment(),
    );
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "abcdef");
});

test("缓存和非法范围", async () => {
    const cached = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`, {
            headers: { "If-None-Match": `"${hash}"` },
        }),
        environment(),
    );
    assert.equal(cached.status, 304);
    for (const range of [
        "bytes=100-",
        "bytes=4-1",
        "bytes=1-2,4-5",
        "bytes=-0",
    ]) {
        const response = await worker.fetch(
            new Request(`https://mag.2j.hk${path}`, {
                headers: { Range: range },
            }),
            environment(),
        );
        assert.equal(response.status, 416);
    }
});

test("只读路由、指针缓存及并发切换", async () => {
    const refused = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`, { method: "POST" }),
        environment(),
    );
    assert.equal(refused.status, 405);
    assert.equal(
        (
            await worker.fetch(
                new Request("https://mag.2j.hk/private"),
                environment(),
            )
        ).status,
        404,
    );
    const latest = await worker.fetch(
        new Request("https://mag.2j.hk/v1/latest.json"),
        environment(),
    );
    assert.equal(latest.headers.get("Cache-Control"), "no-cache");
    const changed = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`),
        environment({ changed: true }),
    );
    assert.equal(changed.status, 503);
});
