export interface Env {
  RUNNER_KV: KVNamespace;
  RUNNER_API_KEY: string;
  TELEGRAM_BOT_TOKEN: string;
  ALLOWED_CHAT_ID: string;
}

interface TelegramUpdate {
  message?: {
    chat: { id: number };
    text?: string;
  };
}

/** Try runner URLs in order, return first reachable one */
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
      const res = await fetch(`${url}/health`, { signal: AbortSignal.timeout(4000) });
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

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    // GET /status — proxy to runners
    if (request.method === "GET" && url.pathname === "/status") {
      const runnerAUrl = await env.RUNNER_KV.get("runner_a_url");
      const runnerBUrl = await env.RUNNER_KV.get("runner_b_url");

      const tryStatus = async (runnerUrl: string | null, slot: string) => {
        if (!runnerUrl) return { slot, status: "no_url" as const, url: null, data: null };
        try {
          const res = await fetch(`${runnerUrl}/status`, { signal: AbortSignal.timeout(4000) });
          const data = await res.json();
          return { slot, status: "ok" as const, url: runnerUrl, data };
        } catch {
          return { slot, status: "unreachable" as const, url: runnerUrl, data: null };
        }
      };

      const [statusA, statusB] = await Promise.all([
        tryStatus(runnerAUrl, "a"),
        tryStatus(runnerBUrl, "b"),
      ]);

      const active = statusA.status === "ok" ? statusA : statusB.status === "ok" ? statusB : null;

      return Response.json({
        status: active ? "ok" : "offline",
        active_slot: active?.slot ?? null,
        runner_a: { status: statusA.status, url: statusA.url },
        runner_b: { status: statusB.status, url: statusB.url },
        ...(active?.data as object ?? {}),
      }, {
        status: active ? 200 : 503,
        headers: corsHeaders,
      });
    }

    // POST /trigger — external API trigger (authenticated)
    if (request.method === "POST" && url.pathname === "/trigger") {
      const apiKey = request.headers.get("x-api-key");
      if (apiKey !== env.RUNNER_API_KEY) {
        return new Response("Unauthorized", { status: 401 });
      }

      const runnerUrl = await getActiveRunner(env);
      if (!runnerUrl) {
        return Response.json({ status: "error", message: "No runner available" }, { status: 503 });
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
        })
      );

      return Response.json({ status: "accepted" });
    }

    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // POST (Telegram webhook)
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

    const chat_id = String(message.chat.id);
    if (chat_id !== env.ALLOWED_CHAT_ID) {
      return new Response("OK", { status: 200 });
    }

    const runnerUrl = await getActiveRunner(env);
    if (!runnerUrl) {
      return new Response("Runner not available", { status: 503 });
    }

    const text = message.text;

    ctx.waitUntil(
      fetch(`${runnerUrl}/task`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": env.RUNNER_API_KEY,
        },
        body: JSON.stringify({ text, chat_id }),
      })
    );

    return new Response("OK", { status: 200 });
  },
} satisfies ExportedHandler<Env>;
