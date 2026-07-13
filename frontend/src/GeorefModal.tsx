import { useRef, useState } from "react";
import * as api from "./api";

// Common printed-grid CRSs for Mexico plus manual entry.
const CRS_OPTIONS = [
  { epsg: 32611, label: "UTM 11N (WGS84) — EPSG:32611" },
  { epsg: 32612, label: "UTM 12N (WGS84) — EPSG:32612" },
  { epsg: 32613, label: "UTM 13N (WGS84) — EPSG:32613" },
  { epsg: 32614, label: "UTM 14N (WGS84) — EPSG:32614" },
  { epsg: 32615, label: "UTM 15N (WGS84) — EPSG:32615" },
  { epsg: 32616, label: "UTM 16N (WGS84) — EPSG:32616" },
];

interface Props {
  meta: api.MapMeta;
  onMeta: (m: api.MapMeta) => void;
  onApply: (georef: api.GeorefResult, meta: api.MapMeta) => void;
  onClose: () => void;
}

export default function GeorefModal({ meta, onMeta, onApply, onClose }: Props) {
  const imgRef = useRef<HTMLImageElement>(null);
  const [epsg, setEpsg] = useState(32613);
  const [customEpsg, setCustomEpsg] = useState("");
  const [useCustom, setUseCustom] = useState(false);
  const [points, setPoints] = useState<api.ControlPoint[]>([]);
  const [pending, setPending] = useState<{ px: number; py: number } | null>(null);
  const [eInput, setEInput] = useState("");
  const [nInput, setNInput] = useState("");
  const [result, setResult] = useState<api.GeorefResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [imgKey, setImgKey] = useState(0);

  const effectiveEpsg = useCustom ? parseInt(customEpsg, 10) : epsg;

  const onImageClick = (e: React.MouseEvent<HTMLImageElement>) => {
    const img = imgRef.current;
    if (!img) return;
    const rect = img.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * meta.width;
    const py = ((e.clientY - rect.top) / rect.height) * meta.height;
    setPending({ px: Math.round(px * 10) / 10, py: Math.round(py * 10) / 10 });
    setEInput("");
    setNInput("");
  };

  const addPoint = () => {
    if (!pending) return;
    const e = parseFloat(eInput);
    const n = parseFloat(nInput);
    if (!isFinite(e) || !isFinite(n)) {
      setError("Type the printed easting and northing (meters) for the clicked point");
      return;
    }
    setPoints([...points, { ...pending, e, n }]);
    setPending(null);
    setResult(null);
    setError(null);
  };

  const removePoint = (i: number) => {
    setPoints(points.filter((_, j) => j !== i));
    setResult(null);
  };

  const compute = async () => {
    setError(null);
    if (!isFinite(effectiveEpsg)) {
      setError("Enter a valid EPSG code");
      return;
    }
    try {
      const r = await api.georefMap(meta.map_id, effectiveEpsg, points);
      setResult(r);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const changePage = async (page: number) => {
    try {
      const m = await api.selectMapPage(meta.map_id, page);
      onMeta(m);
      setImgKey((k) => k + 1);
      setPoints([]);
      setPending(null);
      setResult(null);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const markerStyle = (px: number, py: number): React.CSSProperties => ({
    position: "absolute",
    left: `${(px / meta.width) * 100}%`,
    top: `${(py / meta.height) * 100}%`,
  });

  return (
    <div className="modal-backdrop">
      <div className="modal georef">
        <div className="georef-image">
          <div className="georef-image-inner">
            <img
              key={imgKey}
              ref={imgRef}
              src={api.mapImageUrl(meta.map_id, `${meta.page}-${imgKey}`)}
              onClick={onImageClick}
              alt="uploaded map"
            />
            {points.map((p, i) => (
              <div key={i} className="cp-marker" style={markerStyle(p.px, p.py)}>
                {i + 1}
              </div>
            ))}
            {pending && (
              <div className="cp-marker pending" style={markerStyle(pending.px, pending.py)}>
                ?
              </div>
            )}
          </div>
        </div>

        <div className="georef-side">
          <h2>Georeference this map</h2>
          <p>
            1. Pick the CRS printed on the map's grid. 2. Click a recognizable
            point (grid-line intersection) on the image. 3. Type its printed
            easting/northing. Repeat for at least 2 points (3+ recommended).
          </p>

          {meta.page_count > 1 && (
            <label>
              PDF page:{" "}
              <select
                value={meta.page}
                onChange={(e) => changePage(parseInt(e.target.value, 10))}
              >
                {Array.from({ length: meta.page_count }, (_, i) => (
                  <option key={i} value={i + 1}>
                    {i + 1}
                  </option>
                ))}
              </select>
            </label>
          )}

          <label>
            Grid CRS:{" "}
            <select
              disabled={useCustom}
              value={epsg}
              onChange={(e) => setEpsg(parseInt(e.target.value, 10))}
            >
              {CRS_OPTIONS.map((o) => (
                <option key={o.epsg} value={o.epsg}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="custom-epsg">
            <input
              type="checkbox"
              checked={useCustom}
              onChange={(e) => setUseCustom(e.target.checked)}
            />
            Other EPSG:
            <input
              type="text"
              placeholder="e.g. 6371"
              value={customEpsg}
              disabled={!useCustom}
              onChange={(e) => setCustomEpsg(e.target.value)}
              style={{ width: 70 }}
            />
          </label>

          {pending && (
            <div className="pending-form">
              <b>
                Clicked pixel ({pending.px.toFixed(0)}, {pending.py.toFixed(0)})
              </b>
              <input
                type="text"
                placeholder="Easting (m)"
                value={eInput}
                onChange={(e) => setEInput(e.target.value)}
              />
              <input
                type="text"
                placeholder="Northing (m)"
                value={nInput}
                onChange={(e) => setNInput(e.target.value)}
              />
              <button onClick={addPoint}>Add control point</button>
            </div>
          )}

          <ul className="cp-list">
            {points.map((p, i) => (
              <li key={i}>
                #{i + 1} px({p.px.toFixed(0)}, {p.py.toFixed(0)}) → E {p.e} / N {p.n}{" "}
                <button onClick={() => removePoint(i)}>✕</button>
              </li>
            ))}
          </ul>

          <button disabled={points.length < 2} onClick={compute}>
            Compute fit ({points.length} point{points.length === 1 ? "" : "s"} —{" "}
            {points.length === 2 ? "similarity" : "affine"})
          </button>

          {result && (
            <div className={`rms ${result.rms_m > 5 ? "bad" : "good"}`}>
              RMS error: {result.rms_m} m
              {result.rms_m > 5 &&
                " — poor fit: re-click your points or check the coordinates"}
            </div>
          )}
          {error && <div className="rms bad">{error}</div>}

          <div className="georef-actions">
            <button
              disabled={!result}
              className="primary"
              onClick={() => result && onApply(result, meta)}
            >
              Apply overlay to map
            </button>
            <button onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}
