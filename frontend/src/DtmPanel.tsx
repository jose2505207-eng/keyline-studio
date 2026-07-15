import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";

const SOURCE_LABEL: Record<string, string> = {
  upload: "Uploaded",
  survey: "Generated from drone survey",
  imported_path: "Imported from server path",
  external_path: "Server path (in place)",
};

function fmtBytes(b: number | null): string {
  if (b == null) return "";
  if (b > 1 << 30) return `${(b / (1 << 30)).toFixed(2)} GB`;
  if (b > 1 << 20) return `${(b / (1 << 20)).toFixed(1)} MB`;
  return `${Math.ceil(b / 1024)} KB`;
}

function fmtDate(t: number): string {
  return new Date(t * 1000).toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

interface Props {
  projectId: string | null;
  analyzeDisabledReason?: string | null; // non-DTM reasons (no AOI, busy...)
  onAnalyze: (dtmId: string) => void;
}

export default function DtmPanel({ projectId, analyzeDisabledReason, onAnalyze }: Props) {
  const [dtms, setDtms] = useState<api.Dtm[] | null>(null); // null = loading
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState<{ name: string; pct: number } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [serverPath, setServerPath] = useState("");
  const [pathResult, setPathResult] = useState<api.DtmPathValidation | null>(null);
  const [copyToLibrary, setCopyToLibrary] = useState(true);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const items = await api.listDtms();
      setDtms(items);
      // drop a selection whose record vanished or went missing
      setSelectedId((cur) => {
        const found = cur ? items.find((d) => d.id === cur) : undefined;
        return found && found.status === "ready" ? cur : null;
      });
    } catch (err) {
      setDtms([]);
      setLoadError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const selected = dtms?.find((d) => d.id === selectedId) ?? null;

  // ------------------------------------------------------------- upload
  const onFilePicked = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // same file can be re-picked after an error
    if (!file) return;
    setUploadError(null);
    if (!/\.tiff?$/i.test(file.name)) {
      setUploadError(`"${file.name}" is not a .tif/.tiff GeoTIFF`);
      return;
    }
    setUploading({ name: file.name, pct: 0 });
    try {
      const dtm = await api.uploadDtmWithProgress(file, projectId, (pct) =>
        setUploading({ name: file.name, pct }));
      await refresh();
      setSelectedId(dtm.id); // auto-select the fresh upload
    } catch (err) {
      setUploadError((err as Error).message);
    } finally {
      setUploading(null);
    }
  };

  // ------------------------------------------------------ custom server path
  const validatePath = async () => {
    setPathResult(null);
    try {
      setPathResult(await api.validateDtmPath(serverPath));
    } catch (err) {
      setPathResult({ valid: false, reason: (err as Error).message, metadata: null });
    }
  };

  const importPath = async () => {
    setImporting(true);
    try {
      const dtm = await api.importDtmPath(serverPath, copyToLibrary, projectId);
      await refresh();
      setSelectedId(dtm.id);
      setPathResult(null);
      setServerPath("");
    } catch (err) {
      setPathResult({ valid: false, reason: (err as Error).message, metadata: null });
    } finally {
      setImporting(false);
    }
  };

  // ------------------------------------------------------------- analyze gate
  const dtmReason = !selected
    ? "Select or upload a valid DTM before analyzing."
    : selected.status !== "ready"
      ? "The selected DTM file is missing on the server — pick another."
      : null;
  const disabledReason = analyzeDisabledReason ?? dtmReason;

  return (
    <div className="dtm-panel">
      <div className="dtm-head">
        <b>Choose an existing DTM</b>
        <button type="button" className="linkish" onClick={() => void refresh()}>
          ⟳ Refresh
        </button>
      </div>

      {dtms === null && <div className="muted">Loading DTM library…</div>}
      {loadError && <div className="warn">✗ {loadError}</div>}

      {dtms !== null && dtms.length === 0 && !loadError && (
        <div className="muted dtm-empty">
          No saved DTMs yet. Upload a GeoTIFF or generate one from a drone
          survey.
        </div>
      )}

      {dtms !== null && dtms.length > 0 && (
        <select
          aria-label="Saved DTMs"
          value={selectedId ?? ""}
          onChange={(e) => setSelectedId(e.target.value || null)}
        >
          <option value="">— choose saved DTM —</option>
          {dtms.map((d) => (
            <option key={d.id} value={d.id} disabled={d.status !== "ready"}>
              {d.display_name}
              {d.size_bytes != null ? ` · ${fmtBytes(d.size_bytes)}` : ""}
              {d.status !== "ready" ? " · (file missing)" : ""}
            </option>
          ))}
        </select>
      )}

      {selected && (
        <div className="dtm-selected" data-testid="dtm-selected">
          <b>Selected: {selected.display_name}</b>
          <div className="muted">
            {[
              fmtBytes(selected.size_bytes),
              selected.crs ?? undefined,
              selected.width && selected.height
                ? `${selected.width}×${selected.height}px`
                : undefined,
              selected.resolution_m?.[0] != null
                ? `${selected.resolution_m[0]} m/px`
                : undefined,
              `${SOURCE_LABEL[selected.source_type] ?? selected.source_type} ${fmtDate(selected.created_at)}`,
            ]
              .filter(Boolean)
              .join(" · ")}
          </div>
          <div className={selected.status === "ready" ? "dtm-ready" : "warn"}>
            Status: {selected.status === "ready" ? "Ready" : selected.status}
          </div>
        </div>
      )}

      {dtms?.some((d) => d.status === "missing") && (
        <div className="warn">
          ⚠ Some DTMs reference files that no longer exist on the server;
          they cannot be analyzed.
        </div>
      )}

      <button
        type="button"
        onClick={() => fileRef.current?.click()}
        disabled={uploading !== null}
      >
        ⤒ Upload new DTM
        {uploading ? ` — ${uploading.name} (${uploading.pct.toFixed(0)}%)` : ""}
      </button>
      <input
        ref={fileRef}
        type="file"
        accept=".tif,.tiff,image/tiff,image/geotiff"
        style={{ display: "none" }}
        data-testid="dtm-file-input"
        onChange={onFilePicked}
      />
      {uploadError && <div className="warn">✗ {uploadError}</div>}

      <button
        type="button"
        className="linkish"
        aria-expanded={advancedOpen}
        onClick={() => setAdvancedOpen((o) => !o)}
      >
        {advancedOpen ? "▾" : "▸"} Advanced: use a custom server filepath
      </button>
      {advancedOpen && (
        <div className="dtm-advanced">
          <div className="muted">
            This path must already exist on the Keyline server. Browser-local
            paths cannot be accessed directly.
          </div>
          <input
            type="text"
            placeholder="/data/imports/my-dtm.tif"
            value={serverPath}
            onChange={(e) => {
              setServerPath(e.target.value);
              setPathResult(null);
            }}
          />
          <label className="dtm-copy">
            <input
              type="checkbox"
              checked={copyToLibrary}
              onChange={(e) => setCopyToLibrary(e.target.checked)}
            />
            Import into DTM library (copy the file)
          </label>
          <div className="dtm-path-actions">
            <button type="button" disabled={!serverPath.trim()} onClick={validatePath}>
              Validate path
            </button>
            <button
              type="button"
              disabled={!pathResult?.valid || importing}
              onClick={importPath}
            >
              {importing ? "Importing…" : copyToLibrary ? "Import" : "Use in place"}
            </button>
          </div>
          {pathResult && (
            <div className={pathResult.valid ? "dtm-ready" : "warn"}>
              {pathResult.valid && pathResult.metadata
                ? `✓ ${pathResult.metadata.filename} · ${fmtBytes(pathResult.metadata.size_bytes)} · ${pathResult.metadata.crs} · ${pathResult.metadata.width}×${pathResult.metadata.height}px`
                : `✗ ${pathResult.reason}`}
            </div>
          )}
        </div>
      )}

      <button
        type="button"
        className="primary"
        disabled={disabledReason !== null}
        onClick={() => selected && onAnalyze(selected.id)}
      >
        ▶ Analyze with selected DTM
      </button>
      {disabledReason && <div className="muted">{disabledReason}</div>}
    </div>
  );
}
