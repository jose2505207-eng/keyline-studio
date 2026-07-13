// Thin client for the Keyline Studio backend. In dev, paths are relative and
// the Vite proxy (see vite.config.ts) forwards them to :8000. For a split
// deployment (e.g. static frontend on Vercel, backend elsewhere), set
// VITE_API_BASE to the backend origin at build time.

const API_BASE: string = import.meta.env.VITE_API_BASE ?? "";

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

export async function uploadDroneDem(projectId: string, file: File): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(url(`/api/projects/${projectId}/drone-dem`), {
    method: "POST",
    body: form,
  });
  await jsonOrThrow(res);
}

export async function startAnalysis(projectId: string): Promise<string> {
  const res = await fetch(url(`/api/projects/${projectId}/analyze`), { method: "POST" });
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
