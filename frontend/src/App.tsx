import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Bot, Database, Loader2, Send, User } from "lucide-react";

type Role = "user" | "assistant";

type Source = {
  id: number;
  score: number;
  policy_name?: string;
  file_name?: string;
  page?: number;
  department?: string;
  version?: string;
  effective_date?: string;
  policy_title?: string;
  text: string;
};

type ChatMessage = {
  id: string;
  role: Role;
  content: string;
  sources?: Source[];
  warnings?: string[];
};

type ChatResponse = {
  answer: string;
  sources: Source[];
  warnings: string[];
};

const API_BASE = import.meta.env.VITE_API_URL || "/api";
const DEFAULT_TOP_K = 6;

const starterMessages: ChatMessage[] = [
  {
    id: crypto.randomUUID(),
    role: "assistant",
    content: "Ask me anything about the company policies.",
  },
];

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>(starterMessages);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [apiStatus, setApiStatus] = useState("Checking");
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  const recentHistory = useMemo(
    () =>
      messages
        .filter((message) => message.content.trim())
        .slice(-8)
        .map((message) => ({
          role: message.role,
          content: message.content,
        })),
    [messages],
  );

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading]);

  useEffect(() => {
    async function loadStatus() {
      try {
        const response = await fetch(`${API_BASE}/health`);
        const health = await response.json();
        setApiStatus(
          health.status === "ok"
            ? `Ready, ${health.points_count ?? 0} chunks`
            : "Degraded",
        );
      } catch {
        setApiStatus("Offline");
      }
    }

    loadStatus();
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = input.trim();
    if (!question || isLoading) {
      return;
    }

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: question,
    };
    setMessages((current) => [...current, userMessage]);
    setInput("");
    setIsLoading(true);

    try {
      const response = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: question,
          top_k: DEFAULT_TOP_K,
          history: recentHistory,
        }),
      });

      if (!response.ok) {
        throw new Error(`Request failed with ${response.status}`);
      }

      const data = (await response.json()) as ChatResponse;
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: data.answer,
          sources: data.sources,
          warnings: data.warnings,
        },
      ]);
    } catch (error) {
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content:
            error instanceof Error
              ? `The chat request failed: ${error.message}`
              : "The chat request failed.",
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <main className="app-shell">
      <section className="chat-panel" aria-label="Policy chatbot">
        <header className="top-bar">
          <div>
            <p className="eyebrow">Company Policy RAG</p>
            <h1>Policy Chat</h1>
          </div>
          <div className="status-pill">
            <Database size={16} aria-hidden="true" />
            <span>{apiStatus}</span>
          </div>
        </header>

        <div className="messages">
          {messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <div className="avatar" aria-hidden="true">
                {message.role === "assistant" ? <Bot size={18} /> : <User size={18} />}
              </div>
              <div className="message-body">
                <div className="bubble">
                  {message.content.split("\n").map((line, index) => (
                    <p key={`${message.id}-${index}`}>{line || "\u00a0"}</p>
                  ))}
                </div>

                {message.warnings && message.warnings.length > 0 && (
                  <div className="warnings">
                    {message.warnings.map((warning) => (
                      <p key={warning}>{warning}</p>
                    ))}
                  </div>
                )}

                {message.sources && message.sources.length > 0 && (
                  <div className="sources" aria-label="Retrieved sources">
                    {message.sources.map((source) => (
                      <details
                        className="source-card"
                        key={`${message.id}-${source.id}-${source.file_name}-${source.page}`}
                      >
                        <summary>
                          <span>[{source.id}]</span>
                          <strong>{source.policy_name || source.policy_title || source.file_name}</strong>
                          <small>{source.page ? `p. ${source.page}` : "page unknown"}</small>
                        </summary>
                        <dl>
                          <div>
                            <dt>Score</dt>
                            <dd>{source.score}</dd>
                          </div>
                          <div>
                            <dt>Department</dt>
                            <dd>{source.department ?? "-"}</dd>
                          </div>
                          <div>
                            <dt>Version</dt>
                            <dd>{source.version ?? "-"}</dd>
                          </div>
                          <div>
                            <dt>Effective</dt>
                            <dd>{source.effective_date ?? "-"}</dd>
                          </div>
                        </dl>
                        <p>{source.text}</p>
                      </details>
                    ))}
                  </div>
                )}
              </div>
            </article>
          ))}

          {isLoading && (
            <article className="message assistant">
              <div className="avatar" aria-hidden="true">
                <Bot size={18} />
              </div>
              <div className="message-body">
                <div className="bubble loading">
                  <Loader2 size={18} aria-hidden="true" />
                  <span>Searching policies</span>
                </div>
              </div>
            </article>
          )}

          <div ref={messagesEndRef} />
        </div>

        <form className="composer" onSubmit={handleSubmit}>
          <input
            aria-label="Policy question"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Ask a policy question"
          />
          <button
            aria-label="Send message"
            className={isLoading ? "is-loading" : ""}
            disabled={isLoading || !input.trim()}
          >
            {isLoading ? <Loader2 size={18} aria-hidden="true" /> : <Send size={18} aria-hidden="true" />}
          </button>
        </form>
      </section>
    </main>
  );
}

export default App;
