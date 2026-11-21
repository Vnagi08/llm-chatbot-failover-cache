import { useEffect, useRef, useState } from "react";
import { API_BASE_URL, sendChat } from "./api.js";
import { Markdown } from "./markdown.jsx";

function newSessionId() {
  const rand =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
      : Math.random().toString(36).slice(2, 14);
  return `s-${rand}`;
}

let nextId = 1;
const uid = () => nextId++;

function MetadataBar({ meta }) {
  const cached = meta.resolution_source === "LLM_CACHE";
  return (
    <div className="meta" aria-label="Response metadata">
      <span className={`chip source ${cached ? "cache" : "live"}`} data-testid="meta-source" title="Resolution source">
        {meta.resolution_source}
      </span>
      <span className="chip" title="Model">
        <span className="chip-label">model</span>
        <span data-testid="meta-model">{meta.model}</span>
      </span>
      <span className="chip" title="Server-measured latency">
        <span className="chip-label">latency</span>
        <span data-testid="meta-latency">{meta.latency_ms}</span>
        <span className="chip-label">ms</span>
      </span>
      <span className="chip" title="Input (prompt) tokens for this request">
        <span className="chip-label">in</span>
        <span data-testid="meta-input-tokens">{meta.input_tokens}</span>
      </span>
      <span className="chip" title="Output (completion) tokens for this request">
        <span className="chip-label">out</span>
        <span data-testid="meta-output-tokens">{meta.output_tokens}</span>
      </span>
      <span className="chip subtle" title="API key that served the response">
        <span className="chip-label">key</span>
        <span>{meta.key_id ?? "—"}</span>
      </span>
      <span className={`chip subtle ${meta.fallback_attempts > 0 ? "warn" : ""}`} title="Failed keys before success">
        <span className="chip-label">fallbacks</span>
        <span>{meta.fallback_attempts}</span>
      </span>
    </div>
  );
}

function Message({ msg }) {
  if (msg.role === "user") {
    return (
      <div className="row user">
        <div className={`bubble user ${msg.failed ? "failed" : ""}`}>
          {msg.content}
          {msg.failed && <div className="failed-note">Failed — not saved to the conversation history.</div>}
        </div>
      </div>
    );
  }
  if (msg.role === "error") {
    return (
      <div className="row assistant">
        <div className="avatar error" aria-hidden="true">!</div>
        <div className="bubble error" role="alert" data-testid="error-message">
          <strong>Request failed.</strong> {msg.content}
        </div>
      </div>
    );
  }
  return (
    <div className="row assistant">
      <div className="avatar" aria-hidden="true">AI</div>
      <div className="assistant-block" data-testid="assistant-message">
        <div className="bubble assistant">
          <Markdown text={msg.content} />
        </div>
        <MetadataBar meta={msg.metadata} />
      </div>
    </div>
  );
}

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulateFailure, setSimulateFailure] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  const trimmed = input.trim();
  const tooLong = trimmed.length > 4000;
  const canSend = !loading && trimmed.length > 0 && !tooLong;

  async function handleSend() {
    if (!canSend) return;
    const text = trimmed;
    const userMsg = { id: uid(), role: "user", content: text };
    setMessages((m) => [...m, userMsg]);
    setLoading(true);
    try {
      const data = await sendChat({ sessionId, message: text, systemPrompt, simulateFailure });
      setMessages((m) => [...m, { id: uid(), role: "assistant", content: data.reply, metadata: data.metadata }]);
      setInput("");
    } catch (err) {
      setMessages((m) => [
        ...m.map((x) => (x.id === userMsg.id ? { ...x, failed: true } : x)),
        { id: uid(), role: "error", content: err.message || "Something went wrong." },
      ]);
    } finally {
      setLoading(false);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  }

  function startNewChat() {
    setSessionId(newSessionId());
    setMessages([]);
    setInput("");
    inputRef.current?.focus();
  }

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <span className="logo" aria-hidden="true">◆</span>
          <div>
            <h1>Failover Chat</h1>
            <p className="subtitle">
              Session <code>{sessionId}</code>
            </p>
          </div>
        </div>
        <div className="header-actions">
          <button
            type="button"
            className={`ghost ${showSettings ? "active" : ""}`}
            onClick={() => setShowSettings((s) => !s)}
            aria-expanded={showSettings}
          >
            Failover test
          </button>
          <button type="button" className="ghost" onClick={startNewChat} disabled={loading}>
            New chat
          </button>
        </div>
      </header>

      <section className="settings">
        <label className="field">
          <span>System prompt (optional)</span>
          <textarea
            data-testid="system-prompt-input"
            rows={1}
            placeholder="You are a helpful assistant."
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
          />
        </label>
        {showSettings && (
          <>
            <label className="field">
              <span>
                X-Simulate-Failure header <em>(testing; requires ENABLE_FAILURE_SIMULATION=true on the backend)</em>
              </span>
              <input
                type="text"
                placeholder="e.g. key_1=429  or  *=429"
                value={simulateFailure}
                onChange={(e) => setSimulateFailure(e.target.value)}
              />
            </label>
            <p className="hint">
              Backend: <code>{API_BASE_URL}</code>
            </p>
          </>
        )}
      </section>

      <main className="messages" ref={scrollRef}>
        {messages.length === 0 && !loading && (
          <div className="empty">
            <h2>How can I help?</h2>
            <p>Each reply shows where it came from (live inference or cache), latency and token usage.</p>
          </div>
        )}
        {messages.map((m) => (
          <Message key={m.id} msg={m} />
        ))}
        {loading && (
          <div className="row assistant">
            <div className="avatar" aria-hidden="true">AI</div>
            <div className="bubble assistant typing" aria-live="polite" aria-label="Assistant is thinking">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
      </main>

      <footer className="composer">
        <div className={`composer-box ${tooLong ? "invalid" : ""}`}>
          <textarea
            ref={inputRef}
            data-testid="chat-input"
            rows={1}
            placeholder="Message… (Enter to send, Shift+Enter for a new line)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={loading}
            autoFocus
          />
          <button
            type="button"
            className="send"
            data-testid="send-button"
            onClick={handleSend}
            disabled={!canSend}
            aria-label="Send message"
          >
            {loading ? "…" : "Send"}
          </button>
        </div>
        <div className="composer-meta">
          <span className={tooLong ? "over" : ""}>{trimmed.length}/4000</span>
        </div>
      </footer>
    </div>
  );
}
