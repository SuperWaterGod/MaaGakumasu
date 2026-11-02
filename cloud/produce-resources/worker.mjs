// 培育资源只读分发；构建、发布及回退由 Actions 完成。
export default {
    async fetch(request, env) {
        if (!["GET", "HEAD"].includes(request.method)) {
            return new Response("Method Not Allowed", {
                status: 405,
                headers: { Allow: "GET, HEAD" },
            });
        }
        const path = new URL(request.url).pathname;
        if (
            !/^\/v1\/(latest\.json|releases\/[a-f0-9]{64}\/(manifest\.json|full\.zip)|objects\/[a-f0-9]{64})$/.test(
                path,
            )
        ) {
            return new Response("Not Found", { status: 404 });
        }
        const key = path.slice(1);
        // 普通 GET 直接读取对象，元数据和内容来自同一次读取。
        // HEAD 及需要 If-Range 校验的续传请求保留元数据检查。
        const needsHead =
            request.method === "HEAD" || request.headers.has("Range");
        const metadata = needsHead
            ? await env.PRODUCE_RESOURCES.head(key)
            : await env.PRODUCE_RESOURCES.get(key);
        if (!metadata) return new Response("Not Found", { status: 404 });
        const etag = `"${metadata.customMetadata?.sha256 || metadata.etag}"`;
        const headers = new Headers({
            ETag: etag,
            "Accept-Ranges": "bytes",
            "Cache-Control":
                key === "v1/latest.json"
                    ? "no-cache"
                    : "public, max-age=31536000, immutable",
            "Content-Type":
                metadata.httpMetadata?.contentType ||
                "application/octet-stream",
            "X-Content-Type-Options": "nosniff",
        });
        if (
            (request.headers.get("If-None-Match") || "")
                .split(",")
                .map((value) => value.trim().replace(/^W\//, ""))
                .includes(etag) ||
            request.headers.get("If-None-Match") === "*"
        ) {
            if ("body" in metadata) await metadata.body.cancel();
            return new Response(null, { status: 304, headers });
        }
        let range;
        const requested = request.headers.get("Range");
        if (
            requested &&
            (!request.headers.has("If-Range") ||
                request.headers.get("If-Range") === etag)
        ) {
            const match = /^bytes=(\d*)-(\d*)$/.exec(requested);
            if (!match || (!match[1] && !match[2])) {
                headers.set("Content-Range", `bytes */${metadata.size}`);
                return new Response(null, { status: 416, headers });
            }
            const start = match[1]
                ? Number(match[1])
                : Math.max(0, metadata.size - Number(match[2]));
            const end =
                match[1] && match[2]
                    ? Math.min(Number(match[2]), metadata.size - 1)
                    : metadata.size - 1;
            if (
                !Number.isSafeInteger(start) ||
                !Number.isSafeInteger(end) ||
                start > end ||
                start >= metadata.size
            ) {
                headers.set("Content-Range", `bytes */${metadata.size}`);
                return new Response(null, { status: 416, headers });
            }
            range = { offset: start, length: end - start + 1 };
            headers.set(
                "Content-Range",
                `bytes ${start}-${end}/${metadata.size}`,
            );
        }
        headers.set("Content-Length", String(range?.length ?? metadata.size));
        if (request.method === "HEAD")
            return new Response(null, { status: range ? 206 : 200, headers });
        // 续传使用内部 ETag，拒绝两次读取之间被替换的对象。
        const object = needsHead
            ? await env.PRODUCE_RESOURCES.get(key, {
                  range,
                  onlyIf: { etagMatches: metadata.etag },
              })
            : metadata;
        if (!object || !("body" in object))
            return new Response("Resource changed; retry", { status: 503 });
        return new Response(object.body, {
            status: range ? 206 : 200,
            headers,
        });
    },
};
