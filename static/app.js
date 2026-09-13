const form = document.getElementById("start-form");
const logEl = document.getElementById("log");
const meta = document.getElementById("meta");
const gate = document.getElementById("gate");
const shot = document.getElementById("shot");
const startBtn = document.getElementById("start-btn");

let currentTaskId = null;
let source = null;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  startBtn.disabled = true;
  logEl.innerHTML = "";
  gate.classList.add("hidden");
  gate.innerHTML = "";
  try {
    const body = {
      goal: document.getElementById("goal").value.trim(),
      start_url: document.getElementById("start-url").value.trim() || null,
      profile: {
        name: document.getElementById("name").value.trim(),
        email: document.getElementById("email").value.trim(),
        phone: document.getElementById("phone").value.trim(),
      },
    };
    const res = await fetch("/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not start task");
    currentTaskId = data.id;
    meta.textContent = `Task ${data.id} · ${data.status}`;
    listen(data.id);
  } catch (err) {
    addLine("fail", String(err.message || err));
  } finally {
    startBtn.disabled = false;
  }
});

function listen(taskId) {
  if (source) source.close();
  source = new EventSource(`/tasks/${taskId}/events`);
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    handleEvent(payload);
  };
  source.onerror = () => {
    meta.textContent = `${meta.textContent} · stream ended`;
  };
}

function handleEvent(event) {
  if (event.status) {
    meta.textContent = `Task ${event.task_id || currentTaskId} · ${event.status}`;
  }
  if (event.type === "screenshot" && currentTaskId) {
    shot.classList.remove("hidden");
    shot.src = `/tasks/${currentTaskId}/screenshot?t=${Date.now()}`;
    return;
  }
  if (event.type === "pause") {
    addLine("pause", formatEvent(event));
    renderGate(event);
    return;
  }
  if (event.type === "completed") {
    addLine("ok", formatEvent(event));
    gate.classList.add("hidden");
    if (source) source.close();
    return;
  }
  if (event.type === "failed") {
    addLine("fail", formatEvent(event));
    gate.classList.add("hidden");
    if (source) source.close();
    return;
  }
  addLine("", formatEvent(event));
}

function formatEvent(event) {
  if (event.type === "tool") {
    return `Step ${event.step}: ${event.tool} ${JSON.stringify(event.args || {})}`;
  }
  if (event.type === "observation") {
    const err = event.error ? `\nError: ${event.error}` : "";
    return `Observed ${event.title || ""} ${event.url || ""}\n${event.refs_text || ""}${err}`;
  }
  if (event.type === "pause") {
    const p = event.payload || {};
    if (event.status === "needs_confirmation") return `Needs confirmation: ${p.summary || p.action || ""}`;
    if (event.status === "needs_human") return `Needs you: ${p.reason || ""}`;
    return `Needs info: ${p.question || ""}`;
  }
  if (event.type === "completed") {
    return `Done. ${event.summary}\nEvidence: ${event.evidence}`;
  }
  if (event.type === "failed") return `Failed: ${event.reason}`;
  if (event.type === "status") return `Status: ${event.status}${event.backend ? " · browser " + event.backend : ""}`;
  return JSON.stringify(event);
}

function addLine(kind, text) {
  const li = document.createElement("li");
  if (kind) li.className = kind;
  li.textContent = text;
  logEl.prepend(li);
}

function renderGate(event) {
  const payload = event.payload || {};
  gate.classList.remove("hidden");
  gate.innerHTML = "";
  const title = document.createElement("strong");
  gate.appendChild(title);

  if (event.status === "needs_info") {
    title.textContent = payload.question || "The agent needs information.";
    const fields = payload.fields && payload.fields.length ? payload.fields : ["answer"];
    fields.forEach((field) => {
      const label = document.createElement("label");
      label.textContent = field;
      const input = document.createElement("input");
      input.dataset.field = field;
      label.appendChild(input);
      gate.appendChild(label);
    });
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Send answers";
    btn.onclick = () => {
      const answers = {};
      gate.querySelectorAll("input[data-field]").forEach((input) => {
        answers[input.dataset.field] = input.value;
      });
      resume({ answers });
    };
    gate.appendChild(btn);
    return;
  }

  if (event.status === "needs_confirmation") {
    title.textContent = payload.summary || "Confirm this consequential action?";
    const row = document.createElement("div");
    row.className = "row";
    const yes = document.createElement("button");
    yes.type = "button";
    yes.textContent = "Confirm";
    yes.onclick = () => resume({ confirmed: true });
    const no = document.createElement("button");
    no.type = "button";
    no.className = "secondary";
    no.textContent = "Decline";
    no.onclick = () => resume({ confirmed: false });
    row.appendChild(yes);
    row.appendChild(no);
    gate.appendChild(row);
    return;
  }

  title.textContent = payload.reason || "Handle the page, then continue.";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "I finished — continue";
  btn.onclick = () => resume({ human_done: true });
  gate.appendChild(btn);
}

async function resume(payload) {
  if (!currentTaskId) return;
  const res = await fetch(`/tasks/${currentTaskId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    addLine("fail", data.detail || "Resume failed");
    return;
  }
  gate.classList.add("hidden");
  gate.innerHTML = "";
}
