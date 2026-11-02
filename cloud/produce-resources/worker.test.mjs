// 内存 R2 回归，不读取真实云端凭据。
import test from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.mjs";

const hash = "a".repeat(64);
const path = `/v1/objects/${hash}`;
function environment({ changed = false, missing = false } = {}) {
    const calls = { head: 0, get: 0, cancelled: 0 };
    const metadata = {
        size: 6,
        etag: "r2-etag",
        customMetadata: { sha256: hash },
    };
    return {
        calls,
        PRODUCE_RESOURCES: {
            async head() {
                calls.head++;
                return missing ? null : metadata;
            },
            async get(key, options = {}) {
                calls.get++;
                if (missing) return null;
                if (options.onlyIf) {
                    assert.equal(options.onlyIf.etagMatches, "r2-etag");
                    if (changed) return {};
                }
                const start = options.range?.offset ?? 0;
                const end = options.range ? start + options.range.length : 6;
                const content = changed ? "uvwxyz" : "abcdef";
                const body = new ReadableStream({
                    start(controller) {
                        controller.enqueue(
                            new TextEncoder().encode(content.slice(start, end)),
                        );
                        controller.close();
                    },
                });
                const cancel = body.cancel.bind(body);
                body.cancel = (...args) => {
                    calls.cancelled++;
                    return cancel(...args);
                };
                return {
                    ...metadata,
                    customMetadata: { sha256: changed ? "b".repeat(64) : hash },
                    body,
                };
            },
        },
    };
}

test("完整下载和 HEAD", async () => {
    const env = environment();
    const response = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`),
        env,
    );
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "abcdef");
    assert.equal(response.headers.get("ETag"), `"${hash}"`);
    assert.deepEqual(env.calls, { head: 0, get: 1, cancelled: 0 });
    const headEnv = environment();
    const head = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`, { method: "HEAD" }),
        headEnv,
    );
    assert.equal(head.headers.get("Content-Length"), "6");
    assert.equal(await head.text(), "");
    assert.deepEqual(headEnv.calls, { head: 1, get: 0, cancelled: 0 });
});

test("断点、后缀范围与 If-Range", async () => {
    for (const [range, expected] of [
        ["bytes=2-", "cdef"],
        ["bytes=-2", "ef"],
        ["bytes=1-3", "bcd"],
    ]) {
        const env = environment();
        const response = await worker.fetch(
            new Request(`https://mag.2j.hk${path}`, {
                headers: { Range: range, "If-Range": `"${hash}"` },
            }),
            env,
        );
        assert.equal(response.status, 206);
        assert.equal(await response.text(), expected);
        assert.deepEqual(env.calls, { head: 1, get: 1, cancelled: 0 });
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
    const env = environment();
    const cached = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`, {
            headers: { "If-None-Match": `"${hash}"` },
        }),
        env,
    );
    assert.equal(cached.status, 304);
    assert.deepEqual(env.calls, { head: 0, get: 1, cancelled: 1 });
    const weak = await worker.fetch(
        new Request(`https://mag.2j.hk${path}`, {
            headers: { "If-None-Match": `W/"${hash}"` },
        }),
        environment(),
    );
    assert.equal(weak.status, 304);
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
        new Request(`https://mag.2j.hk${path}`, {
            headers: { Range: "bytes=2-" },
        }),
        environment({ changed: true }),
    );
    assert.equal(changed.status, 503);
    const latestChanged = await worker.fetch(
        new Request("https://mag.2j.hk/v1/latest.json"),
        environment({ changed: true }),
    );
    assert.equal(latestChanged.status, 200);
    assert.equal(await latestChanged.text(), "uvwxyz");
    assert.equal(latestChanged.headers.get("ETag"), `"${"b".repeat(64)}"`);
    const missing = environment({ missing: true });
    assert.equal(
        (await worker.fetch(new Request(`https://mag.2j.hk${path}`), missing))
            .status,
        404,
    );
    assert.deepEqual(missing.calls, { head: 0, get: 1, cancelled: 0 });
});
