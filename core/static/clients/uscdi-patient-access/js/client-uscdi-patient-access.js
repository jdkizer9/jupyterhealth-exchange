// ────────────────────────────────────────────────────
// USCDI Patient Access client - SMART on FHIR patient EHR-records flow, run server-side.
// Registers the connect step (hospital picker + "choose what to share") and the status route for
// the shared patient-facing app (core/static/common/js/patient-facing.js).
// Sketch (scenario D): the Patient Access Module runs the EHR sign-in, the pull and the import
// server-side, in JHE. This page asks for the authorize URL, and after the EHR returns to JHE's
// callback it follows the job. API_ENDPOINT and pfApi come from patient-facing.js.
// ────────────────────────────────────────────────────

// How often the status route polls the job.
const EPP_POLL_MS = 2000;

// Search hospital brands for the picker. Returns an array of facility rows (or []).
async function eppSearchBrands(jheToken, query) {
  const url = `${API_ENDPOINT}ehr-patient-portal/brands?q=${encodeURIComponent(query || "")}`;
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${jheToken}`, "Cache-Control": "no-cache" },
  });
  if (!response.ok) return [];
  const data = await response.json();
  return data.results || [];
}

// Ask JHE for the EHR authorize URL (the server does SMART discovery and PKCE and remembers the
// attempt), then go there. The EHR returns to /clients/uscdi-patient-access/callback, which starts
// the import and sends the patient to the status route.
async function eppLaunch(row, acceptedScopes, sourceId) {
  const out = await pfApi("POST", "uscdi-patient-access/launch", {
    locationId: row.id,
    scopes: acceptedScopes,
    dataSourceId: Number(sourceId),
  });
  window.location.assign(out.authorizeUrl);
}

// One row per resource scope the brand supports (row.supportedScopes, space-delimited patient/<Type>.read tokens), pre-checked, labeled from CONSTANTS.EHR_SUPPORTED_SCOPES; unrecognized tokens are skipped since there is no label to show. Calls onContinue(acceptedTokens) when the patient clicks Continue.
function eppRenderScopePicker(container, continueButton, row, onContinue) {
  container.innerHTML = "";
  const supported = (row.supportedScopes || "").split(/\s+/).filter(Boolean);
  const checkboxes = [];
  supported.forEach((token) => {
    const label = CONSTANTS.EHR_SUPPORTED_SCOPES[token];
    if (!label) return;
    const card = document.createElement("label");
    card.className = "pf-card";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    const body = document.createElement("div");
    body.className = "pf-card__body";
    const title = document.createElement("div");
    title.className = "pf-card__title";
    title.textContent = label;
    body.appendChild(title);
    card.appendChild(checkbox);
    card.appendChild(body);
    container.appendChild(card);
    checkboxes.push({ token: token, checkbox: checkbox });
  });
  continueButton.onclick = () => {
    onContinue(checkboxes.filter((c) => c.checkbox.checked).map((c) => c.token));
  };
}

// Render hospital search results as clickable rows (name + address); clicking a row calls onSelect(row). Returns the number of rows rendered (0 => shows a message).
function eppRenderBrandResults(container, results, onSelect) {
  container.innerHTML = "";
  if (!results || results.length === 0) {
    const empty = document.createElement("div");
    empty.className = "text-muted p-2";
    empty.textContent = "No hospitals found. Try a different name, city, or state.";
    container.appendChild(empty);
    return 0;
  }
  const list = document.createElement("div");
  list.className = "list-group text-start";
  results.forEach((row) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "list-group-item list-group-item-action";
    item.setAttribute("data-brand-result", "");
    const title = document.createElement("div");
    title.className = "fw-bold";
    const facility = row.facilityName && row.facilityName !== row.brandName ? ` — ${row.facilityName}` : "";
    title.textContent = row.brandName + facility;
    const addr = document.createElement("div");
    addr.className = "small text-muted";
    addr.textContent = row.addressText || "";
    item.appendChild(title);
    item.appendChild(addr);
    item.addEventListener("click", () => {
      onSelect(row);
    });
    list.appendChild(item);
  });
  container.appendChild(list);
  return results.length;
}

// Connect step: the hospital picker, then the scope checklist; Continue launches the server-side sign-in.
pfClient.connect = async (source) => {
  pfRender("t-connect", { rail: pfRail(1) });
  const picker = {
    container: document.getElementById("hospital-picker"),
    input: document.getElementById("hospital-search"),
    results: document.getElementById("hospital-results"),
  };
  const jheToken = getStoredToken();
  const onSelect = (row) => {
    picker.container.hidden = true;
    const scopePicker = document.getElementById("scope-picker");
    scopePicker.hidden = false;
    eppRenderScopePicker(
      document.getElementById("scope-picker-list"),
      document.getElementById("scope-picker-continue"),
      row,
      async (acceptedScopes) => {
        pfShowLoading();
        try {
          await eppLaunch(row, acceptedScopes, source.id);
        } catch (e) {
          pfHideLoading();
          showFlowError("We couldn't start the connection", e && e.message ? e.message : String(e), {
            retryLabel: "Choose a different organization",
          });
        }
      }
    );
  };
  const runSearch = async () => {
    eppRenderBrandResults(picker.results, await eppSearchBrands(jheToken, picker.input.value), onSelect);
  };
  let timer = null;
  picker.input.addEventListener("input", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(runSearch, 200);
  });
  picker.container.hidden = false;
  await runSearch();
};

// One line per record type: what the job has done for it so far.
function eppTypeLine(t) {
  if (t.status === "ok") return `${t.label}: ${t.count} record(s)`;
  if (t.status === "failed" || t.status === "error") return `${t.label}: could not fetch${t.error ? ` (${t.error})` : ""}`;
  if (t.status === "skipped") return `${t.label}: skipped`;
  if (t.status === "not_requested") return `${t.label}: not selected`;
  if (t.status === "not_granted") return `${t.label}: not shared by your organization`;
  return `${t.label}: waiting…`;
}

// Patient-facing names for destinations; anything else shows its configured name.
const EPP_DESTINATION_LABELS = { jhe: "JupyterHealth", s3: "your study's secure storage" };

// One line per destination: what was saved there, or where it stands.
function eppDeliveryLine(name, d) {
  const where = EPP_DESTINATION_LABELS[name] || name;
  if (d.receipt) {
    const extra = d.receipt.rejected ? ` (${d.receipt.rejected} could not be saved)` : "";
    return `Saved ${d.receipt.resources} record(s) to ${where}.${extra}`;
  }
  if (d.status === "failed") return `Saving to ${where} hit a problem; retrying automatically.`;
  if (d.status === "failed_terminal") return `Saving to ${where} did not succeed.`;
  if (d.status === "delivering") return `Saving to ${where}…`;
  return `Waiting to save to ${where}.`;
}

// The importing screen's text for a job: the organization, each type's progress, and what was saved where.
function eppJobText(job) {
  const lines = [`Connected to ${job.source.brand}`, ""].concat(job.types.map(eppTypeLine));
  const deliveries = Object.entries(job.deliveries || {});
  if (deliveries.length) lines.push("", ...deliveries.map(([name, d]) => eppDeliveryLine(name, d)));
  else if (job.status === "failed") lines.push("", "Saving hit a problem; retrying automatically.");
  return lines.join("\n");
}

// The progress card's one line for a job: where it is, in the patient's terms.
function eppProgressCopy(job) {
  const fetched = job.types.filter((t) => t.status !== "pending");
  const records = job.types.reduce((sum, t) => sum + (t.count || 0), 0);
  if (job.status === "queued") return "Starting…";
  if (job.status === "fetching") {
    return `${fetched.length} of ${job.types.length} record types fetched · ${records} records so far`;
  }
  if (job.status === "failed") return "Saving hit a problem; retrying automatically.";
  return `Saving ${records} records to JupyterHealth…`;
}

// Status route (?route=status&job=...&source=...): the importing screen, fed by polling the job
// until it finishes, then the done screen (or the error callout). The pull runs on the server, so
// the patient may close the page; it keeps going.
async function eppStatus(params) {
  await renderImporting();
  const lede = document.querySelector("#pf_main .pf-lede");
  if (lede) lede.textContent = "Securely syncing your records into your study. You can close this page; the import keeps going.";
  const out = document.getElementById("out");
  const copy = document.querySelector("#pf_main .pf-import-card__copy");
  const retryHref = pfUrl("connect", { source: String(params.source || PATIENT_FACING_CONFIG.dataSourceIds[0]) });
  for (;;) {
    const job = await pfApi("GET", `uscdi-patient-access/jobs/${encodeURIComponent(params.job || "")}`);
    if (out) out.textContent = eppJobText(job);
    if (copy) copy.textContent = eppProgressCopy(job);
    // The router keeps its overlay up until a route returns; this one keeps polling, so drop it now.
    pfHideLoading();
    if (job.status === "done") {
      return pfNav("done", { source: String(params.source || job.dataSourceId) }, true);
    }
    if (job.terminal) {
      return showFlowError("We couldn't import your records", job.failure || "The import did not finish.", {
        retryLabel: "Choose a different organization",
        retryHref: retryHref,
      });
    }
    // Stop polling once the patient has navigated elsewhere.
    if (pfRouteAndParams().route !== "status") return;
    await new Promise((resolve) => setTimeout(resolve, EPP_POLL_MS));
  }
}

// The status route is this client's own; the shared router dispatches it like the others.
window.PF_ROUTES.status = eppStatus;
window.PF_ROUTE_TITLES.status = "Importing records";

// Exposed for unit tests; browser runs load this as a plain <script> and ignore it.
if (typeof window !== "undefined") {
  window.eppSearchBrands = eppSearchBrands;
  window.eppLaunch = eppLaunch;
  window.eppRenderBrandResults = eppRenderBrandResults;
  window.eppRenderScopePicker = eppRenderScopePicker;
  window.eppTypeLine = eppTypeLine;
  window.eppDeliveryLine = eppDeliveryLine;
  window.eppJobText = eppJobText;
  window.eppProgressCopy = eppProgressCopy;
  window.eppStatus = eppStatus;
}
