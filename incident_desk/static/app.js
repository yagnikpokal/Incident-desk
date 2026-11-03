const STOP = new Set(["awaiting_approval", "awaiting_input", "paused", "resolved", "failed"]);

const state = {
  incidents: [],
  runs: [],
  current: null,
  incident: null,
  busy: false,
  error: "",
  gateKey: "",
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || response.statusText);
  }
  return body;
}

function statusLabel(status) {
  return status.replaceAll("_", " ");
}

async function refreshRuns() {
  state.runs = await api("/api/runs");
  renderRuns();
}

async function selectRun(id) {
  state.error = "";
  state.current = await api(`/api/runs/${id}`);
  state.incident = await api(`/api/incidents/${state.current.incident_id}`);
  state.gateKey = "";
  render();
}

async function start(incidentId) {
  state.busy = true;
  state.error = "";
  render();
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ incident_id: incidentId, policy: $("policy").value }),
    });
    await refreshRuns();
    await selectRun(run.id);
  } catch (error) {
    state.error = error.message;
  } finally {
    state.busy = false;
    render();
  }
}

async function advance() {
  const result = await api(`/api/runs/${state.current.id}/advance`, { method: "POST" });
  state.current = result.run;
  return result;
}

async function step() {
  await act(async () => {
    await advance();
    await refreshRuns();
  });
}

async function runUntilGate() {
  await act(async () => {
    while (state.current && !STOP.has(state.current.status)) {
      const result = await advance();
      render();
      if (!result.applied) break;
      await new Promise((resolve) => setTimeout(resolve, 280));
    }
    await refreshRuns();
  });
}

async function pause() {
  await act(async () => {
    state.current = await api(`/api/runs/${state.current.id}/pause`, { method: "POST" });
    await refreshRuns();
  });
}

async function resume() {
  await act(async () => {
    state.current = await api(`/api/runs/${state.current.id}/resume`, { method: "POST" });
    await refreshRuns();
  });
}

async function intervene(kind) {
  const note = $("note") ? $("note").value.trim() : "";
  await act(async () => {
    state.current = await api(`/api/runs/${state.current.id}/intervene`, {
      method: "POST",
      body: JSON.stringify({ kind, note }),
    });
    state.gateKey = "";
    await refreshRuns();
  });
}

async function act(work) {
  if (!state.current || state.busy) return;
  state.busy = true;
  state.error = "";
  render();
  try {
    await work();
  } catch (error) {
    state.error = error.message;
  } finally {
    state.busy = false;
    render();
  }
}

function renderIncidents() {
  $("incidents").replaceChildren(
    ...state.incidents.map((incident) =>
      h(
        "button",
        {
          class: "choice" + (state.incident && state.incident.id === incident.id ? " active" : ""),
          type: "button",
          onclick: () => start(incident.id),
          disabled: state.busy ? "disabled" : null,
        },
        h("strong", {}, incident.title),
        h("span", {}, `${incident.severity} · ${incident.service}`),
      ),
    ),
  );
}

function renderRuns() {
  if (!state.runs.length) {
    $("runs").replaceChildren(h("p", { class: "empty" }, "No runs yet. Pick an incident to open one."));
    return;
  }
  $("runs").replaceChildren(
    ...state.runs.map((run) =>
      h(
        "button",
        {
          class: "choice run-button" + (state.current && state.current.id === run.id ? " active" : ""),
          type: "button",
          onclick: () => selectRun(run.id),
        },
        h("strong", {}, run.id),
        h("span", { class: `status ${run.status}` }, statusLabel(run.status)),
      ),
    ),
  );
}

function renderBrief() {
  const incident = state.incident;
  if (!incident) {
    $("brief").replaceChildren(
      h("h3", {}, "Nothing open"),
      h("p", { class: "muted" }, "Start a run. Step through it, or let it investigate until it needs you."),
    );
    return;
  }
  const alert = incident.alert;
  $("brief").replaceChildren(
    h("p", { class: "eyebrow" }, `${incident.id} · ${incident.severity} · ${incident.service}`),
    h("h3", {}, incident.title),
    h("p", {}, incident.symptom),
    h(
      "p",
      { class: "muted" },
      `Alert ${alert.name} cites ${alert.metric} at ${alert.value} (baseline ${alert.baseline}) over ${alert.window}.`,
    ),
  );
}

function renderTimeline() {
  const run = state.current;
  if (!run) {
    $("timeline").replaceChildren();
    return;
  }
  if (!run.events.length) {
    $("timeline").replaceChildren(h("li", { class: "empty" }, "No steps yet. Step once to draft the plan."));
    return;
  }
  const nodes = run.events.map((event) => {
    const verifyClass = event.kind === "verification" ? (event.data.ok ? " ok" : " bad") : "";
    const body = [
      h("header", {}, h("span", { class: "kind" }, event.kind), h("time", { class: "muted" }, event.at.slice(11, 19))),
      h("p", {}, event.summary),
    ];
    if (event.data && Object.keys(event.data).length) {
      body.push(
        h("details", {}, h("summary", {}, "Payload"), h("pre", {}, JSON.stringify(event.data, null, 2))),
      );
    }
    return h("li", { class: `event ${event.kind}${verifyClass}` }, ...body);
  });
  $("timeline").replaceChildren(...nodes);
  const last = $("timeline").lastElementChild;
  if (last) last.scrollIntoView({ block: "nearest" });
}

function renderMeta() {
  const run = state.current;
  $("step").disabled = state.busy || !run || STOP.has(run.status);
  $("run-gate").disabled = state.busy || !run || STOP.has(run.status);
  $("pause").disabled = state.busy || !run || ["paused", "resolved", "failed"].includes(run.status);
  $("resume").disabled = state.busy || !run || run.status !== "paused";
  if (!run) {
    $("meta").replaceChildren(h("p", { class: "muted" }, "No run selected."));
    return;
  }
  const bits = [
    h("div", { class: "meta-row" }, h("span", { class: `status ${run.status}` }, statusLabel(run.status)), h("span", { class: "muted" }, `${run.step_count}/${run.max_steps} steps`)),
    h("p", { class: "muted" }, `Policy ${run.policy}${run.model ? " · " + run.model : ""}`),
  ];
  if (state.error) bits.unshift(h("p", { class: "error-banner" }, state.error));
  if (run.summary) bits.push(h("p", {}, run.summary));
  const banned = run.memory.banned_actions || [];
  if (banned.length) bits.push(h("p", { class: "muted" }, `Banned: ${banned.join(", ")}`));
  $("meta").replaceChildren(...bits);
}

function renderPlan() {
  const steps = state.current ? state.current.plan : [];
  if (!steps.length) {
    $("plan").replaceChildren(h("li", { class: "empty" }, "The plan appears on the first step."));
    return;
  }
  const mark = { done: "done", active: "now", pending: "wait" };
  $("plan").replaceChildren(
    ...steps.map((step) =>
      h("li", { class: `step ${step.status}` }, h("span", { class: "mark" }, mark[step.status]), h("span", {}, step.title)),
    ),
  );
}

function renderHypotheses() {
  const items = state.current ? state.current.hypotheses : [];
  if (!items.length) {
    $("hypotheses").replaceChildren(h("p", { class: "empty" }, "No hypothesis until a tool returns evidence."));
    return;
  }
  $("hypotheses").replaceChildren(
    ...items.map((item) =>
      h(
        "article",
        { class: "hyp" },
        h("header", {}, h("span", { class: "kind" }, item.status), h("span", { class: "muted" }, item.confidence.toFixed(2))),
        h("p", {}, item.claim),
        item.status === "rejected"
          ? null
          : h("div", { class: "meter" }, h("span", { style: `width:${Math.round(item.confidence * 100)}%` })),
        item.evidence && item.evidence.length ? h("p", { class: "muted" }, item.evidence.join(" · ")) : null,
      ),
    ),
  );
}

function renderGate() {
  const run = state.current;
  const key = run
    ? `${run.id}:${run.status}:${run.memory.pending_verification}:${run.proposal ? run.proposal.action + run.proposal.outcome : ""}:${(run.memory.proposal_history || []).length}`
    : "";
  if (key === state.gateKey && $("gate").childElementCount) {
    const note = $("note");
    if (note) note.disabled = state.busy;
    $("gate").querySelectorAll("button").forEach((button) => {
      button.disabled = state.busy;
    });
    return;
  }
  state.gateKey = key;
  if (!run) {
    $("gate").replaceChildren(h("p", { class: "empty" }, "Approve, reject, or answer here when the agent stops."));
    return;
  }

  const history = (run.memory.proposal_history || []).map((item) =>
    h("p", { class: "history" }, `${item.outcome}: ${item.action} on ${item.service}`),
  );

  if (run.status === "resolved" || run.status === "failed") {
    $("gate").replaceChildren(
      h("div", { class: "gate-card" }, h("p", { class: "eyebrow" }, statusLabel(run.status)), h("p", {}, run.summary || "Run finished.")),
      ...history,
    );
    return;
  }

  if (run.memory.pending_verification && run.proposal) {
    $("gate").replaceChildren(
      h(
        "div",
        { class: "gate-card" },
        h("p", { class: "eyebrow" }, "Change applied"),
        h("p", {}, `${run.proposal.action} on ${run.proposal.service} is in. Step once so the agent can check whether the elevated series actually fell.`),
      ),
      ...history,
    );
    return;
  }

  if (run.status === "paused") {
    $("gate").replaceChildren(
      h("div", { class: "gate-card" }, h("p", { class: "eyebrow" }, "Paused"), h("p", {}, "The run is saved. Resume, then step, to continue from this point.")),
      ...history,
    );
    return;
  }

  if (run.status === "awaiting_approval" && run.proposal) {
    const proposal = run.proposal;
    $("gate").replaceChildren(
      h(
        "div",
        { class: "gate-card" },
        h("p", { class: "eyebrow" }, "Waiting for approval"),
        h("p", {}, `${proposal.action} on ${proposal.service}`),
        h("p", { class: "muted" }, proposal.rationale),
        h("p", { class: "muted" }, Object.entries(proposal.args).map(([k, v]) => `${k}=${v}`).join(" · ") || "no extra arguments"),
        proposal.evidence.map((line) => h("p", { class: "muted" }, line)),
        h("label", {}, "Note", h("textarea", { id: "note", rows: "3", placeholder: "Required if you reject." })),
        h(
          "div",
          { class: "actions" },
          h("button", { class: "ok", type: "button", onclick: () => intervene("approve") }, "Approve"),
          h("button", { class: "danger", type: "button", onclick: () => intervene("reject") }, "Reject"),
        ),
      ),
      ...history,
    );
    return;
  }

  if (run.status === "awaiting_input") {
    $("gate").replaceChildren(
      h(
        "div",
        { class: "gate-card" },
        h("p", { class: "eyebrow" }, "Needs a decision"),
        h("p", {}, run.memory.question || "The agent is waiting."),
        h(
          "label",
          {},
          "Hint",
          h("textarea", {
            id: "note",
            rows: "3",
            placeholder: "Name an action: rollback, expand the volume, or restart.",
          }),
        ),
        h("div", { class: "actions" }, h("button", { class: "primary", type: "button", onclick: () => intervene("hint") }, "Send hint")),
      ),
      ...history,
    );
    return;
  }

  $("gate").replaceChildren(
    h("p", { class: "empty" }, "The agent is still investigating. It will stop here before any change."),
    ...history,
  );
}

function render() {
  renderIncidents();
  renderRuns();
  renderBrief();
  renderTimeline();
  renderMeta();
  renderPlan();
  renderHypotheses();
  renderGate();
}

$("step").addEventListener("click", step);
$("run-gate").addEventListener("click", runUntilGate);
$("pause").addEventListener("click", pause);
$("resume").addEventListener("click", resume);

api("/api/incidents").then((incidents) => {
  state.incidents = incidents;
  return refreshRuns();
}).then(() => {
  if (state.runs[0]) return selectRun(state.runs[0].id);
  render();
}).catch((error) => {
  state.error = error.message;
  render();
});
