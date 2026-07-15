// Thin client for the Keyline Studio backend. In dev, paths are relative and
// the Vite proxy (see vite.config.ts) forwards them to :8000. For a split
// deployment (e.g. static frontend on Vercel, backend elsewhere), set
// VITE_API_BASE to the backend origin at build time.

// Runtime override too: open the app as /?api=http://localhost:8000 to point
// a hosted frontend at a backend running on your own machine.
const API_BASE: string =
  new URLSearchParams(window.location.search).get("api") ??
  import.meta.env.VITE_API_BASE ??
  "";

function url(path: string): string {
  return `${API_BASE}${path}`;
}

export interface JobStatus {
  job_id?: string;
  state: string; // queued | running:<step> | done | error:<message> | none
  log: { t: number; msg: string }[];
}

export interface FeatureCollection {
  type: "FeatureCollection";
  features: GeoJSON.Feature[];
}

async function jsonOrThrow(res: Response) {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.json();
}

export async function createProject(name: string, aoi: GeoJSON.Polygon): Promise<string> {
  const res = await fetch(url("/api/projects"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, aoi }),
  });
  const body = await jsonOrThrow(res);
  return body.project_id;
}

export interface DroneInfo {
  crs: string;
  resolution_m: [number, number];
  size_px: [number, number];
  elevation_range_m: [number, number];
  footprint: GeoJSON.Polygon;
}

export async function uploadDroneDem(projectId: string, file: File): Promise<DroneInfo> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(url(`/api/projects/${projectId}/drone-dem`), {
    method: "POST",
    body: form,
  });
  return jsonOrThrow(res);
}

export async function importBoundary(file: File): Promise<GeoJSON.Polygon> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(url("/api/import-boundary"), { method: "POST", body: form });
  const body = await jsonOrThrow(res);
  return body.aoi;
}

export function exportKmlUrl(projectId: string): string {
  return url(`/api/projects/${projectId}/export.kml`);
}

// ---- georeferenced map scans ------------------------------------------------

export interface MapMeta {
  map_id: string;
  width: number;
  height: number;
  page_count: number;
  page: number;
}

export interface GeorefResult {
  corners: [number, number][]; // UL, UR, LR, LL lng/lat
  rms_m: number;
}

export async function uploadMapScan(file: File): Promise<MapMeta> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(url("/api/maps"), { method: "POST", body: form });
  return jsonOrThrow(res);
}

export async function selectMapPage(mapId: string, page: number): Promise<MapMeta> {
  const res = await fetch(url(`/api/maps/${mapId}/page`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page }),
  });
  return jsonOrThrow(res);
}

export function mapImageUrl(mapId: string, cacheKey?: number | string): string {
  return url(`/api/maps/${mapId}/image${cacheKey !== undefined ? `?v=${cacheKey}` : ""}`);
}

export interface ControlPoint {
  px: number;
  py: number;
  e: number;
  n: number;
}

export async function georefMap(
  mapId: string,
  epsg: number,
  points: ControlPoint[]
): Promise<GeorefResult> {
  const res = await fetch(url(`/api/maps/${mapId}/georef`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ epsg, points }),
  });
  return jsonOrThrow(res);
}

export async function attachMap(projectId: string, mapId: string): Promise<void> {
  const res = await fetch(url(`/api/projects/${projectId}/attach-map`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ map_id: mapId }),
  });
  await jsonOrThrow(res);
}

export async function startAnalysis(
  projectId: string,
  options: { dtmId?: string; demMode?: string; terrain?: Record<string, number> } = {}
): Promise<string> {
  const res = await fetch(url(`/api/projects/${projectId}/analyze`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      dtm_id: options.dtmId ?? null,
      dem_mode: options.demMode ?? "auto",
      terrain: options.terrain ?? null,
    }),
  });
  const body = await jsonOrThrow(res);
  return body.job_id;
}

export async function getStatus(projectId: string): Promise<JobStatus> {
  return jsonOrThrow(await fetch(url(`/api/projects/${projectId}/status`)));
}

export async function getResults(projectId: string): Promise<FeatureCollection> {
  return jsonOrThrow(await fetch(url(`/api/projects/${projectId}/results`)));
}

export interface HillshadeInfo {
  url: string;
  coordinates: [number, number][]; // UL, UR, LR, LL lng/lat corners
}

export async function getHillshade(projectId: string): Promise<HillshadeInfo> {
  const bounds = await jsonOrThrow(
    await fetch(url(`/api/projects/${projectId}/hillshade-bounds`))
  );
  return {
    url: url(`/api/projects/${projectId}/hillshade?t=${Date.now()}`),
    coordinates: bounds.coordinates,
  };
}

export interface MoveResult {
  keypoint: GeoJSON.Feature;
  keylines: GeoJSON.Feature[];
}

export async function moveKeypoint(
  projectId: string,
  kid: string,
  lng: number,
  lat: number
): Promise<MoveResult> {
  const res = await fetch(url(`/api/projects/${projectId}/keypoints/${kid}/move`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lng, lat }),
  });
  return jsonOrThrow(res);
}

// ---- drone surveys ----------------------------------------------------------

export interface ImageMeta {
  filename: string;
  type: string;
  size: number;
  lastModified?: number;
}

export interface PresignedUpload {
  key: string;
  url: string;
  headers: Record<string, string>;
  method: string;
  filename: string;
  size: number;
}

export interface SurveyPlan {
  survey_id: string;
  uploads: PresignedUpload[];
  min_images: number;
  max_images: number;
  max_file_bytes: number;
  max_total_bytes: number;
  upload_concurrency: number;
}

export interface CompleteUploadResult {
  ok: boolean;
  uploaded_count: number;
  missing: string[];
  size_mismatch: string[];
}

export interface ProviderTaskInfo {
  state?: string | null;
  progress?: number | null;
  processing_time_ms?: number | null;
  last_error?: string | null;
}

export interface Survey {
  id: string;
  project_id: string;
  provider: string;
  external_task_id: string | null;
  state: string;
  stage: string | null;
  progress_percent: number;
  image_count: number;
  uploaded_count: number;
  total_bytes: number;
  warnings: string[];
  error_message: string | null;
  cancel_requested: boolean;
  preflight: Record<string, unknown> | null;
  provider_task: ProviderTaskInfo | null;
  gcp_supplied: boolean;
  dtm_available: boolean;
  orthophoto_available: boolean;
  manifest: Record<string, unknown> | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  updated_at: number;
}

export interface PhotogrammetryHealth {
  provider: string;
  configured_url: string;
  reachable: boolean;
  version: string;
  engine: string;
  engine_version: string;
  queue_count: number | null;
  max_images: number | null;
  error: string | null;
}

export async function createSurvey(
  projectId: string,
  images: ImageMeta[],
  options: Record<string, unknown> = {}
): Promise<SurveyPlan> {
  const res = await fetch(url(`/api/projects/${projectId}/drone-surveys`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ images, options }),
  });
  return jsonOrThrow(res);
}

export async function refreshPresigned(
  projectId: string,
  surveyId: string,
  keys: string[] | null
): Promise<PresignedUpload[]> {
  const res = await fetch(
    url(`/api/projects/${projectId}/drone-surveys/${surveyId}/presign`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys }),
    }
  );
  return jsonOrThrow(res);
}

export async function completeUpload(
  projectId: string,
  surveyId: string
): Promise<CompleteUploadResult> {
  const res = await fetch(
    url(`/api/projects/${projectId}/drone-surveys/${surveyId}/complete-upload`),
    { method: "POST" }
  );
  return jsonOrThrow(res);
}

export async function uploadGcp(
  projectId: string,
  surveyId: string,
  file: File
): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    url(`/api/projects/${projectId}/drone-surveys/${surveyId}/gcp`),
    { method: "POST", body: form }
  );
  await jsonOrThrow(res);
}

export async function startSurvey(
  projectId: string,
  surveyId: string
): Promise<{ ok: boolean; state: string }> {
  const res = await fetch(
    url(`/api/projects/${projectId}/drone-surveys/${surveyId}/start`),
    { method: "POST" }
  );
  return jsonOrThrow(res);
}

export async function getSurvey(
  projectId: string,
  surveyId: string
): Promise<Survey> {
  return jsonOrThrow(
    await fetch(url(`/api/projects/${projectId}/drone-surveys/${surveyId}`))
  );
}

export async function cancelSurvey(
  projectId: string,
  surveyId: string
): Promise<{ ok: boolean; detail?: string }> {
  const res = await fetch(
    url(`/api/projects/${projectId}/drone-surveys/${surveyId}/cancel`),
    { method: "POST" }
  );
  return jsonOrThrow(res);
}

export async function retrySurvey(
  projectId: string,
  surveyId: string
): Promise<{ ok: boolean; state: string }> {
  const res = await fetch(
    url(`/api/projects/${projectId}/drone-surveys/${surveyId}/retry`),
    { method: "POST" }
  );
  return jsonOrThrow(res);
}

export async function photogrammetryHealth(): Promise<PhotogrammetryHealth> {
  return jsonOrThrow(await fetch(url("/api/photogrammetry/health")));
}

export interface OrthophotoInfo {
  url: string;
  coordinates: [number, number][];
}

export async function getOrthophoto(projectId: string): Promise<OrthophotoInfo | null> {
  const res = await fetch(url(`/api/projects/${projectId}/orthophoto-bounds`));
  if (res.status === 404) return null;
  const bounds = await jsonOrThrow(res);
  return {
    url: url(`/api/projects/${projectId}/orthophoto?t=${Date.now()}`),
    coordinates: bounds.coordinates,
  };
}

export function dtmDownloadUrl(projectId: string): string {
  return url(`/api/projects/${projectId}/assets/dtm`);
}

export function orthophotoDownloadUrl(projectId: string): string {
  return url(`/api/projects/${projectId}/assets/orthophoto`);
}

/** PUT one file to a presigned URL with progress; works for S3 and the
 * local-dev backend alike. */
export function putWithProgress(
  upload: PresignedUpload,
  file: File,
  onProgress: (pct: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    // S3 presigned URLs are absolute; the local storage backend returns
    // API-relative URLs which must resolve against the API base, not the
    // (possibly separate) frontend origin.
    const target = upload.url.startsWith("http") ? upload.url : url(upload.url);
    xhr.open("PUT", target);
    for (const [k, v] of Object.entries(upload.headers)) {
      xhr.setRequestHeader(k, v);
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress((e.loaded / e.total) * 100);
    };
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`upload failed (${xhr.status})`));
    xhr.onerror = () => reject(new Error("network error during upload"));
    xhr.send(file);
  });
}

// ---- analysis runs / reanalysis ----------------------------------------------

export interface ResultCounts {
  valleys: number;
  ridges: number;
  keypoints: number;
  keylines: number;
}

export interface QAIssue {
  code: string;
  severity: string;
  message: string;
}

export interface QAReport {
  metrics: Record<string, unknown>;
  issues: QAIssue[];
  mode: string;
  passed: boolean;
  severe: boolean;
}

export interface ResultsProperties {
  warning?: string | null;
  relief_m?: number;
  keylines_suppressed?: boolean;
  dem_mode?: string;
  drone_coverage?: number | null;
  dem_resolution_m?: number;
  project_id?: string;
  survey_id?: string | null;
  analysis_run_id?: string | null;
  analysis_crs?: string;
  dem_bounds_wgs84?: number[];
  counts?: ResultCounts & { contours?: number };
  notices?: string[];
  keypoint_reasons?: string[];
  status?: string;
  bbox_wgs84?: number[];
  center_wgs84?: number[];
  qa?: QAReport | null;
  qa_mode?: string;
  watermark?: string | null;
}

export interface AnalysisRun {
  id: string;
  project_id: string;
  survey_id: string | null;
  state: string;
  stage: string | null;
  dem_mode: string | null;
  dem_path: string | null;
  params: Record<string, unknown>;
  counts: ResultCounts | null;
  notices: string[];
  qa: QAReport | null;
  error_message: string | null;
  created_at: number;
  completed_at: number | null;
}

export async function reanalyze(
  projectId: string,
  surveyId: string | null = null
): Promise<{ run_id: string; state: string }> {
  const res = await fetch(url(`/api/projects/${projectId}/reanalyze`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ survey_id: surveyId }),
  });
  return jsonOrThrow(res);
}

export async function getAnalysisRuns(projectId: string): Promise<AnalysisRun[]> {
  const body = await jsonOrThrow(
    await fetch(url(`/api/projects/${projectId}/analysis-runs`))
  );
  return body.runs;
}

export async function getAnalysisRun(
  projectId: string,
  runId: string
): Promise<AnalysisRun> {
  return jsonOrThrow(
    await fetch(url(`/api/projects/${projectId}/analysis-runs/${runId}`))
  );
}

// ---- managed DTM library ------------------------------------------------------

export interface Dtm {
  id: string;
  display_name: string;
  original_filename: string | null;
  source_type: "upload" | "survey" | "imported_path" | "external_path";
  status: "ready" | "missing" | string;
  size_bytes: number | null;
  created_at: number;
  crs: string | null;
  width: number | null;
  height: number | null;
  nodata: number | null;
  survey_id: string | null;
  project_id: string | null;
  resolution_m: number[] | null;
  bbox_wgs84: number[] | null;
  center_wgs84: number[] | null;
  footprint_geojson: GeoJSON.Geometry | null;
  elevation_range_m: number[] | null;
  valid_pct: number | null;
}

export interface DtmPathValidation {
  valid: boolean;
  reason: string | null;
  metadata: {
    filename: string;
    size_bytes: number;
    crs: string;
    width: number;
    height: number;
  } | null;
}

export async function listDtms(): Promise<Dtm[]> {
  const body = await jsonOrThrow(await fetch(url("/api/dtms")));
  return body.items;
}

export function uploadDtmWithProgress(
  file: File,
  projectId: string | null,
  onProgress: (pct: number) => void
): Promise<Dtm> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const q = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url(`/api/dtms/upload${q}`));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress((e.loaded / e.total) * 100);
    };
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(body as Dtm);
        else reject(new Error(body.detail ?? `upload failed (${xhr.status})`));
      } catch {
        reject(new Error(`upload failed (${xhr.status})`));
      }
    };
    xhr.onerror = () => reject(new Error("network error during upload"));
    xhr.send(form);
  });
}

export async function validateDtmPath(path: string): Promise<DtmPathValidation> {
  const res = await fetch(url("/api/dtms/validate-path"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return jsonOrThrow(res);
}

export async function importDtmPath(
  path: string,
  copyToLibrary: boolean,
  projectId: string | null
): Promise<Dtm> {
  const res = await fetch(url("/api/dtms/import-path"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path,
      copy_to_library: copyToLibrary,
      project_id: projectId,
    }),
  });
  return jsonOrThrow(res);
}

// ---- DTM detail + specialized exports -----------------------------------------

export async function getDtm(dtmId: string): Promise<Dtm> {
  return jsonOrThrow(await fetch(url(`/api/dtms/${dtmId}`)));
}

export interface ExportAvailability {
  geojson: boolean;
  kml: boolean;
  keylines_geojson: boolean;
  keylines_kml: boolean;
  keylines_dxf: boolean;
  gpkg: boolean;
  unavailable_reason: string | null;
}

export async function getExportAvailability(
  projectId: string
): Promise<ExportAvailability> {
  return jsonOrThrow(
    await fetch(url(`/api/projects/${projectId}/exports/availability`))
  );
}

export function exportUrl(projectId: string, kind: string): string {
  return url(`/api/projects/${projectId}/exports/${kind}`);
}
