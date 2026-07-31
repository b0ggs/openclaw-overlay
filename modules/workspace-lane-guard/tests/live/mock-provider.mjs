#!/usr/bin/env node
import http from "node:http";

const port = Number(process.env.WLG_MOCK_PORT);
const sentinel = process.env.WLG_SENTINEL;
if (!Number.isInteger(port) || port < 1 || !sentinel)
  throw new Error("WLG_MOCK_PORT and WLG_SENTINEL are required");

function text(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => item?.text ?? "").join("\n");
  return "";
}
function toolCalls(messages, name) {
  return messages.flatMap((message) =>
    Array.isArray(message?.tool_calls)
      ? message.tool_calls.filter((call) => call?.function?.name === name)
      : [],
  );
}
function call(name, args) {
  return {
    kind: "tool",
    name,
    args,
    id: `call_${Date.now()}_${Math.random().toString(16).slice(2)}`,
  };
}
function final(content) {
  return { kind: "text", content };
}
function decide(body) {
  const messages = Array.isArray(body?.messages) ? body.messages : [];
  const all = messages.map((message) => text(message?.content)).join("\n");
  const lastUser = [...messages].reverse().find((message) => message?.role === "user");
  const latest = text(lastUser?.content);

  if (toolCalls(messages, "sessions_spawn").length === 0 && latest.includes("CANARY_CHILD")) {
    if (toolCalls(messages, "exec").length === 0) {
      return call("exec", { command: `printf '%s' '${sentinel}'` });
    }
    return final("CANARY_CHILD_FINAL");
  }
  if (latest.includes("CANARY_NOOP")) return final("CANARY_NOOP_OK");
  if (latest.includes("CANARY_CONTEXT")) {
    return final(
      `CANARY_CONTEXT sentinel=${all.includes(sentinel)} execCalls=${toolCalls(messages, "exec").length}`,
    );
  }
  if (all.includes("CANARY_SPAWN")) {
    if (toolCalls(messages, "sessions_spawn").length === 0) {
      return call("sessions_spawn", { task: "CANARY_CHILD", taskName: "canary_child" });
    }
    if (!all.includes("CANARY_CHILD_FINAL") && toolCalls(messages, "sessions_yield").length === 0) {
      return call("sessions_yield", {});
    }
    if (all.includes("CANARY_CHILD_FINAL")) return final("CANARY_PARENT_RECEIVED");
    return final("CANARY_PARENT_PENDING");
  }
  return final("CANARY_UNEXPECTED");
}
function responseMessage(decision) {
  if (decision.kind === "text") return { role: "assistant", content: decision.content };
  return {
    role: "assistant",
    content: null,
    tool_calls: [
      {
        id: decision.id,
        type: "function",
        function: { name: decision.name, arguments: JSON.stringify(decision.args) },
      },
    ],
  };
}
function writeJson(res, decision) {
  res.writeHead(200, { "content-type": "application/json" });
  res.end(
    JSON.stringify({
      id: `chatcmpl_${Date.now()}`,
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: "deterministic",
      choices: [
        {
          index: 0,
          message: responseMessage(decision),
          finish_reason: decision.kind === "tool" ? "tool_calls" : "stop",
        },
      ],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    }),
  );
}
function writeSse(res, decision) {
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
  const message = responseMessage(decision);
  const delta =
    decision.kind === "tool"
      ? {
          role: "assistant",
          tool_calls: message.tool_calls.map((item, index) => ({ ...item, index })),
        }
      : { role: "assistant", content: decision.content };
  const base = {
    id: `chatcmpl_${Date.now()}`,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model: "deterministic",
  };
  res.write(
    `data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`,
  );
  res.write(
    `data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: decision.kind === "tool" ? "tool_calls" : "stop" }] })}\n\n`,
  );
  res.end("data: [DONE]\n\n");
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/v1/models") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(
      JSON.stringify({
        object: "list",
        data: [{ id: "deterministic", object: "model", owned_by: "synthetic" }],
      }),
    );
    return;
  }
  if (req.method !== "POST" || req.url !== "/v1/chat/completions") {
    res.writeHead(404);
    res.end();
    return;
  }
  let raw = "";
  req.setEncoding("utf8");
  req.on("data", (chunk) => {
    raw += chunk;
  });
  req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      const decision = decide(body);
      if (body.stream) writeSse(res, decision);
      else writeJson(res, decision);
    } catch (error) {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { message: String(error) } }));
    }
  });
});
server.listen(port, "127.0.0.1");
for (const signal of ["SIGINT", "SIGTERM"])
  process.on(signal, () => server.close(() => process.exit(0)));
