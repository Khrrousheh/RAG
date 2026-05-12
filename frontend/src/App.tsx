import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  Database,
  Loader2,
  LogOut,
  MessageSquare,
  Pencil,
  Plus,
  Send,
  Trash2,
  User,
} from "lucide-react";

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

type ChatSession = {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
  last_message_at?: string | null;
};

type UserProfile = {
  id: string;
  email: string;
  display_name?: string | null;
  role: string;
};

type AuthResponse = {
  access_token: string;
  expires_at: string;
  user: UserProfile;
};

type ConversationTurn = {
  id: string;
  session_id: string;
  sequence: number;
  role: Role;
  content: string;
  sources?: Source[] | null;
  warnings?: string[] | null;
  metrics?: Record<string, unknown> | null;
  created_at: string;
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
  session?: ChatSession;
};

type ChatStreamEvent =
  | { event: "session"; session: ChatSession }
  | { event: "sources"; sources: Source[]; warnings?: string[] }
  | { event: "token"; content: string }
  | { event: "warning"; message: string }
  | { event: "metrics"; metrics: Record<string, unknown> }
  | { event: "done" }
  | { event: "error"; message: string };

const API_BASE = import.meta.env.VITE_API_URL || "/api";
const DEFAULT_TOP_K = 6;

function createClientId() {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.randomUUID) {
    return cryptoApi.randomUUID();
  }

  if (cryptoApi?.getRandomValues) {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex
      .slice(6, 8)
      .join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10, 16).join("")}`;
  }

  return `client-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

const starterMessages: ChatMessage[] = [
  {
    id: createClientId(),
    role: "assistant",
    content: "Ask me anything about the company policies.",
  },
];

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>(starterMessages);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [apiStatus, setApiStatus] = useState("Checking");
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [currentUser, setCurrentUser] = useState<UserProfile | null>(null);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authName, setAuthName] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const tokenRef = useRef<string | null>(accessToken);

  useEffect(() => {
    tokenRef.current = accessToken;
  }, [accessToken]);

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

  useEffect(() => {
    async function restoreSession() {
      try {
        const auth = await refreshAccessToken();
        setAccessToken(auth.access_token);
        setCurrentUser(auth.user);
        await loadSessions(auth.access_token);
      } catch {
        setAccessToken(null);
        setCurrentUser(null);
      } finally {
        setAuthLoading(false);
      }
    }

    restoreSession();
  }, []);

  async function refreshAccessToken(): Promise<AuthResponse> {
    const response = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
    if (!response.ok) {
      throw new Error("Refresh failed");
    }
    return (await response.json()) as AuthResponse;
  }

  async function apiFetch(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
    const headers = new Headers(init.headers);
    if (!headers.has("Content-Type") && init.body) {
      headers.set("Content-Type", "application/json");
    }
    if (tokenRef.current) {
      headers.set("Authorization", `Bearer ${tokenRef.current}`);
    }

    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      credentials: "include",
    });
    if (response.status !== 401 || !retry) {
      return response;
    }

    const refreshed = await refreshAccessToken();
    setAccessToken(refreshed.access_token);
    setCurrentUser(refreshed.user);
    tokenRef.current = refreshed.access_token;
    return apiFetch(path, init, false);
  }

  async function loadSessions(tokenOverride?: string) {
    if (tokenOverride) {
      tokenRef.current = tokenOverride;
    }
    const response = await apiFetch("/chat/sessions");
    if (!response.ok) {
      throw new Error(`Could not load sessions (${response.status})`);
    }
    const data = (await response.json()) as ChatSession[];
    setSessions(data);
    if (!activeSessionId && data.length > 0) {
      setActiveSessionId(data[0].id);
      await loadMessages(data[0].id);
    }
  }

  async function loadMessages(sessionId: string) {
    const response = await apiFetch(`/chat/session/${sessionId}/messages?limit=100`);
    if (!response.ok) {
      throw new Error(`Could not load messages (${response.status})`);
    }
    const data = (await response.json()) as { session: ChatSession; messages: ConversationTurn[] };
    setActiveSessionId(data.session.id);
    setMessages(
      data.messages.length > 0
        ? data.messages.map((turn) => ({
            id: turn.id,
            role: turn.role,
            content: turn.content,
            sources: turn.sources ?? undefined,
            warnings: turn.warnings ?? undefined,
          }))
        : starterMessages,
    );
  }

  async function handleAuthSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAuthError(null);
    setAuthLoading(true);
    try {
      const response = await fetch(`${API_BASE}/auth/${authMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          email: authEmail,
          password: authPassword,
          display_name: authMode === "register" ? authName : undefined,
        }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `Authentication failed (${response.status})`);
      }
      const auth = (await response.json()) as AuthResponse;
      setAccessToken(auth.access_token);
      setCurrentUser(auth.user);
      setAuthPassword("");
      await loadSessions(auth.access_token);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Authentication failed.");
    } finally {
      setAuthLoading(false);
    }
  }

  async function logout() {
    await apiFetch("/auth/logout", { method: "POST" }, false).catch(() => undefined);
    setAccessToken(null);
    setCurrentUser(null);
    setSessions([]);
    setActiveSessionId(null);
    setMessages(starterMessages);
  }

  async function createSession() {
    const response = await apiFetch("/chat/session", {
      method: "POST",
      body: JSON.stringify({ title: "New chat" }),
    });
    if (!response.ok) {
      return;
    }
    const session = (await response.json()) as ChatSession;
    setSessions((current) => [session, ...current.filter((item) => item.id !== session.id)]);
    setActiveSessionId(session.id);
    setMessages(starterMessages);
  }

  async function renameSession(session: ChatSession) {
    const nextTitle = window.prompt("Session title", session.title);
    if (!nextTitle?.trim()) {
      return;
    }
    const response = await apiFetch(`/chat/session/${session.id}`, {
      method: "PATCH",
      body: JSON.stringify({ title: nextTitle.trim() }),
    });
    if (!response.ok) {
      return;
    }
    const updated = (await response.json()) as ChatSession;
    setSessions((current) => current.map((item) => (item.id === updated.id ? updated : item)));
  }

  async function deleteSession(sessionId: string) {
    const response = await apiFetch(`/chat/session/${sessionId}`, { method: "DELETE" });
    if (!response.ok) {
      return;
    }
    setSessions((current) => current.filter((session) => session.id !== sessionId));
    if (activeSessionId === sessionId) {
      setActiveSessionId(null);
      setMessages(starterMessages);
    }
  }

  function updateAssistantMessage(
    messageId: string,
    update: (message: ChatMessage) => ChatMessage,
  ) {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? update(message) : message)),
    );
  }

  function upsertSession(session: ChatSession) {
    setSessions((current) => [
      session,
      ...current.filter((item) => item.id !== session.id),
    ]);
    setActiveSessionId(session.id);
  }

  async function requestChatFallback(question: string, assistantId: string) {
    const response = await apiFetch("/chat/message", {
      method: "POST",
      body: JSON.stringify({
        session_id: activeSessionId,
        message: question,
        top_k: DEFAULT_TOP_K,
        history: recentHistory,
      }),
    });

    if (!response.ok) {
      throw new Error(`Request failed with ${response.status}`);
    }

    const data = (await response.json()) as ChatResponse;
    if (data.session) {
      upsertSession(data.session);
    }
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
    const response = await apiFetch("/chat/stream", {
      method: "POST",
      body: JSON.stringify({
        session_id: activeSessionId,
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
      if (streamEvent.event === "session") {
        upsertSession(streamEvent.session);
        return;
      }

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
        loadSessions().catch(() => undefined);
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
    if (!question || isLoading || !currentUser) {
      return;
    }

    const userMessage: ChatMessage = {
      id: createClientId(),
      role: "user",
      content: question,
    };
    const assistantId = createClientId();
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

  if (authLoading && !currentUser) {
    return (
      <main className="app-shell center-shell">
        <Loader2 className="spin-icon" size={26} aria-hidden="true" />
      </main>
    );
  }

  if (!currentUser) {
    return (
      <main className="app-shell auth-shell">
        <section className="auth-panel" aria-label="Authentication">
          <div>
            <p className="eyebrow">Company Policy RAG</p>
            <h1>{authMode === "login" ? "Sign in" : "Create account"}</h1>
          </div>
          <form className="auth-form" onSubmit={handleAuthSubmit}>
            {authMode === "register" && (
              <input
                aria-label="Display name"
                value={authName}
                onChange={(event) => setAuthName(event.target.value)}
                placeholder="Display name"
              />
            )}
            <input
              aria-label="Username or email"
              autoComplete="username"
              value={authEmail}
              onChange={(event) => setAuthEmail(event.target.value)}
              placeholder="Username or email"
            />
            <input
              aria-label="Password"
              autoComplete={authMode === "login" ? "current-password" : "new-password"}
              type="password"
              value={authPassword}
              onChange={(event) => setAuthPassword(event.target.value)}
              placeholder="Password"
            />
            {authError && <p className="auth-error">{authError}</p>}
            <button className="primary-action" disabled={authLoading}>
              {authLoading ? <Loader2 size={18} aria-hidden="true" /> : null}
              <span>{authMode === "login" ? "Sign in" : "Register"}</span>
            </button>
          </form>
          <button
            className="text-action"
            type="button"
            onClick={() => {
              setAuthMode(authMode === "login" ? "register" : "login");
              setAuthError(null);
            }}
          >
            {authMode === "login" ? "Create account" : "Use existing account"}
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell workspace-shell">
      <aside className="session-rail" aria-label="Chat sessions">
        <div className="rail-top">
          <div>
            <p className="eyebrow">Company Policy RAG</p>
            <h1>Policy Chat</h1>
          </div>
          <button className="icon-button" type="button" onClick={createSession} title="New chat">
            <Plus size={18} aria-hidden="true" />
          </button>
        </div>

        <div className="session-list">
          {sessions.map((session) => (
            <div
              className={`session-row ${session.id === activeSessionId ? "active" : ""}`}
              key={session.id}
            >
              <button type="button" onClick={() => loadMessages(session.id)}>
                <MessageSquare size={16} aria-hidden="true" />
                <span>{session.title}</span>
              </button>
              <button
                className="mini-button"
                type="button"
                onClick={() => renameSession(session)}
                title="Rename"
              >
                <Pencil size={14} aria-hidden="true" />
              </button>
              <button
                className="mini-button danger"
                type="button"
                onClick={() => deleteSession(session.id)}
                title="Delete"
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>

        <div className="rail-account">
          <div>
            <strong>{currentUser.display_name || currentUser.email}</strong>
            <small>{currentUser.email}</small>
          </div>
          <button className="icon-button" type="button" onClick={logout} title="Sign out">
            <LogOut size={18} aria-hidden="true" />
          </button>
        </div>
      </aside>

      <section className="chat-panel" aria-label="Policy chatbot">
        <header className="top-bar">
          <div>
            <p className="eyebrow">Memory-aware assistant</p>
            <h2>{sessions.find((session) => session.id === activeSessionId)?.title || "New chat"}</h2>
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
