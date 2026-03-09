export interface Env {
  RUNNER_KV: KVNamespace;
  RUNNER_API_KEY: string;
  TELEGRAM_BOT_TOKEN: string;
  ALLOWED_CHAT_ID: string;
  CALLBACK_TOKEN: string;
  APPS_ORG: string;
}

interface TelegramUpdate {
  message?: {
    chat: { id: number };
    text?: string;
  };
}

interface HistoryEntry {
  role: "user" | "bot";
  text: string;
  ts: number;
}

const MAX_HISTORY_PER_ROLE = 20;
const MAX_HISTORY_JSON_CHARS = 2000;

const corsHeaders: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Api-Key, X-Secret",
};

// ---------------------------------------------------------------------------
// KV Chat Memory
// ---------------------------------------------------------------------------

async function appendHistory(
  kv: KVNamespace,
  chatId: string,
  role: "user" | "bot",
  text: string,
): Promise<void> {
  const key = `chat:${chatId}:${role}`;
  const raw = await kv.get(key);
  const entries: HistoryEntry[] = raw ? JSON.parse(raw) : [];
  entries.push({ role, text, ts: Date.now() });
  if (entries.length > MAX_HISTORY_PER_ROLE) {
    entries.splice(0, entries.length - MAX_HISTORY_PER_ROLE);
  }
  await kv.put(key, JSON.stringify(entries));
}

async function getHistory(
  kv: KVNamespace,
  chatId: string,
): Promise<HistoryEntry[]> {
  const [userRaw, botRaw] = await Promise.all([
    kv.get(`chat:${chatId}:user`),
    kv.get(`chat:${chatId}:bot`),
  ]);
  const userEntries: HistoryEntry[] = userRaw ? JSON.parse(userRaw) : [];
  const botEntries: HistoryEntry[] = botRaw ? JSON.parse(botRaw) : [];
  const merged = [...userEntries, ...botEntries];
  merged.sort((a, b) => a.ts - b.ts);
  return merged;
}

function truncateHistoryForDispatch(history: HistoryEntry[]): string {
  let json = JSON.stringify(history);
  while (json.length > MAX_HISTORY_JSON_CHARS && history.length > 0) {
    history.shift();
    json = JSON.stringify(history);
  }
  return json;
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

async function incrementStats(
  kv: KVNamespace,
  ...counters: string[]
): Promise<void> {
  for (const counter of counters) {
    const key = `stats:${counter}`;
    const raw = await kv.get(key);
    const val = raw ? parseInt(raw, 10) : 0;
    await kv.put(key, String(val + 1));
  }
}

// ---------------------------------------------------------------------------
// Telegram helper
// ---------------------------------------------------------------------------

async function sendTelegram(
  token: string,
  chatId: string,
  text: string,
): Promise<void> {
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      parse_mode: "Markdown",
    }),
  });
}

// ---------------------------------------------------------------------------
// Runner discovery
// ---------------------------------------------------------------------------

async function getActiveRunner(env: Env): Promise<string | null> {
  const keys = ["runner_a_url", "runner_b_url"];
  const urls: string[] = [];

  for (const key of keys) {
    const url = await env.RUNNER_KV.get(key);
    if (url) urls.push(url);
  }

  if (urls.length === 0) return null;

  const checks = urls.map(async (url) => {
    try {
      const res = await fetch(`${url}/health`, {
        signal: AbortSignal.timeout(4000),
      });
      if (res.ok) return url;
    } catch {}
    return null;
  });

  const results = await Promise.all(checks);
  for (const r of results) {
    if (r) return r;
  }
  return urls[0];
}

// ---------------------------------------------------------------------------
// JSON response helper
// ---------------------------------------------------------------------------

function jsonResponse(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: corsHeaders });
}

// ---------------------------------------------------------------------------
// Main handler
// ---------------------------------------------------------------------------

export default {
  async fetch(
    request: Request,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    // -----------------------------------------------------------------------
    // GET /status — proxy to runners
    // -----------------------------------------------------------------------
    if (request.method === "GET" && url.pathname === "/status") {
      const runnerAUrl = await env.RUNNER_KV.get("runner_a_url");
      const runnerBUrl = await env.RUNNER_KV.get("runner_b_url");

      const tryStatus = async (runnerUrl: string | null, slot: string) => {
        if (!runnerUrl)
          return { slot, status: "no_url" as const, url: null, data: null };
        try {
          const res = await fetch(`${runnerUrl}/status`, {
            signal: AbortSignal.timeout(4000),
          });
          const data = await res.json();
          return { slot, status: "ok" as const, url: runnerUrl, data };
        } catch {
          return {
            slot,
            status: "unreachable" as const,
            url: runnerUrl,
            data: null,
          };
        }
      };

      const [statusA, statusB] = await Promise.all([
        tryStatus(runnerAUrl, "a"),
        tryStatus(runnerBUrl, "b"),
      ]);

      const active =
        statusA.status === "ok"
          ? statusA
          : statusB.status === "ok"
            ? statusB
            : null;

      return jsonResponse(
        {
          status: active ? "ok" : "offline",
          active_slot: active?.slot ?? null,
          runner_a: { status: statusA.status, url: statusA.url },
          runner_b: { status: statusB.status, url: statusB.url },
          ...((active?.data as object) ?? {}),
        },
        active ? 200 : 503,
      );
    }

    // -----------------------------------------------------------------------
    // POST /trigger — external API trigger (authenticated)
    // -----------------------------------------------------------------------
    if (request.method === "POST" && url.pathname === "/trigger") {
      const apiKey = request.headers.get("x-api-key");
      if (apiKey !== env.RUNNER_API_KEY) {
        return new Response("Unauthorized", { status: 401 });
      }

      const runnerUrl = await getActiveRunner(env);
      if (!runnerUrl) {
        return jsonResponse(
          { status: "error", message: "No runner available" },
          503,
        );
      }

      const body = await request.text();

      ctx.waitUntil(
        fetch(`${runnerUrl}/trigger`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-api-key": env.RUNNER_API_KEY,
          },
          body,
        }),
      );

      return jsonResponse({ status: "accepted" });
    }

    // -----------------------------------------------------------------------
    // POST /api/callback — runner callback endpoint
    // -----------------------------------------------------------------------
    if (request.method === "POST" && url.pathname === "/api/callback") {
      const secret = request.headers.get("X-Secret");
      if (secret !== env.CALLBACK_TOKEN) {
        return new Response("Unauthorized", { status: 401 });
      }

      const payload = (await request.json()) as Record<string, unknown>;
      const type = payload.type as string;

      if (type === "bot_reply") {
        const chatId = String(payload.chat_id);
        const text = String(payload.text ?? "");
        if (chatId && text) {
          await appendHistory(env.RUNNER_KV, chatId, "bot", text);
        }
        return jsonResponse({ ok: true });
      }

      if (type === "repo_created") {
        const name = String(payload.name ?? "");
        if (name) {
          await env.RUNNER_KV.put(
            `repo:${name}`,
            JSON.stringify({
              name,
              created_at: Date.now(),
              chat_id: payload.chat_id,
              url: payload.url ?? null,
            }),
          );
        }
        return jsonResponse({ ok: true });
      }

      if (type === "repo_activity") {
        const name = String(payload.name ?? "");
        if (name) {
          const raw = await env.RUNNER_KV.get(`repo:${name}`);
          const record = raw ? JSON.parse(raw) : { name };
          Object.assign(record, {
            last_activity: Date.now(),
            ...(payload.data as object ?? {}),
          });
          await env.RUNNER_KV.put(`repo:${name}`, JSON.stringify(record));
        }
        return jsonResponse({ ok: true });
      }

      return jsonResponse({ ok: false, error: "unknown type" }, 400);
    }

    // -----------------------------------------------------------------------
    // GET /api/history/:chatId — return merged chat history
    // -----------------------------------------------------------------------
    if (
      request.method === "GET" &&
      url.pathname.startsWith("/api/history/")
    ) {
      const chatId = url.pathname.split("/api/history/")[1];
      if (!chatId) {
        return jsonResponse({ error: "missing chatId" }, 400);
      }
      const history = await getHistory(env.RUNNER_KV, chatId);
      return jsonResponse({ chat_id: chatId, history });
    }

    // -----------------------------------------------------------------------
    // GET /api/stats — return stats from KV
    // -----------------------------------------------------------------------
    if (request.method === "GET" && url.pathname === "/api/stats") {
      const [totalMessages, totalApps, totalBuilds] = await Promise.all([
        env.RUNNER_KV.get("stats:totalMessages"),
        env.RUNNER_KV.get("stats:totalApps"),
        env.RUNNER_KV.get("stats:totalBuilds"),
      ]);
      return jsonResponse({
        totalMessages: parseInt(totalMessages ?? "0", 10),
        totalApps: parseInt(totalApps ?? "0", 10),
        totalBuilds: parseInt(totalBuilds ?? "0", 10),
      });
    }

    // -----------------------------------------------------------------------
    // GET /api/repos — return all repo metadata
    // -----------------------------------------------------------------------
    if (request.method === "GET" && url.pathname === "/api/repos") {
      const list = await env.RUNNER_KV.list({ prefix: "repo:" });
      const repos: unknown[] = [];
      for (const key of list.keys) {
        const raw = await env.RUNNER_KV.get(key.name);
        if (raw) repos.push(JSON.parse(raw));
      }
      return jsonResponse({ repos });
    }

    // -----------------------------------------------------------------------
    // POST (root) — Telegram webhook
    // -----------------------------------------------------------------------
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    let update: TelegramUpdate;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    const message = update.message;
    if (!message?.text) {
      return new Response("OK", { status: 200 });
    }

    const chatId = String(message.chat.id);
    if (chatId !== env.ALLOWED_CHAT_ID) {
      return new Response("OK", { status: 200 });
    }

    const text = message.text.trim();

    // Return 200 to Telegram immediately, process in background
    ctx.waitUntil(handleTelegramMessage(env, chatId, text));
    return new Response("OK", { status: 200 });
  },
} satisfies ExportedHandler<Env>;

// ---------------------------------------------------------------------------
// Telegram message handler (runs in ctx.waitUntil)
// ---------------------------------------------------------------------------

async function handleTelegramMessage(
  env: Env,
  chatId: string,
  text: string,
): Promise<void> {
  // /reset — clear KV memory, reply instantly
  if (text === "/reset") {
    await Promise.all([
      env.RUNNER_KV.delete(`chat:${chatId}:user`),
      env.RUNNER_KV.delete(`chat:${chatId}:bot`),
    ]);
    await sendTelegram(
      env.TELEGRAM_BOT_TOKEN,
      chatId,
      "記憶已清除，我們可以重新開始了",
    );
    return;
  }

  // Store user message and increment totalMessages for all non-reset commands
  await appendHistory(env.RUNNER_KV, chatId, "user", text);
  await incrementStats(env.RUNNER_KV, "totalMessages");

  // /build owner/repo
  const buildMatch = text.match(/^\/build\s+(\S+)$/);
  if (buildMatch) {
    await incrementStats(env.RUNNER_KV, "totalBuilds");
    const repo = buildMatch[1];
    const runnerUrl = await getActiveRunner(env);
    if (!runnerUrl) {
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, "No runner available.");
      return;
    }
    try {
      const res = await fetch(`${runnerUrl}/task-sync`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": env.RUNNER_API_KEY,
        },
        body: JSON.stringify({ action: "build", repo, chat_id: chatId }),
      });
      const result = await res.text();
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, result);
    } catch (err) {
      await sendTelegram(
        env.TELEGRAM_BOT_TOKEN,
        chatId,
        `Build error: ${err}`,
      );
    }
    return;
  }

  // /msg owner/repo#N message
  const msgMatch = text.match(/^\/msg\s+(\S+)#(\d+)\s+([\s\S]+)$/);
  if (msgMatch) {
    const repo = msgMatch[1];
    const issueNumber = parseInt(msgMatch[2], 10);
    const msgText = msgMatch[3];
    const runnerUrl = await getActiveRunner(env);
    if (!runnerUrl) {
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, "No runner available.");
      return;
    }
    try {
      const res = await fetch(`${runnerUrl}/task-sync`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": env.RUNNER_API_KEY,
        },
        body: JSON.stringify({
          action: "msg",
          repo,
          issue_number: issueNumber,
          message: msgText,
          chat_id: chatId,
        }),
      });
      const result = await res.text();
      await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, result);
    } catch (err) {
      await sendTelegram(
        env.TELEGRAM_BOT_TOKEN,
        chatId,
        `Message error: ${err}`,
      );
    }
    return;
  }

  // Async commands that forward to runner /task with history
  const history = await getHistory(env.RUNNER_KV, chatId);
  const truncatedHistory = truncateHistoryForDispatch([...history]);

  let command: string;

  // /app [fork:owner/repo] description
  if (text.startsWith("/app ") || text === "/app") {
    command = "app";
    await incrementStats(env.RUNNER_KV, "totalApps");
  }
  // /issue owner/repo description
  else if (text.startsWith("/issue ") || text === "/issue") {
    command = "issue";
  }
  // /research topic
  else if (text.startsWith("/research ") || text === "/research") {
    command = "research";
  }
  // Everything else — general chat
  else {
    command = "chat";
  }

  const runnerUrl = await getActiveRunner(env);
  if (!runnerUrl) {
    await sendTelegram(env.TELEGRAM_BOT_TOKEN, chatId, "No runner available.");
    return;
  }

  try {
    await fetch(`${runnerUrl}/task`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": env.RUNNER_API_KEY,
      },
      body: JSON.stringify({
        text,
        chat_id: chatId,
        history: truncatedHistory,
        command,
      }),
    });
  } catch (err) {
    console.error("Failed to dispatch to runner:", err);
  }
}
