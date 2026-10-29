import { describe, test, expect, beforeAll, beforeEach, jest } from "@jest/globals";
const fs = require("fs");
const path = require("path");

const STATIC = path.resolve(__dirname, "../../../core/static");
const TEMPLATES = path.resolve(__dirname, "../../../core/templates");

function componentHtml(file) {
  return fs.readFileSync(file, "utf8").replace(/{% ?verbatim ?%}|{% ?endverbatim ?%}/g, "");
}

// The status route after JHE's callback: the importing screen, fed by polling the job.
beforeAll(() => {
  global.Handlebars = require(path.join(STATIC, "common/js/handlebars.min.js"));
  require(path.join(STATIC, "common/js/common.js"));
  require(path.join(STATIC, "common/js/patient-facing.js"));
  require("../../../core/static/clients/ehr-patient-portal/js/client-ehr-patient-portal.js");
});

const JOB = {
  id: "j1",
  status: "done",
  terminal: true,
  dataSourceId: 5,
  source: { brand: "SMART Sandbox Health", location: "Sandbox Clinic" },
  types: [
    { label: "Labs", status: "ok", count: 12, error: null },
    { label: "Devices", status: "failed", count: 0, error: "403" },
  ],
  deliveries: {
    jhe: { status: "done", attempts: 1, failure: null, receipt: { resources: 12, rejected: 1, failures: {}, warnings: {}, fhirSourceId: 3 } },
    s3: { status: "done", attempts: 1, failure: null, receipt: { resources: 13, rejected: 0, failures: {}, warnings: {}, fhirSourceId: null } },
  },
  failure: null,
};

function jobFetch(job) {
  return jest.fn(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(job) }));
}

beforeEach(() => {
  document.body.innerHTML =
    `<div id="pf_main"></div>` +
    componentHtml(path.join(TEMPLATES, "common/patient_facing/components/rail.html")) +
    componentHtml(path.join(TEMPLATES, "common/patient_facing/components/importing.html")) +
    componentHtml(path.join(TEMPLATES, "common/patient_facing/components/error.html"));
  window.pfRegisterPartials();
  global.PATIENT_FACING_CONFIG = { dataSourceIds: [5], pageUrl: "/clients/ehr-patient-portal/", siteTitle: "T", expectedResourceTypes: [] };
  window.storeToken("tok");
});

describe("eppJobText", () => {
  test("names the organization, each type's progress and what was saved", () => {
    const text = window.eppJobText(JOB);
    expect(text).toContain("Connected to SMART Sandbox Health");
    expect(text).toContain("Labs: 12 record(s)");
    expect(text).toContain("Devices: could not fetch (403)");
    expect(text).toContain("Saved 12 record(s) to JupyterHealth. (1 could not be saved)");
    expect(text).toContain("Saved 13 record(s) to your study's secure storage.");
  });

  test("says which delivery is being retried", () => {
    const deliveries = { ...JOB.deliveries, s3: { status: "failed", attempts: 1, failure: "boom", receipt: null } };
    const text = window.eppJobText({ ...JOB, status: "failed", terminal: false, deliveries });
    expect(text).toContain("Saved 12 record(s) to JupyterHealth.");
    expect(text).toContain("Saving to your study's secure storage hit a problem; retrying automatically.");
  });

  test("explains types the patient left out or the organization withheld", () => {
    expect(window.eppTypeLine({ label: "Goals", status: "not_requested" })).toBe("Goals: not selected");
    expect(window.eppTypeLine({ label: "Devices", status: "not_granted" })).toBe("Devices: not shared by your organization");
  });
});

describe("eppProgressCopy", () => {
  test("counts fetched types and records while fetching, then says it is saving", () => {
    const fetching = {
      ...JOB,
      status: "fetching",
      types: [
        { label: "Labs", status: "ok", count: 12 },
        { label: "Goals", status: "pending", count: 0 },
      ],
    };
    expect(window.eppProgressCopy(fetching)).toBe("1 of 2 record types fetched · 12 records so far");
    expect(window.eppProgressCopy({ ...JOB, status: "delivering" })).toBe("Saving 12 records to JupyterHealth…");
  });
});

describe("the status route", () => {
  test("is registered with the shared router", () => {
    expect(window.PF_ROUTES.status).toBe(window.eppStatus);
  });

  test("polls the patient's job and opens the done screen when it finishes", async () => {
    global.fetch = jobFetch(JOB);
    const nav = jest.spyOn(window, "pfNav").mockResolvedValue(undefined);

    await window.eppStatus({ job: "j1", source: "5" });

    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toContain("/api/v1/patient-access/jobs/j1");
    expect(opts.headers.Authorization).toBe("Bearer tok");
    expect(nav).toHaveBeenCalledWith("done", { source: "5" }, true);
    nav.mockRestore();
  });

  test("shows the job's failure with a way back to the picker when it fails for good", async () => {
    global.fetch = jobFetch({ ...JOB, status: "failed_terminal", deliveries: {}, failure: "The authorization has expired." });

    await window.eppStatus({ job: "j1", source: "5" });

    const main = document.getElementById("pf_main");
    expect(main.textContent).toContain("We couldn't import your records");
    expect(main.textContent).toContain("The authorization has expired.");
  });
});
