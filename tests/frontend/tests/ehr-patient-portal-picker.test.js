import { describe, test, expect, beforeAll, beforeEach, jest } from "@jest/globals";
const fs = require("fs");
const path = require("path");

const STATIC = path.resolve(__dirname, "../../../core/static");
const TEMPLATES = path.resolve(__dirname, "../../../core/templates");

// A Django component file is a {% verbatim %}-wrapped <script type="text/template">; strip the tags to get the HTML.
function componentHtml(file) {
  return fs.readFileSync(file, "utf8").replace(/{% ?verbatim ?%}|{% ?endverbatim ?%}/g, "");
}

// The connect page as the shell serves it: the shared rail partial plus this client's picker component.
function renderPickerPage() {
  document.body.innerHTML =
    `<div id="pf_main"></div>` +
    componentHtml(path.join(TEMPLATES, "common/patient_facing/components/rail.html")) +
    componentHtml(path.join(TEMPLATES, "clients/ehr-patient-portal/components/connect.html"));
  window.pfRegisterPartials();
}

// Loads patient-facing.js (router, pfRender, pfRail, ...) then client-ehr-patient-portal.js,
// which exposes the picker helpers and the connect step on window.
beforeAll(() => {
  global.Handlebars = require(path.join(STATIC, "common/js/handlebars.min.js"));
  require(path.join(STATIC, "common/js/common.js"));
  require(path.join(STATIC, "common/js/patient-facing.js"));
  require("../../../core/static/clients/ehr-patient-portal/js/client-ehr-patient-portal.js");
});

beforeEach(() => {
  global.fetch = jest.fn();
  global.PATIENT_FACING_CONFIG = { dataSourceIds: [5], pageUrl: "/clients/ehr-patient-portal/", siteTitle: "T", expectedResourceTypes: [] };
  // The scope picker's catalog + always-required plumbing scopes, normally injected by server-settings.js.
  global.CONSTANTS = {
    EHR_SUPPORTED_SCOPES: { "patient/Patient.read": "Demographics", "patient/Condition.read": "Conditions" },
    EHR_PATIENT_PORTAL_BASE_SCOPES: "openid profile launch/patient",
  };
});

describe("eppSearchBrands", () => {
  test("queries the brands API with the JHE token and returns the results", async () => {
    const rows = [{ brandName: "Mount Sinai", facilityName: "MSH", fhirBaseUrl: "https://s/FHIR/R4", addressText: "NY" }];
    global.fetch = jest.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ results: rows }) }));

    const out = await window.eppSearchBrands("tok", "sinai");

    expect(out).toEqual(rows);
    const [url, opts] = global.fetch.mock.calls[0];
    // Hyphen, matching the route in core/urls.py. An underscore 404s and eppSearchBrands
    // swallows it into [], so the picker just says "no hospitals found".
    expect(url).toContain("/api/v1/ehr-patient-portal/brands");
    expect(url).toContain("q=sinai");
    expect(opts.headers.Authorization).toBe("Bearer tok");
  });

  test("returns [] on a failed response", async () => {
    global.fetch = jest.fn(() => Promise.resolve({ ok: false }));
    const out = await window.eppSearchBrands("tok", "x");
    expect(out).toEqual([]);
  });
});

// The brands API answers searches; the launch API answers with the EHR authorize URL.
function apiFetch(brandRow) {
  return jest.fn((url) =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve(
          String(url).includes("patient-access/launch")
            ? { authorizeUrl: "https://ehr.example/authorize?state=s" }
            : { results: brandRow ? [brandRow] : [] },
        ),
    }),
  );
}

function launchCall() {
  return global.fetch.mock.calls.find(([url]) => String(url).includes("/api/v1/patient-access/launch"));
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("eppLaunch", () => {
  test("asks JHE for the authorize URL with the location, the chosen scopes and the data source", async () => {
    global.fetch = apiFetch();
    window.storeToken("tok");

    await window.eppLaunch({ id: 9 }, ["patient/Patient.read"], "5");

    const [, opts] = launchCall();
    expect(opts.method).toBe("POST");
    expect(opts.headers.Authorization).toBe("Bearer tok");
    expect(JSON.parse(opts.body)).toEqual({ locationId: 9, scopes: ["patient/Patient.read"], dataSourceId: 5 });
  });
});

describe("eppRenderBrandResults", () => {
  test("renders a clickable row per result and fires onSelect with that result", () => {
    const container = document.createElement("div");
    const rows = [
      { brandName: "Mount Sinai", facilityName: "MSH", fhirBaseUrl: "https://a/FHIR/R4", addressText: "1 Levy Pl" },
      { brandName: "Mercy", facilityName: "Mercy STL", fhirBaseUrl: "https://b/FHIR/R4", addressText: "MO" },
    ];
    const onSelect = jest.fn();

    const n = window.eppRenderBrandResults(container, rows, onSelect);

    expect(n).toBe(2);
    const items = container.querySelectorAll("[data-brand-result]");
    expect(items.length).toBe(2);
    // the row shows the hospital name + address to the patient
    expect(items[0].textContent).toContain("Mount Sinai");
    expect(items[0].textContent).toContain("1 Levy Pl");
    items[1].click();
    expect(onSelect).toHaveBeenCalledWith(rows[1]);
  });

  test("shows a no-results message when empty", () => {
    const container = document.createElement("div");
    const n = window.eppRenderBrandResults(container, [], jest.fn());
    expect(n).toBe(0);
    expect(container.textContent.toLowerCase()).toContain("no ");
  });
});

describe("pfClient.connect on the EHR page", () => {
  const ROW = {
    id: 9,
    brandName: "Epic Sandbox",
    facilityName: "Madison",
    fhirBaseUrl: "https://epic/FHIR/R4",
    addressText: "WI",
    ehrClientId: "brand-cid",
    supportedScopes: "patient/Patient.read patient/Condition.read",
  };

  test("renders the picker at rail step 1, then the scope picker, then launches server-side with the chosen scopes", async () => {
    renderPickerPage();
    window.storeToken("tok");
    global.fetch = apiFetch(ROW);

    await window.pfClient.connect({ id: 5, name: "EHR Patient Portal" });

    const main = document.getElementById("pf_main");
    expect(main.querySelector(".pf-rail__step").className).toBe("pf-rail__step is-active");
    expect(main.querySelector(".pf-h1").textContent).toBe("Share your medical records");
    expect(main.querySelector("#hospital-picker").hidden).toBe(false);
    expect(main.querySelectorAll("#hospital-results [data-brand-result]")).toHaveLength(1);
    main.querySelector("#hospital-results [data-brand-result]").click();

    // Picking a hospital swaps the picker for a checklist of that brand's supported scopes, all pre-checked.
    expect(main.querySelector("#hospital-picker").hidden).toBe(true);
    expect(main.querySelector("#scope-picker").hidden).toBe(false);
    const checkboxes = main.querySelectorAll("#scope-picker-list input[type=checkbox]");
    expect(checkboxes).toHaveLength(2);
    expect([...checkboxes].every((c) => c.checked)).toBe(true);
    expect(launchCall()).toBeUndefined();

    document.getElementById("scope-picker-continue").click();
    await flush();

    // No client id, iss or PKCE in the browser any more: the server builds the authorize URL.
    expect(JSON.parse(launchCall()[1].body)).toEqual({
      locationId: 9,
      scopes: ["patient/Patient.read", "patient/Condition.read"],
      dataSourceId: 5,
    });
  });

  test("unchecking a scope before continuing leaves it out of the launch", async () => {
    renderPickerPage();
    window.storeToken("tok");
    global.fetch = apiFetch(ROW);
    await window.pfClient.connect({ id: 5, name: "EHR Patient Portal" });
    const main = document.getElementById("pf_main");
    main.querySelector("#hospital-results [data-brand-result]").click();

    main.querySelectorAll("#scope-picker-list input[type=checkbox]")[1].click();
    document.getElementById("scope-picker-continue").click();
    await flush();

    expect(JSON.parse(launchCall()[1].body).scopes).toEqual(["patient/Patient.read"]);
  });

  test("launches for the data source being connected", async () => {
    // A client can be linked to more than one data source; the launch names the one picked.
    global.PATIENT_FACING_CONFIG.dataSourceIds = [5, 6];
    renderPickerPage();
    window.storeToken("tok");
    global.fetch = apiFetch(ROW);
    await window.pfClient.connect({ id: 6, name: "EHR Patient Portal" });
    document.querySelector("#hospital-results [data-brand-result]").click();
    document.getElementById("scope-picker-continue").click();
    await flush();

    expect(JSON.parse(launchCall()[1].body).dataSourceId).toBe(6);
  });

  test("typing is debounced into one search", async () => {
    renderPickerPage();
    window.storeToken("tok");
    global.fetch = jest.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ results: [] }) }));
    await window.pfClient.connect({ id: 5, name: "EHR Patient Portal" });
    const initial = global.fetch.mock.calls.length;

    jest.useFakeTimers();
    const input = document.getElementById("hospital-search");
    input.value = "sinai";
    input.dispatchEvent(new Event("input"));
    input.dispatchEvent(new Event("input"));
    jest.advanceTimersByTime(200);
    jest.useRealTimers();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(global.fetch.mock.calls).toHaveLength(initial + 1);
    expect(global.fetch.mock.calls[initial][0]).toContain("q=sinai");
  });
});
