import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Cloud, RefreshCw, ShieldCheck, X } from "lucide-react";
import "./CloudPanel.css";

// Browser contract: credentials stay in the local Python service. Never add tokens here.
export type CloudAccount = {
  state: string;
  user: { user_id: string; email: string } | null;
  credits: { available_credits?: number; reserved_credits?: number } | null;
  settings?: unknown;
};
type Project = { project_id: string; name: string };
type Directory = { directory_id: string; name: string; path?: string };
type SyncStatus = {
  enabled?: boolean;
  state?: string;
  project_id?: string;
  directory_id?: string;
  includes?: string[];
  last_sync_at?: string;
  conflicts?: Record<string, { path: string; reason?: string }>;
};

async function cloud<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1/cloud${path}`, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  // Do not render raw upstream errors: they may contain credentials or request bodies.
  if (!response.ok) {
    const messages: Record<number, string> = {
      401: "登录已过期，请重新登录。", 403: "当前操作未获授权。",
      404: "本地服务暂不支持此功能，请更新服务后重试。",
      409: "状态已变化或存在冲突，请刷新后重试。",
      422: "请检查输入内容和目录白名单。",
    };
    throw new Error(messages[response.status] ?? `操作失败（${response.status}），请稍后重试。`);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

export function useCloudAccount(refreshVersion: number) {
  const [account, setAccount] = useState<CloudAccount | null>(null);
  const [accountError, setAccountError] = useState("");
  const request = useRef(0);
  const refreshAccount = useCallback(async () => {
    const sequence = ++request.current;
    try {
      const next = await cloud<CloudAccount>("/account");
      if (sequence === request.current) { setAccount(next); setAccountError(""); }
    } catch (error) {
      if (sequence === request.current) {
        setAccount(null);
        setAccountError(error instanceof Error ? error.message : "无法连接账户服务。");
      }
    }
  }, []);
  useEffect(() => { void refreshAccount(); }, [refreshAccount, refreshVersion]);
  return { account, accountError, refreshAccount };
}

function creditsLabel(account: CloudAccount | null) {
  const value = account?.credits?.available_credits;
  return typeof value === "number" ? `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)} 积分` : "积分待查询";
}

export function CloudAccountIndicator({ account, error, onClick }: {
  account: CloudAccount | null; error: string; onClick: () => void;
}) {
  return <button className="cloud-account-indicator" onClick={onClick} title="账户与云协作">
    <Cloud size={18} />
    <span><strong>{account?.user?.email ?? "Aero 官方云"}</strong>
      <small>{error ? "账户状态不可用 · 点击重试" : account?.user ? creditsLabel(account) : "登录账户 · 云同步与远程智能体"}</small></span>
    <span className={`cloud-dot ${account?.user ? "connected" : ""}`} />
  </button>;
}

export function CloudPanel({ account, accountError, refreshAccount, sessionId, onClose, onAccountChanged }: {
  account: CloudAccount | null;
  accountError: string;
  refreshAccount: () => Promise<void>;
  sessionId: string;
  onClose: () => void;
  onAccountChanged: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [tab, setTab] = useState<"account" | "sync" | "agents">("account");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [directories, setDirectories] = useState<Directory[]>([]);
  const [projectId, setProjectId] = useState("");
  const [directoryId, setDirectoryId] = useState("");
  const [projectName, setProjectName] = useState("");
  const [includes, setIncludes] = useState("");
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const loggedIn = Boolean(account?.user);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => { previous?.focus(); };
  }, []);

  async function act(action: () => Promise<void>, message = "操作已完成。") {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice("");
    try { await action(); setNotice(message); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法完成操作，请重试。"); }
    finally { busyRef.current = false; setBusy(false); }
  }

  const refreshSync = useCallback(async (initialize = false) => {
    const [list, status] = await Promise.all([
      cloud<{ items: Project[] }>("/projects"), cloud<SyncStatus>("/sync"),
    ]);
    setProjects(list.items); setSync(status);
    if (initialize) {
      setProjectId(status.project_id ?? list.items[0]?.project_id ?? "");
      setDirectoryId(status.directory_id ?? "");
      setIncludes((status.includes ?? []).join("\n"));
    }
  }, []);

  useEffect(() => {
    if (!(["sync", "agents"] as string[]).includes(tab) || !loggedIn) return;
    void act(() => refreshSync(true), "同步状态已更新。");
  }, [tab, loggedIn, refreshSync]);

  useEffect(() => {
    let active = true;
    setDirectories([]);
    if (projectId && loggedIn) {
      void cloud<{ items: Directory[] }>(`/projects/${encodeURIComponent(projectId)}/directories`)
        .then((result) => { if (active) setDirectories(result.items); })
        .catch((reason) => { if (active) setError(reason.message); });
    }
    return () => { active = false; };
  }, [projectId, loggedIn]);

  function login(event: FormEvent) {
    event.preventDefault();
    const submittedPassword = password;
    setPassword("");
    void act(async () => {
      await cloud("/account/login", "POST", { email: email.trim(), password: submittedPassword });
      await refreshAccount(); onAccountChanged();
    }, "已登录官方账户。");
  }

  return <dialog ref={dialog} className="cloud-dialog" aria-labelledby="cloud-title" onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
    <header className="cloud-header"><div><span className="eyebrow">官方云</span><h2 id="cloud-title"><Cloud size={21} />账户与云协作</h2></div>
      <button className="icon-button" aria-label="关闭云面板" disabled={busy} onClick={onClose}><X size={20} /></button></header>
    <nav className="cloud-tabs" aria-label="云功能">
      {([['account', '官方账户'], ['sync', '项目同步'], ['agents', '远程智能体']] as const).map(([id, label]) =>
        <button key={id} aria-current={tab === id ? "page" : undefined} disabled={busy} onClick={() => { setTab(id); setError(""); setNotice(""); }}>{label}</button>)}
    </nav>
    <div className="cloud-body" aria-busy={busy}>
      {(error || accountError) && <p className="cloud-alert" role="alert">{error || accountError}</p>}
      {notice && <p className="cloud-notice" role="status">{notice}</p>}
      {busy && <p className="cloud-muted" role="status">正在处理，请稍候…</p>}
      {tab === "account" && <>
        <section className="cloud-card"><h3>官方账户</h3><p>凭据由本机服务管理，浏览器不接收账户或智能体令牌。</p>
          {loggedIn ? <><div className="cloud-account-summary"><strong>{account?.user?.email}</strong><span className="cloud-badge">{account?.state}</span></div>
            <div className="cloud-credit"><strong>{creditsLabel(account)}</strong><span>可用余额 · 任务结束后自动刷新</span></div>
            <div className="cloud-actions"><button disabled={busy} onClick={() => void act(refreshAccount, "账户状态已刷新。")}><RefreshCw size={14} />刷新余额</button>
              <button disabled={busy} onClick={() => void act(async () => {
                await cloud("/account/logout", "POST", {}); await refreshAccount();
                setProjects([]); setDirectories([]); setSync(null); onAccountChanged();
              }, "已退出官方账户。")}>退出登录</button></div></> :
            <form onSubmit={login} className="cloud-form"><label>邮箱<input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
              <label>密码<input type="password" autoComplete="off" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
              <p className="cloud-muted">密码仅用于本次登录，提交后立即从输入框清除，不写入浏览器存储。</p>
              <button className="primary-button" disabled={busy || !email.trim() || !password}>登录官方账户</button></form>}
        </section>
        <aside className="cloud-security"><ShieldCheck size={19} /><span>本地控制，按需上云。同步白名单、远程权限和加密记忆分别授权，不会因登录自动开启。</span></aside>
      </>}
      {tab !== "account" && !loggedIn && <section className="cloud-empty"><Cloud size={28} /><h3>请先登录官方账户</h3><p>登录后可绑定云项目并管理本机远程智能体。</p><button onClick={() => setTab("account")}>前往登录</button></section>}
      {tab === "sync" && loggedIn && <>
        <section className="cloud-card"><div className="cloud-section-title"><h3>项目与目录</h3><button disabled={busy} onClick={() => void act(() => refreshSync(), "已刷新同步状态。")} aria-label="刷新同步状态"><RefreshCw size={14} /></button></div>
          <form className="cloud-inline-form" onSubmit={(event) => { event.preventDefault(); void act(async () => {
            await cloud("/projects", "POST", { name: projectName.trim() }); setProjectName(""); await refreshSync();
          }, "云项目已创建，请在下方选择。"); }}><label>新建云项目<input required maxLength={128} value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="例如：海洋研究" /></label><button disabled={busy || !projectName.trim()}>创建项目</button></form>
          <form className="cloud-form" onSubmit={(event) => { event.preventDefault(); void act(async () => {
            await cloud("/sync", "PUT", { project_id: projectId, directory_id: directoryId, includes: includes.split("\n").map((line) => line.trim()).filter(Boolean), enabled: true });
            await refreshSync();
          }, "目录绑定和白名单已保存。"); }}>
            <div className="cloud-grid"><label>云项目<select required value={projectId} onChange={(e) => { setProjectId(e.target.value); setDirectoryId(""); }}><option value="">选择项目</option>{projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}</select></label>
              <label>云目录<select disabled={!projectId || !directories.length} value={directoryId} onChange={(e) => setDirectoryId(e.target.value)}><option value="">项目根目录</option>{directories.map((directory) => <option key={directory.directory_id} value={directory.directory_id}>{directory.name || directory.path || directory.directory_id}</option>)}</select></label></div>
            {projectId && !directories.length && <p className="cloud-muted">此项目尚无可用目录。请先在云端创建目录，然后刷新。</p>}
            <label>本地文件白名单（每行一项）<textarea rows={4} required value={includes} onChange={(e) => setIncludes(e.target.value)} placeholder={"papers/**\noutputs/**"} /></label>
            <p className="cloud-muted">仅同步当前工作区中明确列出的相对路径。不要包含凭据、私钥或整个主目录。</p>
            <button disabled={busy || !projectId || !includes.trim()}>保存绑定并启用</button>
          </form>
        </section>
        <section className="cloud-card"><div className="cloud-section-title"><h3>同步状态</h3><span className="cloud-badge">{sync?.state ?? (sync?.enabled ? "已启用" : "未启用")}</span></div>
          {sync?.last_sync_at && <p>上次同步：{sync.last_sync_at}</p>}
          <div className="cloud-actions"><button disabled={busy || !sync?.project_id} onClick={() => void act(async () => { await cloud("/sync/run", "POST", {}); await refreshSync(); await refreshAccount(); }, "同步请求已完成。")}>立即同步</button>
            <button disabled={busy || !sync?.enabled} onClick={() => void act(async () => { await cloud("/sync/pause", "POST", {}); await refreshSync(); }, "同步已暂停。")}>暂停同步</button></div>
          <h4>文件冲突</h4>{!sync ? <p>尚未取得同步状态。</p> : !Object.keys(sync.conflicts ?? {}).length ? <p className="cloud-muted">暂无待处理冲突。</p> : Object.values(sync.conflicts ?? {}).map((conflict) => <div className="cloud-conflict" key={conflict.path}><code>{conflict.path}</code><p>{conflict.reason ?? "两端内容发生变化，请选择保留版本。"}</p><div className="cloud-actions">{([['local', '保留本地'], ['cloud', '保留云端'], ['both', '保留两份']] as const).map(([choice, label]) => <button key={choice} disabled={busy} onClick={() => {
            if (choice !== "both" && !window.confirm(`${label}并替换另一版本？\n${conflict.path}`)) return;
            void act(async () => { await cloud("/sync/resolve", "POST", { path: conflict.path, choice }); await refreshSync(); }, "冲突已处理。");
          }}>{label}</button>)}</div></div>)}
        </section>
      </>}
      {tab === "agents" && loggedIn && <AgentSection sessionId={sessionId} projectId={projectId || sync?.project_id || ""} />}
    </div>
  </dialog>;
}

type AgentMessage = { message_id: string; sender: string; content: string; created_at?: string | null; delivery_status?: string };
type AgentApproval = { approval_id: string; command_id: string; status: string; payload?: { tool?: string; message?: string }; created_at?: string };
type Agent = { agent_id: string; name: string; status: string; unread_count?: number; last_message?: AgentMessage | null };

function AgentSection({ sessionId, projectId }: { sessionId: string; projectId: string }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState<Agent | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [approvals, setApprovals] = useState<AgentApproval[]>([]);
  const [memoryRecoveryKey, setMemoryRecoveryKey] = useState("");
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [reply, setReply] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const result = await cloud<{ items: Agent[] }>("/agents");
    setAgents(result.items);
    if (selected) {
      const current = result.items.find((item) => item.agent_id === selected.agent_id);
      if (current) setSelected(current);
    }
  }

  async function openAgent(agent: Agent) {
    setSelected(agent); setError("");
    try {
      const [messageResult, approvalResult] = await Promise.all([
        cloud<{ messages: AgentMessage[] }>(`/agents/${agent.agent_id}/messages`),
        cloud<{ approvals: AgentApproval[] }>(`/agents/${agent.agent_id}/approvals`),
      ]);
      setMessages(messageResult.messages); setApprovals(approvalResult.approvals);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取智能体会话"); }
  }

  useEffect(() => { void refresh().catch((reason) => setError(reason.message)); }, []);

  async function register(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await cloud("/agents", "POST", {
        name: name.trim(), project_id: projectId, session_id: sessionId || undefined,
      });
      setName(""); await refresh();
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "注册失败"); }
    finally { setBusy(false); }
  }

  async function act(action: () => Promise<void>) {
    setBusy(true); setError("");
    try { await action(); await refresh(); if (selected) await openAgent(selected); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  }

  function downloadRecoveryKey() {
    if (!memoryRecoveryKey) return;
    const blob = new Blob([`${memoryRecoveryKey}\n`], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `aerolytica-memory-${selected?.agent_id ?? "recovery"}.txt`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return <section className="cloud-card"><h3>远程智能体</h3><p className="cloud-muted">关闭窗口后由本机常驻服务继续保持心跳；重新打开可继续查看同一会话。</p>
    {error && <p className="cloud-alert" role="alert">{error}</p>}
    <form className="cloud-inline-form" onSubmit={register}><label>注册名称<input required value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：ocean_research" /></label><button disabled={busy || !projectId}>注册智能体</button></form>
    {agents.map((agent) => <div className={`cloud-agent-row ${selected?.agent_id === agent.agent_id ? "selected" : ""}`} key={agent.agent_id}>
      <button className="cloud-agent-summary" onClick={() => void openAgent(agent)}><span><strong>{agent.name}</strong><small>{agent.status}{agent.unread_count ? ` · ${agent.unread_count} 条未读` : ""}</small></span><span>{selected?.agent_id === agent.agent_id ? "收起" : "查看会话"}</span></button>
      <div className="cloud-agent-actions"><button disabled={busy || !projectId} onClick={() => void act(async () => { await cloud(`/agents/${agent.agent_id}/start`, "POST", { project_id: projectId, session_id: sessionId }); })}>接管/托管当前会话</button><button disabled={busy} onClick={() => void act(async () => { await cloud(`/agents/${agent.agent_id}/stop`, "POST", {}); })}>停止</button></div>
      {selected?.agent_id === agent.agent_id && <div className="cloud-agent-detail">
        <div className="cloud-agent-messages">{messages.length ? messages.map((message) => <div className={`cloud-agent-message ${message.sender}`} key={message.message_id}><strong>{message.sender === "user" ? "我" : "智能体"}</strong><p>{message.content}</p></div>) : <p className="cloud-muted">暂无会话消息。</p>}</div>
        <form className="cloud-agent-reply" onSubmit={(event) => { event.preventDefault(); if (!reply.trim()) return; void act(async () => { await cloud(`/agents/${agent.agent_id}/messages`, "POST", { content: reply.trim(), client_message_id: `web-${Date.now()}` }); setReply(""); }); }}><input value={reply} onChange={(event) => setReply(event.target.value)} placeholder="向后台智能体发送消息" /><button disabled={busy || !reply.trim()}>发送</button></form>
        {approvals.filter((item) => item.status === "pending").map((approval) => <div className="cloud-approval" key={approval.approval_id}><span>等待审批：{approval.payload?.tool || "本地操作"}</span><div className="cloud-actions"><button disabled={busy} onClick={() => void act(async () => { await cloud(`/agents/${agent.agent_id}/approvals/${approval.approval_id}/resolve`, "POST", { decision: "approved" }); })}>允许</button><button disabled={busy} onClick={() => void act(async () => { await cloud(`/agents/${agent.agent_id}/approvals/${approval.approval_id}/resolve`, "POST", { decision: "denied" }); })}>拒绝</button><button disabled={busy} onClick={() => void act(async () => { await cloud(`/agents/${agent.agent_id}/commands/${approval.command_id}/cancel`, "POST", {}); })}>取消任务</button></div></div>)}
        <div className="cloud-memory"><strong>加密云记忆</strong><p className="cloud-muted">默认只保存在本机。开启后只上传密文；恢复密钥不会上传，请自行保存。</p><div className="cloud-actions"><button disabled={busy} onClick={() => void act(async () => { const result = await cloud<{ recovery_key: string }>(`/agents/${agent.agent_id}/memory/enable`, "POST", {}); setMemoryEnabled(true); setMemoryRecoveryKey(result.recovery_key); })}>{memoryEnabled ? "已开启" : "开启备份"}</button><button disabled={busy || !memoryEnabled} onClick={() => void act(async () => { await cloud(`/agents/${agent.agent_id}/memory/export`, "POST", {}); })}>立即备份</button></div>{memoryRecoveryKey && <p className="cloud-recovery-key"><span>请保存恢复密钥：</span><code>{memoryRecoveryKey}</code><button type="button" onClick={downloadRecoveryKey}>下载密钥</button></p>}<form className="cloud-agent-reply" onSubmit={(event) => { event.preventDefault(); if (!memoryRecoveryKey.trim()) return; void act(async () => { await cloud(`/agents/${agent.agent_id}/memory/import`, "POST", { recovery_key: memoryRecoveryKey.trim() }); }); }}><input value={memoryRecoveryKey} onChange={(event) => setMemoryRecoveryKey(event.target.value)} placeholder="输入恢复密钥以恢复云端记忆" /><button disabled={busy || !memoryRecoveryKey.trim()}>恢复</button></form></div>
      </div>}
    </div>)}
  </section>;
}
