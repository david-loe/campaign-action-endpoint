"use strict";
const progress = document.getElementById("transfer-progress");
if (progress) {
  const form = document.getElementById("transfer-step");
  const start = document.getElementById("transfer-continue");
  const pause = document.getElementById("transfer-pause");
  const message = document.getElementById("transfer-message");
  let running = false;
  let inFlight = false;
  pause.hidden = false;
  function controls() {
    start.disabled = running || inFlight;
    pause.disabled = !running;
  }
  async function step() {
    if (!running || inFlight) return;
    inFlight = true;
    controls();
    let delay = 300;
    try {
      const response = await fetch(form.action, {
        method: "POST", body: new FormData(form), headers: {Accept: "application/json"}, credentials: "same-origin"
      });
      if (!response.ok || !response.headers.get("Content-Type")?.includes("application/json")) {
        throw new Error("Die Anfrage konnte nicht abgeschlossen werden. Laden Sie die Seite neu und melden Sie sich bei Bedarf erneut an.");
      }
      const result = await response.json();
      message.textContent = result.message || "Charge abgeschlossen.";
      if (result.processed !== undefined) document.getElementById("transfer-count").textContent = result.processed;
      if (result.total !== undefined) document.getElementById("transfer-total").textContent = result.total;
      if (!["loading", "running", "busy"].includes(result.status)) {
        running = false;
        window.location.replace(window.location.pathname);
        return;
      }
      if (result.status === "busy") delay = (result.retry_after || 3) * 1000;
    } catch (error) {
      running = false;
      message.textContent = error.message;
    } finally {
      inFlight = false;
      controls();
    }
    if (running) setTimeout(step, delay);
    else message.textContent += " Verarbeitung pausiert; abgeschlossene Chargen bleiben gespeichert.";
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!running && !inFlight) {
      running = true;
      step();
    }
  });
  pause.addEventListener("click", () => {
    running = false;
    controls();
    message.textContent = "Die Verarbeitung pausiert nach der laufenden Charge.";
  });
  controls();
  if (progress.dataset.autostart === "true") {
    running = true;
    step();
  }
}
