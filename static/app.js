const form = document.getElementById("start-form");
const logEl = document.getElementById("log");
const meta = document.getElementById("meta");
const gate = document.getElementById("gate");
const shot = document.getElementById("shot");
const startBtn = document.getElementById("start-btn");
const scheduleField = document.getElementById("schedule-field");
const runAtInput = document.getElementById("run-at");
const recurrenceInput = document.getElementById("recurrence");
const scheduledList = document.getElementById("scheduled-list");
const scheduledEmpty = document.getElementById("scheduled-empty");

let currentTaskId = null;
let source = null;

document.querySelectorAll('input[name="run-mode"]').forEach((input) => {
  input.addEventListener("change", () => {
    const scheduling = input.checked && input.value === "schedule";
    if (scheduling) {
      scheduleField.classList.remove("hidden");
      startBtn.textContent = "Schedule task";
      runAtInput.required = true;
    } else if (input.checked) {
      scheduleField.classList.add("hidden");
      startBtn.textContent = "Start task";
      runAtInput.required = false;
    }
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  startBtn.disabled = true;
  logEl.innerHTML = "";
  gate.classList.add("hidden");
  gate.innerHTML = "";
  try {
    const mode = document.querySelector('input[name="run-mode"]:checked').value;
    const body = {
      goal: document.getElementById("goal").value.trim(),
      start_url: document.getElementById("start-url").value.trim() || null,
      profile: {
        name: document.getElementById("name").value.trim(),
        email: document.getElementById("email").value.trim(),
        phone: document.getElementById("phone").value.trim(),
      },
    };
    if (mode === "schedule") {
      if (!runAtInput.value) {
        throw new Error("Choose a date and time for the scheduled task.");
      }
      const runAt = new Date(runAtInput.value);
      if (Number.isNaN(runAt.getTime()) || runAt <= new Date()) {
        throw new Error("Choose a future date and time.");
      }
      body.run_at = runAt.toISOString();
      body.recurrence = recurrenceInput.value;
    }
    const res = await fetch(mode === "schedule" ? "/scheduled-tasks" : "/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not process task");
    if (mode === "schedule") {
      meta.textContent = `Scheduled for ${formatDate(data.run_at)} · ${data.goal}`;
      form.reset();
      document.querySelector('input[name="run-mode"][value="now"]').checked = true;
      scheduleField.classList.add("hidden");
      runAtInput.required = false;
      recurrenceInput.value = "none";
      startBtn.textContent = "Start task";
      await loadScheduledTasks();
      return;
    }
    currentTaskId = data.id;
    meta.textContent = `Task ${data.id} · ${data.status}`;
    listen(data.id);
  } catch (err) {
    addLine("fail", String(err.message || err));
  } finally {
    startBtn.disabled = false;
  }
});

async function loadScheduledTasks() {
  const res = await fetch("/scheduled-tasks");
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Could not load scheduled tasks");
  const scheduledTasks = data.filter((task) => task.status !== "cancelled");
  const activeTask = scheduledTasks.find(
    (task) => task.task_id && ["running", "needs_info", "needs_confirmation", "needs_human"].includes(task.status),
  ) || scheduledTasks.find((task) => task.task_id);
  if (activeTask && activeTask.task_id !== currentTaskId) {
    currentTaskId = activeTask.task_id;
    meta.textContent = `Task ${activeTask.task_id} · ${activeTask.status}`;
    listen(activeTask.task_id);
  }
  scheduledList.innerHTML = "";
  scheduledEmpty.classList.toggle("hidden", scheduledTasks.length > 0);
  scheduledTasks.forEach((task) => {
    const item = document.createElement("li");
    item.className = "scheduled-item";
    const details = document.createElement("div");
    const goal = document.createElement("strong");
    goal.textContent = task.goal;
    const time = document.createElement("span");
    time.className = "scheduled-time";
    const nextRun = task.next_run_at || task.run_at;
    const recurrence = formatRecurrence(task.recurrence);
    time.textContent = `${recurrence} · next ${formatDate(nextRun)} · ${task.status}`;
    details.appendChild(goal);
    details.appendChild(time);
    if (task.error) {
      const error = document.createElement("span");
      error.className = "scheduled-error";
      error.textContent = `Latest error: ${task.error}`;
      details.appendChild(error);
    }
    item.appendChild(details);
    if (task.status === "scheduled") {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "secondary";
      cancel.textContent = "Cancel";
      cancel.onclick = () => cancelScheduledTask(task.id);
      item.appendChild(cancel);
    }
    scheduledList.appendChild(item);
  });
}

async function cancelScheduledTask(taskId) {
  try {
    const res = await fetch(`/scheduled-tasks/${taskId}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not cancel task");
    meta.textContent = `Cancelled scheduled task · ${data.goal}`;
    await loadScheduledTasks();
  } catch (err) {
    addLine("fail", String(err.message || err));
  }
}

function formatDate(value) {
  return new Date(value).toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function formatRecurrence(value) {
  const labels = {
    none: "Once",
    daily: "Daily",
    weekly: "Weekly",
    monthly: "Monthly",
  };
  return labels[value] || "Once";
}

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
    loadScheduledTasks().catch((err) => {
      addLine("fail", String(err.message || err));
    });
    return;
  }
  if (event.type === "completed") {
    addLine("ok", formatEvent(event));
    gate.classList.add("hidden");
    if (source) source.close();
    loadScheduledTasks().catch((err) => {
      addLine("fail", String(err.message || err));
    });
    return;
  }
  if (event.type === "failed") {
    addLine("fail", formatEvent(event));
    gate.classList.add("hidden");
    if (source) source.close();
    loadScheduledTasks().catch((err) => {
      addLine("fail", String(err.message || err));
    });
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

loadScheduledTasks().catch((err) => {
  addLine("fail", String(err.message || err));
});

setInterval(() => {
  loadScheduledTasks().catch((err) => {
    addLine("fail", String(err.message || err));
  });
}, 1000);
