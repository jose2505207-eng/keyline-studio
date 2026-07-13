import { useCallback, useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import { TerraDraw, TerraDrawPolygonMode } from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import * as api from "./api";

const COLORS = {
  valley: "#2b6bd6",
  ridge: "#9c6b3c",
  keyline: "#12841f",
  keypoint: "#12841f",
};

const RESULT_LAYER_IDS = ["hillshade", "valleys", "ridges", "keylines", "keypoints"];

type LayerKey = (typeof RESULT_LAYER_IDS)[number];

export default function App() {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const drawRef = useRef<TerraDraw | null>(null);
  const resultsRef = useRef<api.FeatureCollection | null>(null);
  const pollRef = useRef<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const projectIdRef = useRef<string | null>(null);

  const [projectId, setProjectId] = useState<string | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [droneName, setDroneName] = useState<string | null>(null);
  const [jobState, setJobState] = useState<string>("");
  const [jobLog, setJobLog] = useState<string[]>([]);
  const [hasResults, setHasResults] = useState(false);
  const [busy, setBusy] = useState(false);
  const [visible, setVisible] = useState<Record<LayerKey, boolean>>({
    hillshade: true,
    valleys: true,
    ridges: true,
    keylines: true,
    keypoints: true,
  });

  projectIdRef.current = projectId;

  // ---------------------------------------------------------------- map init
  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            maxzoom: 19,
            attribution: "&copy; OpenStreetMap contributors",
          },
        },
        layers: [{ id: "osm", type: "raster", source: "osm" }],
      },
      center: [6.35, 44.45], // southern French Alps — plenty of relief to try
      zoom: 11,
      attributionControl: { compact: false },
    });
    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.addControl(new maplibregl.ScaleControl());
    mapRef.current = map;

    // terra-draw registers its own map sources, which must wait until the
    // style has finished loading (in production builds the style is still
    // loading when this effect runs; starting early throws and blanks the app).
    map.once("load", () => {
      const draw = new TerraDraw({
        adapter: new TerraDrawMapLibreGLAdapter({ map }),
        modes: [new TerraDrawPolygonMode()],
      });
      draw.start();
      draw.setMode("static");
      drawRef.current = draw;

      draw.on("finish", (id) => {
        const feat = draw.getSnapshot().find((f) => f.id === id);
        draw.setMode("static");
        setDrawing(false);
        if (feat && feat.geometry.type === "Polygon") {
          void handleAoiDrawn(feat.geometry as GeoJSON.Polygon);
        }
      });
    });

    setupKeypointInteractions(map);

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ----------------------------------------------------------- AOI / project
  const handleAoiDrawn = useCallback(async (aoi: GeoJSON.Polygon) => {
    clearResults();
    setDroneName(null);
    setJobState("");
    setJobLog([]);
    try {
      const pid = await api.createProject(
        `AOI ${new Date().toISOString().slice(0, 16)}`,
        aoi
      );
      setProjectId(pid);
    } catch (err) {
      setJobState(`error:${(err as Error).message}`);
    }
  }, []);

  const startDrawing = () => {
    const draw = drawRef.current;
    if (!draw) return;
    draw.clear(); // one AOI at a time
    setProjectId(null);
    clearResults();
    draw.setMode("polygon");
    setDrawing(true);
  };

  // ------------------------------------------------------------- drone DEM
  const onDroneFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !projectId) return;
    setBusy(true);
    try {
      await api.uploadDroneDem(projectId, file);
      setDroneName(file.name);
      setJobState("");
    } catch (err) {
      setJobState(`error:${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // --------------------------------------------------------------- analysis
  const analyze = async () => {
    if (!projectId) return;
    clearResults();
    setBusy(true);
    setJobLog([]);
    try {
      await api.startAnalysis(projectId);
      setJobState("queued");
      pollRef.current = window.setInterval(async () => {
        try {
          const st = await api.getStatus(projectId);
          setJobState(st.state);
          setJobLog(st.log.map((l) => l.msg));
          if (st.state === "done" || st.state.startsWith("error:")) {
            stopPolling();
            setBusy(false);
            if (st.state === "done") await loadResults(projectId);
          }
        } catch {
          /* transient poll failure — keep polling */
        }
      }, 2000);
    } catch (err) {
      setJobState(`error:${(err as Error).message}`);
      setBusy(false);
    }
  };

  const stopPolling = () => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };
  useEffect(() => stopPolling, []);

  // ---------------------------------------------------------------- results
  const loadResults = async (pid: string) => {
    const map = mapRef.current;
    if (!map) return;
    const [fc, hs] = await Promise.all([api.getResults(pid), api.getHillshade(pid)]);
    resultsRef.current = fc;

    map.addSource("hillshade", {
      type: "image",
      url: hs.url,
      coordinates: hs.coordinates as [
        [number, number],
        [number, number],
        [number, number],
        [number, number]
      ],
    });
    map.addLayer({
      id: "hillshade",
      type: "raster",
      source: "hillshade",
      paint: { "raster-opacity": 0.55 },
    });

    map.addSource("results", { type: "geojson", data: fc });
    map.addLayer({
      id: "valleys",
      type: "line",
      source: "results",
      filter: ["==", ["get", "kind"], "valley"],
      paint: { "line-color": COLORS.valley, "line-width": 2 },
    });
    map.addLayer({
      id: "ridges",
      type: "line",
      source: "results",
      filter: ["==", ["get", "kind"], "ridge"],
      paint: { "line-color": COLORS.ridge, "line-width": 2 },
    });
    map.addLayer({
      id: "keylines",
      type: "line",
      source: "results",
      filter: ["==", ["get", "kind"], "keyline"],
      paint: {
        "line-color": COLORS.keyline,
        "line-width": 4,
        "line-opacity": 0.9,
      },
    });
    // Keypoints: solid circle when drone-derived, hollow when satellite;
    // size and opacity scale with confidence.
    const conf = ["coalesce", ["get", "confidence"], 0.5] as unknown as number;
    map.addLayer({
      id: "keypoints",
      type: "circle",
      source: "results",
      filter: ["==", ["get", "kind"], "keypoint"],
      paint: {
        "circle-radius": ["+", 4, ["*", 7, conf]] as never,
        "circle-color": COLORS.keypoint,
        "circle-opacity": [
          "case",
          ["==", ["get", "source"], "drone"],
          ["+", 0.35, ["*", 0.65, conf]],
          0,
        ] as never,
        "circle-stroke-color": COLORS.keypoint,
        "circle-stroke-width": 2.5,
        "circle-stroke-opacity": ["+", 0.35, ["*", 0.65, conf]] as never,
      },
    });

    for (const id of RESULT_LAYER_IDS) {
      map.setLayoutProperty(id, "visibility", visible[id] ? "visible" : "none");
    }
    setHasResults(true);
  };

  const clearResults = () => {
    stopPolling();
    const map = mapRef.current;
    resultsRef.current = null;
    setHasResults(false);
    if (!map) return;
    for (const id of RESULT_LAYER_IDS) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    if (map.getSource("results")) map.removeSource("results");
    if (map.getSource("hillshade")) map.removeSource("hillshade");
  };

  // -------------------------------------------------- keypoint drag + popup
  const setupKeypointInteractions = (map: maplibregl.Map) => {
    map.on("mouseenter", "keypoints", () => {
      map.getCanvas().style.cursor = "grab";
    });
    map.on("mouseleave", "keypoints", () => {
      map.getCanvas().style.cursor = "";
    });

    map.on("click", "keypoints", (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties as Record<string, unknown>;
      new maplibregl.Popup({ closeButton: true })
        .setLngLat(e.lngLat)
        .setHTML(
          `<b>Keypoint ${p.id}</b><br/>` +
            `Elevation: ${p.elevation} m<br/>` +
            `Confidence: ${p.confidence}<br/>` +
            `Source: ${p.source}<br/>` +
            `<i>Drag to move</i>`
        )
        .addTo(map);
    });

    map.on("mousedown", "keypoints", (e) => {
      const f = e.features?.[0];
      if (!f) return;
      e.preventDefault(); // keep the map from panning
      const kid = (f.properties as { id: string }).id;
      map.getCanvas().style.cursor = "grabbing";

      const onMove = (ev: maplibregl.MapMouseEvent) => {
        patchKeypointGeometry(kid, ev.lngLat.lng, ev.lngLat.lat);
      };
      const onUp = async (ev: maplibregl.MapMouseEvent) => {
        map.off("mousemove", onMove);
        map.getCanvas().style.cursor = "";
        const pid = projectIdRef.current;
        if (!pid) return;
        try {
          const res = await api.moveKeypoint(pid, kid, ev.lngLat.lng, ev.lngLat.lat);
          applyMoveResult(kid, res);
        } catch (err) {
          setJobState(`error:${(err as Error).message}`);
        }
      };
      map.on("mousemove", onMove);
      map.once("mouseup", onUp);
    });
  };

  const refreshResultsSource = () => {
    const map = mapRef.current;
    const fc = resultsRef.current;
    if (!map || !fc) return;
    const src = map.getSource("results") as maplibregl.GeoJSONSource | undefined;
    src?.setData(fc as GeoJSON.GeoJSON);
  };

  const patchKeypointGeometry = (kid: string, lng: number, lat: number) => {
    const fc = resultsRef.current;
    if (!fc) return;
    for (const f of fc.features) {
      const p = f.properties as Record<string, unknown> | null;
      if (p?.kind === "keypoint" && p?.id === kid) {
        f.geometry = { type: "Point", coordinates: [lng, lat] };
      }
    }
    refreshResultsSource();
  };

  const applyMoveResult = (kid: string, res: api.MoveResult) => {
    const fc = resultsRef.current;
    if (!fc) return;
    fc.features = fc.features.filter((f) => {
      const p = f.properties as Record<string, unknown> | null;
      if (p?.kind === "keypoint" && p?.id === kid) return false;
      if (p?.kind === "keyline" && p?.keypoint_id === kid) return false;
      return true;
    });
    fc.features.push(res.keypoint, ...res.keylines);
    refreshResultsSource();
  };

  // ----------------------------------------------------------------- export
  const exportGeoJSON = () => {
    const fc = resultsRef.current;
    if (!fc) return;
    const blob = new Blob([JSON.stringify(fc, null, 2)], {
      type: "application/geo+json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "keyline-results.geojson";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // ------------------------------------------------------------------ layers
  const toggleLayer = (id: LayerKey) => {
    const next = { ...visible, [id]: !visible[id] };
    setVisible(next);
    const map = mapRef.current;
    if (map?.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", next[id] ? "visible" : "none");
    }
  };

  const running =
    jobState === "queued" || jobState.startsWith("running") ? jobState : null;
  const error = jobState.startsWith("error:") ? jobState.slice(6) : null;

  return (
    <div className="app">
      <div ref={mapContainer} className="map" />

      <div className="toolbar">
        <h1>Keyline Studio</h1>
        <button className={drawing ? "active" : ""} onClick={startDrawing}>
          ▰ Draw AOI {drawing ? "(click map, click first point to finish)" : ""}
        </button>
        <button
          disabled={!projectId || busy}
          onClick={() => fileInputRef.current?.click()}
        >
          ⛰ Upload drone DEM {droneName ? `✓ ${droneName}` : "(optional)"}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".tif,.tiff,image/tiff"
          style={{ display: "none" }}
          onChange={onDroneFile}
        />
        <button disabled={!projectId || busy} onClick={analyze}>
          ▶ Analyze
        </button>
        <button disabled={!hasResults} onClick={exportGeoJSON}>
          ⇩ Export GeoJSON
        </button>

        {(running || error || jobState === "done") && (
          <div className="status">
            <div className={`state ${error ? "error" : ""}`}>
              {error
                ? `Error: ${error}`
                : jobState === "done"
                ? "Analysis complete"
                : `⏳ ${jobState.replace("running:", "")}`}
            </div>
            {jobLog.length > 0 && (
              <ul className="log">
                {jobLog.slice(-6).map((l, i) => (
                  <li key={i}>{l}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {hasResults && (
          <div className="layers">
            <label>
              <input
                type="checkbox"
                checked={visible.hillshade}
                onChange={() => toggleLayer("hillshade")}
              />
              <span className="swatch" style={{ background: "#888" }} />
              Hillshade
            </label>
            <label>
              <input
                type="checkbox"
                checked={visible.valleys}
                onChange={() => toggleLayer("valleys")}
              />
              <span className="swatch" style={{ background: COLORS.valley }} />
              Valleys
            </label>
            <label>
              <input
                type="checkbox"
                checked={visible.ridges}
                onChange={() => toggleLayer("ridges")}
              />
              <span className="swatch" style={{ background: COLORS.ridge }} />
              Ridges
            </label>
            <label>
              <input
                type="checkbox"
                checked={visible.keylines}
                onChange={() => toggleLayer("keylines")}
              />
              <span className="swatch" style={{ background: COLORS.keyline }} />
              Keylines
            </label>
            <label>
              <input
                type="checkbox"
                checked={visible.keypoints}
                onChange={() => toggleLayer("keypoints")}
              />
              <span
                className="swatch"
                style={{ background: COLORS.keypoint, height: 10, width: 10, borderRadius: 5 }}
              />
              Keypoints (solid = drone, hollow = satellite)
            </label>
          </div>
        )}
      </div>

      <div className="footer">
        Elevation data: Copernicus GLO-30 © DLR/Airbus, provided by the European
        Union and ESA. Candidate keypoints are computational suggestions — field
        verification required before any earthworks.
      </div>
    </div>
  );
}
