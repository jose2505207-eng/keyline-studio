import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";

/** Timeline shown to the user. A stage is only marked done when the backend
 * state has demonstrably moved past it — no fabricated progress. */
const TIMELINE: { label: string; states: string[]; stages?: string[] }[] = [
  { label: "Preparing survey", states: ["created"] },
  { label: "Uploading images", states: ["uploading"] },
  { label: "Validating photographs", states: ["preflight"] },
  { label: "Queued for photogrammetry", states: ["queued", "submitting", "provider_queued"] },
  { label: "Creating reconstruction", states: ["provider_running"], stages: ["creating reconstruction", "processing"] },
  { label: "Generating point cloud", states: ["provider_running"], stages: ["generating point cloud"] },
  { label: "Classifying ground", states: ["provider_running"], stages: ["classifying ground"] },
  { label: "Generating DTM", states: ["provider_running"], stages: ["generating DTM"] },
  { label: "Generating orthophoto", states: ["provider_running"], stages: ["generating orthophoto"] },
  { label: "Downloading terrain products", states: ["downloading"] },
  { label: "Validating terrain", states: ["validating"] },
  { label: "Running keyline analysis", states: ["terrain_queued", "terrain_running"] },
  { label: "Complete", states: ["completed"] },
];

const ACTIVE_STATES = new Set([
  "created", "uploading", "uploaded", "queued", "preflight", "submitting",
  "provider_queued", "provider_running", "downloading", "validating",
  "terrain_queued", "terrain_running",
]);

function timelineIndex(survey: api.Survey): number {
  const { state, stage } = survey;
  let best = -1;
  TIMELINE.forEach((item, i) => {
    if (!item.states.includes(state)) return;
    if (item.stages) {
      if (stage && item.stages.some((s) => stage.startsWith(s))) best = i;
    } else {
      best = i;
    }
  });
  if (best === -1 && state === "provider_running") best = 4;
  if (state === "uploaded") best = 1;
  return best;
}

interface FileEntry {
  file: File;
  status: "pending" | "uploading" | "done" | "error";
  pct: number;
  error?: string;
  key?: string;
}

interface Props {
  projectId: string | null;
  initialSurveyId: string | null;
  onSurveyCreated: (surveyId: string) => void;
  onCompleted: () => void; // reload results + orthophoto
  onError: (msg: string) => void;
}

export default function DroneSurveyPanel({
  projectId, initialSurveyId, onSurveyCreated, onCompleted, onError,
}: Props) {
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [gcpFile, setGcpFile] = useState<File | null>(null);
  const [surveyId, setSurveyId] = useState<string | null>(initialSurveyId);
  const [survey, setSurvey] = useState<api.Survey | null>(null);
  const [uploading, setUploading] = useState(false);
  const [overallPct, setOverallPct] = useState(0);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [demRes, setDemRes] = useState("");
  const [orthoRes, setOrthoRes] = useState("");
  const [dragOver, setDragOver] = useState(false);

  const entriesRef = useRef(entries);
  entriesRef.current = entries;
  const pollTimer = useRef<number | null>(null);
  const completedFired = useRef(false);
  const pickerRef = useRef<HTMLInputElement>(null);
  const gcpRef = useRef<HTMLInputElement>(null);

  const totalBytes = entries.reduce((a, e) => a + e.file.size, 0);
  const dupNames = new Set(
    entries.map((e) => e.file.name)
      .filter((n, i, arr) => arr.indexOf(n) !== i)
  );

  // ------------------------------------------------------------- polling
  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const pollOnce = useCallback(
    async (pid: string, sid: string, failures = 0) => {
      let next = 5000;
      try {
        const s = await api.getSurvey(pid, sid);
        setSurvey(s);
        failures = 0;
        if (!ACTIVE_STATES.has(s.state) || s.state === "uploaded") {
          if (s.state === "completed" && !completedFired.current) {
            completedFired.current = true;
            onCompleted();
          }
          return; // terminal or waiting for the user — stop polling
        }
        next = s.state === "provider_running" || s.state === "provider_queued"
          ? 8000
          : 2000;
      } catch {
        failures += 1;
        next = Math.min(2000 * 2 ** failures, 60000); // network backoff
      }
      pollTimer.current = window.setTimeout(
        () => void pollOnce(pid, sid, failures), next);
    },
    [onCompleted]
  );

  useEffect(() => {
    // page-reload recovery: resume polling an active survey
    if (projectId && surveyId) {
      completedFired.current = false;
      void pollOnce(projectId, surveyId);
    }
    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, surveyId]);

  // ------------------------------------------------------------- files
  const addFiles = (list: FileList | File[]) => {
    const jpegs = Array.from(list).filter((f) =>
      /\.jpe?g$/i.test(f.name));
    setEntries((prev) => [
      ...prev,
      ...jpegs.map((file) => ({ file, status: "pending" as const, pct: 0 })),
    ]);
  };

  const removeEntry = (i: number) =>
    setEntries((prev) => prev.filter((_, j) => j !== i));

  // ------------------------------------------------------------- upload
  const uploadAll = async () => {
    if (!projectId || entries.length === 0) return;
    setUploading(true);
    try {
      const plan = await api.createSurvey(
        projectId,
        entries.map((e) => ({
          filename: e.file.name,
          type: "image/jpeg",
          size: e.file.size,
          lastModified: e.file.lastModified,
        })),
        buildOptions()
      );
      setSurveyId(plan.survey_id);
      onSurveyCreated(plan.survey_id);
      // uploads[i] corresponds to images[i] (same order)
      setEntries((prev) =>
        prev.map((e, i) => ({ ...e, key: plan.uploads[i]?.key })));
      await runUploads(plan.survey_id, plan.uploads, plan.upload_concurrency);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setUploading(false);
    }
  };

  const runUploads = async (
    sid: string,
    uploads: api.PresignedUpload[],
    concurrency: number
  ) => {
    if (!projectId) return;
    const queue = uploads.map((u, i) => ({ u, i }));
    let done = 0;
    const worker = async () => {
      for (;;) {
        const item = queue.shift();
        if (!item) return;
        const { u, i } = item;
        setEntries((p) => p.map((e, j) =>
          j === i ? { ...e, status: "uploading", pct: 0 } : e));
        try {
          await api.putWithProgress(u, entriesRef.current[i].file, (pct) => {
            setEntries((p) => p.map((e, j) =>
              j === i ? { ...e, pct } : e));
          });
          setEntries((p) => p.map((e, j) =>
            j === i ? { ...e, status: "done", pct: 100 } : e));
        } catch (err) {
          setEntries((p) => p.map((e, j) =>
            j === i
              ? { ...e, status: "error", error: (err as Error).message }
              : e));
        }
        done += 1;
        setOverallPct((done / uploads.length) * 100);
      }
    };
    await Promise.all(
      Array.from({ length: Math.max(concurrency, 1) }, worker));

    if (gcpFile) {
      try {
        await api.uploadGcp(projectId, sid, gcpFile);
      } catch (err) {
        onError(`GCP file rejected: ${(err as Error).message}`);
      }
    }
    const result = await api.completeUpload(projectId, sid);
    const s = await api.getSurvey(projectId, sid);
    setSurvey(s);
    if (!result.ok) {
      onError(
        `${result.missing.length + result.size_mismatch.length} uploads did ` +
        "not verify — retry the failed files");
    }
  };

  const retryFailed = async () => {
    if (!projectId || !surveyId) return;
    const failed = entriesRef.current
      .map((e, i) => ({ e, i }))
      .filter(({ e }) => e.status !== "done" && e.key);
    if (failed.length === 0) return;
    setUploading(true);
    try {
      const fresh = await api.refreshPresigned(
        projectId, surveyId, failed.map(({ e }) => e.key!));
      const byKey = new Map(fresh.map((u) => [u.key, u]));
      const uploads = failed
        .map(({ e }) => byKey.get(e.key!))
        .filter((u): u is api.PresignedUpload => !!u);
      // remap indices for the shared runner: rebuild a sparse plan
      const planUploads: api.PresignedUpload[] = [];
      entriesRef.current.forEach((e) => {
        const u = e.status !== "done" && e.key ? byKey.get(e.key) : undefined;
        planUploads.push(u ?? ({ key: e.key ?? "", url: "", headers: {}, method: "PUT", filename: e.file.name, size: e.file.size }));
      });
      // upload only failed ones sequentially-bounded
      let done = 0;
      const queue = failed.map(({ i }) => i);
      const worker = async () => {
        for (;;) {
          const i = queue.shift();
          if (i === undefined) return;
          const u = byKey.get(entriesRef.current[i].key!);
          if (!u) continue;
          setEntries((p) => p.map((e, j) =>
            j === i ? { ...e, status: "uploading", pct: 0, error: undefined } : e));
          try {
            await api.putWithProgress(u, entriesRef.current[i].file, (pct) => {
              setEntries((p) => p.map((e, j) => (j === i ? { ...e, pct } : e)));
            });
            setEntries((p) => p.map((e, j) =>
              j === i ? { ...e, status: "done", pct: 100 } : e));
          } catch (err) {
            setEntries((p) => p.map((e, j) =>
              j === i ? { ...e, status: "error", error: (err as Error).message } : e));
          }
          done += 1;
        }
      };
      await Promise.all([worker(), worker(), worker(), worker()]);
      void uploads;
      void done;
      const result = await api.completeUpload(projectId, surveyId);
      setSurvey(await api.getSurvey(projectId, surveyId));
      if (!result.ok) onError("Some uploads still failed to verify");
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setUploading(false);
    }
  };

  const buildOptions = (): Record<string, unknown> => {
    const opts: Record<string, unknown> = {};
    if (demRes.trim()) opts["dem-resolution"] = parseFloat(demRes);
    if (orthoRes.trim()) opts["orthophoto-resolution"] = parseFloat(orthoRes);
    return opts;
  };

  // ------------------------------------------------------------- actions
  const start = async () => {
    if (!projectId || !surveyId) return;
    try {
      await api.startSurvey(projectId, surveyId);
      completedFired.current = false;
      // after processing completes, browsers can release the File handles
      setEntries([]);
      void pollOnce(projectId, surveyId);
    } catch (err) {
      onError((err as Error).message);
    }
  };

  const cancel = async () => {
    if (!projectId || !surveyId) return;
    try {
      await api.cancelSurvey(projectId, surveyId);
      void pollOnce(projectId, surveyId);
    } catch (err) {
      onError((err as Error).message);
    }
  };

  const retryProcessing = async () => {
    if (!projectId || !surveyId) return;
    try {
      await api.retrySurvey(projectId, surveyId);
      completedFired.current = false;
      void pollOnce(projectId, surveyId);
    } catch (err) {
      onError((err as Error).message);
    }
  };

  // ------------------------------------------------------------- render
  const fmtBytes = (b: number) =>
    b > 1 << 30 ? `${(b / (1 << 30)).toFixed(2)} GB`
      : b > 1 << 20 ? `${(b / (1 << 20)).toFixed(1)} MB`
        : `${Math.ceil(b / 1024)} KB`;

  const failedCount = entries.filter((e) => e.status === "error").length;
  const uploadedOk = survey?.state === "uploaded";
  const processing = survey !== null &&
    ACTIVE_STATES.has(survey.state) && survey.state !== "uploading" &&
    survey.state !== "created" && survey.state !== "uploaded";
  const tlIndex = survey ? timelineIndex(survey) : -1;
  const elapsed = survey?.started_at
    ? Math.max(0, Math.floor(
      ((survey.completed_at ?? Date.now() / 1000) - survey.started_at) / 60))
    : null;
  const manifest = survey?.manifest as
    | { dtm?: { resolution_m?: number[]; aoi_coverage?: number } }
    | null;

  if (!projectId) {
    return <div className="drone-panel muted">Draw or import an AOI first.</div>;
  }

  return (
    <div className="drone-panel">
      {!processing && survey?.state !== "completed" && (
        <>
          <div
            className={`dropzone ${dragOver ? "over" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              addFiles(e.dataTransfer.files);
            }}
            onClick={() => pickerRef.current?.click()}
          >
            Drop geotagged drone JPGs here or click to pick
            <div className="muted">100–500 photos recommended</div>
          </div>
          <input
            ref={pickerRef}
            type="file"
            accept=".jpg,.jpeg,image/jpeg"
            multiple
            style={{ display: "none" }}
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />

          {entries.length > 0 && (
            <div className="file-summary">
              <b>{entries.length} photos · {fmtBytes(totalBytes)}</b>
              {dupNames.size > 0 && (
                <span className="warn"> ⚠ {dupNames.size} duplicate name(s)</span>
              )}
              <button className="linkish" onClick={() => setEntries([])}>
                Clear all
              </button>
              <ul className="file-list">
                {entries.slice(0, 200).map((e, i) => (
                  <li key={i} className={e.status}>
                    <span className="fname">{e.file.name}</span>
                    {e.status === "uploading" && <span>{e.pct.toFixed(0)}%</span>}
                    {e.status === "done" && <span>✓</span>}
                    {e.status === "error" && (
                      <span className="warn" title={e.error}>✗</span>
                    )}
                    {e.status === "pending" && (
                      <button className="linkish" onClick={() => removeEntry(i)}>
                        remove
                      </button>
                    )}
                  </li>
                ))}
                {entries.length > 200 && (
                  <li className="muted">…and {entries.length - 200} more</li>
                )}
              </ul>
            </div>
          )}

          <div className="gcp-row">
            <button className="linkish" onClick={() => gcpRef.current?.click()}>
              {gcpFile ? `GCP: ${gcpFile.name}` : "Optional: add GCP file"}
            </button>
            {gcpFile && (
              <button className="linkish" onClick={() => setGcpFile(null)}>✗</button>
            )}
            <input
              ref={gcpRef}
              type="file"
              accept=".txt"
              style={{ display: "none" }}
              onChange={(e) => setGcpFile(e.target.files?.[0] ?? null)}
            />
          </div>

          <button className="linkish" onClick={() => setShowAdvanced(!showAdvanced)}>
            Advanced settings {showAdvanced ? "▴" : "▾"}
          </button>
          {showAdvanced && (
            <div className="advanced">
              <label>
                DTM resolution (cm/px):
                <input value={demRes} placeholder="10"
                  onChange={(e) => setDemRes(e.target.value)} />
              </label>
              <label>
                Orthophoto resolution (cm/px):
                <input value={orthoRes} placeholder="5"
                  onChange={(e) => setOrthoRes(e.target.value)} />
              </label>
            </div>
          )}

          {!uploadedOk && (
            <button
              disabled={entries.length === 0 || uploading}
              onClick={uploadAll}
            >
              ⤒ Upload {entries.length > 0 ? `${entries.length} photos` : ""}
              {uploading ? ` (${overallPct.toFixed(0)}%)` : ""}
            </button>
          )}
          {failedCount > 0 && !uploading && (
            <button onClick={retryFailed}>
              ↻ Retry {failedCount} failed upload{failedCount > 1 ? "s" : ""}
            </button>
          )}
          {uploadedOk && (
            <button className="primary" onClick={start}>
              ▶ Start processing
            </button>
          )}
          {(survey?.state === "failed" || survey?.state === "cancelled") && (
            <button onClick={retryProcessing}>↻ Retry processing</button>
          )}
        </>
      )}

      {(processing || survey?.state === "completed" ||
        survey?.state === "failed" || survey?.state === "cancelled") && survey && (
        <div className="survey-status">
          <div className="survey-head">
            <b>{survey.state === "completed" ? "Survey complete"
              : survey.state === "failed" ? "Survey failed"
                : survey.state === "cancelled" ? "Survey cancelled"
                  : "Processing survey"}</b>
            <span className="muted"> · {survey.image_count} images
              {elapsed !== null ? ` · ${elapsed} min` : ""}</span>
          </div>
          {processing && (
            <div className="muted">
              {survey.stage ?? survey.state}
              {survey.provider_task?.progress != null &&
                survey.state === "provider_running" &&
                ` — ${survey.provider_task.progress.toFixed(0)}%`}
            </div>
          )}
          <ol className="timeline">
            {TIMELINE.map((t, i) => (
              <li key={t.label} className={
                survey.state === "completed" || i < tlIndex ? "done"
                  : i === tlIndex ? "current" : ""
              }>
                {t.label}
              </li>
            ))}
          </ol>
          {survey.warnings.length > 0 && (
            <div className="warn">
              {survey.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
            </div>
          )}
          {survey.error_message && (
            <div className="warn">✗ {survey.error_message}</div>
          )}
          {processing && (
            <button onClick={cancel} disabled={survey.cancel_requested}>
              {survey.cancel_requested ? "Cancelling…" : "✕ Cancel processing"}
            </button>
          )}
          {(survey.state === "failed" || survey.state === "cancelled") && (
            <button onClick={retryProcessing}>↻ Retry processing</button>
          )}
          {survey.state === "completed" && manifest?.dtm && (
            <div className="muted">
              DTM {manifest.dtm.resolution_m?.[0]} m/px ·{" "}
              {((manifest.dtm.aoi_coverage ?? 0) * 100).toFixed(1)}% AOI
              coverage · high-resolution <i>candidate</i> design — field
              verification still required.
              <div>
                <a href={api.dtmDownloadUrl(survey.project_id)}>Download DTM</a>
                {survey.orthophoto_available && (
                  <>
                    {" · "}
                    <a href={api.orthophotoDownloadUrl(survey.project_id)}>
                      Download orthophoto
                    </a>
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
