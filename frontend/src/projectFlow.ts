import * as api from "./api";

export interface ProjectDeps {
  getProject: (id: string) => Promise<api.ProjectSummary | null>;
  createProject: (name: string, aoi: GeoJSON.Polygon) => Promise<string>;
}

const defaultDeps: ProjectDeps = {
  getProject: api.getProject,
  createProject: api.createProject,
};

/**
 * Validate a browser-stored project id against the backend and create a
 * fresh project from the AOI when it is missing, empty, or the server no
 * longer has it (e.g. an ephemeral SQLite store reset on redeploy). The
 * caller never has to reason about project ids.
 */
export async function resolveProject(
  currentId: string | null | undefined,
  aoi: GeoJSON.Polygon,
  deps: ProjectDeps = defaultDeps,
  name = `AOI ${new Date().toISOString().slice(0, 16)}`
): Promise<{ projectId: string; created: boolean }> {
  const trimmed = (currentId ?? "").trim();
  if (trimmed) {
    const existing = await deps.getProject(trimmed);
    if (existing) return { projectId: trimmed, created: false };
  }
  const projectId = await deps.createProject(name, aoi);
  return { projectId, created: true };
}
