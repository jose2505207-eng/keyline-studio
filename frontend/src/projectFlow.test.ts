import { describe, expect, it, vi } from "vitest";
import type { ProjectSummary } from "./api";
import { resolveProject, type ProjectDeps } from "./projectFlow";

const AOI: GeoJSON.Polygon = {
  type: "Polygon",
  coordinates: [[[-98.09, 30.17], [-98.08, 30.17], [-98.08, 30.18],
                 [-98.09, 30.18], [-98.09, 30.17]]],
};

const SUMMARY: ProjectSummary = {
  project_id: "p_existing",
  name: "AOI",
  has_drone_dtm: true,
  has_results: false,
};

function deps(over: Partial<ProjectDeps> = {}): ProjectDeps {
  return {
    getProject: vi.fn(async () => SUMMARY),
    createProject: vi.fn(async () => "p_new"),
    ...over,
  };
}

describe("resolveProject", () => {
  it("reuses an existing valid project without creating a new one", async () => {
    const d = deps();
    const r = await resolveProject("p_existing", AOI, d);
    expect(r).toEqual({ projectId: "p_existing", created: false });
    expect(d.getProject).toHaveBeenCalledWith("p_existing");
    expect(d.createProject).not.toHaveBeenCalled();
  });

  it("creates a project when the stored id is missing/empty", async () => {
    for (const id of [null, undefined, "", "   "]) {
      const d = deps();
      const r = await resolveProject(id, AOI, d);
      expect(r).toEqual({ projectId: "p_new", created: true });
      expect(d.getProject).not.toHaveBeenCalled();
      expect(d.createProject).toHaveBeenCalledWith(expect.any(String), AOI);
    }
  });

  it("creates a replacement when a stale id 404s (getProject -> null)", async () => {
    // this is the backend-restart / ephemeral-reset case: the browser has a
    // project id but the server no longer knows it
    const d = deps({ getProject: vi.fn(async () => null) });
    const r = await resolveProject("p_stale", AOI, d);
    expect(d.getProject).toHaveBeenCalledWith("p_stale");
    expect(r).toEqual({ projectId: "p_new", created: true });
    expect(d.createProject).toHaveBeenCalledWith(expect.any(String), AOI);
  });

  it("associates the current AOI with the recreated project", async () => {
    const create = vi.fn(
      async (_name: string, _aoi: GeoJSON.Polygon) => "p_new");
    await resolveProject("p_stale", AOI, {
      getProject: vi.fn(async () => null),
      createProject: create,
    });
    expect(create.mock.calls[0][1]).toBe(AOI); // boundary passed through
  });
});
