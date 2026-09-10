import { StrictMode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import {
  Activity,
  Archive,
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleStop,
  Code2,
  Copy,
  File,
  FileCode2,
  FileImage,
  FileText,
  Folder,
  FolderOpen,
  GitCompare,
  Image as ImageIcon,
  KeyRound,
  LayoutPanelTop,
  Menu,
  MessageSquarePlus,
  MoreHorizontal,
  Paperclip,
  PanelRight,
  Play,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import "katex/dist/katex.min.css";
import "./styles.css";
import { CloudAccountIndicator, CloudPanel, useCloudAccount } from "./CloudPanel";

type Session = {
  id: string;
  name: string;
  updated_at: number;
  message_count: number;
  title_source?: string;
  active_runs?: string[];
  messages?: Message[];
};
type Message = { role: "user" | "assistant"; content: string };
type Settings = {
  provider: string;
  model: string;
  base_url?: string;
  reasoning_effort: string;
  language: string;
  mode: string;
  max_tool_rounds: number;
  llm_configured: boolean;
  vision: {
    mode: string;
    provider: string;
    model: string;
    configured: boolean;
  };
  web_search: {
    enabled: boolean;
    provider: string;
    model: string;
    configured: boolean;
  };
};
type Provider = {
  id: string;
  name: string;
  base_url: string;
  default_model: string;
  models: string[];
  api_key_url: string;
  api_key_hint: string;
};
type TreeItem = {
  name: string;
  path: string;
  kind: "file" | "directory";
  size: number | null;
  artifact_id: string | null;
};
type ActivityItem = {
  id: string;
  text: string;
  kind: "tool" | "state" | "success" | "error";
};
type Confirmation = {
  runId: string;
  tool?: string;
  args?: unknown;
  batch_args?: unknown[];
  message?: string;
};
type ConfirmationSummary = {
  title: string;
  explanation: string;
  details: Array<{ label: string; value: string }>;
  warning?: string;
  executionApproval?: boolean;
};
type SecretPrompt = {
  runId: string;
  request_id: string;
  title?: string;
  instructions?: string;
  scope?: string;
  multiline?: boolean;
};

const API = "/api/v1";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail ?? `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

function formatTime(timestamp: number) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp * 1000);
}

function fileIcon(item: TreeItem) {
  if (item.kind === "directory") return <Folder size={15} />;
  if (/\.(py|ts|tsx|js|json|yaml|yml|toml|sh)$/i.test(item.name))
    return <FileCode2 size={15} />;
  if (/\.(png|jpg|jpeg|webp|gif)$/i.test(item.name))
    return <FileImage size={15} />;
  if (/\.(md|txt|pdf)$/i.test(item.name)) return <FileText size={15} />;
  return <File size={15} />;
}

function formatRunState(state: string) {
  return (
    {
      queued: "任务已排队",
      running: "智能体正在运行",
      waiting_confirmation: "等待确认",
      waiting_secret: "等待安全凭据",
      cancelling: "正在取消任务",
      completed: "任务完成",
      cancelled: "任务已取消",
      failed: "任务失败",
    }[state] ?? state
  );
}

const CONFIRM_TOOL_LABELS: Record<string, string> = {
  delete_file: "删除文件",
  run_shell: "执行 Shell 命令",
  ensure_runtime_tools: "安装命令行工具",
  record_memo: "加入研究备忘录",
  update_memo: "更新研究备忘录",
  delete_memo: "删除研究备忘录",
  clear_memos: "清理研究备忘录",
  restore_paper_version: "恢复论文版本",
  propose_execution: "开始执行计划",
};

const CONFIRM_ARG_LABELS: Record<string, string> = {
  command: "命令",
  description: "操作说明",
  workdir: "工作目录",
  file_path: "文件路径",
  tools: "需要的命令行工具",
  memo_id: "备忘录",
  version_id: "版本",
  title: "标题",
  content: "正文",
  evidence: "依据或限制",
  tags: "标签",
};

function confirmationValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => confirmationValue(item)).join("、");
  if (value && typeof value === "object") {
    return Object.entries(value)
      .map(([key, item]) => `${CONFIRM_ARG_LABELS[key] ?? key}：${confirmationValue(item)}`)
      .join("\n");
  }
  if (value === null || value === undefined || value === "") return "未提供";
  return String(value);
}

function summarizeConfirmation(confirmation: Confirmation): ConfirmationSummary {
  const tool = confirmation.tool ?? "";
  const args = (confirmation.args ?? {}) as Record<string, unknown>;
  const batchArgs = Array.isArray(confirmation.batch_args)
    ? confirmation.batch_args
    : null;
  if (tool === "propose_execution") {
    return {
      title: "开始执行计划",
      explanation: "Aero 已完成计划，是否开始执行其中的操作？",
      details: [],
      executionApproval: true,
    };
  }
  if (tool === "ensure_runtime_tools") {
    return {
      title: "安装命令行工具",
      explanation: "Aero 需要先补齐这些本机命令，才能继续处理数据。",
      details: [
        { label: "需要的命令", value: confirmationValue(args.tools) },
        {
          label: "将会做什么",
          value:
            "创建或更新 aero-agent 环境，并从 conda-forge 安装对应软件包。",
        },
      ],
      warning: "这会修改本机 conda 环境。",
    };
  }
  if (tool === "run_shell") {
    const commands = batchArgs?.map((item) => (item as Record<string, unknown>).command);
    return {
      title: "执行 Shell 命令",
      explanation: "Aero 请求在本机运行命令，请确认命令内容和工作目录。",
      details: [
        ...(args.description
          ? [{ label: "操作说明", value: confirmationValue(args.description) }]
          : []),
        {
          label: "命令",
          value: commands
            ? commands.map((command, index) => `${index + 1}. ${confirmationValue(command)}`).join("\n")
            : confirmationValue(args.command),
        },
        { label: "工作目录", value: confirmationValue(args.workdir ?? ".") },
      ],
    };
  }
  if (tool === "delete_file") {
    const paths = batchArgs?.map((item) => (item as Record<string, unknown>).file_path);
    return {
      title: "删除文件",
      explanation: "Aero 请求删除以下项目文件。",
      details: [
        {
          label: "文件",
          value: paths
            ? paths.map((path, index) => `${index + 1}. ${confirmationValue(path)}`).join("\n")
            : confirmationValue(args.file_path),
        },
      ],
      warning: "删除后无法通过 Aero 撤销，请确认路径无误。",
    };
  }
  const details = batchArgs
    ? [
        { label: "调用次数", value: String(batchArgs.length) },
        ...batchArgs.slice(0, 8).map((item, index) => ({
          label: `第 ${index + 1} 次调用`,
          value: confirmationValue(item),
        })),
      ]
    : Object.entries(args).map(([key, value]) => ({
        label: CONFIRM_ARG_LABELS[key] ?? key,
        value: confirmationValue(value),
      }));
  return {
    title: CONFIRM_TOOL_LABELS[tool] ?? "执行操作",
    explanation: confirmation.message ?? "Aero 请求执行一项需要你确认的操作。",
    details,
  };
}

function activityProgressSlot(text: string) {
  if (text.startsWith("下载进度#")) return text.split(" ", 1)[0];
  if (text.startsWith("下载进度 ")) return "下载进度";
  if (text.startsWith("GCS ARCO ") && text.includes("，已等待 ")) {
    return text.split("，已等待 ", 1)[0];
  }
  return null;
}

function isCompletedActivityProgress(text: string) {
  return text.startsWith("下载进度") && /\b100(?:\.0+)?%/.test(text);
}

function upsertActivity(items: ActivityItem[], next: ActivityItem[]) {
  let result = items;
  for (const item of next) {
    const slot = activityProgressSlot(item.text);
    if (slot && isCompletedActivityProgress(item.text)) {
      result = result.filter((current) => activityProgressSlot(current.text) !== slot);
      continue;
    }
    if (slot) {
      const index = result.findIndex((current) => activityProgressSlot(current.text) === slot);
      if (index >= 0) {
        result = [...result];
        result[index] = item;
        continue;
      }
    }
    result = [...result, item].slice(-80);
  }
  return result;
}

function App() {
  const [showCloud, setShowCloud] = useState(false);
  const [cloudRefreshVersion, setCloudRefreshVersion] = useState(0);
  const { account, accountError, refreshAccount } = useCloudAccount(cloudRefreshVersion);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [projectName, setProjectName] = useState("Aerolytica");
  const [tree, setTree] = useState<TreeItem[]>([]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({
    ".": true,
  });
  const [children, setChildren] = useState<Record<string, TreeItem[]>>({});
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [draft, setDraft] = useState("");
  const [running, setRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [runId, setRunId] = useState("");
  const [streaming, setStreaming] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [secretPrompt, setSecretPrompt] = useState<SecretPrompt | null>(null);
  const [secretValue, setSecretValue] = useState("");
  const [selectedFile, setSelectedFile] = useState<{
    item: TreeItem;
    text?: string;
  } | null>(null);
  const [inspector, setInspector] = useState<
    "activity" | "changes" | "artifacts" | "settings"
  >("activity");
  const [showSettings, setShowSettings] = useState(false);
  const [firstLaunchSetup, setFirstLaunchSetup] = useState(false);
  const [showMobileNav, setShowMobileNav] = useState(false);
  const [error, setError] = useState("");
  const eventSource = useRef<EventSource | null>(null);
  const treeRefreshTimer = useRef<number | null>(null);
  const childrenRef = useRef<Record<string, TreeItem[]>>({});
  childrenRef.current = children;

  useEffect(() => {
    void (async () => {
      try {
        const bootstrap = await api<{
          sessions: Session[];
          settings: Settings;
          providers: Provider[];
          project_name: string;
        }>("/bootstrap");
        setSessions(bootstrap.sessions);
        setSettings(bootstrap.settings);
        setProviders(bootstrap.providers);
        if (!bootstrap.settings.llm_configured) {
          setFirstLaunchSetup(true);
          setShowSettings(true);
        }
        setProjectName(bootstrap.project_name);
        const first =
          bootstrap.sessions[0] ??
          (await api<Session>("/sessions", { method: "POST" }));
        setSessionId(first.id);
        setMessages(first.messages ?? []);
        await loadTree(".");
      } catch (reason) {
        setError(
          reason instanceof Error ? reason.message : "无法连接本地智能体服务",
        );
      }
    })();
    return () => {
      eventSource.current?.close();
      if (treeRefreshTimer.current !== null) {
        window.clearTimeout(treeRefreshTimer.current);
      }
    };
  }, []);

  async function loadTree(path: string) {
    try {
      const result = await api<{ items: TreeItem[] }>(
        `/workspace/tree?path=${encodeURIComponent(path)}`,
      );
      setChildren((current) => ({ ...current, [path]: result.items }));
      if (path === ".") setTree(result.items);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取项目文件");
    }
  }

  async function refreshTree() {
    const paths = [
      ".",
      ...Object.keys(childrenRef.current).filter((path) => path !== "."),
    ];
    await Promise.all(paths.map((path) => loadTree(path)));
  }

  function scheduleTreeRefresh() {
    if (treeRefreshTimer.current !== null) return;
    treeRefreshTimer.current = window.setTimeout(() => {
      treeRefreshTimer.current = null;
      void refreshTree();
    }, 350);
  }

  async function chooseSession(id: string) {
    if (running) return;
    const session = await api<Session>(`/sessions/${id}`);
    setSessionId(id);
    setMessages(session.messages ?? []);
    setStreaming("");
    setActivity([]);
    setShowMobileNav(false);
  }

  async function newSession() {
    const session = await api<Session>("/sessions", { method: "POST" });
    setSessions((current) => [session, ...current]);
    await chooseSession(session.id);
  }

  async function deleteSession(session: Session) {
    if (running && session.id === sessionId) {
      setError("当前会话正在运行，请先停止任务后再删除。");
      return;
    }
    if (!window.confirm(`确定删除会话“${session.name}”吗？此操作无法撤销。`)) return;
    try {
      await api(`/sessions/${session.id}`, { method: "DELETE" });
      const remaining = sessions.filter((item) => item.id !== session.id);
      setSessions(remaining);
      if (session.id !== sessionId) return;
      if (remaining.length > 0) {
        await chooseSession(remaining[0].id);
        return;
      }
      const replacement = await api<Session>("/sessions", { method: "POST" });
      setSessions([replacement]);
      setSessionId(replacement.id);
      setMessages(replacement.messages ?? []);
      setStreaming("");
      setActivity([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除会话失败");
    }
  }

  function recordActivity(item: ActivityItem) {
    setActivity((items) => upsertActivity(items, [item]));
  }

  function processEvent(event: MessageEvent<string>, activeRunId: string) {
    const payload = JSON.parse(event.data) as {
      type: string;
      data: Record<string, unknown>;
    };
    const data = payload.data ?? {};
    if (payload.type === "assistant_delta")
      setStreaming((value) => value + String(data.text ?? ""));
    if (payload.type === "tool_progress") {
      scheduleTreeRefresh();
      recordActivity({
        id: `${Date.now()}-${Math.random()}`,
        text: String(data.text ?? ""),
        kind: "tool",
      });
    }
    if (payload.type === "vision_setup_required") {
      const visionMessage = "未配置视觉模型，已跳过图片分析。可在设置中配置后重试。";
      setActivity((items) => {
        const reversedIndex = [...items]
          .reverse()
          .findIndex((item) => item.text.includes("调用视觉模型分析图片"));
        if (reversedIndex >= 0) {
          const index = items.length - 1 - reversedIndex;
          const next = [...items];
          next[index] = { ...next[index], text: visionMessage, kind: "error" };
          return next;
        }
        return upsertActivity(items, [
          { id: `${Date.now()}-vision-setup`, text: visionMessage, kind: "error" },
        ]);
      });
    }
    if (payload.type === "run_state") {
      recordActivity({
        id: `${Date.now()}-state`,
        text: formatRunState(String(data.state ?? "running")),
        kind: "state",
      });
    }
    if (payload.type === "confirmation_required") {
      setConfirmation({
        runId: activeRunId,
        ...(data as Omit<Confirmation, "runId">),
      });
      recordActivity({
        id: `${Date.now()}-confirm`,
        text: "等待你的确认",
        kind: "state",
      });
    }
    if (payload.type === "secret_required") {
      setSecretPrompt({
        runId: activeRunId,
        ...(data as Omit<SecretPrompt, "runId">),
      });
    }
    if (payload.type === "content_blocked" || payload.type === "error") {
      recordActivity({
        id: `${Date.now()}-error`,
        text: String(data.text ?? data.message ?? "运行失败"),
        kind: "error",
      });
    }
    if (payload.type === "session_title_updated") {
      const nextName = String(data.name ?? "").trim();
      if (nextName) {
        setSessions((current) =>
          current.map((session) =>
            session.id === sessionId
              ? { ...session, name: nextName, title_source: "auto" }
              : session,
          ),
        );
      }
      eventSource.current?.close();
    }
    if (payload.type === "session_title_failed") eventSource.current?.close();
    if (payload.type === "run_completed" || payload.type === "run_cancelled") {
      setCloudRefreshVersion((version) => version + 1);
      void refreshTree();
      setRunning(false);
      setCancelling(false);
      setConfirmation(null);
      setSecretPrompt(null);
      if (!data.title_pending) eventSource.current?.close();
      void api<Session>(`/sessions/${sessionId}`).then((session) => {
        setMessages(session.messages ?? []);
        setSessions((current) =>
          current.map((item) => (item.id === session.id ? { ...item, ...session } : item)),
        );
      });
      if (streaming.trim()) {
        setMessages((current) => [
          ...current,
          { role: "assistant", content: streaming },
        ]);
      }
      setStreaming("");
      setRunId("");
      recordActivity({
        id: `${Date.now()}-done`,
        text: payload.type === "run_cancelled" ? "任务已取消" : "任务完成",
        kind: "success",
      });
    }
  }

  async function submitPrompt() {
    const prompt = draft.trim();
    if (!prompt || !sessionId || running) return;
    setError("");
    setDraft("");
    setMessages((current) => [...current, { role: "user", content: prompt }]);
    setStreaming("");
    setRunning(true);
    setCancelling(false);
    setActivity([
      { id: `${Date.now()}-start`, text: "正在启动智能体", kind: "state" },
    ]);
    try {
      const status = await api<{ run_id: string }>(
        `/sessions/${sessionId}/runs`,
        {
          method: "POST",
          body: JSON.stringify({ prompt }),
        },
      );
      setRunId(status.run_id);
      const source = new EventSource(
        `${API}/runs/${status.run_id}/events?session_id=${encodeURIComponent(sessionId)}`,
      );
      eventSource.current = source;
      source.onmessage = (event) => processEvent(event, status.run_id);
      [
        "assistant_delta",
        "tool_progress",
        "run_state",
        "confirmation_required",
        "secret_required",
        "vision_setup_required",
        "content_blocked",
        "error",
        "session_title_updated",
        "session_title_failed",
        "run_completed",
        "run_cancelled",
      ].forEach((name) => {
        source.addEventListener(name, (event) =>
          processEvent(event as MessageEvent<string>, status.run_id),
        );
      });
      source.onerror = () =>
        recordActivity({
          id: `${Date.now()}-stream`,
          text: "事件流暂时断开，正在等待重连…",
          kind: "state",
        });
    } catch (reason) {
      setRunning(false);
      setError(reason instanceof Error ? reason.message : "无法启动任务");
    }
  }

  async function decide(choice: "allow" | "always" | "deny") {
    if (!confirmation) return;
    await api(
      `/runs/${confirmation.runId}/confirmation?session_id=${encodeURIComponent(sessionId)}`,
      {
        method: "POST",
        body: JSON.stringify({ choice }),
      },
    );
    setConfirmation(null);
  }

  async function submitSecret() {
    if (!secretPrompt || !secretValue) return;
    await api(
      `/runs/${secretPrompt.runId}/secret?session_id=${encodeURIComponent(sessionId)}`,
      {
        method: "POST",
        body: JSON.stringify({
          request_id: secretPrompt.request_id,
          secret: secretValue,
        }),
      },
    );
    setSecretPrompt(null);
    setSecretValue("");
  }

  async function cancelRun() {
    if (!runId || cancelling) return;
    setCancelling(true);
    recordActivity({
      id: `${Date.now()}-cancelling`,
      text: "正在取消任务…",
      kind: "state",
    });
    try {
      await api(`/runs/${runId}?session_id=${encodeURIComponent(sessionId)}`, {
        method: "DELETE",
      });
    } catch (reason) {
      setCancelling(false);
      setError(reason instanceof Error ? reason.message : "取消任务失败");
    }
  }

  async function toggleFolder(item: TreeItem) {
    const next = !expanded[item.path];
    setExpanded((current) => ({ ...current, [item.path]: next }));
    if (next && !children[item.path]) await loadTree(item.path);
  }

  async function openFile(item: TreeItem) {
    if (!item.artifact_id) return;
    try {
      const result = await api<{ text: string }>(
        `/artifacts/${item.artifact_id}/text`,
      );
      setSelectedFile({ item, text: result.text });
    } catch {
      setSelectedFile({ item });
    }
  }

  const activeSession = sessions.find((item) => item.id === sessionId);
  const shownMessages = useMemo(
    () =>
      messages.filter(
        (item) => item.role === "user" || item.role === "assistant",
      ),
    [messages],
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <button
          className="icon-button mobile-only"
          onClick={() => setShowMobileNav((value) => !value)}
          aria-label="打开导航"
        >
          <Menu size={18} />
        </button>
        <div className="brand-mark">
          <span className="brand-orbit">✦</span>
          <span>Aerolytica</span>
          <span className="brand-caption">RESEARCH AGENT</span>
        </div>
        <div className="topbar-project">
          <FolderOpen size={15} />
          <span>{projectName}</span>
          <ChevronDown size={14} />
        </div>
        <div className="topbar-spacer" />
        <div className="connection-pill">
          <span className="connection-dot" />
          本地运行
        </div>
        <button
          className="icon-button"
          onClick={() => {
            setFirstLaunchSetup(false);
            setShowSettings(true);
          }}
          aria-label="设置"
        >
          <Settings2 size={17} />
        </button>
        <button className="avatar" aria-label="用户">
          A
        </button>
      </header>

      {error && (
        <div className="error-banner">
          <CircleAlert size={15} />
          {error}
          <button onClick={() => setError("")}>
            <X size={14} />
          </button>
        </div>
      )}

      <div className={`workspace ${showMobileNav ? "mobile-nav-open" : ""}`}>
        <aside className="left-sidebar">
          <div className="sidebar-section-heading">
            <span>SESSIONS</span>
            <button
              className="small-icon-button"
              onClick={() => void newSession()}
              title="新建会话"
            >
              <Plus size={15} />
            </button>
          </div>
          <div className="session-list">
            {sessions.map((session) => (
              <div
                key={session.id}
                className={`session-row ${session.id === sessionId ? "selected" : ""}`}
              >
                <button
                  className="session-main"
                  onClick={() => void chooseSession(session.id)}
                  aria-label={`打开会话 ${session.name}`}
                >
                  <MessageSquarePlus size={15} />
                  <span className="session-name">{session.name}</span>
                  <span className="session-time">
                    {formatTime(session.updated_at)}
                  </span>
                </button>
                <button
                  className="session-delete"
                  onClick={(event) => {
                    event.stopPropagation();
                    void deleteSession(session);
                  }}
                  aria-label={`删除会话 ${session.name}`}
                  title="删除会话"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
            {!sessions.length && (
              <div className="empty-sidebar">
                还没有会话
                <br />
                <button onClick={() => void newSession()}>开始一次研究</button>
              </div>
            )}
          </div>
          <div className="sidebar-section-heading project-heading">
            <span>PROJECT</span>
            <button className="small-icon-button" title="搜索文件">
              <Search size={14} />
            </button>
          </div>
          <div className="file-tree">
            <TreeNodes
              items={tree}
              expanded={expanded}
              childrenMap={children}
              onFolder={toggleFolder}
              onFile={openFile}
            />
          </div>
          <div className="sidebar-bottom">
            <CloudAccountIndicator account={account} error={accountError} onClick={() => { setShowCloud(true); void refreshAccount(); }} />
            <button
              className="sidebar-link"
              onClick={() => setInspector("artifacts")}
            >
              <Archive size={15} />
              产物库
            </button>
            <button
              className="sidebar-link"
              onClick={() => {
                setFirstLaunchSetup(false);
                setInspector("settings");
              }}
            >
              <Settings2 size={15} />
              工作区设置
            </button>
          </div>
        </aside>

        <main className="chat-pane">
          <div className="chat-header">
            <div>
              <div className="eyebrow">
                WORKSPACE / {settings?.mode?.toUpperCase() ?? "EXECUTE"}
              </div>
              <h1>{activeSession?.name ?? "新研究任务"}</h1>
            </div>
            <div className="chat-header-actions">
              <button className="quiet-button">
                <Sparkles size={14} />
                {settings?.model ?? "选择模型"}
              </button>
              <button className="icon-button">
                <MoreHorizontal size={18} />
              </button>
            </div>
          </div>
          <div className="message-scroll">
            {shownMessages.length === 0 && !streaming && (
              <Welcome onPrompt={(value) => setDraft(value)} />
            )}
            {shownMessages.map((message, index) => (
              <MessageCard key={`${index}-${message.role}`} message={message} />
            ))}
            {streaming && (
              <MessageCard
                message={{ role: "assistant", content: streaming }}
                streaming
              />
            )}
            {activity.length > 0 && (
              <InlineActivity
                items={activity}
                running={running}
                onCancel={() => void cancelRun()}
              />
            )}
            {running && !streaming && activity.length === 0 && (
              <div className="thinking-card">
                <div className="thinking-pulse">
                  <span />
                  <span />
                  <span />
                </div>
                <span>智能体正在检查任务…</span>
                <button
                  onClick={() => void cancelRun()}
                  className="cancel-inline"
                >
                  <CircleStop size={14} />
                  停止
                </button>
              </div>
            )}
            {confirmation && (
              <ConfirmationCard confirmation={confirmation} onDecide={decide} />
            )}
            {secretPrompt && (
              <SecretCard
                prompt={secretPrompt}
                value={secretValue}
                onChange={setSecretValue}
                onSubmit={() => void submitSecret()}
              />
            )}
          </div>
          <div className="composer-wrap">
            <div className={`composer ${running ? "composer-running" : ""}`}>
              <textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void submitPrompt();
                  }
                }}
                placeholder="描述你的研究任务…"
                disabled={running}
                rows={1}
              />
              <div className="composer-toolbar">
                <div className="composer-tools">
                  <button className="tool-button">
                    <Paperclip size={15} />
                    附件
                  </button>
                  <button className="tool-button">
                    <Code2 size={15} />
                    上下文
                  </button>
                  <span className="mode-chip">
                    <Play size={12} />
                    {settings?.mode ?? "execute"}
                    <ChevronDown size={12} />
                  </span>
                </div>
                <div className="composer-actions">
                  <span className="shortcut-hint">
                    Enter 发送 · Shift+Enter 换行
                  </span>
                  {running ? (
                    <button
                      className="send-button stop"
                      onClick={() => void cancelRun()}
                      aria-label="停止任务"
                      disabled={cancelling}
                      title={cancelling ? "正在取消任务…" : "停止任务"}
                    >
                      {cancelling ? <span className="cancel-spinner" /> : <Square size={16} fill="currentColor" />}
                    </button>
                  ) : (
                    <button
                      className="send-button"
                      onClick={() => void submitPrompt()}
                      disabled={!draft.trim()}
                      aria-label="发送"
                    >
                      <ArrowUp size={17} />
                    </button>
                  )}
                </div>
              </div>
            </div>
            <div className="composer-footnote">
              <ShieldCheck size={12} />
              本地项目安全边界已启用 · 智能体的文件操作会请求确认
            </div>
          </div>
        </main>

        <aside className="right-inspector">
          <div className="inspector-tabs">
            <button
              className={inspector === "activity" ? "active" : ""}
              onClick={() => setInspector("activity")}
            >
              <Activity size={14} />
              活动
            </button>
            <button
              className={inspector === "changes" ? "active" : ""}
              onClick={() => setInspector("changes")}
            >
              <GitCompare size={14} />
              变更
            </button>
            <button
              className={inspector === "artifacts" ? "active" : ""}
              onClick={() => setInspector("artifacts")}
            >
              <Archive size={14} />
              产物
            </button>
            <button
              className={inspector === "settings" ? "active" : ""}
              onClick={() => setInspector("settings")}
            >
              <PanelRight size={14} />
            </button>
          </div>
          {inspector === "activity" && (
            <ActivityPanel
              itemCount={activity.length}
              running={running}
              onCancel={() => void cancelRun()}
            />
          )}
          {inspector === "changes" && <ChangesPanel />}
          {inspector === "artifacts" && <ArtifactsPanel />}
          {inspector === "settings" && (
            <QuickSettings
              settings={settings}
              onOpen={() => {
                setFirstLaunchSetup(false);
                setShowSettings(true);
              }}
            />
          )}
        </aside>
      </div>

      {selectedFile && (
        <FilePreview
          file={selectedFile}
          onClose={() => setSelectedFile(null)}
        />
      )}
      {showSettings && (
        <SettingsModal
          settings={settings}
          providers={providers}
          firstLaunch={firstLaunchSetup}
          onClose={() => {
            setShowSettings(false);
            setFirstLaunchSetup(false);
          }}
          onSave={(next) => {
            setSettings(next);
            setShowSettings(false);
            setFirstLaunchSetup(false);
          }}
        />
      )}
      {showCloud && <CloudPanel account={account} accountError={accountError} refreshAccount={refreshAccount} sessionId={sessionId} onClose={() => setShowCloud(false)} onAccountChanged={() => {
        void api<{ settings: Settings }>("/bootstrap").then((result) => setSettings(result.settings)).catch(() => setError("账户已更新，但工作区设置刷新失败，请刷新页面。"));
      }} />}
    </div>
  );
}

function TreeNodes({
  items,
  expanded,
  childrenMap,
  onFolder,
  onFile,
  depth = 0,
}: {
  items: TreeItem[];
  expanded: Record<string, boolean>;
  childrenMap: Record<string, TreeItem[]>;
  onFolder: (item: TreeItem) => void;
  onFile: (item: TreeItem) => void;
  depth?: number;
}) {
  return (
    <>
      {items.map((item) => (
        <div key={item.path}>
          <button
            className="tree-row"
            style={{ paddingLeft: `${14 + depth * 14}px` }}
            onClick={() =>
              item.kind === "directory" ? onFolder(item) : onFile(item)
            }
          >
            {item.kind === "directory" ? (
              expanded[item.path] ? (
                <ChevronDown size={13} />
              ) : (
                <ChevronRight size={13} />
              )
            ) : (
              <span className="tree-spacer" />
            )}
            {item.kind === "directory" && expanded[item.path] ? (
              <FolderOpen size={15} />
            ) : (
              fileIcon(item)
            )}
            <span>{item.name}</span>
          </button>
          {item.kind === "directory" && expanded[item.path] && (
            <TreeNodes
              items={childrenMap[item.path] ?? []}
              expanded={expanded}
              childrenMap={childrenMap}
              onFolder={onFolder}
              onFile={onFile}
              depth={depth + 1}
            />
          )}
        </div>
      ))}
    </>
  );
}

function Welcome({ onPrompt }: { onPrompt: (value: string) => void }) {
  const suggestions = [
    "下载 ERA5 的 2 米气温并绘制华北月平均图",
    "检查北京站 2023 年 7 月的逐小时观测缺测情况",
    "检索东亚夏季风与极端降水的近五年文献",
  ];
  return (
    <div className="welcome">
      <div className="welcome-icon">
        <Sparkles size={23} />
      </div>
      <div className="eyebrow">AEROLYTICA RESEARCH WORKSPACE</div>
      <h2>把下一个气象问题交给智能体</h2>
      <p>从数据发现、下载、分析到可复现图件，在一个研究上下文里完成。</p>
      <div className="suggestion-grid">
        {suggestions.map((item) => (
          <button key={item} onClick={() => onPrompt(item)}>
            <span>{item}</span>
            <ArrowUp size={14} />
          </button>
        ))}
      </div>
    </div>
  );
}

function MessageCard({
  message,
  streaming = false,
}: {
  message: Message;
  streaming?: boolean;
}) {
  if (message.role === "user")
    return (
      <div className="user-message">
        <div className="message-meta">
          <span className="user-badge">你</span>
          <span>刚刚</span>
        </div>
        <div className="user-bubble">{message.content}</div>
      </div>
    );
  return (
    <div className="assistant-message">
      <div className="message-meta">
        <span className="agent-badge">
          <Bot size={13} />
        </span>
        <span>Aero</span>
        {streaming && <span className="streaming-label">正在输出</span>}
        <button
          className="copy-button"
          title="复制回复"
          onClick={() => void navigator.clipboard?.writeText(message.content)}
        >
          <Copy size={13} />
        </button>
      </div>
      <div className="markdown-body">
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkMath]}
          rehypePlugins={[rehypeKatex]}
        >
          {message.content}
        </ReactMarkdown>
      </div>
    </div>
  );
}

function InlineActivity({
  items,
  running,
  onCancel,
}: {
  items: ActivityItem[];
  running: boolean;
  onCancel: () => void;
}) {
  const [expanded, setExpanded] = useState(true);

  useEffect(() => {
    setExpanded(running);
  }, [running]);

  if (!items.length) return null;
  const open = running || expanded;
  const latest = items[items.length - 1];
  const hasError = items.some((item) => item.kind === "error");

  return (
    <section className={`inline-activity ${open ? "expanded" : "collapsed"}`}>
      <div className="inline-activity-header">
        <button
          className="inline-activity-toggle"
          onClick={() => {
            if (!running) setExpanded((value) => !value);
          }}
          aria-expanded={open}
          title={running ? "操作进行中" : open ? "折叠操作记录" : "展开操作记录"}
        >
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span className="inline-activity-title">
            {running
              ? "正在执行操作"
              : hasError
                ? "操作未完成"
                : `${items.length} 项操作已完成`}
          </span>
          {!open && <span className="inline-activity-summary">{latest.text}</span>}
        </button>
        {running && (
          <button
            className="inline-activity-cancel"
            onClick={onCancel}
            title="停止任务"
          >
            <CircleStop size={13} />
            停止
          </button>
        )}
      </div>
      {open && (
        <div className="inline-activity-list">
          {items.map((item) => (
            <div className={`inline-activity-row ${item.kind}`} key={item.id}>
              <span className="activity-marker" />
              <span>{item.text}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function ConfirmationCard({
  confirmation,
  onDecide,
}: {
  confirmation: Confirmation;
  onDecide: (choice: "allow" | "always" | "deny") => void;
}) {
  const summary = summarizeConfirmation(confirmation);

  return (
    <div className="decision-card">
      <div className="decision-title">
        <ShieldCheck size={17} />
        <div>
          <strong>{summary.title}</strong>
          <span>{summary.explanation}</span>
        </div>
      </div>
      {summary.details.length > 0 && (
        <div className="confirmation-details">
          {summary.details.map((detail) => (
            <div className="confirmation-detail" key={detail.label}>
              <strong>{detail.label}</strong>
              <span>{detail.value}</span>
            </div>
          ))}
        </div>
      )}
      {summary.warning && <div className="confirmation-warning">{summary.warning}</div>}
      <div className="decision-actions">
        <button className="danger-ghost" onClick={() => onDecide("deny")}>
          {summary.executionApproval ? "暂不执行" : "拒绝"}
        </button>
        {!summary.executionApproval && (
          <button className="quiet-button" onClick={() => onDecide("always")}>
            本次会话允许
          </button>
        )}
        <button className="primary-button" onClick={() => onDecide("allow")}>
          <Check size={14} />
          {summary.executionApproval ? "开始执行" : "允许一次"}
        </button>
      </div>
    </div>
  );
}

function SecretCard({
  prompt,
  value,
  onChange,
  onSubmit,
}: {
  prompt: SecretPrompt;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  const multiline = prompt.multiline === true;

  return (
    <div className="decision-card secret-card">
      <div className="decision-title">
        <KeyRound size={17} />
        <div>
          <strong>{prompt.title ?? "需要安全凭据"}</strong>
          <span>
            {prompt.instructions ?? "凭据只会交给当前本地任务，不会写入对话。"}
          </span>
        </div>
      </div>
      {multiline ? (
        <textarea
          className="secret-multiline-input"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="粘贴需要的完整多行内容"
          rows={5}
          spellCheck={false}
          autoFocus
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
            }
          }}
        />
      ) : (
        <input
          type="password"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="输入凭据"
          autoFocus
          onKeyDown={(event) => {
            if (event.key === "Enter") onSubmit();
          }}
        />
      )}
      {multiline && (
        <div className="secret-input-hint">Enter 提交 · Shift+Enter 换行</div>
      )}
      <div className="decision-actions">
        <button className="primary-button" onClick={onSubmit}>
          安全提交
        </button>
      </div>
    </div>
  );
}

function ActivityPanel({
  itemCount,
  running,
  onCancel,
}: {
  itemCount: number;
  running: boolean;
  onCancel: () => void;
}) {
  return (
    <div className="inspector-body">
      <div className="inspector-title">
        <div>
          <span className="eyebrow">LIVE RUN</span>
              <h3>{running ? "智能体正在工作" : "最近活动"}</h3>
        </div>
        {running && (
          <button
            className="icon-button danger-icon"
            onClick={onCancel}
            title="停止"
          >
            <CircleStop size={16} />
          </button>
        )}
      </div>
      {running && (
        <div className="run-progress">
          <div className="progress-line">
            <span />
            <span />
            <span />
          </div>
          <span>操作记录会实时显示在对话中</span>
        </div>
      )}
      <div className="activity-location-card">
        <TerminalSquare size={23} />
        <p>{itemCount ? `${itemCount} 项操作记录` : "还没有运行活动"}</p>
        <span>
              智能体的操作过程会直接显示在对话中，任务完成后自动折叠，可点击记录标题重新展开。
        </span>
      </div>
    </div>
  );
}

function ChangesPanel() {
  return (
    <div className="inspector-body">
      <div className="inspector-title">
        <div>
          <span className="eyebrow">WORKSPACE DIFF</span>
          <h3>变更</h3>
        </div>
        <button className="quiet-button">
          <GitCompare size={14} />
          检查点
        </button>
      </div>
      <div className="inspector-empty">
        <GitCompare size={24} />
        <p>当前没有待查看的变更</p>
                <span>智能体修改项目文件后，这里会显示安全检查点与差异。</span>
      </div>
    </div>
  );
}
function ArtifactsPanel() {
  return (
    <div className="inspector-body">
      <div className="inspector-title">
        <div>
          <span className="eyebrow">OUTPUTS</span>
          <h3>产物</h3>
        </div>
        <button className="icon-button">
          <Upload size={16} />
        </button>
      </div>
      <div className="inspector-empty">
        <ImageIcon size={24} />
        <p>还没有产物</p>
        <span>图件、PDF 和报告会在生成后出现在这里。</span>
      </div>
    </div>
  );
}
function QuickSettings({
  settings,
  onOpen,
}: {
  settings: Settings | null;
  onOpen: () => void;
}) {
  return (
    <div className="inspector-body">
      <div className="inspector-title">
        <div>
          <span className="eyebrow">CONFIGURATION</span>
          <h3>工作区设置</h3>
        </div>
      </div>
      <div className="quick-setting">
        <span>主模型</span>
        <strong>{settings?.model ?? "未配置"}</strong>
      </div>
      <div className="quick-setting">
        <span>模式</span>
        <strong>{settings?.mode ?? "execute"}</strong>
      </div>
      <div className="quick-setting">
        <span>联网搜索</span>
        <strong>{settings?.web_search.enabled ? "已开启" : "未开启"}</strong>
      </div>
      <div className="quick-setting">
        <span>图片分析</span>
        <strong>{settings?.vision.configured ? "已配置" : "未配置（需要时跳过）"}</strong>
      </div>
      <button className="full-width-button" onClick={onOpen}>
        <Settings2 size={15} />
        打开完整设置
      </button>
    </div>
  );
}

function FilePreview({
  file,
  onClose,
}: {
  file: { item: TreeItem; text?: string };
  onClose: () => void;
}) {
  return (
    <div className="preview-overlay">
      <div className="file-preview">
        <div className="preview-header">
          <div>
            <span className="eyebrow">READ ONLY PREVIEW</span>
            <h3>{file.item.path}</h3>
          </div>
          <button className="icon-button" onClick={onClose}>
            <X size={17} />
          </button>
        </div>
        {file.text !== undefined ? (
          <pre>{file.text}</pre>
        ) : (
          <div className="preview-placeholder">
            <File size={26} />
            <p>该文件暂不支持内嵌文本预览</p>
            <a
              href={`${API}/artifacts/${file.item.artifact_id}`}
              target="_blank"
              rel="noreferrer"
            >
              在新标签页打开
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

function SettingsModal({
  settings,
  providers,
  firstLaunch,
  onClose,
  onSave,
}: {
  settings: Settings | null;
  providers: Provider[];
  firstLaunch: boolean;
  onClose: () => void;
  onSave: (settings: Settings) => void;
}) {
  const [draft, setDraft] = useState(settings);
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  if (!draft) return null;
  const currentDraft = draft;
  const selectedProvider = providers.find(
    (provider) => provider.id === currentDraft.provider,
  );
  const knownProvider = Boolean(selectedProvider);
  const modelOptions = selectedProvider
    ? Array.from(new Set([...selectedProvider.models, currentDraft.model]))
    : [];
  const providerChanged = Boolean(
    settings &&
      (currentDraft.provider !== settings.provider ||
        currentDraft.model !== settings.model ||
        currentDraft.base_url !== settings.base_url),
  );
  function chooseProvider(providerId: string) {
    const provider = providers.find((item) => item.id === providerId);
    setDraft({
      ...currentDraft,
      provider: providerId,
      model: provider?.default_model ?? "",
      base_url: provider?.base_url ?? "",
    });
  }
  async function save() {
    setSaving(true);
    setSaveError("");
    try {
      if (!currentDraft.llm_configured || apiKey.trim() || providerChanged) {
        if (!apiKey.trim()) throw new Error("请填写 API Key");
        const next = await api<Settings>("/settings/setup", {
          method: "POST",
          body: JSON.stringify({
            provider: currentDraft.provider,
            model: currentDraft.model,
            base_url: currentDraft.base_url,
            api_key: apiKey.trim(),
            reasoning_effort: currentDraft.reasoning_effort,
            language: currentDraft.language,
            mode: currentDraft.mode,
            max_tool_rounds: currentDraft.max_tool_rounds,
          }),
        });
        onSave(next);
        return;
      }
      const next = await api<Settings>("/settings", {
        method: "PATCH",
        body: JSON.stringify({
          model: currentDraft.model,
          provider: currentDraft.provider,
          base_url: currentDraft.base_url,
          reasoning_effort: currentDraft.reasoning_effort,
          language: currentDraft.language,
          mode: currentDraft.mode,
          max_tool_rounds: currentDraft.max_tool_rounds,
          vision: currentDraft.vision,
          web_search: currentDraft.web_search,
        }),
      });
      if (!next.llm_configured) throw new Error("请输入 API Key 后再保存");
      onSave(next);
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : "保存配置失败");
    } finally {
      setSaving(false);
    }
  }
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="settings-modal">
        <div className="modal-header">
          <div>
            <span className="eyebrow">
              {firstLaunch ? "FIRST RUN SETUP" : "WORKSPACE SETTINGS"}
            </span>
            <h2>{firstLaunch ? "先配置你的智能体" : "配置智能体"}</h2>
          </div>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="关闭设置"
          >
            <X size={18} />
          </button>
        </div>
        {firstLaunch && (
          <div className="setup-intro">
            <KeyRound size={18} />
            <div>
              <strong>欢迎使用 Aerolytica</strong>
              <span>
                首次运行需要一个模型 API
                Key。先进行一次最小请求测试，成功后才会保存配置。
              </span>
            </div>
          </div>
        )}
        <div className="settings-grid">
          <label>
            服务商
            <select
              value={knownProvider ? currentDraft.provider : "custom"}
              onChange={(event) => chooseProvider(event.target.value)}
            >
              {providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                </option>
              ))}
              <option value="custom">其他 OpenAI-compatible 接口</option>
            </select>
          </label>
          <label>
            模型
            {knownProvider ? (
              <select
                value={currentDraft.model}
                onChange={(event) =>
                  setDraft({ ...currentDraft, model: event.target.value })
                }
              >
                {modelOptions.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={currentDraft.model}
                onChange={(event) =>
                  setDraft({ ...currentDraft, model: event.target.value })
                }
                placeholder="模型 ID"
              />
            )}
          </label>
          <label className="settings-wide">
            API Key
            <input
              type="password"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={
                currentDraft.llm_configured && !providerChanged
                  ? "已配置，留空表示不修改"
                  : "粘贴模型服务商 API Key"
              }
              autoFocus={!currentDraft.llm_configured || providerChanged}
            />
          </label>
          <label className="settings-wide">
            接口地址
            <input
              value={currentDraft.base_url ?? ""}
              readOnly={knownProvider}
              onChange={(event) =>
                setDraft({ ...currentDraft, base_url: event.target.value })
              }
              placeholder="https://…/v1"
            />
          </label>
          {selectedProvider && (
            <div className="provider-help settings-wide">
              <strong>获取 {selectedProvider.name} API Key</strong>
              <a
                href={selectedProvider.api_key_url}
                target="_blank"
                rel="noreferrer"
              >
                打开 API Key 管理页面
              </a>
              <span>{selectedProvider.api_key_hint}</span>
            </div>
          )}
          <label>
            推理强度
            <select
              value={currentDraft.reasoning_effort}
              onChange={(event) =>
                setDraft({
                  ...currentDraft,
                  reasoning_effort: event.target.value,
                })
              }
            >
              <option value="">自动</option>
              <option value="low">低</option>
              <option value="medium">中</option>
              <option value="high">高</option>
              <option value="max">最大</option>
            </select>
          </label>
          <label>
            工作模式
            <select
              value={currentDraft.mode}
              onChange={(event) =>
                setDraft({ ...currentDraft, mode: event.target.value })
              }
            >
              <option value="plan">Plan</option>
              <option value="execute">Execute</option>
              <option value="qa">QA</option>
            </select>
          </label>
        </div>
        <div className="settings-note">
          <ShieldCheck size={15} />
          <span>
            {currentDraft.llm_configured && !providerChanged
              ? "主模型凭据已配置。"
              : "尚未配置主模型凭据。请先输入 API Key。"}{" "}
            保存前会先用 max_tokens=1 测试服务商、接口地址、模型和密钥。
          </span>
        </div>
        {saveError && (
          <div className="settings-error">
            <CircleAlert size={15} />
            {saveError}
          </div>
        )}
        <div className="modal-actions">
          <button className="quiet-button" onClick={onClose} disabled={saving}>
            {firstLaunch ? "稍后配置" : "取消"}
          </button>
          <button
            className="primary-button"
            onClick={() => void save()}
            disabled={
              saving ||
              ((!currentDraft.llm_configured || providerChanged) &&
                !apiKey.trim())
            }
          >
            {saving ? (
              "测试中…"
            ) : (
              <>
                <Check size={14} />
                测试并保存
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
