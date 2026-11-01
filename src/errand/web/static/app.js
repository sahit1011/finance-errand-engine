"use strict";

// All text reaches the page through textContent, never as markup, so hostile review or chat text cannot run.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}

const byId = (id) => document.getElementById(id);

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return response.json();
}

async function postJson(path, body) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const payload = await response.json();
  if (!response.ok) throw Object.assign(new Error(payload.error), { status: response.status });
  return payload;
}

function seconds(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms < 10 ? ms.toFixed(1) : Math.round(ms)} ms`;
}

function shorten(text, limit) {
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

// The workflow view, shared by live chat and replay.

const LANES = ["Chat", "State", "Model", "Tools", "Approval", "Engine"];
const STEP_GAP = 2;
const CHARACTER_PX = 6.7;
const STEP_PADDING_PX = 18;
const STATE_CLASS = { AWAITING_APPROVAL: "state-gate", DONE: "state-done", FAILED: "state-failed", ABANDONED: "state-stopped" };
const STATE_MEANING = {
  COLLECTING: "Finding out what is still missing. Known facts are never asked again.",
  SEARCHING: "Asking the provider for options. Budget and time are filtered in code.",
  PRESENTING: "Showing options and waiting for the user to choose.",
  HOLDING: "Holding the chosen slot to get the real quoted price, before asking for approval.",
  AWAITING_APPROVAL: "Waiting for the user to confirm these exact terms. Only a bare yes matched by code can approve them.",
  EXECUTING: "Sending the approved write with its saved idempotency key.",
  VERIFYING: "Checking what really happened. Nothing is reported as done until this answers.",
  UNKNOWN_NEEDS_CHECK: "The outcome is not known yet, so nothing is reported and nothing new starts.",
  DONE: "Finished and verified.",
  FAILED: "Ended, and the user was told what happened.",
  ABANDONED: "Stopped by the user. Nothing further will happen.",
};

function textWidth(label) {
  return Math.ceil(label.length * CHARACTER_PX) + STEP_PADDING_PX;
}

// Steps range from 1 ms to several seconds, so a log scale keeps the short ones visible and clickable.
function durationWidth(ms) {
  return Math.round(44 + 34 * Math.log10(Math.max(ms, 1)));
}

function toolStep(event) {
  const failed = "error" in event;
  const label = `${failed ? "✗" : "✓"} ${event.tool} ${seconds(event.latency_ms)}`;
  const kind = failed ? "bad" : "tool";
  if (event.tool === "get_booking_status" && event.result) {
    return { lane: "Tools", label: `✓ status: ${event.result.status}`, kind: "tool", ms: event.latency_ms, title: "Status check", meaning: "The provider's record is the source of truth. Only this can confirm a booking." };
  }
  const meanings = {
    timeout: "The provider did not answer in time. The write may or may not have happened, so the engine checks status before doing anything else.",
    slot_taken: "The provider gave the slot away. The approval is voided, and a new slot needs a new approval.",
    slot_conflict: "The new slot conflicts. The original booking stays, and a new slot needs a new approval.",
    hold_expired: "The hold ran out. The engine holds again, and if the price changed, it asks again.",
    slot_unavailable: "The slot is gone, so the engine searches again.",
    503: "A temporary failure. The engine retries with the same key, so it cannot act twice.",
  };
  const meaning = failed ? (meanings[event.error] ?? "This call failed.") : `${event.tool} answered.`;
  return { lane: "Tools", label, kind, ms: event.latency_ms, title: event.tool, meaning };
}

function stepFor(event) {
  switch (event.type) {
    case "user_message":
      return { lane: "Chat", label: `you: ${shorten(event.text, 22)}`, kind: "chat-user", title: "Your message", meaning: "Code checks the raw text first. A bare yes, no or stop never reaches a model." };
    case "reply":
      return { lane: "Chat", label: "reply", kind: "chat-reply", title: "Reply", meaning: "All reply text comes from code templates, so a model cannot misstate a price or claim success." };
    case "model_call_started":
      return { lane: "Model", label: "reading the message", kind: "model pending", title: "Model working", meaning: "The local model is reading the message. It can only return structured data." };
    case "model_call":
      return { lane: "Model", label: `${String(event.model).replace("llama.cpp", "model")} ${seconds(event.latency_ms)}`, kind: "model", ms: event.latency_ms, title: "Model returned", meaning: "The model returned structured data only. The engine checks all of it before any of it applies." };
    case "model_output_rejected":
      return { lane: "Model", label: "✗ output rejected", kind: "bad", title: "Model output rejected", meaning: "The output broke the rules, so none of it applied." };
    case "tool_call":
      return toolStep(event);
    case "approval": {
      const good = event.status === "approved" || event.status === "consumed";
      const meaning = {
        pending: "A record of the exact action, provider, slot and price, waiting for the user.",
        approved: "The user's own yes approved this record, and the key was saved in the same transaction.",
        consumed: "The approved write is verified, so this record is used up.",
        voided: "The terms changed or the user declined, so this record can never approve anything.",
      }[event.status];
      return { lane: "Approval", label: `${good ? "✓ " : event.status === "voided" ? "✗ " : ""}${event.status}`, kind: event.status === "voided" ? "bad" : "gate", title: `Approval ${event.status}`, meaning };
    }
    case "verified":
      return { lane: "Approval", label: "✓ verified", kind: "good", title: "Verified", meaning: "The provider confirmed it. Only now may the user be told it is done." };
    case "approval_violation":
      return { lane: "Approval", label: "✗ write blocked", kind: "bad", title: "Write blocked", meaning: "The write did not match its approval record, so the gateway stopped it before it reached the provider." };
    case "stale_confirmation":
      return { lane: "Approval", label: "✗ stale yes", kind: "bad", title: "Stale yes", meaning: "This yes was sent before the user saw these terms, so it does not count." };
    case "cap_reached":
      return { lane: "Engine", label: `✗ cap: ${event.kind}`, kind: "bad", title: "Cap reached", meaning: "A hard limit stopped the work, and the user gets a clear message." };
    case "error":
      return { lane: "Engine", label: "✗ error", kind: "bad", title: "Error", meaning: "An unexpected error. The user is told what is known." };
    case "retry":
      return { lane: "Engine", label: `retry ${event.retries}`, kind: "engine", title: "Retry", meaning: "Trying again with the same idempotency key, so it cannot act twice." };
    case "memory":
      return { lane: "Engine", label: `memory: ${event.key}`, kind: "engine", title: "Memory changed", meaning: "A preference changed, and the user is told in the same reply." };
    case "memory_used":
      return { lane: "Engine", label: `used: ${event.key}`, kind: "engine", title: "Memory used", meaning: "A stated preference filled in a detail, and the user is told." };
    case "run_started":
      return { lane: "Engine", label: event.run_id, kind: "engine", title: "New errand", meaning: `A new ${event.workflow} errand started.` };
    case "resumed":
      return { lane: "Engine", label: "resumed", kind: "engine", title: "Resumed", meaning: "The app restarted and carried on from what was saved." };
    case "transition":
      return { lane: "State", label: event.to_state, kind: `state ${STATE_CLASS[event.to_state] ?? ""}`, title: `${event.from_state} to ${event.to_state}`, meaning: `${STATE_MEANING[event.to_state]} Reason: ${event.reason}.` };
    default:
      return { lane: "Engine", label: event.type, kind: "engine", title: event.type, meaning: "" };
  }
}

function layout(events) {
  const finished = new Set();
  const unanswered = new Set();
  // A started model call is drawn only until its result arrives, and the result takes its place.
  // If the turn ended with no result, the call failed, so it must stop looking as if it is still working.
  let answer = null;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const type = events[i].type;
    if (type === "model_call" || type === "reply" || type === "error") answer = type;
    else if (type === "model_call_started" && answer !== null) {
      (answer === "model_call" ? finished : unanswered).add(events[i].seq);
      answer = null;
    }
  }
  const steps = [];
  let x = 0;
  for (const event of events) {
    if (finished.has(event.seq)) continue;
    const step = { ...stepFor(event), event };
    if (unanswered.has(event.seq)) Object.assign(step, { label: "✗ no answer from the model", kind: "bad", title: "Model did not answer", meaning: "The turn ended with no answer from the model, so nothing it might have said was applied." });
    // A run begins in COLLECTING with no transition to mark it, so its start draws that state too.
    if (event.type === "run_started") steps.push({ lane: "State", label: "COLLECTING", kind: "state", title: "New errand", meaning: STATE_MEANING.COLLECTING, event, x, width: 0 });
    const width = Math.max(textWidth(step.label), step.ms === undefined ? 0 : durationWidth(step.ms));
    Object.assign(step, { x, width });
    x += width + STEP_GAP;
    steps.push(step);
  }
  // Each state's bar runs on until the next state begins, so a glance along the row shows how far the errand got.
  const stateSteps = steps.filter((step) => step.lane === "State");
  stateSteps.forEach((step, index) => {
    const next = stateSteps[index + 1];
    step.width = (next ? next.x : x) - step.x - STEP_GAP;
  });
  return { steps, width: x };
}

function summarise(events) {
  const sum = (type) => events.filter((e) => e.type === type).reduce((total, e) => total + e.latency_ms, 0);
  const modelCalls = events.filter((e) => e.type === "model_call").length;
  const toolCalls = events.filter((e) => e.type === "tool_call").length;
  return `${modelCalls} model ${modelCalls === 1 ? "call" : "calls"} in ${seconds(sum("model_call"))} · ${toolCalls} tool ${toolCalls === 1 ? "call" : "calls"} in ${seconds(sum("tool_call"))}`;
}

function currentActivity(events, busy) {
  const last = events[events.length - 1];
  const reversed = [...events].reverse();
  const transition = reversed.find((e) => e.type === "transition");
  if (last && last.type === "model_call_started") return { text: "The local model is reading your message", working: true };
  if (busy) return { text: "The engine is working", working: true };
  if (!transition) return { text: events.length ? "Waiting for you" : "Waiting for your first message", working: false };
  const friendly = {
    AWAITING_APPROVAL: "Waiting for your approval",
    PRESENTING: "Waiting for you to choose",
    COLLECTING: "Waiting for your answer",
    UNKNOWN_NEEDS_CHECK: "Outcome not known yet, will check on your next message",
    DONE: "Done and verified",
    FAILED: "Ended, nothing further will happen",
    ABANDONED: "Stopped",
  };
  return { text: friendly[transition.to_state] ?? transition.to_state, working: false };
}

class Workflow {
  constructor(root) {
    this.root = root;
    this.events = [];
    this.busy = false;
    this.view = "timeline";
    this.selected = null;
    this.head = el("div", { class: "flow-head" });
    this.body = el("div");
    this.detail = el("div", { class: "step-detail", hidden: true });
    root.append(this.head, this.body, this.detail);
    this.scroller = null;
    this.render();
  }

  set(events) {
    this.events = events;
    // Whatever step was reached is the one to see, so a fresh set of steps opens at its end.
    this.scroller = null;
    this.render();
  }

  add(event) {
    this.events.push(event);
    this.render();
  }

  setBusy(busy) {
    this.busy = busy;
    this.renderHead();
  }

  render() {
    this.renderHead();
    const previous = this.scroller;
    const followEnd = !previous || previous.scrollLeft + previous.clientWidth >= previous.scrollWidth - 40;
    const left = previous ? previous.scrollLeft : 0;
    this.body.replaceChildren(this.view === "timeline" ? this.timeline() : this.list());
    if (this.scroller) this.scroller.scrollLeft = followEnd ? this.scroller.scrollWidth : left;
    this.renderDetail();
  }

  renderHead() {
    const activity = currentActivity(this.events, this.busy);
    const button = (view, label) => el("button", { type: "button", "aria-pressed": String(this.view === view), onclick: () => { this.view = view; this.render(); } }, label);
    this.head.replaceChildren(
      el("span", { class: `now${activity.working ? " working" : ""}`, role: "status" }, activity.text),
      el("span", { class: "stats" }, summarise(this.events)),
      el("span", { class: "flow-actions" }, button("timeline", "Timeline"), button("list", "List")),
    );
  }

  timeline() {
    if (!this.events.length) {
      this.scroller = null;
      return el("p", { class: "empty" }, "Each step the engine takes appears here as it happens.");
    }
    const { steps, width } = layout(this.events);
    const lanes = LANES.map((lane) => el("div", { class: "lane" }, ...steps.filter((step) => step.lane === lane).map((step) => this.stepButton(step))));
    this.scroller = el("div", { class: "flow-scroll" }, el("div", { class: "flow-lanes", style: { width: `${width}px` } }, ...lanes));
    return el("div", {},
      this.overview(steps, width),
      el("div", { class: "flow" }, el("div", { class: "flow-labels" }, ...LANES.map((lane) => el("div", {}, lane))), this.scroller),
      el("p", { class: "flow-note" }, "Steps run left to right, and time waiting for you is left out. Widths use a log scale so that 1 ms steps stay visible, so read the times on the bars. Click any step."),
    );
  }

  // The whole run at a glance, because a long run is far wider than the screen. Clicking it jumps along the timeline.
  overview(steps, width) {
    const scrollTo = (click) => {
      const box = click.currentTarget.getBoundingClientRect();
      this.scroller.scrollLeft = ((click.clientX - box.left) / box.width) * width - this.scroller.clientWidth / 2;
    };
    const parts = steps.filter((step) => step.lane !== "State").map((step) => el("span", { class: `part ${step.kind}`, style: { flexGrow: String(step.width) } }));
    return el("div", { class: "overview", title: "The whole run. Click to jump.", "aria-hidden": "true", onclick: scrollTo }, ...parts);
  }

  stepButton(step) {
    const selected = this.selected === step.event.seq;
    return el("button", {
      type: "button",
      class: `step ${step.kind}${selected ? " selected" : ""}`,
      style: { left: `${step.x}px`, width: `${step.width}px` },
      title: `${step.title}: ${step.label}`,
      onclick: () => { this.selected = step.event.seq; this.render(); },
    }, step.label);
  }

  list() {
    this.scroller = null;
    if (!this.events.length) return el("p", { class: "empty" }, "Each step the engine takes appears here as it happens.");
    const { steps } = layout(this.events);
    const rows = steps.map((step) => el("tr", {},
      el("td", { class: "num" }, String(step.event.seq)),
      el("td", {}, step.lane),
      el("td", {}, el("button", { type: "button", onclick: () => { this.selected = step.event.seq; this.render(); } }, step.label)),
      el("td", { class: "num" }, step.ms === undefined ? "" : seconds(step.ms)),
    ));
    return el("div", { class: "table-wrap" }, el("table", { class: "step-list" },
      el("thead", {}, el("tr", {}, el("th", { class: "num" }, "#"), el("th", {}, "Where"), el("th", {}, "Step"), el("th", { class: "num" }, "Time"))),
      el("tbody", {}, ...rows)));
  }

  renderDetail() {
    const event = this.events.find((e) => e.seq === this.selected);
    if (!event) {
      this.detail.hidden = true;
      return;
    }
    const step = stepFor(event);
    this.detail.hidden = false;
    this.detail.replaceChildren(
      el("h3", {}, step.title),
      step.meaning ? el("p", {}, step.meaning) : null,
      el("pre", {}, JSON.stringify(event, null, 2)),
    );
  }
}

function addMessage(container, role, text, isProposal = false) {
  const node = el("div", { class: `message ${role}${isProposal ? " proposal" : ""}` }, text);
  if (isProposal) node.append(el("span", { class: "hint" }, "Type yes to approve. Code checks this, and a model cannot approve it."));
  container.append(node);
  container.scrollTop = container.scrollHeight;
}

// Live chat, only when the local server is there.

const BUSY_POLL_MS = 500;
const chat = {
  workflow: null,
  stream: null,
  position: 0,
  // The reply the user has actually seen, which is what makes a yes count. It only moves when a reply is drawn.
  lastReplyId: null,
  session: null,
};

function showState(state) {
  chat.session = state.session;
  const messages = byId("messages");
  messages.replaceChildren();
  const presets = { normal: "normal", timeout_after_success: "next booking times out, but books", timeout_not_booked: "next booking times out, does not book", slot_taken: "next booking: slot gets taken" };
  byId("session-name").textContent = `Session ${state.session}${state.preset === "normal" ? "" : ` · ${presets[state.preset]}`}`;
  state.messages.forEach((message, index) => {
    const last = index === state.messages.length - 1;
    addMessage(messages, message.role, message.text, last && state.run_state === "AWAITING_APPROVAL");
  });
  if (!state.messages.length) addMessage(messages, "note", "Try: Find a plumber for my kitchen tap next week, mornings only, under ₹1,500");
  chat.lastReplyId = state.last_reply_id;
}

function followSteps(session) {
  if (chat.stream) chat.stream.close();
  chat.position = 0;
  chat.workflow.set([]);
  chat.stream = new EventSource(`api/events?session=${encodeURIComponent(session)}&after=0`);
  chat.stream.onmessage = (message) => {
    const event = JSON.parse(message.data);
    // A reconnect replays from the start, so only steps not yet drawn are added.
    if (event.seq > chat.position) {
      chat.position = event.seq;
      chat.workflow.add(event);
    }
  };
}

function setBusy(busy) {
  byId("chat-input").disabled = busy;
  byId("send").disabled = busy;
  byId("new-session").disabled = busy;
  chat.workflow.setBusy(busy);
  if (!busy) byId("chat-input").focus();
}

async function sendMessage(submitEvent) {
  submitEvent.preventDefault();
  const input = byId("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  const messages = byId("messages");
  messages.querySelector(".note")?.remove();
  addMessage(messages, "user", text);
  setBusy(true);
  try {
    showState(await postJson("api/message", { session: chat.session, text, last_reply_id: chat.lastReplyId }));
  } catch (error) {
    if (error.status === 409) {
      // Another tab moved on, so this page shows the session as it is now, and the message waits to be sent again.
      const state = await getJson("api/state");
      showState(state);
      followSteps(state.session);
      addMessage(messages, "note", `${error.message}. Here is that session.`);
    } else {
      addMessage(messages, "note", `The app could not confirm that arrived: ${error.message}. Check the steps before sending it again.`);
    }
  } finally {
    setBusy(false);
  }
}

async function startSession() {
  setBusy(true);
  try {
    const state = await postJson("api/session", { preset: byId("preset").value });
    showState(state);
    followSteps(state.session);
  } catch (error) {
    addMessage(byId("messages"), "note", `Could not start a session: ${error.message}.`);
  } finally {
    setBusy(false);
  }
}

async function showModelStatus() {
  const status = await getJson("api/status");
  const pill = byId("model-status");
  pill.className = `pill ${status.model_ok ? "ok" : "down"}`;
  pill.replaceChildren(el("span", { class: "mark" }, status.model_ok ? "✓" : "✗"), status.model_ok ? "Local model ready" : "Local model not running");
  const help = byId("model-help");
  help.hidden = status.model_ok;
  if (!status.model_ok) {
    help.replaceChildren("Replies like yes, no or an option number still work, but anything else needs the model. Start it in another terminal, then reload: ", el("code", {}, status.start_command));
  }
}

// A page opened during a turn cannot send anything until that turn ends, and then shows its result.
async function waitForTurn(state) {
  if (!state.busy) return;
  setBusy(true);
  let latest = state;
  while (latest.busy) {
    await new Promise((resolve) => setTimeout(resolve, BUSY_POLL_MS));
    latest = await getJson("api/state");
  }
  showState(latest);
  setBusy(false);
}

async function startChat() {
  chat.workflow = new Workflow(byId("live-workflow"));
  byId("chat-form").addEventListener("submit", sendMessage);
  byId("new-session").addEventListener("click", startSession);
  showModelStatus();
  const state = await getJson("api/state");
  showState(state);
  followSteps(state.session);
  await waitForTurn(state);
}

// Results.

const NAMES = { "Cheap LLM only": "4B alone", "Strong LLM only": "9B alone", "Cheap LLM routes itself": "4B routes itself" };
const RECOMMENDED = "Strong LLM only";

function pipelineName(name) {
  if (name.startsWith("Perfect router")) return "Perfect router (upper limit)";
  if (name.startsWith("EmbeddingGemma")) return "Embedding router";
  return NAMES[name] ?? name;
}

function tile(label, value, sub) {
  return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value), el("div", { class: "sub" }, sub));
}

function table(headers, rows, numeric = []) {
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, ...headers.map((header, i) => el("th", { class: numeric.includes(i) ? "num" : "" }, header)))),
    el("tbody", {}, ...rows.map((row) => el("tr", {}, ...row.map((cell, i) => el("td", { class: numeric.includes(i) ? "num" : "" }, String(cell))))))));
}

function asTable(headers, rows, numeric) {
  return el("details", {}, el("summary", {}, "Show as a table"), table(headers, rows, numeric));
}

function panel(title, intro, ...content) {
  return el("section", { class: "panel" }, el("h2", {}, title), intro ? el("p", { class: "sub-head" }, intro) : null, ...content);
}

function evalsPanel(evals) {
  const detail = el("div", { class: "cell-detail" });
  const select = (row, cell) => {
    grid.querySelectorAll(".selected").forEach((node) => node.classList.remove("selected"));
    cell.classList.add("selected");
    detail.replaceChildren(
      el("strong", {}, `${row.number}. ${row.name}`),
      el("dl", {},
        el("dt", {}, "Expected"), el("dd", {}, row.expected),
        el("dt", {}, "Measured"), el("dd", {}, row.actual),
        el("dt", {}, "Result"), el("dd", {}, row.result === "PASS" ? "✓ Pass" : "✗ Fail"),
        row.notes ? el("dt", {}, "Notes") : null, row.notes ? el("dd", {}, row.notes) : null),
    );
  };
  const grid = el("div", { class: "grid" });
  evals.forEach((row, index) => {
    const pass = row.result === "PASS";
    const cell = el("button", { type: "button", class: `cell${pass ? "" : " fail"}`, title: `${row.number}. ${row.name}: ${pass ? "pass" : "fail"}`, "aria-label": `Scenario ${row.number}, ${row.name}, ${pass ? "passed" : "failed"}`, onclick: () => select(row, cell) },
      el("span", { class: "mark" }, pass ? "✓" : "✗"), String(row.number));
    grid.append(cell);
    if (index === 0) select(row, cell);
  });
  return panel("Eval scenarios", "Generated by the test run, with no live model. The measured column is read from the provider's ledger, the store and the trace, not from what the agent says. Click a scenario.",
    grid, detail,
    asTable(["#", "Scenario", "Expected", "Measured", "Result"], evals.map((row) => [row.number, row.name, row.expected, row.actual, row.result]), [0]));
}

function rankedBars(title, rows, value, format, higherIsBetter) {
  const ordered = [...rows].sort((a, b) => (higherIsBetter ? value(b) - value(a) : value(a) - value(b)));
  const most = Math.max(...rows.map(value));
  return el("div", { class: "chart" }, el("h3", {}, title), el("div", { class: "bars" }, ...ordered.map((row) =>
    el("div", { class: `bar-row${row.name === RECOMMENDED ? " picked" : ""}`, title: `${pipelineName(row.name)}: ${format(row)}` },
      el("span", { class: "name" }, pipelineName(row.name)),
      // The tip's text sits beside the bar, so bars stop short of the full width to leave it room.
      el("span", { class: "track" }, el("span", { class: "fill", style: { width: `${(value(row) / most) * 62}%` } }), el("span", { class: "tip" }, format(row)))))));
}

function routingPanel(routing) {
  const rows = routing.test;
  const total = rows[0].total;
  return panel("Model routing, on held-out turns", `A cheap model for easy messages and a strong one for hard ones, measured on ${total} hand-written turns kept out of choosing the router's examples and threshold. We saw earlier results on them while building, so a few turns either way is noise. The router tied 9B alone on correct answers with less compute, but it needs three model servers instead of one, so we recommend 9B alone, shown in blue.`,
    el("div", { class: "multiples" },
      rankedBars(`Correct, of ${total}`, rows, (r) => r.correct, (r) => String(r.correct), true),
      rankedBars("Median seconds per message", rows, (r) => r.p50, (r) => r.p50.toFixed(2), false),
      rankedBars("Total compute, seconds", rows, (r) => r.compute, (r) => r.compute.toFixed(0), false)),
    asTable(["Pipeline", "Correct", "Wrong values", "p50 s", "p95 s", "Compute s", "Tokens", "None", "Cheap", "Strong"],
      rows.map((r) => [pipelineName(r.name), `${r.correct}/${r.total}`, r.wrong, r.p50.toFixed(2), r.p95.toFixed(2), r.compute.toFixed(1), r.tokens.toLocaleString(), r.routes.none, r.routes.cheap, r.routes.strong]), [1, 2, 3, 4, 5, 6, 7, 8, 9]));
}

function kindsPanel(routing) {
  const row = (kind) => {
    const differs = kind.cheap !== kind.strong;
    const line = (key, value) => el("span", { class: "line" }, el("span", { class: `fill ${key}`, style: { width: `${(value / kind.turns) * 70}%` } }), differs ? el("span", { class: "tip" }, `${value}/${kind.turns}`) : null);
    return el("div", { class: "bar-row", title: `${kind.kind}: 4B ${kind.cheap} of ${kind.turns}, 9B ${kind.strong} of ${kind.turns}` },
      el("span", { class: "name" }, kind.kind.replaceAll("_", " ")),
      el("span", { class: "pair" }, line("a", kind.cheap), line("b", kind.strong)));
  };
  return panel("Where each model goes wrong, across all turns", `Share of each kind of turn read correctly. Numbers appear only where the models differ. Overall: 9B ${routing.all_turns.strong.correct} of ${routing.all_turns.strong.total}, 4B ${routing.all_turns.cheap.correct}.`,
    el("div", { class: "legend" }, el("span", {}, el("span", { class: "swatch a" }), "4B"), el("span", {}, el("span", { class: "swatch b" }), "9B")),
    el("div", { class: "kinds" }, ...routing.by_kind.map(row)),
    asTable(["Kind of turn", "Turns", "4B correct", "9B correct"], routing.by_kind.map((k) => [k.kind.replaceAll("_", " "), k.turns, k.cheap, k.strong]), [1, 2, 3]));
}

function approvalPanel(routing) {
  const strong = routing.answers.find((a) => a.model === "strong");
  const cheap = routing.answers.find((a) => a.model === "cheap");
  return panel("Why a model never decides approval", "The assignment says approval is a record your code checks, \"not something inferred from model output\". We measured what would happen if a model decided anyway: both approved hijack text. So a model reads stops, memory requests and other answers in any words, because a wrong one spends nothing, and a worded yes to a proposal only earns a request to type yes.",
    el("div", { class: "tiles" },
      tile("9B wrongly approved", `${strong.false_approvals} of ${strong.must_not_approve}`, "replies that must not approve"),
      tile("4B wrongly approved", `${cheap.false_approvals} of ${cheap.must_not_approve}`, "replies that must not approve"),
      tile("9B false stops", `${routing.stops.strong.false} of ${routing.stops.strong.other_turns}`, `found ${routing.stops.strong.found} of ${routing.stops.strong.real} real stops`),
      tile("4B false stops", `${routing.stops.cheap.false} of ${routing.stops.cheap.other_turns}`, `found ${routing.stops.cheap.found} of ${routing.stops.cheap.real} real stops`)),
    asTable(["Model", "Replies it wrongly approved"], routing.answers.map((a) => [a.model === "strong" ? "9B" : "4B", a.wrongly_approved.join("; ")]), []));
}

function memoryPanel(routing) {
  return panel("Memory used by the models", routing.memory.how,
    table(["Server", "Maximum resident MB", "Peak footprint MB"], routing.memory.servers.map((s) => [s.server, s.resident_mb.toLocaleString(), s.footprint_mb.toLocaleString()]), [1, 2]));
}

function bugsPanel(bugs) {
  return panel(`${bugs.length} real bugs found while building`, "Each one really happened, and each is guarded by a regression test.",
    el("div", { class: "bugs" }, ...bugs.map((bug) => el("details", {},
      el("summary", {}, `${bug.number}. ${bug.title}`),
      el("dl", {},
        el("dt", {}, "What broke"), el("dd", {}, bug.what_broke.replaceAll("`", "")),
        el("dt", {}, "Why"), el("dd", {}, bug.why.replaceAll("`", "")),
        el("dt", {}, "Fix"), el("dd", {}, bug.fix.replaceAll("`", "")),
        el("dt", {}, "Guarded by"), el("dd", {}, bug.regression.replaceAll("`", "")))))));
}

async function startResults() {
  const root = byId("results");
  try {
    const { evals, routing, bugs } = await getJson("data/results.json");
    const passed = evals.filter((row) => row.result === "PASS").length;
    const strong = routing.test.find((r) => r.name === RECOMMENDED);
    const router = routing.test.find((r) => r.name.startsWith("EmbeddingGemma"));
    root.replaceChildren(el("div", { class: "results" },
      el("div", { class: "tiles" },
        tile("Eval scenarios passed", `${passed} of ${evals.length}`, "scripted, offline, checked against the ledger"),
        tile("9B correct, held-out turns", `${strong.correct} of ${strong.total}`, `${strong.wrong} with a wrong value`),
        tile("Router correct, held-out turns", `${router.correct} of ${router.total}`, "no better than 9B alone, so routing stays off"),
        tile("Median time per message, 9B", `${strong.p50.toFixed(1)} s`, "on an Apple M5 with 16 GB, fully local"),
        tile("Real bugs found and guarded", String(bugs.length), "each guarded by a regression test")),
      evalsPanel(evals), routingPanel(routing), kindsPanel(routing), approvalPanel(routing), memoryPanel(routing), bugsPanel(bugs)));
  } catch (error) {
    root.replaceChildren(el("p", { class: "notice" }, `Results could not be loaded: ${error.message}. Run uv run pytest and try again.`));
  }
}

// Replay of saved runs.

const replay = { workflow: null, events: [], position: 0, timer: null };
const REPLAY_STEP_MS = 450;

function showReplayPosition() {
  const shown = replay.events.slice(0, replay.position);
  replay.workflow.set(shown);
  byId("replay-position").value = replay.position;
  byId("replay-count").textContent = `Step ${replay.position} of ${replay.events.length}`;
  const messages = byId("replay-messages");
  messages.replaceChildren();
  for (const event of shown) {
    if (event.type === "user_message") addMessage(messages, "user", event.text);
    if (event.type === "reply") addMessage(messages, "agent", event.text, event.state === "AWAITING_APPROVAL");
  }
}

function stopReplay() {
  clearInterval(replay.timer);
  replay.timer = null;
  byId("replay-play").textContent = "Play";
}

function toggleReplay() {
  if (replay.timer) {
    stopReplay();
    return;
  }
  if (replay.position >= replay.events.length) replay.position = 0;
  byId("replay-play").textContent = "Pause";
  replay.timer = setInterval(() => {
    replay.position += 1;
    showReplayPosition();
    if (replay.position >= replay.events.length) stopReplay();
  }, REPLAY_STEP_MS);
}

async function loadReplay(summaries) {
  stopReplay();
  const chosen = summaries.find((r) => r.id === byId("replay-select").value);
  byId("replay-summary").textContent = chosen.summary;
  const { events } = await getJson(`data/replays/${encodeURIComponent(chosen.id)}.json`);
  replay.events = events;
  replay.position = events.length;
  replay.workflow.selected = null;
  byId("replay-position").max = events.length;
  showReplayPosition();
}

async function startReplay() {
  replay.workflow = new Workflow(byId("replay-workflow"));
  const { replays } = await getJson("data/replays.json");
  byId("replay-select").replaceChildren(...replays.map((r) => el("option", { value: r.id }, r.title)));
  byId("replay-select").addEventListener("change", () => loadReplay(replays));
  byId("replay-play").addEventListener("click", toggleReplay);
  byId("replay-position").addEventListener("input", (event) => {
    stopReplay();
    replay.position = Number(event.target.value);
    showReplayPosition();
  });
  await loadReplay(replays);
}

// Pages.

function showPage(live) {
  const fallback = live ? "chat" : "results";
  const wanted = location.hash.slice(1);
  const page = ["results", "replay"].includes(wanted) || (live && wanted === "chat") ? wanted : fallback;
  for (const name of ["chat", "results", "replay"]) {
    byId(`page-${name}`).hidden = name !== page;
    if (name === page) byId(`tab-${name}`).setAttribute("aria-current", "page");
    else byId(`tab-${name}`).removeAttribute("aria-current");
  }
}

async function isLive() {
  const response = await fetch("api/status").catch(() => null);
  return response !== null && response.ok;
}

async function start() {
  const live = await isLive();
  byId("tab-chat").hidden = !live;
  window.addEventListener("hashchange", () => showPage(live));
  showPage(live);
  // Each page loads on its own, so trouble with live chat cannot keep Results or Replay from showing.
  if (live) startChat().catch((error) => addMessage(byId("messages"), "note", `Chat could not start: ${error.message}.`));
  startResults();
  startReplay().catch((error) => { byId("replay-summary").textContent = `Replays could not be loaded: ${error.message}.`; });
}

start();
