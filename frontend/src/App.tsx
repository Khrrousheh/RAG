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
  isStreaming?: boolean;
};

type ChatResponse = {
  answer: string;
  sources: Source[];
  warnings: string[];
};

type ChatStreamEvent =
  | { event: "sources"; sources: Source[]; warnings?: string[] }
  | { event: "token"; content: string }
  | { event: "warning"; message: string }
  | { event: "metrics"; metrics: Record<string, unknown> }
  | { event: "done" }
  | { event: "error"; message: string };

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

  function updateAssistantMessage(
    messageId: string,
    update: (message: ChatMessage) => ChatMessage,
  ) {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? update(message) : message)),
    );
  }

  async function requestChatFallback(question: string, assistantId: string) {
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
    updateAssistantMessage(assistantId, (message) => ({
      ...message,
      content: data.answer,
      sources: data.sources,
      warnings: data.warnings,
      isStreaming: false,
    }));
  }

  async function streamAssistantResponse(
    question: string,
    assistantId: string,
    markTokenReceived: () => void,
  ) {
    const response = await fetch(`${API_BASE}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: question,
        top_k: DEFAULT_TOP_K,
        history: recentHistory,
      }),
    });

    if (!response.ok || !response.body) {
      throw new Error(`Streaming request failed with ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    function applyStreamEvent(streamEvent: ChatStreamEvent) {
      if (streamEvent.event === "sources") {
        updateAssistantMessage(assistantId, (message) => ({
          ...message,
          sources: streamEvent.sources,
          warnings: streamEvent.warnings ?? message.warnings,
        }));
        return;
      }

      if (streamEvent.event === "token") {
        markTokenReceived();
        updateAssistantMessage(assistantId, (message) => ({
          ...message,
          content: `${message.content}${streamEvent.content}`,
        }));
        return;
      }

      if (streamEvent.event === "warning") {
        updateAssistantMessage(assistantId, (message) => ({
          ...message,
          warnings: [...(message.warnings ?? []), streamEvent.message],
        }));
        return;
      }

      if (streamEvent.event === "done") {
        updateAssistantMessage(assistantId, (message) => ({
          ...message,
          isStreaming: false,
        }));
        return;
      }

      if (streamEvent.event === "error") {
        throw new Error(streamEvent.message);
      }
    }

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!line.trim()) {
          continue;
        }
        applyStreamEvent(JSON.parse(line) as ChatStreamEvent);
      }

      if (done) {
        break;
      }
    }

    if (buffer.trim()) {
      applyStreamEvent(JSON.parse(buffer) as ChatStreamEvent);
    }
  }

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
    const assistantId = crypto.randomUUID();
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      isStreaming: true,
    };
    setMessages((current) => [...current, userMessage, assistantMessage]);
    setInput("");
    setIsLoading(true);

    let receivedToken = false;
    try {
      await streamAssistantResponse(question, assistantId, () => {
        receivedToken = true;
      });
    } catch (error) {
      if (!receivedToken) {
        try {
          await requestChatFallback(question, assistantId);
        } catch (fallbackError) {
          updateAssistantMessage(assistantId, (message) => ({
            ...message,
            content:
              fallbackError instanceof Error
                ? `The chat request failed: ${fallbackError.message}`
                : "The chat request failed.",
            isStreaming: false,
          }));
        }
      } else {
        updateAssistantMessage(assistantId, (message) => ({
          ...message,
          warnings: [
            ...(message.warnings ?? []),
            error instanceof Error ? error.message : "The streamed response stopped unexpectedly.",
          ],
          isStreaming: false,
        }));
      }
    } finally {
      updateAssistantMessage(assistantId, (message) => ({
        ...message,
        isStreaming: false,
      }));
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
                <div className={`bubble ${message.isStreaming && !message.content ? "loading" : ""}`}>
                  {message.isStreaming && !message.content ? (
                    <>
                      <Loader2 size={18} aria-hidden="true" />
                      <span>Searching policies</span>
                    </>
                  ) : (
                    message.content.split("\n").map((line, index) => (
                      <p key={`${message.id}-${index}`}>{line || "\u00a0"}</p>
                    ))
                  )}
                </div>

                {message.warnings && message.warnings.length > 0 && (
                  <div className="warnings">
                    {message.warnings.map((warning, index) => (
                      <p key={`${message.id}-warning-${index}`}>{warning}</p>
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
