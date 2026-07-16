import { useState } from "react";
import * as api from "./api";

function fmtDuration(s: number | null | undefined): string {
  if (s == null || s < 0) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  if (m <= 0) return `${sec}s`;
  if (m < 60) return `${m}m ${sec}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

const STATE_LABEL: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  completed: "Complete",
  completed_with_warnings: "Complete (with warnings)",
  failed: "Failed",
  cancelled: "Cancelled",
};

const HEALTH_TONE: Record<string, string> = {
  active: "ok",
  complete: "ok",
  slow: "warn",
  possibly_stalled: "warn",
  worker_missing: "error",
  failed: "error",
};

interface Props {
  run: api.AnalysisRun | null;
  onCancel?: () => void;
  onRetry?: () => void;
  onRerun?: () => void;
}

/** Honest, structured terrain-analysis monitor. Renders the dynamic stage
 * timeline, an overall progress bar, elapsed/heartbeat health, warnings, and
 * the appropriate cancel/retry/re-run action for the run's state. Purely
 * presentational — the parent owns polling and passes the canonical run. */
export default function AnalysisProgressPanel({
  run,
  onCancel,
  onRetry,
  onRerun,
}: Props) {
  const [logOpen, setLogOpen] = useState(false);
  if (!run) return null;

  const terminal = ["completed", "completed_with_warnings", "failed", "cancelled"].includes(
    run.state
  );
  const failedOrStalled =
    run.state === "failed" ||
    run.state === "cancelled" ||
    run.health === "worker_missing";
  const plan = run.stage_plan ?? [];
  const currentIdx = plan.indexOf(run.stage ?? "");
  const tone = HEALTH_TONE[run.health] ?? "ok";
  const src = run.terrain_source ?? run.dem_mode ?? "";

  return (
    <div className="progress-panel" data-testid="analysis-progress">
      <div className="pp-head">
        <span className={`pp-state pp-${run.state}`}>
          {STATE_LABEL[run.state] ?? run.state}
        </span>
        {src && (
          <span className="pp-source">
            {api.TERRAIN_SOURCE_LABELS[src] ?? src}
          </span>
        )}
      </div>

      {!terminal && (
        <div className="pp-current">
          <b>{api.stageLabel(run.stage)}</b>
          {run.current_message && run.current_message !== api.stageLabel(run.stage) && (
            <div className="muted pp-msg">{run.current_message}</div>
          )}
        </div>
      )}

      <div className="pp-bar" aria-label="overall progress">
        <div
          className={`pp-fill pp-fill-${tone}`}
          style={{ width: `${Math.min(100, Math.max(0, run.progress_percent))}%` }}
        />
      </div>
      <div className="pp-meta muted">
        <span>{Math.round(run.progress_percent)}%</span>
        {run.stage_count > 0 && (
          <span>
            Stage {Math.min(run.stage_index, run.stage_count)} of {run.stage_count}
          </span>
        )}
        <span>Elapsed {fmtDuration(run.elapsed_seconds)}</span>
        {!terminal && run.seconds_since_heartbeat != null && (
          <span className={`pp-hb pp-hb-${tone}`}>
            Worker active {run.seconds_since_heartbeat}s ago
          </span>
        )}
      </div>

      {run.health_message && (
        <div className={`pp-health pp-health-${tone}`} role="status">
          {run.health_message}
        </div>
      )}
      {run.state === "failed" && run.error_message && (
        <div className="pp-health pp-health-error" role="alert">
          {run.error_message}
        </div>
      )}
      {run.warnings?.map((w, i) => (
        <div className="pp-health pp-health-warn" key={i}>
          {w.message}
        </div>
      ))}

      {plan.length > 0 && (
        <ol className="pp-timeline">
          {plan
            .filter((s) => s !== "completed")
            .map((stage) => {
              const idx = plan.indexOf(stage);
              const done = terminal
                ? run.state !== "failed" && run.state !== "cancelled"
                  ? true
                  : idx < currentIdx
                : currentIdx >= 0 && idx < currentIdx;
              const active = !terminal && stage === run.stage;
              return (
                <li
                  key={stage}
                  className={`pp-step ${done ? "done" : ""} ${active ? "active" : ""}`}
                >
                  <span className="pp-mark">{done ? "✓" : active ? "●" : "○"}</span>
                  <span className="pp-step-label">{api.stageLabel(stage)}</span>
                  {active && run.elapsed_seconds != null && (
                    <span className="muted pp-step-time">
                      {fmtDuration(run.elapsed_seconds)}
                    </span>
                  )}
                </li>
              );
            })}
        </ol>
      )}

      <div className="pp-actions">
        {run.cancellable && onCancel && (
          <button className="pp-btn" onClick={onCancel}>
            Cancel
          </button>
        )}
        {failedOrStalled && onRetry && (
          <button className="pp-btn" onClick={onRetry}>
            ↻ Retry analysis
          </button>
        )}
        {(run.state === "completed" || run.state === "completed_with_warnings") &&
          onRerun && (
            <button className="pp-btn" onClick={onRerun}>
              ↻ Re-run terrain analysis
            </button>
          )}
      </div>

      {run.log && run.log.length > 0 && (
        <div className="pp-log">
          <button className="pp-log-toggle" onClick={() => setLogOpen((o) => !o)}>
            {logOpen ? "▾" : "▸"} Technical log ({run.log.length})
          </button>
          {logOpen && (
            <ul className="pp-log-list">
              {run.log.slice(-40).map((l, i) => (
                <li key={i} className={`pp-log-${l.level ?? "info"}`}>
                  {l.msg}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
