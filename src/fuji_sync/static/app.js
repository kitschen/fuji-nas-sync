"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function fmtBytes(n) {
  if (n == null) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");
const fmtDuration = (s) => (s == null ? "" : s < 1 ? "less than 1 s" : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
const fmtCaptured = (iso) => (iso ? iso.replace("T", " ") : "");

async function api(method, url, body) {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem("apiToken");
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (resp.status === 401) {
    const entered = prompt("API token required:");
    if (entered) { localStorage.setItem("apiToken", entered); return api(method, url, body); }
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(typeof data.detail === "string" ? data.detail : resp.statusText);
  return data;
}

// Tabs and links: #<tab> opens a tab, #help/<section> a section of the help
const TAB_LOADERS = { cameras: loadCameras, nas: loadNas, history: loadHistory, status: loadNasHealth, settings: loadSettings };

async function route() {
  const [requested, section] = location.hash.slice(1).split("/");
  const tab = requested && $(`#tab-${requested}`) ? requested : "status";
  document.querySelectorAll("button.tab, .panel").forEach((el) => el.classList.remove("active"));
  document.querySelector(`button.tab[data-tab="${tab}"]`).classList.add("active");
  $(`#tab-${tab}`).classList.add("active");
  await TAB_LOADERS[tab]?.();
  const target = section && document.getElementById(`help-${section}`);
  if (target) target.scrollIntoView({ block: "start" });
  else window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
document.querySelectorAll("button.tab").forEach((btn) => btn.addEventListener("click", () => {
  if (location.hash === `#${btn.dataset.tab}`) route();
  else location.hash = btn.dataset.tab;
}));

// Help pop-overs next to form fields
function togglePop(trigger) {
  const pop = document.getElementById(trigger.dataset.pop);
  if (pop.matches(":popover-open")) { pop.hidePopover(); return; }
  pop.showPopover();
  const r = trigger.getBoundingClientRect();
  pop.style.top = `${Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 8)}px`;
  pop.style.left = `${Math.max(8, Math.min(r.left - 12, innerWidth - pop.offsetWidth - 8))}px`;
}
document.addEventListener("click", (ev) => {
  const trigger = ev.target.closest(".info[data-pop]");
  if (trigger) { ev.preventDefault(); togglePop(trigger); }
});
document.addEventListener("keydown", (ev) => {
  const trigger = ev.target.closest?.(".info[data-pop]");
  if (trigger && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); togglePop(trigger); }
});

function showImporter(name) {
  document.querySelectorAll(".js-importer").forEach((el) => (el.textContent = name));
}

// Setup checklist (welcome dialog and Status tab)
let setup = { nas: false, nasTarget: "", cameras: [], synced: false };

async function refreshSetup() {
  try {
    const [{ status }, { cameras, importer }] = await Promise.all([api("GET", "/api/nas"), api("GET", "/api/cameras")]);
    setup = {
      nas: status.mounted && status.writable && !status.errors.length,
      nasTarget: status.target_dir,
      cameras,
      importer,
      synced: cameras.some((c) => c.last_sync_at && !c.last_error),
    };
  } catch (e) { return; }
  const items = [
    { done: setup.nas, title: "Connect the NAS", href: "#nas",
      text: setup.nas ? `Photos go to ${setup.nasTarget}` : "Enter your NAS share in the NAS tab, test it and save." },
    { done: setup.cameras.length > 0, title: "Register the camera", href: "#cameras",
      text: setup.cameras.length ? `Registered: ${setup.cameras.map((c) => c.name).join(", ")}`
        : `Click Register camera, then select "${setup.importer}" in the camera's PC AUTO SAVE SETTING.` },
    { done: setup.synced, title: "Transfer your photos", href: "#help/transfer",
      text: setup.synced ? "Your first transfer has completed." : "Start PC AUTO SAVE in the camera's playback menu." },
  ];
  const html = items.map((it, i) =>
    `<li class="${it.done ? "done" : ""}"><span class="num">${it.done ? "✓" : i + 1}</span><div>` +
    `<a href="${it.href}" data-close><strong>${esc(it.title)}</strong></a><div class="muted">${esc(it.text)}</div></div></li>`).join("");
  $("#welcome-checklist").innerHTML = html;
  $("#status-checklist").innerHTML = html;
  $("#getting-started").hidden = items.every((it) => it.done);
  $("#welcome-start").textContent = items.every((it) => it.done) ? "Go to status" : "Start setup";
}

function nextSetupTab() {
  if (!setup.nas) return "nas";
  if (!setup.cameras.length) return "cameras";
  return "status";
}

const welcome = $("#welcome");
$("#open-welcome").addEventListener("click", () => { refreshSetup(); welcome.showModal(); });
welcome.addEventListener("click", (ev) => {
  if (ev.target.closest("a[data-close]")) welcome.close();
  else if (ev.target === welcome) welcome.close(); // click on the backdrop
});
welcome.addEventListener("close", () => {
  localStorage.setItem("welcomeSeen", "1");
  if (welcome.returnValue === "start") location.hash = nextSetupTab();
  welcome.returnValue = "";
});

// Status
const PHASES = {
  starting: "Camera connected...", preflight: "Checking NAS...", listing: "Reading the camera's file list...",
  transferring: "Transferring", finishing: "Ending the session on the camera...",
};
const STEP_ICONS = { done: "✓", running: "", failed: "✗", stopped: "■" };

function renderSteps(steps) {
  return (steps || []).map((st) =>
    `<li class="step ${st.state}"><span class="icon">${STEP_ICONS[st.state] ?? ""}</span>` +
    `<span class="label">${esc(st.label)}</span>` +
    `<span class="elapsed">${fmtDuration(st.elapsed)}</span>` +
    (st.detail ? `<div class="detail">${esc(st.detail)}</div>` : "") +
    (st.error ? `<div class="detail err">${esc(st.error)}</div>` : "") + "</li>").join("");
}

function renderIdle(s) {
  if (s.listen_error) return `<span class="err">${esc(s.listen_error)}</span>`;
  if (!s.listening) return "Starting...";
  return `<span class="waiting">Waiting for a camera to start PC AUTO SAVE to <strong>${esc(s.importer)}</strong></span>`;
}

function renderRegistration(r) {
  $("#register").disabled = r.open;
  $("#register-close").hidden = !r.open;
  $("#register-mark-existing").disabled = r.open;
  const done = r.registered.map((c) => `<div class="ok">Registered ${esc(c.name)} (${esc(c.camera_id)})</div>`).join("");
  if (r.open) {
    const left = Math.ceil(r.seconds_left);
    $("#register-status").innerHTML =
      `<span class="waiting">Accepting registrations for <span class="countdown">${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}</span>.` +
      ` Select <strong>${esc(currentImporter)}</strong> on the camera.</span>` +
      (r.mark_existing ? '<div class="muted">The photos on the card will be marked as copied on the first transfer.</div>' : "") + done;
  } else {
    $("#register-status").innerHTML = done;
  }
}

let currentImporter = "";
let lastRegistered = 0;

function renderStatus(s) {
  currentImporter = s.importer;
  showImporter(s.importer);
  renderRegistration(s.registration);
  if (s.registration.registered.length !== lastRegistered) {
    lastRegistered = s.registration.registered.length;
    loadCameras();
    refreshSetup();
  }
  const job = s.job;
  $("#stop").hidden = !s.busy;
  $("#steps").hidden = !job;
  if (!job) {
    $("#phase").innerHTML = renderIdle(s);
    $("#current").hidden = true;
    $("#listing").hidden = true;
  } else {
    $("#phase").innerHTML = `<strong>${esc(job.camera_name)}</strong> (${esc(job.address)}): ${esc(PHASES[job.phase] || job.phase)}`;
    $("#steps").innerHTML = renderSteps(job.steps);
    $("#listing").hidden = job.phase !== "listing" || !job.list_total;
    if (job.list_total) {
      $("#listing-bar").style.width = `${((100 * job.list_done) / job.list_total).toFixed(1)}%`;
      $("#listing-stats").textContent = `${job.list_done} of ${job.list_total} files read` +
        (job.list_done ? `, about ${fmtDuration(((job.list_total - job.list_done) * (Date.now() / 1000 - job.started_at)) / job.list_done)} left` : "");
    }
    const cur = job.current;
    $("#current").hidden = !cur;
    if (cur) {
      const pct = cur.bytes_total ? (100 * cur.bytes_done) / cur.bytes_total : 0;
      $("#current-file").textContent = `${cur.filename}  ->  ${cur.nas_path}`;
      $("#progress-bar").style.width = `${pct.toFixed(1)}%`;
      $("#current-stats").textContent =
        `${fmtBytes(cur.bytes_done)} of ${fmtBytes(cur.bytes_total)} (${pct.toFixed(1)}%) at ${fmtBytes(cur.speed)}/s` +
        (cur.eta ? `, about ${fmtDuration(cur.eta)} left` : "");
      $("#queue").textContent = `File ${job.queue_index} of ${job.files_new}`;
    }
  }
  const last = s.last_job;
  if (last) {
    const outcome = last.phase === "done" ? '<span class="ok">succeeded</span>'
      : last.phase === "stopped" ? '<span class="muted">stopped</span>' : '<span class="err">failed</span>';
    $("#last-job").innerHTML =
      `<div><strong>${esc(last.camera_name)}</strong> ${outcome} at ${fmtTime(last.finished_at)}</div>` +
      `<div>${esc(last.message)}</div>` +
      (last.error ? `<div class="err">${esc(last.error)}</div>` : "") +
      `<div class="muted">${last.files_found} file(s) on the card, ${last.files_new} new, ${last.files_done} copied (${fmtBytes(last.bytes_done)})</div>` +
      (last.steps?.length ? `<details><summary>Steps</summary><ol class="steps">${renderSteps(last.steps)}</ol></details>` : "");
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  let lastFinished = null;
  source.onmessage = (ev) => {
    const s = JSON.parse(ev.data);
    renderStatus(s);
    const finished = s.last_job && s.last_job.finished_at;
    if (finished && finished !== lastFinished) {
      lastFinished = finished;
      loadNasHealth();
      loadCameras();
      refreshSetup();
      if ($("#tab-history").classList.contains("active")) loadHistory();
    }
  };
  source.onerror = () => { source.close(); setTimeout(connectEvents, 3000); };
}

$("#stop").addEventListener("click", async () => {
  if (!confirm("Stop the transfer? Copied files are kept; the next transfer continues with the rest.")) return;
  try { await api("POST", "/api/transfer/stop"); } catch (e) { alert(e.message); }
});
$("#demo-autosave").addEventListener("click", async () => {
  try { await api("POST", "/api/demo/autosave"); } catch (e) { alert(e.message); }
});
$("#demo-register").addEventListener("click", async () => {
  try { await api("POST", "/api/demo/register"); } catch (e) { alert(e.message); }
});

async function loadNasHealth() {
  try {
    const { status } = await api("GET", "/api/nas");
    const errors = status.errors.map((e) => `<div class="err">${esc(e)}</div>`).join("");
    $("#nas-health").innerHTML =
      `<div>${esc(status.type.toUpperCase())} target <span class="filename">${esc(status.target_dir)}</span></div>` +
      `<div>${status.mounted && status.writable ? '<span class="ok">Mounted and writable</span>' : '<span class="err">Not ready</span>'}` +
      (status.free_bytes != null ? `, ${fmtBytes(status.free_bytes)} free` : "") + "</div>" +
      (status.route ? `<div class="${status.route.ok ? "muted" : "err"}">${esc(status.route.message)}</div>` : "") + errors;
  } catch (e) { $("#nas-health").textContent = e.message; }
}

// Cameras
async function loadCameras() {
  const data = await api("GET", "/api/cameras");
  const opts = ['<option value="">All cameras</option>'];
  $("#camera-rows").innerHTML = data.cameras.map((c) => {
    const id = esc(c.camera_id);
    opts.push(`<option value="${id}">${esc(c.name)}</option>`);
    return `<tr>
      <td><input value="${esc(c.name)}" data-rename="${id}"></td>
      <td>${esc(c.model)}</td>
      <td class="mono">${id}</td>
      <td>${fmtTime(c.last_sync_at)}${c.last_error ? `<div class="err">${esc(c.last_error)}</div>` : ""}
        ${c.mark_existing ? '<div class="muted">Next transfer: mark the card as copied</div>' : ""}</td>
      <td><input type="checkbox" ${c.enabled ? "checked" : ""} data-enable="${id}"></td>
      <td><button data-skip="${id}" data-on="${c.mark_existing ? 0 : 1}" title="On the next transfer, mark the photos on the card as copied without copying them">${c.mark_existing ? "Don't skip" : "Skip card"}</button>
        <button class="danger" data-forget="${id}">Forget</button></td>
    </tr>`;
  }).join("") || '<tr><td colspan="6" class="muted">No camera registered yet.</td></tr>';
  const sel = $("#history-camera"), current = sel.value;
  sel.innerHTML = opts.join("");
  sel.value = current;
}

$("#camera-rows").addEventListener("change", async (ev) => {
  const t = ev.target;
  try {
    if (t.dataset.rename) await api("PATCH", `/api/cameras/${encodeURIComponent(t.dataset.rename)}`, { name: t.value });
    if (t.dataset.enable) await api("PATCH", `/api/cameras/${encodeURIComponent(t.dataset.enable)}`, { enabled: t.checked });
  } catch (e) { alert(e.message); }
});
$("#camera-rows").addEventListener("click", async (ev) => {
  const t = ev.target;
  try {
    if (t.dataset.skip) {
      await api("PATCH", `/api/cameras/${encodeURIComponent(t.dataset.skip)}`, { mark_existing: t.dataset.on === "1" });
      loadCameras();
    }
    if (t.dataset.forget && confirm("Forget this camera? Its list of copied files is kept. " +
      "The camera can still transfer to this destination; it is added again when it does.")) {
      await api("DELETE", `/api/cameras/${encodeURIComponent(t.dataset.forget)}`);
      loadCameras();
    }
  } catch (e) { alert(e.message); }
});
$("#register").addEventListener("click", async () => {
  try { await api("POST", "/api/cameras/register", { mark_existing: $("#register-mark-existing").checked }); }
  catch (e) { alert(e.message); }
});
$("#register-close").addEventListener("click", async () => {
  try { await api("DELETE", "/api/cameras/register"); } catch (e) { alert(e.message); }
});

// NAS
const form = $("#nas-form");
function updateNasVisibility() {
  const type = form.type.value;
  form.querySelectorAll(".only-remote").forEach((el) => (el.hidden = type === "path"));
  form.querySelectorAll(".only-smb").forEach((el) => (el.hidden = type !== "smb"));
  form.querySelectorAll(".only-nfs").forEach((el) => (el.hidden = type !== "nfs"));
  form.querySelectorAll(".only-path").forEach((el) => (el.hidden = type !== "path"));
}
form.type.addEventListener("change", updateNasVisibility);

async function loadNas() {
  const { config, layout } = await api("GET", "/api/nas");
  for (const [k, v] of Object.entries(config)) if (form[k]) form[k].value = v ?? "";
  form.layout.value = layout;
  form.password.value = "";
  updateNasVisibility();
}

function nasPayload() {
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.password) delete data.password;
  return data;
}

function showResult(el, text, ok) {
  el.hidden = false;
  el.className = ok ? "ok" : "err";
  el.textContent = text;
}
const showNasResult = (text, ok) => showResult($("#nas-result"), text, ok);

$("#nas-test").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/nas/test", nasPayload());
    showNasResult(r.messages.join("\n"), r.ok);
  } catch (e) { showNasResult(e.message, false); }
});
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("PUT", "/api/nas", nasPayload());
    const errs = r.status.errors;
    showNasResult(errs.length ? errs.join("\n") : `Saved. ${r.status.target_dir} is mounted and writable.`, !errs.length);
    loadNas();
    refreshSetup();
  } catch (e) { showNasResult(e.message, false); }
});
$("#nas-mount").addEventListener("click", async () => {
  try { await api("POST", "/api/nas/mount"); showNasResult("Mounted.", true); } catch (e) { showNasResult(e.message, false); }
});
$("#nas-unmount").addEventListener("click", async () => {
  try { await api("POST", "/api/nas/unmount"); showNasResult("Unmounted.", true); } catch (e) { showNasResult(e.message, false); }
});

// History
let searchTimer;
async function loadHistory() {
  const params = new URLSearchParams({ search: $("#search").value, camera: $("#history-camera").value });
  if (!params.get("camera")) params.delete("camera");
  const data = await api("GET", `/api/transferred?${params}`);
  $("#history-rows").innerHTML = data.items.map((f) => `<tr>
      <td class="mono">${esc(f.filename)}</td><td>${esc(fmtCaptured(f.captured))}</td><td>${fmtBytes(f.size)}</td>
      <td class="mono">${f.nas_path ? esc(f.nas_path) : '<span class="muted">marked only</span>'}</td><td>${fmtTime(f.transferred_at)}</td>
      <td><button data-remove="${f.id}" title="Copy this file again on the next transfer">Copy again</button></td>
    </tr>`).join("") || '<tr><td colspan="6" class="muted">Nothing copied yet.</td></tr>';
  $("#history-total").textContent = `${data.total} file(s)` + (data.total > data.items.length ? `, showing ${data.items.length}` : "");
}
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadHistory, 300); });
$("#history-camera").addEventListener("change", loadHistory);
$("#history-rows").addEventListener("click", async (ev) => {
  const id = ev.target.dataset.remove;
  if (!id) return;
  try { await api("DELETE", `/api/transferred/${id}`); loadHistory(); } catch (e) { alert(e.message); }
});
$("#reset-all").addEventListener("click", async () => {
  const camera = $("#history-camera").value;
  if (!confirm(camera ? "Reset the list for this camera? All photos on its card will be copied again." :
    "Reset the whole list? All photos on all cameras will be copied again.")) return;
  try {
    await api("DELETE", "/api/transferred" + (camera ? `?camera=${encodeURIComponent(camera)}` : ""));
    loadHistory();
  } catch (e) { alert(e.message); }
});

// Settings
const settingsForm = $("#settings-form");
async function loadSettings() {
  const s = await api("GET", "/api/settings");
  settingsForm.importer.value = s.importer;
  settingsForm.registration_minutes.value = s.registration_minutes;
  settingsForm.lan_interface.value = s.lan_interface;
  $("#settings-port").textContent = `Web UI port ${s.port} (change it in the config file)`;
}
settingsForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {
    importer: settingsForm.importer.value.trim(),
    registration_minutes: Number(settingsForm.registration_minutes.value),
    lan_interface: settingsForm.lan_interface.value.trim(),
  };
  if (body.importer !== currentImporter &&
    !confirm(`Rename the destination to "${body.importer}"? Cameras registered under "${currentImporter}" must register again.`)) return;
  try {
    const r = await api("PUT", "/api/settings", body);
    showResult($("#settings-result"), r.listen_error || (r.restarted ? `Saved. Now listening as "${body.importer}".` : "Saved."), !r.listen_error);
    refreshSetup();
  } catch (e) { showResult($("#settings-result"), e.message, false); }
});

// Init
(async () => {
  const s = await api("GET", "/api/status");
  $("#footer").innerHTML = `fuji-nas-sync ${esc(s.version)} · <a href="#help">Help</a>`;
  document.querySelectorAll(".js-origin").forEach((el) => (el.textContent = location.origin));
  $("#demo-autosave").hidden = $("#demo-register").hidden = !s.demo;
  renderStatus(s);
  connectEvents();
  loadCameras();
  await Promise.all([route(), refreshSetup()]);
  if (!localStorage.getItem("welcomeSeen")) welcome.showModal();
})();
