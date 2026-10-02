// The GUI polls a backend job for as long as the backend calls it live (server.LIVE_JOB_STATUSES).
// A repository download that waits for another lease's transfer or extraction of an object of the accession
// download store is waiting_for_shared_download: still running, and it finishes on its own. The pollers
// used to poll only queued and running, so the download poller stopped at the wait, re-enabled the Download
// button, reported "Repository download failed." and never loaded the files the job went on to recognise.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const server = fs.readFileSync(path.join(__dirname, "../msdial_app/server.py"), "utf8");

// The GUI's live states are the backend's.
const waiting = server.match(/^WAITING_FOR_SHARED_DOWNLOAD = "([^"]+)"/m)[1];
const backendLive = server.match(/^LIVE_JOB_STATUSES = frozenset\(\{([^}]*)\}\)/m)[1]
  .split(",").map((item) => item.trim()).map((item) => item === "WAITING_FOR_SHARED_DOWNLOAD" ? waiting : item.replace(/"/g, ""));
const constants = source.slice(source.indexOf("const WAITING_FOR_SHARED_DOWNLOAD"),
  source.indexOf("async function api("));
assert.ok(constants.includes("const jobIsLive"), "app.js names the live job states once");
const live = vm.runInNewContext(`${constants}; LIVE_JOB_STATUSES`);
assert.deepEqual([...live].sort(), [...backendLive].sort(), "app.js and server.py agree on the live job states");
assert.ok(!/\[\s*"queued",\s*"running"\s*\]\.includes/.test(source), "no poller keeps a list of its own");

// The repository download poller through a wait and on to completion.
const functions = source.slice(source.indexOf("async function pollRepositoryDownload("),
  source.indexOf("async function draftRepositoryQaTargets("));
const elements = {
  "#repositoryDownloadLog": { textContent: "" },
  "#repositoryDownloadProgress": { value: 0 },
  "#repositoryDownloadProgressText": { textContent: "" },
  "#downloadRepositoryRaw": { disabled: true },
  "#repositoryRawRetention": { value: "" },
  "#outputRoot": { value: "" },
  "#repositoryAutoApplyMetadata": { checked: false },
};
const jobs = [
  { status: "running", logs: ["Downloading 1/2"], progress: 10, received: 10, total: 100 },
  { status: waiting, logs: ["Waiting for another download of shared.mzML to finish"], progress: 50,
    received: 50, total: 100, waiting_for: { object: "shared.mzML", job_id: "job-a" } },
  { status: "completed", logs: ["Done"], progress: 100, received: 100, total: 100,
    result: { manifest_path: "m.json", raw_retention_policy: "keep", output_directory: "out",
      recognized: { files: [{ path: "shared.mzML" }], warnings: [], rejected: [] } } },
];
const scheduled = [];
const statuses = [];
const messages = [];
const context = vm.createContext({
  state: { repositoryDownloadJobId: "job-b", files: [] },
  $: (selector) => elements[selector],
  api: async () => jobs.shift(),
  formatBytes: (value) => `${value} B`,
  formatDuration: (value) => `${value} s`,
  setTimeout: (callback) => scheduled.push(callback),
  setStatus: (text) => statuses.push(text),
  renderFiles: () => {},
  applyFormatStartingValues: () => {},
  showImportMessages: (lines) => messages.push(...lines),
  llmConfigured: () => false,
  repositoryInternalStandardEvidence: () => [],
  refreshQuestion: () => {},
});
vm.runInContext(`${constants}\n${functions}`, context);

(async () => {
  await context.pollRepositoryDownload();
  assert.equal(scheduled.length, 1, "a running download is polled again");
  await scheduled.shift()();
  assert.equal(scheduled.length, 1, "a download waiting for a shared object is polled again");
  assert.equal(elements["#downloadRepositoryRaw"].disabled, true, "the Download button stays disabled while it waits");
  assert.ok(!elements["#repositoryDownloadLog"].textContent.includes("failed"), "a wait is no failure");
  assert.match(elements["#repositoryDownloadProgressText"].textContent, /waiting for another download of shared\.mzML/);
  await scheduled.shift()();
  assert.equal(scheduled.length, 0, "a completed download is not polled");
  assert.equal(elements["#downloadRepositoryRaw"].disabled, false);
  assert.deepEqual(context.state.files, [{ path: "shared.mzML" }], "the files the job recognised are loaded");
  assert.equal(context.state.repositoryRunManifest, "m.json");
  assert.ok(messages.includes("Recognized 1 repository analysis file(s)."));
  assert.ok(!statuses.some((text) => /failed/i.test(text)), statuses.join(" | "));
  console.log("job polling tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
