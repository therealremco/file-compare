"use strict";

const $ = (id) => document.getElementById(id);
const categories = {
  name_absent: "Name absent locally",
  same_name_different_size: "Same name / different size",
};
const statuses = { unreviewed: "Unreviewed", keep: "Keep", delete: "Marked for deletion" };
let items = [];
let filtered = [];
let limit = 60;
let current = null;
let saving = false;
let previewObserver;

const bytes = (size) => {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index++; }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
};

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
  return response;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function actions(item) {
  const container = element("div", undefined, "actions");
  for (const [status, label] of Object.entries(statuses)) {
    const button = element("button", label);
    button.setAttribute("aria-pressed", String(item.status === status));
    button.disabled = saving;
    button.addEventListener("click", () => decide(item, status));
    container.append(button);
  }
  return container;
}

async function decide(item, status) {
  if (saving) return;
  saving = true;
  $("error").textContent = "";
  const oldIndex = filtered.findIndex((entry) => entry.id === item.id);
  try {
    await request("api/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: item.id, status }),
    });
    item.status = status;
    saving = false;
    render();
    if ($("viewer").open) {
      const selected = filtered.find((entry) => entry.id === item.id)
        || filtered[Math.min(oldIndex, filtered.length - 1)];
      if (selected) show(selected);
      else $("viewer").close();
    }
  } catch (error) {
    $("error").textContent = `Decision not saved: ${error.message}`;
    $("viewer-error").textContent = `Decision not saved: ${error.message}`;
  } finally {
    saving = false;
  }
}

function render() {
  const search = $("search").value.toLowerCase();
  filtered = items.filter((item) =>
    (!$("category").value || item.category === $("category").value)
    && (!$("status").value || item.status === $("status").value)
    && (!$("kind").value || item.kind === $("kind").value)
    && item.path.toLowerCase().includes(search));
  const marked = items.filter((item) => item.status === "delete");
  const counts = Object.keys(categories).map((category) =>
    `${categories[category]}: ${items.filter((item) => item.category === category).length}`);
  $("summary").textContent = `${Math.min(limit, filtered.length)} displayed / ${filtered.length} matching / ${items.length} total | ${counts.join(" | ")}`
    + ` | Unreviewed: ${items.filter((item) => item.status === "unreviewed").length}`
    + ` | Marked: ${marked.length} (${bytes(marked.reduce((sum, item) => sum + item.size, 0))})`;
  previewObserver?.disconnect();
  previewObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const image = entry.target;
      image.src = image.dataset.src;
      previewObserver.unobserve(image);
    }
  }, { rootMargin: "200px" });
  $("grid").replaceChildren();
  for (const item of filtered.slice(0, limit)) {
    const card = element("article", undefined, `card ${item.status}`);
    const open = element("button", undefined, "thumbnail");
    open.setAttribute("aria-label", `Open ${item.path}`);
    const image = document.createElement("img");
    image.alt = `Preview of ${item.path.split("/").pop()}`;
    image.dataset.src = `thumb/${item.id}`;
    const error = element("p", "", "preview-error");
    image.addEventListener("error", async () => {
      error.textContent = "Preview unavailable.";
      try {
        await request(image.dataset.src);
      } catch (failure) {
        error.textContent = `Preview unavailable: ${failure.message}`;
      }
    }, { once: true });
    open.append(image);
    open.addEventListener("click", () => show(item));
    card.append(
      open,
      element("h2", item.path.split("/").pop()),
      element("p", `${item.kind.toUpperCase()} · ${categories[item.category]}`, "badge"),
      element("p", `${bytes(item.size)} · ${statuses[item.status]}`, "details"),
      element("p", item.path, "details"),
      error,
      actions(item),
    );
    $("grid").append(card);
    previewObserver.observe(image);
  }
  if (!filtered.length) $("grid").append(element("p", "No files match these filters."));
  $("more").hidden = filtered.length <= limit;
}

function show(item) {
  const alreadyOpen = $("viewer").open;
  const unchanged = current?.id === item.id && alreadyOpen;
  current = item;
  $("viewer-name").textContent = item.path.split("/").pop();
  $("viewer-info").textContent =
    `${item.path} | ${bytes(item.size)} | ${categories[item.category]} | ${statuses[item.status]}`;
  $("viewer-actions").replaceChildren(actions(item));
  $("viewer-error").textContent = "";
  const index = filtered.findIndex((entry) => entry.id === item.id);
  $("previous").disabled = index <= 0;
  $("next").disabled = index >= filtered.length - 1;
  if (!unchanged) {
    $("preview").querySelector("video")?.pause();
    const preview = document.createElement(item.kind === "video" ? "video" : "img");
    if (item.kind === "video") {
      preview.controls = true;
      preview.preload = "metadata";
    } else preview.alt = item.path;
    preview.src = `media/${item.id}`;
    preview.addEventListener("error", () => {
      $("viewer-error").textContent =
        "Cannot display this media. The file may have changed, or your browser may not support its codec/format.";
    });
    $("preview").replaceChildren(preview);
  }
  if (!alreadyOpen) $("viewer").showModal();
}

function navigate(delta) {
  if (saving || !current) return;
  const index = filtered.findIndex((item) => item.id === current.id);
  if (filtered[index + delta]) show(filtered[index + delta]);
}

for (const id of ["category", "status", "kind", "search"]) {
  $(id).addEventListener("input", () => { limit = 60; render(); });
}
$("more").addEventListener("click", () => { limit += 60; render(); });
$("previous").addEventListener("click", () => navigate(-1));
$("next").addEventListener("click", () => navigate(1));
$("close").addEventListener("click", () => $("viewer").close());
$("viewer").addEventListener("close", () => {
  $("preview").querySelector("video")?.pause();
  $("preview").replaceChildren();
  current = null;
});
document.addEventListener("keydown", (event) => {
  if (!$("viewer").open || !current || event.ctrlKey || event.metaKey || event.altKey
      || ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
  if (event.key === "Escape") { event.preventDefault(); $("viewer").close(); return; }
  if (event.key === "ArrowLeft") { event.preventDefault(); navigate(-1); }
  if (event.key === "ArrowRight") { event.preventDefault(); navigate(1); }
  const status = { k: "keep", d: "delete", u: "unreviewed" }[event.key.toLowerCase()];
  if (status) { event.preventDefault(); decide(current, status); }
});

request("api/files").then((response) => response.json()).then((data) => {
  items = data;
  render();
}).catch((error) => {
  $("error").textContent = `Cannot load gallery: ${error.message}`;
  $("summary").textContent = "Loading failed.";
});
