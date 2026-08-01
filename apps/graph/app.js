// voiceops live agent graph
// Polls /debug/traces every 1s, maps span names -> graph nodes,
// animates the edge between the caller and each stage as it fires.
// Also supports a mock-data "demo mode" for screen-recording without a live server.

const e = React.createElement;
const { useState, useEffect, useMemo, useCallback, useRef } = React;
const RF = window.ReactFlow;
const ReactFlowProvider = RF.ReactFlowProvider;
const ReactFlow = RF.default;
const Controls = RF.Controls;
const MiniMap = RF.MiniMap;
const Background = RF.Background;
const Handle = RF.Handle;
const Position = RF.Position;

// ---------- graph shape ----------
// A node's `id` matches the span name (or a prefix). Edges are static; we
// animate them when the src or dst node was active in the last poll window.

const NODES = [
  { id: "caller",          label: "Caller",           kind: "entry",  x:   40, y: 240,  sub: "phone / browser sim" },
  { id: "transport",       label: "Twilio / Vapi",    kind: "entry",  x:  220, y: 240,  sub: "PSTN + WebRTC" },
  { id: "voice.stt",       label: "STT",              kind: "stt",    x:  420, y: 100,  sub: "deepgram nova-3" },
  { id: "gen_ai.chat_completion", label: "Brain (LLM)", kind: "llm", x:  660, y: 240,  sub: "groq llama-3.3-70b" },
  { id: "voice.tts_stream",label: "TTS (stream)",     kind: "tts",    x:  660, y: 380,  sub: "chatterbox mlx" },
  { id: "tools.group",     label: "Tools",            kind: "tools-group", x: 920, y: 100, sub: "vertical-specific" },
  { id: "tool.book_reservation", label: "book_reservation", kind: "tool", x: 1120, y:  40,  sub: "restaurant" },
  { id: "tool.book_appointment", label: "book_appointment", kind: "tool", x: 1120, y: 100,  sub: "clinic" },
  { id: "tool.check_availability", label: "check_availability", kind: "tool", x: 1120, y: 160, sub: "both" },
  { id: "tool.get_menu",   label: "get_menu",         kind: "tool",   x: 1120, y: 220, sub: "restaurant" },
  { id: "tool.check_allergen", label: "check_allergen", kind: "tool", x: 1120, y: 280, sub: "restaurant" },
  { id: "tool.verify_insurance", label: "verify_insurance", kind: "tool", x: 1120, y: 340, sub: "clinic" },
  { id: "tool.lookup_faq", label: "lookup_faq",       kind: "tool",   x: 1120, y: 400, sub: "all" },
  { id: "tool.escalate_to_human", label: "escalate_to_human", kind: "tool", x: 1120, y: 460, sub: "all" },
  { id: "exit",            label: "Response → caller", kind: "exit",  x:  920, y: 480, sub: "audio out" },
];

const EDGES = [
  { source: "caller", target: "transport" },
  { source: "transport", target: "voice.stt" },
  { source: "voice.stt", target: "gen_ai.chat_completion" },
  { source: "gen_ai.chat_completion", target: "tools.group" },
  { source: "tools.group", target: "tool.book_reservation" },
  { source: "tools.group", target: "tool.book_appointment" },
  { source: "tools.group", target: "tool.check_availability" },
  { source: "tools.group", target: "tool.get_menu" },
  { source: "tools.group", target: "tool.check_allergen" },
  { source: "tools.group", target: "tool.verify_insurance" },
  { source: "tools.group", target: "tool.lookup_faq" },
  { source: "tools.group", target: "tool.escalate_to_human" },
  { source: "gen_ai.chat_completion", target: "voice.tts_stream" },
  { source: "voice.tts_stream", target: "exit" },
  { source: "exit", target: "caller", type: "return" },
];

// ---------- span → node mapping ----------
function nodeIdForSpan(span) {
  // Direct match
  const direct = NODES.find(n => n.id === span.name);
  if (direct) return direct.id;
  // Tool call spans look like "tool.check_availability" or contain the tool name in attrs
  if (span.name && span.name.startsWith("tool.")) return span.name;
  if (span.attributes && span.attributes["tool.name"]) {
    return "tool." + span.attributes["tool.name"];
  }
  if (span.name === "voice.tts") return "voice.tts_stream";  // group with stream
  return null;
}

// ---------- custom node component ----------
function AgentNode({ data }) {
  const cls = ["rf-node", data.kind, data.active ? "active" : "", data.error ? "error" : ""].filter(Boolean).join(" ");
  return e("div", { className: cls, onClick: data.onClick },
    e(Handle, { type: "target", position: Position.Left, isConnectable: false }),
    e("div", { className: "title" }, data.label),
    e("div", { className: "sub" }, data.sub || ""),
    data.metric ? e("div", { className: "metric" }, data.metric) : null,
    e(Handle, { type: "source", position: Position.Right, isConnectable: false }),
  );
}

const nodeTypes = { agent: AgentNode };

// ---------- data source: live poll or mock ----------
class DataSource {
  constructor(mode, onSpans) {
    this.mode = mode;         // "live" | "mock"
    this.onSpans = onSpans;
    this.timer = null;
    this.mockCursor = 0;
    this.paused = false;
  }
  start() {
    if (this.mode === "live") {
      this._pollLive();
      this.timer = setInterval(() => this._pollLive(), 1000);
    } else {
      this._tickMock();
      this.timer = setInterval(() => this._tickMock(), 1200);
    }
  }
  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
  setPaused(p) { this.paused = p; }
  async _pollLive() {
    if (this.paused) return;
    try {
      const r = await fetch("/debug/traces?limit=100");
      if (!r.ok) throw new Error(r.status);
      const j = await r.json();
      this.onSpans(j.spans || [], "live");
    } catch (err) {
      this.onSpans([], "error", err.message);
    }
  }
  _tickMock() {
    if (this.paused) return;
    // Cycle through a canned "call transcript" — one span per tick
    const now = Date.now();
    const script = MOCK_SCRIPT;
    const cur = script[this.mockCursor % script.length];
    this.mockCursor++;
    const span = {
      span_id: "mock-" + this.mockCursor,
      name: cur.name,
      duration_ms: cur.duration_ms,
      status: "ok",
      cost_usd: cur.cost_usd || 0,
      attributes: { session_id: "demo-call-001", ...cur.attributes },
      start_ms: now,
    };
    this.onSpans([span], "mock");
  }
}

const MOCK_SCRIPT = [
  { name: "voice.stt", duration_ms: 180, attributes: { "stt.provider": "deepgram", "input.text": "Hi, do you have a table for four tonight at seven?" } },
  { name: "gen_ai.chat_completion", duration_ms: 340, cost_usd: 0.00021, attributes: { "gen_ai.system": "groq", "gen_ai.request.model": "llama-3.3-70b-versatile", "gen_ai.usage.input_tokens": 812, "gen_ai.usage.output_tokens": 42 } },
  { name: "tool.check_availability", duration_ms: 12, attributes: { "tool.name": "check_availability", "input": '{"party_size":4,"date":"2026-07-28"}', "output": '{"open_slots":["19:00","19:30","20:00"]}' } },
  { name: "gen_ai.chat_completion", duration_ms: 210, cost_usd: 0.00018, attributes: { "gen_ai.system": "groq", "gen_ai.usage.output_tokens": 28 } },
  { name: "voice.tts_stream", duration_ms: 240, cost_usd: 0.00034, attributes: { "tts.provider": "chatterbox", "text.length": 96, "audio.bytes_total": 41200 } },
  { name: "voice.stt", duration_ms: 165, attributes: { "input.text": "Seven works. Party of four, name is Jane." } },
  { name: "gen_ai.chat_completion", duration_ms: 380, cost_usd: 0.00023, attributes: { "gen_ai.system": "groq" } },
  { name: "tool.book_reservation", duration_ms: 18, attributes: { "tool.name": "book_reservation", "input": '{"caller_name":"Jane","party_size":4,"start_iso":"2026-07-28T19:00"}', "output": '{"booked":true}' } },
  { name: "voice.tts_stream", duration_ms: 260, cost_usd: 0.00041, attributes: { "text.length": 118 } },
];

// ---------- root component ----------
function App() {
  const [spans, setSpans] = useState([]);
  const [mode, setMode] = useState("live");   // live | mock
  const [connState, setConnState] = useState("connecting");
  const [paused, setPaused] = useState(false);
  const [activeNodes, setActiveNodes] = useState(new Set());
  const [selected, setSelected] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [sessionFilter, setSessionFilter] = useState("");
  const dsRef = useRef(null);
  const activeTimer = useRef(null);

  // seed data source
  useEffect(() => {
    if (dsRef.current) dsRef.current.stop();
    const ds = new DataSource(mode, (newSpans, state, err) => {
      if (state === "error") {
        setConnState("error");
        return;
      }
      setConnState(state === "mock" ? "mock" : "live");
      if (newSpans.length === 0) return;
      setSpans(prev => {
        // append + dedup by span_id
        const seen = new Set(prev.map(s => s.span_id));
        const fresh = newSpans.filter(s => !seen.has(s.span_id));
        if (fresh.length === 0) return prev;
        // update sessions list
        const nextSess = new Set(prev.map(s => s.attributes && s.attributes.session_id).filter(Boolean));
        fresh.forEach(s => { if (s.attributes && s.attributes.session_id) nextSess.add(s.attributes.session_id); });
        setSessions([...nextSess]);
        // animate nodes for fresh spans
        const nextActive = new Set();
        fresh.forEach(s => {
          const nid = nodeIdForSpan(s);
          if (nid) nextActive.add(nid);
        });
        setActiveNodes(nextActive);
        if (activeTimer.current) clearTimeout(activeTimer.current);
        activeTimer.current = setTimeout(() => setActiveNodes(new Set()), 1200);
        // keep only last 500 spans
        const combined = [...prev, ...fresh];
        return combined.slice(-500);
      });
    });
    ds.setPaused(paused);
    ds.start();
    dsRef.current = ds;
    return () => ds.stop();
  }, [mode]);

  useEffect(() => {
    if (dsRef.current) dsRef.current.setPaused(paused);
  }, [paused]);

  // derived per-node latest span (for metrics on node cards)
  const latestByNode = useMemo(() => {
    const out = {};
    const filtered = sessionFilter
      ? spans.filter(s => s.attributes && s.attributes.session_id === sessionFilter)
      : spans;
    for (const s of filtered) {
      const nid = nodeIdForSpan(s);
      if (!nid) continue;
      if (!out[nid] || s.start_ms > out[nid].start_ms) out[nid] = s;
    }
    return out;
  }, [spans, sessionFilter]);

  // aggregate stats
  const stats = useMemo(() => computeStats(spans, sessionFilter), [spans, sessionFilter]);

  // React Flow nodes/edges
  const rfNodes = useMemo(() => NODES.map(n => {
    const latest = latestByNode[n.id];
    const metric = latest && latest.duration_ms ? `${Math.round(latest.duration_ms)}ms` : null;
    return {
      id: n.id,
      position: { x: n.x, y: n.y },
      type: "agent",
      data: {
        label: n.label,
        kind: n.kind,
        sub: n.sub,
        active: activeNodes.has(n.id),
        error: latest && latest.status === "error",
        metric,
        onClick: () => setSelected(n.id),
      },
    };
  }), [activeNodes, latestByNode]);

  const rfEdges = useMemo(() => EDGES.map((edge, i) => ({
    id: `e${i}`,
    source: edge.source,
    target: edge.target,
    animated: activeNodes.has(edge.source) || activeNodes.has(edge.target),
    type: edge.type === "return" ? "smoothstep" : "default",
    style: edge.type === "return" ? { strokeDasharray: "4 4" } : {},
  })), [activeNodes]);

  // stats panel updates
  useEffect(() => {
    document.getElementById("stat-calls").textContent = sessions.length;
    document.getElementById("stat-spans").textContent = spans.length;
    document.getElementById("stat-e2e").textContent = stats.e2e ? `${Math.round(stats.e2e)}ms` : "—";
    document.getElementById("stat-llm").textContent = stats.llm ? `${Math.round(stats.llm)}ms` : "—";
    document.getElementById("stat-tts").textContent = stats.tts ? `${Math.round(stats.tts)}ms` : "—";
    document.getElementById("stat-stt").textContent = stats.stt ? `${Math.round(stats.stt)}ms` : "—";
    document.getElementById("stat-cost").textContent = "$" + stats.cost.toFixed(4);

    const badge = document.getElementById("mode-badge");
    badge.className = "badge " + (paused ? "paused" : connState);
    badge.textContent = paused ? "paused" : connState;

    // populate session select without React re-render
    const sel = document.getElementById("session-select");
    if (sel && sessions.length + 1 !== sel.options.length) {
      const cur = sel.value;
      sel.innerHTML = '<option value="">all sessions</option>' +
        sessions.map(s => `<option value="${s}">${s}</option>`).join("");
      sel.value = cur;
    }
  }, [spans, stats, sessions, paused, connState]);

  // wire up top-bar buttons
  useEffect(() => {
    const mockBtn = document.getElementById("mock-btn");
    const pauseBtn = document.getElementById("pause-btn");
    const sel = document.getElementById("session-select");
    const close = document.getElementById("close-inspector");
    const mockClick = () => {
      const next = mode === "mock" ? "live" : "mock";
      setMode(next);
      setSpans([]);
      setSessions([]);
      mockBtn.classList.toggle("active", next === "mock");
      mockBtn.textContent = next === "mock" ? "◼ live mode" : "▶ demo mode";
    };
    const pauseClick = () => {
      setPaused(p => {
        const np = !p;
        pauseBtn.textContent = np ? "▶ resume" : "⏸ pause";
        pauseBtn.classList.toggle("active", np);
        return np;
      });
    };
    const sessClick = (e) => setSessionFilter(e.target.value);
    const closeClick = () => setSelected(null);
    mockBtn.addEventListener("click", mockClick);
    pauseBtn.addEventListener("click", pauseClick);
    sel.addEventListener("change", sessClick);
    close.addEventListener("click", closeClick);
    return () => {
      mockBtn.removeEventListener("click", mockClick);
      pauseBtn.removeEventListener("click", pauseClick);
      sel.removeEventListener("change", sessClick);
      close.removeEventListener("click", closeClick);
    };
  }, [mode]);

  // render inspector into aside via DOM (React manages graph, direct DOM for side panel keeps it snappy)
  useEffect(() => {
    const title = document.getElementById("inspector-title");
    const body = document.getElementById("inspector-body");
    if (!selected) {
      title.textContent = "Click a node";
      body.innerHTML = '<p class="hint">Click any node in the graph to see its input, output, latency, and cost for the most recent call.</p>';
      return;
    }
    const node = NODES.find(n => n.id === selected);
    const latest = latestByNode[selected];
    title.textContent = (node && node.label) || selected;
    if (!latest) {
      body.innerHTML = `<p class="hint">No spans yet for <b>${node ? node.label : selected}</b>. Once a call hits this node it'll show input, output, latency, and cost here.</p>`;
      return;
    }
    body.innerHTML = renderInspector(node, latest);
  }, [selected, latestByNode]);

  // React Flow init
  return e(ReactFlowProvider, null,
    e(ReactFlow, {
      nodes: rfNodes,
      edges: rfEdges,
      nodeTypes,
      fitView: true,
      minZoom: 0.4,
      maxZoom: 1.5,
      proOptions: { hideAttribution: true },
    },
      e(Background, { color: "#1f2632", gap: 24 }),
      e(Controls, { showInteractive: false }),
      e(MiniMap, { nodeColor: (n) => nodeColorFor(n.data && n.data.kind), maskColor: "rgba(0,0,0,0.6)" }),
    ),
  );
}

function nodeColorFor(kind) {
  switch (kind) {
    case "entry": return "#b98cff";
    case "stt": return "#5fa8ff";
    case "llm": return "#ffb347";
    case "tts": return "#6ee7b7";
    case "tool": return "#b98cff";
    case "exit": return "#6ee7b7";
    default: return "#8892a3";
  }
}

function computeStats(spans, sessionFilter) {
  const filtered = sessionFilter
    ? spans.filter(s => s.attributes && s.attributes.session_id === sessionFilter)
    : spans;
  const p50 = (arr) => {
    if (!arr.length) return null;
    const s = [...arr].sort((a,b) => a-b);
    return s[Math.floor(s.length / 2)];
  };
  const durs = (name) => filtered.filter(s => s.name === name && s.duration_ms).map(s => s.duration_ms);
  const cost = filtered.reduce((sum, s) => sum + (s.cost_usd || 0), 0);
  const llm = p50(durs("gen_ai.chat_completion"));
  const stt = p50(durs("voice.stt"));
  const tts = p50(durs("voice.tts_stream").concat(durs("voice.tts")));
  return {
    llm, stt, tts,
    e2e: (stt || 0) + (llm || 0) + (tts || 0),
    cost,
  };
}

function renderInspector(node, span) {
  const attrs = span.attributes || {};
  const kv = (k, v) => `<div class="metric-row"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(v)}</span></div>`;
  const rows = [
    kv("span", span.name),
    kv("session", attrs.session_id || "—"),
    kv("latency", span.duration_ms ? Math.round(span.duration_ms) + "ms" : "—"),
    kv("status", span.status || "ok"),
    kv("cost", span.cost_usd ? "$" + span.cost_usd.toFixed(5) : "—"),
  ].join("");
  const input = attrs.input || attrs["input.text"] || attrs["gen_ai.prompt"] || null;
  const output = attrs.output || attrs["gen_ai.completion"] || null;
  const attrKeys = Object.keys(attrs).filter(k =>
    !["session_id","input","output","input.text","gen_ai.prompt","gen_ai.completion"].includes(k)
  );
  return `
    <div class="inspector-section">
      <h3>metrics</h3>
      ${rows}
    </div>
    ${input ? `<div class="inspector-section"><h3>input</h3><pre>${escapeHtml(pretty(input))}</pre></div>` : ""}
    ${output ? `<div class="inspector-section"><h3>output</h3><pre>${escapeHtml(pretty(output))}</pre></div>` : ""}
    ${attrKeys.length ? `<div class="inspector-section"><h3>attributes</h3><pre>${escapeHtml(pretty(Object.fromEntries(attrKeys.map(k => [k, attrs[k]]))))}</pre></div>` : ""}
  `;
}

function pretty(v) {
  if (typeof v === "string") {
    try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; }
  }
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

ReactDOM.createRoot(document.getElementById("graph")).render(e(App));
