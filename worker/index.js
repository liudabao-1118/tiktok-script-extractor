/**
 * TikTok 余额监控 — 托管 OAuth 收口 Worker（无 KV 版）
 * 用 GitHub 仓库文件存储 token，免去 Cloudflare KV 绑定。
 *
 * 端点：
 *   GET /callback?auth_code=xxx   TikTok 授权回调，换 token 并存入 GitHub 仓库
 *   GET /token?key=WORKER_KEY     返回 { "token": "..." } 供 GitHub Actions 拉取
 *   GET /                         健康检查
 *
 * 需要的 Worker 环境变量：
 *   TT_APP_ID, TT_APP_SECRET, WORKER_KEY   （已配置）
 *   GH_PAT   —— 你的 GitHub Fine-grained PAT（需 Contents: Read and write）
 */
const TT_API = "https://business-api.tiktok.com/open_api/v1.3";
const GH_API = "https://api.github.com";
const REPO = "liudabao-1118/tiktok-script-extractor";
const TOKEN_PATH = "data/tiktok_token.json";

function html(body, status = 200) {
  return new Response(
    `<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>token capture</title>
     <style>body{font-family:-apple-system,system-ui,sans-serif;padding:48px;color:#1a1a1a}
     .ok{color:#0a8f3c}</style></head>
     <body><h2>${body}</h2>
     <p style="color:#888;font-size:13px">TikTok 余额监控 · 托管 OAuth 收口</p></body></html>`,
    { status, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}
function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}
function b64e(s) { return btoa(unescape(encodeURIComponent(s))); }
function b64d(s) { return decodeURIComponent(escape(atob(s))); }

async function ghGet(path, pat) {
  const r = await fetch(`${GH_API}/repos/${REPO}/contents/${path}`, {
    headers: { Authorization: `Bearer ${pat}`, Accept: "application/vnd.github+json" },
  });
  if (r.status === 404) throw new Error("not found");
  if (!r.ok) throw new Error("gh get " + r.status);
  const j = await r.json();
  return b64d(j.content);
}
async function ghPut(path, b64, pat) {
  let sha = null;
  const head = await fetch(`${GH_API}/repos/${REPO}/contents/${path}`, {
    headers: { Authorization: `Bearer ${pat}`, Accept: "application/vnd.github+json" },
  });
  if (head.status === 200) { const hj = await head.json(); sha = hj.sha; }
  const body = { message: "chore: update tiktok token", content: b64, branch: "main" };
  if (sha) body.sha = sha;
  const r = await fetch(`${GH_API}/repos/${REPO}/contents/${path}`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${pat}`, "Content-Type": "application/json", Accept: "application/vnd.github+json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error("gh put " + r.status);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/") {
      return json({ ok: true, service: "tiktok-token-worker" });
    }

    if (url.pathname === "/callback") {
      const code = url.searchParams.get("auth_code");
      if (!code) return html("❌ 缺少 auth_code 参数", 400);
      if (!env.GH_PAT) return html("❌ Worker 未配置 GH_PAT 环境变量（请在 Cloudflare 设置里添加）", 500);
      try {
        const r = await fetch(`${TT_API}/oauth2/access_token/`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ app_id: env.TT_APP_ID, secret: env.TT_APP_SECRET, auth_code: code }),
        });
        const j = await r.json();
        const token = j?.data?.access_token;
        if (!token) return html("❌ 换取 token 失败：" + JSON.stringify(j).slice(0, 400), 500);
        const payload = JSON.stringify({ access_token: token, updated_at: new Date().toISOString() });
        await ghPut(TOKEN_PATH, b64e(payload), env.GH_PAT);
        return html("✅ OK — token 已捕获并保存。可关闭此页面，余额监控将继续运行。", 200);
      } catch (e) {
        return html("❌ Worker 错误：" + e.message, 500);
      }
    }

    if (url.pathname === "/token") {
      if (url.searchParams.get("key") !== env.WORKER_KEY) return json({ error: "unauthorized" }, 401);
      if (!env.GH_PAT) return json({ error: "worker missing GH_PAT" }, 500);
      try {
        const txt = await ghGet(TOKEN_PATH, env.GH_PAT);
        const obj = JSON.parse(txt);
        if (!obj.access_token) return json({ error: "no token" }, 404);
        return json({ token: obj.access_token });
      } catch (e) {
        return json({ error: "no token stored: " + e.message }, 404);
      }
    }

    return html("未知路径", 404);
  },
};
