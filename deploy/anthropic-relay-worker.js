/**
 * Релей к Anthropic API для прод-сервера.
 *
 * Зачем: Anthropic отвечает 403 «Request not allowed» на запросы с IP нашего сервера
 * (региональная блокировка — проверено 27.07.2026: 403 приходит даже на заведомо неверный
 * ключ, значит дело не в ключе). Cloudflare с прода доступен (200 за 0,14 с, узел FRA),
 * поэтому воркер работает мостом: прод → workers.dev → api.anthropic.com.
 *
 * Принципы:
 *  • ключ НЕ хранится в воркере — он приходит заголовком x-api-key от нашего сервера
 *    и просто передаётся дальше. Утечь из воркера нечему;
 *  • без общего секрета (x-relay-secret) релей никого не обслуживает — иначе адрес станет
 *    бесплатным мостом для посторонних;
 *  • тело и заголовки проксируются как есть, включая потоковые ответы (SSE).
 *
 * Установка (панель Cloudflare):
 *  1. Workers & Pages → Create → Worker → вставить этот файл → Deploy.
 *  2. Settings → Variables → Add variable: RELAY_SECRET = <длинная случайная строка>,
 *     отметить Encrypt.
 *  3. Скопировать адрес вида https://<имя>.<аккаунт>.workers.dev.
 *
 * Проверка: GET /health отвечает "ok" без секрета.
 */

const UPSTREAM = "https://api.anthropic.com";
// Заголовки нашего транспорта, которым в апстриме делать нечего.
const STRIP = ["x-relay-secret", "host", "cf-connecting-ip", "cf-ray", "cf-visitor"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }

    if (!env.RELAY_SECRET) {
      return new Response("relay is not configured: set RELAY_SECRET", { status: 500 });
    }
    if (request.headers.get("x-relay-secret") !== env.RELAY_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const headers = new Headers(request.headers);
    for (const name of STRIP) headers.delete(name);

    const upstream = await fetch(UPSTREAM + url.pathname + url.search, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      // Потоковые ответы Anthropic (stream: true) должны идти как есть.
      redirect: "manual",
    });

    // Тело отдаём потоком: ответ модели может быть длинным, буферизовать незачем.
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: upstream.headers,
    });
  },
};
