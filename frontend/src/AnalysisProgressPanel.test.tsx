import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnalysisRun } from "./api";
import AnalysisProgressPanel from "./AnalysisProgressPanel";

afterEach(cleanup);

function makeRun(overrides: Partial<AnalysisRun> = {}): AnalysisRun {
  return {
    id: "run1",
    project_id: "p1",
    survey_id: null,
    state: "running",
    stage: "calculating_flow_accumulation",
    stage_label: "Calculating flow accumulation",
    stage_index: 8,
    stage_count: 15,
    stage_plan: [
      "loading_project",
      "resolving_dtm",
      "validating_dtm",
      "selecting_dem_mode",
      "computing_drone_coverage",
      "preparing_drone_dem",
      "terrain_quality_checks",
      "conditioning_dem",
      "calculating_flow_accumulation",
      "extracting_valleys",
      "extracting_ridges",
      "detecting_keypoints",
      "generating_keylines",
      "validating_spatial_results",
      "generating_hillshade",
      "generating_exports",
      "saving_results",
      "completed",
    ],
    progress_percent: 53,
    current_message: "Whitebox D8 flow accumulation is running",
    current_operation: "Whitebox D8 flow accumulation is running",
    dem_mode: "drone_only",
    terrain_source: "drone_only",
    fill_missing_areas_with_satellite: false,
    analysis_version: "2",
    has_dem: true,
    params: {},
    counts: null,
    feature_counts: { valleys: 0, ridges: 0, keypoints: 0, keylines: 0 },
    notices: [],
    qa: null,
    warnings: [],
    error_code: null,
    error_message: null,
    started_at: 1000,
    stage_started_at: 1266,
    heartbeat_at: 1400,
    last_heartbeat: 1400,
    last_progress_at: 1394,
    updated_at: 1400,
    created_at: 1000,
    completed_at: null,
    elapsed_seconds: 134,
    stage_elapsed_seconds: 134,
    seconds_since_heartbeat: 6,
    seconds_since_progress: 6,
    health: "active",
    health_message: null,
    cancellable: true,
    worker: { rq_job_id: "j1", status: "started", worker_name: "w1" },
    exports: {
      original_dtm: false,
      keylines_geojson: false,
      keylines_kml: false,
      visual_geotiff: false,
      design_bundle: false,
    },
    ...overrides,
  };
}

describe("AnalysisProgressPanel", () => {
  it("renders the current stage, progress, stage X of Y, and heartbeat", () => {
    render(<AnalysisProgressPanel run={makeRun()} />);
    // appears in both the current-stage header and the timeline step
    expect(screen.getAllByText("Calculating flow accumulation").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("53%")).toBeTruthy();
    expect(screen.getByText("Stage 8 of 15")).toBeTruthy();
    expect(screen.getByText(/Worker active 6s ago/)).toBeTruthy();
    expect(screen.getByText("Drone DTM")).toBeTruthy(); // terrain source label
  });

  it("renders the dynamic stage timeline without satellite stages for drone-only", () => {
    render(<AnalysisProgressPanel run={makeRun()} />);
    // drone-only plan should not include satellite fetch
    expect(screen.queryByText("Fetching satellite DEM")).toBeNull();
    expect(screen.getByText("Preparing drone DEM")).toBeTruthy();
    expect(screen.getByText("Extracting valleys")).toBeTruthy();
  });

  it("shows a stale-worker warning for a possibly-stalled run", () => {
    render(
      <AnalysisProgressPanel
        run={makeRun({
          health: "possibly_stalled",
          seconds_since_heartbeat: 150,
          health_message:
            "No heartbeat for 2 minute(s). The worker job still exists and may be processing a slow terrain operation.",
        })}
      />
    );
    expect(screen.getByText(/No heartbeat for 2 minute/)).toBeTruthy();
  });

  it("shows a worker-missing warning distinct from slow processing", () => {
    render(
      <AnalysisProgressPanel
        run={makeRun({
          health: "worker_missing",
          health_message:
            "The analysis worker is no longer running. Retry the analysis.",
        })}
        onRetry={() => {}}
      />
    );
    expect(screen.getByText(/worker is no longer running/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Retry analysis/ })).toBeTruthy();
  });

  it("offers cancel while running and re-run once complete", async () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <AnalysisProgressPanel run={makeRun()} onCancel={onCancel} onRerun={() => {}} />
    );
    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    await userEvent.click(cancelBtn);
    expect(onCancel).toHaveBeenCalled();

    rerender(
      <AnalysisProgressPanel
        run={makeRun({
          state: "completed",
          stage: "completed",
          progress_percent: 100,
          cancellable: false,
          health: "complete",
        })}
        onRerun={() => {}}
      />
    );
    expect(screen.getByText("Complete")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Re-run terrain analysis/ })
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("shows the failure error message", () => {
    render(
      <AnalysisProgressPanel
        run={makeRun({
          state: "failed",
          error_code: "DTM_FILE_MISSING",
          error_message: "The source DTM file is missing: caliterra.tif",
          health: "failed",
        })}
        onRetry={() => {}}
      />
    );
    expect(screen.getByText(/source DTM file is missing/)).toBeTruthy();
  });
});
