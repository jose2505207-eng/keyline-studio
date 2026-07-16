import { useCallback, useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import { TerraDraw, TerraDrawPolygonMode } from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import * as api from "./api";
import AnalysisProgressPanel from "./AnalysisProgressPanel";
import DroneSurveyPanel from "./DroneSurveyPanel";
import DtmPanel from "./DtmPanel";
import GeorefModal from "./GeorefModal";
import { resolveProject } from "./projectFlow";

const STORAGE_KEY = "keyline.active";

const TERMINAL_RUN_STATES = [
  "completed",
  "completed_with_warnings",
  "failed",
  "cancelled",
];
// Stages whose underlying tool can legitimately run for minutes with no new
// sub-step; poll these less aggressively.
const SLOW_STAGES = [
  "fetching_satellite_dem",
  "reprojecting_satellite_dem",
  "preparing_drone_dem",
  "fusing_dem",
  "conditioning_dem",
  "calculating_flow_accumulation",
  "generating_exports",
];

interface PersistedSession {
  projectId: string | null;
  surveyId: string | null;
  aoi: GeoJSON.Polygon | null;
  dtmId?: string | null;
  runId?: string | null;
}

function loadSession(): PersistedSession {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as PersistedSession;
  } catch {
    /* corrupted storage — start fresh */
  }
  return { projectId: null, surveyId: null, aoi: null, dtmId: null };
}

const COLORS = {
  valley: "#3b82f6",
  ridge: "#b4713d",
  keyline: "#22e04a",
  keypoint: "#22e04a",
  aoi: "#ffd000",
};

const MAX_AOI_KM2 = 100;

const RESULT_LAYER_IDS = ["hillshade", "contours", "valleys", "ridges", "keylines", "keypoints"];

type LayerKey = (typeof RESULT_LAYER_IDS)[number];
type Basemap = "satellite" | "streets";

interface SearchResult {
  display_name: string;
  lat: string;
  lon: string;
}

function polygonBbox(poly: GeoJSON.Polygon): [number, number, number, number] {
  const xs = poly.coordinates[0].map((c) => c[0]);
  const ys = poly.coordinates[0].map((c) => c[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

/** Do two WGS84 bboxes [w,s,e,n] overlap at all? Used to decide whether a
 * previously-drawn AOI belongs to the DTM the user is now analyzing. */
function bboxesOverlap(
  a: [number, number, number, number],
  b: [number, number, number, number]
): boolean {
  return a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1];
}

/** Spherical polygon area (same approach as turf.area), in km². */
function ringAreaKm2(ring: [number, number][]): number {
  if (ring.length < 3) return 0;
  const R = 6371008.8;
  const rad = Math.PI / 180;
  let total = 0;
  for (let i = 0; i < ring.length; i++) {
    const [l1, p1] = ring[i];
    const [l2, p2] = ring[(i + 1) % ring.length];
    total += (l2 - l1) * rad * (2 + Math.sin(p1 * rad) + Math.sin(p2 * rad));
  }
  return Math.abs((total * R * R) / 2) / 1e6;
}

export default function App() {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const drawRef = useRef<TerraDraw | null>(null);
  const resultsRef = useRef<api.FeatureCollection | null>(null);
  const pollRef = useRef<number | null>(null);
  const projectIdRef = useRef<string | null>(null);
  const lastGeocodeRef = useRef(0);
  const lastDtmRef = useRef<api.Dtm | null>(null);

  const [projectId, setProjectId] = useState<string | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [droneName, setDroneName] = useState<string | null>(null);
  const [jobState, setJobState] = useState<string>("");
  const [jobLog, setJobLog] = useState<string[]>([]);
  const [hasResults, setHasResults] = useState(false);
  const [busy, setBusy] = useState(false);
  const [basemap, setBasemap] = useState<Basemap>("satellite");
  const [areaKm2, setAreaKm2] = useState<number | null>(null);
  const [searchQ, setSearchQ] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [warning, setWarning] = useState<string | null>(null);
  const [droneInfo, setDroneInfo] = useState<api.DroneInfo | null>(null);
  const [isDsm, setIsDsm] = useState(false);
  const [terrainSource, setTerrainSource] = useState<"drone" | "dtm" | "satellite">("satellite");
  const [dtmId, setDtmId] = useState<string | null>(null);
  const [exportsAvail, setExportsAvail] = useState<api.ExportAvailability | null>(null);
  const [surveyId, setSurveyId] = useState<string | null>(null);
  const [orthoInfo, setOrthoInfo] = useState<api.OrthophotoInfo | null>(null);
  const [orthoVisible, setOrthoVisible] = useState(true);
  const [orthoOpacity, setOrthoOpacity] = useState(0.9);
  const [resultsProps, setResultsProps] = useState<api.ResultsProperties | null>(null);
  const [runs, setRuns] = useState<api.AnalysisRun[]>([]);
  const [rerunning, setRerunning] = useState(false);
  const rerunTimer = useRef<number | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<api.AnalysisRun | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  activeRunIdRef.current = activeRunId;
  const [exportOpen, setExportOpen] = useState(false);
  const [mapMeta, setMapMeta] = useState<api.MapMeta | null>(null);
  const [scanOverlay, setScanOverlay] = useState<{
    mapId: string;
    corners: [number, number][];
    rms: number;
  } | null>(null);
  const [scanOpacity, setScanOpacity] = useState(0.7);
  const importInputRef = useRef<HTMLInputElement>(null);
  const scanInputRef = useRef<HTMLInputElement>(null);
  const [visible, setVisible] = useState<Record<LayerKey, boolean>>({
    hillshade: true,
    contours: true,
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
          esri: {
            type: "raster",
            tiles: [
              "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            ],
            tileSize: 256,
            maxzoom: 19,
            attribution:
              "Imagery © Esri, Maxar, Earthstar Geographics, and the GIS User Community",
          },
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            maxzoom: 19,
            attribution: "&copy; OpenStreetMap contributors",
          },
        },
        layers: [
          { id: "basemap-esri", type: "raster", source: "esri" },
          {
            id: "basemap-osm",
            type: "raster",
            source: "osm",
            layout: { visibility: "none" },
          },
        ],
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
      // The drawn AOI is rendered by our own layers, not terra-draw: on
      // finish we copy the polygon here and wipe the terra-draw store, so no
      // ghost vertex/cursor elements from the draw mode can linger.
      map.addSource("aoi", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "aoi-fill",
        type: "fill",
        source: "aoi",
        paint: { "fill-color": COLORS.aoi, "fill-opacity": 0.07 },
      });
      map.addLayer({
        id: "aoi-line",
        type: "line",
        source: "aoi",
        paint: {
          "line-color": COLORS.aoi,
          "line-width": 2,
          "line-dasharray": [2, 1.5],
        },
      });
      map.addSource("drone-fp", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "drone-fp",
        type: "line",
        source: "drone-fp",
        paint: { "line-color": "#ff7b00", "line-width": 2.5 },
      });

      const draw = new TerraDraw({
        adapter: new TerraDrawMapLibreGLAdapter({ map }),
        modes: [new TerraDrawPolygonMode()],
      });
      draw.start();
      draw.setMode("static");
      drawRef.current = draw;

      // Live area readout while the polygon is being drawn.
      draw.on("change", () => {
        const feat = draw
          .getSnapshot()
          .find((f) => f.geometry.type === "Polygon");
        if (feat) {
          const ring = (feat.geometry as GeoJSON.Polygon)
            .coordinates[0] as [number, number][];
          setAreaKm2(ringAreaKm2(ring));
        }
      });

      draw.on("finish", (id) => {
        const feat = draw.getSnapshot().find((f) => f.id === id);
        draw.setMode("static");
        setDrawing(false);
        map.getCanvas().style.cursor = ""; // draw modes set crosshair et al.
        if (feat && feat.geometry.type === "Polygon") {
          const poly = feat.geometry as GeoJSON.Polygon;
          setAoiOnMap(poly);
          setAreaKm2(ringAreaKm2(poly.coordinates[0] as [number, number][]));
          void handleAoiDrawn(poly);
        }
        // Defer: clearing the store inside its own event handler is unsafe.
        setTimeout(() => draw.clear(), 0);
      });
    });

    setupKeypointInteractions(map);

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -------------------------------------------- session persistence/recovery
  const aoiRef = useRef<GeoJSON.Polygon | null>(null);

  useEffect(() => {
    const session = loadSession();
    if (!session.projectId) return;
    setProjectId(session.projectId);
    setSurveyId(session.surveyId);
    if (session.surveyId) setTerrainSource("drone");
    if (session.dtmId) {
      setDtmId(session.dtmId);
      setTerrainSource("dtm");
    }
    aoiRef.current = session.aoi;
    // resume the live progress monitor after a browser refresh
    if (session.runId) setActiveRunId(session.runId);
    const map = mapRef.current;
    const restore = () => {
      if (session.aoi) {
        setAoiOnMap(session.aoi);
        setAreaKm2(ringAreaKm2(session.aoi.coordinates[0] as [number, number][]));
        flyToPolygon(session.aoi);
      }
      // restore finished results if the backend still has them
      void (async () => {
        try {
          await loadOrthophoto(session.projectId!);
          await loadResults(session.projectId!);
        } catch {
          /* no results yet — fine */
        }
      })();
    };
    if (map?.isStyleLoaded()) restore();
    else map?.once("load", restore);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      projectId, surveyId, aoi: aoiRef.current, dtmId, runId: activeRunId,
    } satisfies PersistedSession));
  }, [projectId, surveyId, dtmId, activeRunId]);

  // -------- canonical analysis-run monitor (survives refresh) --------------
  // The run is the single source of truth: poll it with an adaptive cadence,
  // back off on network errors, pause while the tab is hidden, resume on
  // visibility, and stop at a terminal state. Never polls a survey id.
  useEffect(() => {
    const pid = projectId;
    const rid = activeRunId;
    if (!pid || !rid) return;
    let cancelled = false;
    let timer = 0;
    let failures = 0;

    const schedule = (ms: number) => {
      timer = window.setTimeout(tick, ms);
    };
    const tick = async () => {
      if (document.hidden) {
        schedule(3000);
        return;
      }
      try {
        const run = await api.getAnalysisRun(pid, rid);
        if (cancelled) return;
        failures = 0;
        setActiveRun(run);
        if (TERMINAL_RUN_STATES.includes(run.state)) {
          setBusy(false);
          setRerunning(false);
          if (run.state === "completed" || run.state === "completed_with_warnings") {
            setJobState("done");
            try {
              await loadOrthophoto(pid);
            } catch {
              /* optional */
            }
            await loadResults(pid).catch(() => undefined);
            api.getAnalysisRuns(pid).then(setRuns).catch(() => setRuns([]));
            api.getExportAvailability(pid).then(setExportsAvail).catch(() => undefined);
          } else if (run.state === "failed") {
            setJobState(`error:${run.error_message ?? "analysis failed"}`);
          } else if (run.state === "cancelled") {
            setJobState("");
          }
          return; // terminal — stop polling
        }
        const slow = SLOW_STAGES.includes(run.stage ?? "");
        schedule(slow ? 5000 : 2000);
      } catch {
        if (cancelled) return;
        failures += 1;
        schedule(Math.min(2000 * 2 ** failures, 15000)); // network backoff
      }
    };
    // poll immediately, then on the adaptive cadence
    tick();
    const onVisible = () => {
      if (!document.hidden) {
        window.clearTimeout(timer);
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRunId]);

  const setAoiOnMap = (poly: GeoJSON.Polygon | null) => {
    const src = mapRef.current?.getSource("aoi") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData({
      type: "FeatureCollection",
      features: poly
        ? [{ type: "Feature", geometry: poly, properties: {} }]
        : [],
    });
  };

  const setDroneFootprint = (poly: GeoJSON.Polygon | null) => {
    const src = mapRef.current?.getSource("drone-fp") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData({
      type: "FeatureCollection",
      features: poly ? [{ type: "Feature", geometry: poly, properties: {} }] : [],
    });
  };

  const flyToPolygon = (poly: GeoJSON.Polygon) => {
    const ring = poly.coordinates[0];
    const lons = ring.map((c) => c[0]);
    const lats = ring.map((c) => c[1]);
    mapRef.current?.fitBounds(
      [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ],
      { padding: 80, duration: 1200 }
    );
  };

  /** Shared AOI intake for imported boundaries and map-extent AOIs. */
  const adoptAoi = async (poly: GeoJSON.Polygon, mapId?: string) => {
    drawRef.current?.clear();
    setAoiOnMap(poly);
    setAreaKm2(ringAreaKm2(poly.coordinates[0] as [number, number][]));
    flyToPolygon(poly);
    await handleAoiDrawn(poly);
    if (mapId) {
      // link the georeferenced scan to the new project (restores overlay later)
      try {
        const pid = projectIdRef.current;
        if (pid) await api.attachMap(pid, mapId);
      } catch {
        /* non-fatal */
      }
    }
  };

  const onImportBoundary = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    try {
      const poly = await api.importBoundary(file);
      await adoptAoi(poly);
      setJobState("");
    } catch (err) {
      setJobState(`error:${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // ------------------------------------------------------- map scan overlay
  const onScanFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    try {
      const meta = await api.uploadMapScan(file);
      setMapMeta(meta); // opens the georeferencing modal
    } catch (err) {
      setJobState(`error:${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const applyScanOverlay = (georef: api.GeorefResult, meta: api.MapMeta) => {
    const map = mapRef.current;
    if (!map) return;
    if (map.getLayer("scan")) map.removeLayer("scan");
    if (map.getSource("scan")) map.removeSource("scan");
    map.addSource("scan", {
      type: "image",
      url: api.mapImageUrl(meta.map_id, meta.page),
      coordinates: georef.corners as [
        [number, number],
        [number, number],
        [number, number],
        [number, number]
      ],
    });
    // beneath the AOI outline (and any result layers added later go on top)
    map.addLayer(
      {
        id: "scan",
        type: "raster",
        source: "scan",
        paint: { "raster-opacity": scanOpacity },
      },
      map.getLayer("aoi-fill") ? "aoi-fill" : undefined
    );
    setScanOverlay({ mapId: meta.map_id, corners: georef.corners, rms: georef.rms_m });
    setMapMeta(null);
    const lons = georef.corners.map((c) => c[0]);
    const lats = georef.corners.map((c) => c[1]);
    map.fitBounds(
      [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ],
      { padding: 60, duration: 1200 }
    );
  };

  const setOverlayOpacity = (v: number) => {
    setScanOpacity(v);
    const map = mapRef.current;
    if (map?.getLayer("scan")) map.setPaintProperty("scan", "raster-opacity", v);
  };

  const removeScanOverlay = () => {
    const map = mapRef.current;
    if (map?.getLayer("scan")) map.removeLayer("scan");
    if (map?.getSource("scan")) map.removeSource("scan");
    setScanOverlay(null);
  };

  const useScanExtentAsAoi = async () => {
    if (!scanOverlay) return;
    const ring = [...scanOverlay.corners, scanOverlay.corners[0]].map(
      (c) => [c[0], c[1]] as [number, number]
    );
    await adoptAoi({ type: "Polygon", coordinates: [ring] }, scanOverlay.mapId);
  };

  // -------------------------------------------------------------- basemap
  const switchBasemap = (next: Basemap) => {
    setBasemap(next);
    const map = mapRef.current;
    if (!map) return;
    map.setLayoutProperty(
      "basemap-esri",
      "visibility",
      next === "satellite" ? "visible" : "none"
    );
    map.setLayoutProperty(
      "basemap-osm",
      "visibility",
      next === "streets" ? "visible" : "none"
    );
  };

  // ------------------------------------------------------------- geocoding
  // Nominatim usage policy: explicit submit only (no per-keystroke
  // autocomplete), at most one request per second; the browser supplies the
  // Referer header identifying this app.
  const doSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = searchQ.trim();
    if (!q) return;
    const now = Date.now();
    if (now - lastGeocodeRef.current < 1000) return; // debounce 1 s
    lastGeocodeRef.current = now;
    try {
      const res = await fetch(
        `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(q)}&format=json&limit=5`,
        { headers: { Accept: "application/json" } }
      );
      setSearchResults(res.ok ? await res.json() : []);
    } catch {
      setSearchResults([]);
    }
  };

  const goToResult = (r: SearchResult) => {
    setSearchResults([]);
    setSearchQ(r.display_name.split(",")[0]);
    mapRef.current?.flyTo({
      center: [parseFloat(r.lon), parseFloat(r.lat)],
      zoom: 13,
    });
  };

  // ----------------------------------------------------------- AOI / project
  const handleAoiDrawn = useCallback(async (aoi: GeoJSON.Polygon) => {
    clearResults();
    removeOrthoLayer();
    setDroneName(null);
    setDroneInfo(null);
    setDroneFootprint(null);
    setIsDsm(false);
    setSurveyId(null);
    aoiRef.current = aoi;
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
    setAoiOnMap(null);
    setAreaKm2(null);
    setProjectId(null);
    clearResults();
    draw.setMode("polygon");
    setDrawing(true);
  };

  // ------------------------------------------------------------- drone DEM

  // --------------------------------------------------------------- analysis

  /** Start analysis on a known-good project and poll to completion. Throws
   * synchronously if the project 404s (caller recreates + retries). */
  const runAnalysisOn = async (
    pid: string,
    options: { dtmId?: string; demMode?: string; terrain?: Record<string, number> }
  ) => {
    // Start the run and hand tracking to the canonical run monitor (which
    // survives refresh). Throws synchronously if the project 404s.
    const { run_id } = await api.startAnalysisRun(pid, options);
    setActiveRun(null);
    setActiveRunId(run_id); // effect begins polling the run immediately
    setJobState("running:queued");
  };

  /** Resolve the analysis area. For a DTM run it is the DTM footprint unless
   * the user has drawn an AOI that actually overlaps the DTM. */
  const resolveAoi = (isDtmRun: boolean): GeoJSON.Polygon | null => {
    const dtm = lastDtmRef.current;
    const footprint: GeoJSON.Polygon | null =
      isDtmRun && dtm?.bbox_wgs84
        ? {
            type: "Polygon",
            coordinates: [
              [
                [dtm.bbox_wgs84[0], dtm.bbox_wgs84[1]],
                [dtm.bbox_wgs84[2], dtm.bbox_wgs84[1]],
                [dtm.bbox_wgs84[2], dtm.bbox_wgs84[3]],
                [dtm.bbox_wgs84[0], dtm.bbox_wgs84[3]],
                [dtm.bbox_wgs84[0], dtm.bbox_wgs84[1]],
              ],
            ],
          }
        : null;
    const drawn = aoiRef.current;
    if (isDtmRun && footprint && dtm?.bbox_wgs84) {
      if (drawn && bboxesOverlap(polygonBbox(drawn), dtm.bbox_wgs84 as
          [number, number, number, number])) {
        return drawn; // a real AOI drawn over this DTM
      }
      // no AOI, or a stale AOI from another parcel -> use the DTM footprint
      aoiRef.current = footprint;
      setAoiOnMap(footprint);
      setAreaKm2(ringAreaKm2(footprint.coordinates[0] as [number, number][]));
      return footprint;
    }
    return drawn;
  };

  /** Validate the stored project, creating a fresh one from the AOI when it
   * is missing or the backend no longer has it (ephemeral-store reset). */
  const ensureProject = async (aoi: GeoJSON.Polygon): Promise<string> => {
    if (projectIdRef.current) setJobState("running:validating project");
    const { projectId: pid, created } = await resolveProject(
      projectIdRef.current, aoi);
    if (created) {
      setJobState(
        projectIdRef.current
          ? "running:the previous project record was unavailable — creating " +
              "a new project from the selected area"
          : "running:creating project"
      );
      setProjectId(pid);
      projectIdRef.current = pid;
      aoiRef.current = aoi;
    }
    return pid;
  };

  const analyze = async (
    options: { dtmId?: string; demMode?: string; terrain?: Record<string, number> } = {}
  ) => {
    const aoi = resolveAoi(Boolean(options.dtmId));
    if (!aoi) {
      setJobState("error:Draw or import an area — or select a DTM — before analyzing.");
      return;
    }
    clearResults();
    setBusy(true);
    setJobLog([]);
    try {
      let pid = await ensureProject(aoi);
      if (options.dtmId) setJobState("running:attaching DTM");
      try {
        await runAnalysisOn(pid, options);
      } catch (err) {
        // The project vanished between validation and the analyze call
        // (ephemeral reset race) — recreate once and retry, no loop.
        if (!/not found/i.test((err as Error).message)) throw err;
        setProjectId(null);
        projectIdRef.current = null;
        setJobState(
          "running:the previous project record was unavailable — recreating " +
            "from the selected DTM"
        );
        pid = await api.createProject(
          `AOI ${new Date().toISOString().slice(0, 16)}`,
          aoi
        );
        setProjectId(pid);
        projectIdRef.current = pid;
        await runAnalysisOn(pid, options);
      }
    } catch (err) {
      stopPolling();
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

  // -------------------------------------------------------- DTM locate flow
  const locateDtm = useCallback((dtm: api.Dtm) => {
    const map = mapRef.current;
    if (!map || !dtm.bbox_wgs84) return;
    const switching = lastDtmRef.current?.id !== dtm.id;
    lastDtmRef.current = dtm;
    // switching to a different DTM invalidates the current project/results —
    // a fresh project bound to this footprint is created at Analyze time
    if (switching) {
      clearResults();
      setProjectId(null);
      projectIdRef.current = null;
    }
    const [w, s, e, n] = dtm.bbox_wgs84;
    map.fitBounds([[w, s], [e, n]], {
      padding: 70,
      maxZoom: 18, // tiny rasters must not zoom to blur
      duration: 1200,
    });
    // draw the footprint (orange dashed source already styled)
    if (dtm.footprint_geojson) {
      const src = map.getSource("drone-fp") as
        | maplibregl.GeoJSONSource
        | undefined;
      src?.setData({
        type: "FeatureCollection",
        features: [{ type: "Feature", geometry: dtm.footprint_geojson,
                     properties: {} }],
      });
    }
    // show the DTM footprint as the working AOI immediately (editable). No
    // project is created here — ensureProject() does that at Analyze time so
    // a stale project id from a previous session cannot cause a 404.
    const ring: [number, number][] = [
      [w, s], [e, s], [e, n], [w, n], [w, s],
    ];
    const poly: GeoJSON.Polygon = { type: "Polygon", coordinates: [ring] };
    aoiRef.current = poly;
    setAoiOnMap(poly);
    setAreaKm2(ringAreaKm2(ring));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ------------------------------------------------------------ orthophoto
  const removeOrthoLayer = () => {
    const map = mapRef.current;
    if (map?.getLayer("orthophoto")) map.removeLayer("orthophoto");
    if (map?.getSource("orthophoto")) map.removeSource("orthophoto");
    setOrthoInfo(null);
  };

  const loadOrthophoto = async (pid: string) => {
    const map = mapRef.current;
    if (!map) return;
    const info = await api.getOrthophoto(pid);
    if (!info) return;
    if (map.getLayer("orthophoto")) map.removeLayer("orthophoto");
    if (map.getSource("orthophoto")) map.removeSource("orthophoto");
    map.addSource("orthophoto", {
      type: "image",
      url: info.url,
      coordinates: info.coordinates as [
        [number, number], [number, number],
        [number, number], [number, number]
      ],
    });
    // ordering: basemap < orthophoto < hillshade < vectors < AOI editing
    const before = map.getLayer("hillshade") ? "hillshade"
      : map.getLayer("aoi-fill") ? "aoi-fill" : undefined;
    map.addLayer({
      id: "orthophoto",
      type: "raster",
      source: "orthophoto",
      layout: { visibility: orthoVisible ? "visible" : "none" },
      paint: { "raster-opacity": orthoOpacity },
    }, before);
    setOrthoInfo(info);
  };

  const surveyCompleted = async () => {
    if (!projectId) return;
    clearResults();
    try {
      await loadOrthophoto(projectId);
    } catch {
      /* orthophoto optional */
    }
    try {
      await loadResults(projectId);
      setJobState("done");
    } catch (err) {
      setJobState(`error:${(err as Error).message}`);
    }
  };

  // ---------------------------------------------------------------- results
  const loadResults = async (pid: string) => {
    const map = mapRef.current;
    if (!map) return;
    const [fc, hs] = await Promise.all([api.getResults(pid), api.getHillshade(pid)]);
    resultsRef.current = fc;
    const fcProps = (fc as { properties?: api.ResultsProperties }).properties ?? null;
    setResultsProps(fcProps);
    setWarning(fcProps?.warning ?? null);
    api.getAnalysisRuns(pid).then(setRuns).catch(() => setRuns([]));
    api.getExportAvailability(pid).then(setExportsAvail)
      .catch(() => setExportsAvail(null));

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
    // Semi-transparent so the satellite imagery reads through; every vector
    // layer below is added after it, so valleys/keylines/keypoints render on
    // top of the hillshade.
    map.addLayer({
      id: "hillshade",
      type: "raster",
      source: "hillshade",
      paint: { "raster-opacity": 0.6 },
    });

    map.addSource("results", { type: "geojson", data: fc });
    map.addLayer({
      id: "contours",
      type: "line",
      source: "results",
      filter: ["==", ["get", "kind"], "contour"],
      paint: { "line-color": "#9a938a", "line-width": 0.7,
               "line-opacity": 0.7 },
    });
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
        "line-width": 3.5,
        "line-opacity": 0.95,
      },
    });
    // Keypoints: solid circle when drone-derived, hollow when satellite;
    // size and opacity scale with confidence. Added last => on top.
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
    setWarning(null);
    setExportOpen(false);
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
  const exportGeoJSON = async () => {
    if (!projectId) return;
    // Always export the server's current, spatially-validated result for
    // *this* project — never a stale in-browser copy.
    try {
      const fc = await api.getResults(projectId);
      const blob = new Blob([JSON.stringify(fc, null, 2)], {
        type: "application/geo+json",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `keyline-${projectId}-diagnostic-layers.geojson`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) {
      setJobState(`error:${(err as Error).message}`);
    }
  };

  // ------------------------------------------------------------- reanalysis
  const stopRerunPolling = () => {
    if (rerunTimer.current !== null) {
      window.clearTimeout(rerunTimer.current);
      rerunTimer.current = null;
    }
  };

  const rerunAnalysis = async () => {
    if (!projectId) return;
    // If the backend lost this project (ephemeral reset), fall through to the
    // full create-then-analyze workflow instead of a reanalyze 404.
    const existing = await api.getProject(projectId).catch(() => null);
    if (!existing) {
      setProjectId(null);
      projectIdRef.current = null;
      await analyze({ dtmId: lastDtmRef.current?.id });
      return;
    }
    setRerunning(true);
    setJobState("");
    clearResults();
    try {
      const { run_id } = await api.reanalyze(projectId, {
        dtmId: lastDtmRef.current?.id ?? dtmId ?? null,
      });
      setActiveRun(null);
      setActiveRunId(run_id); // the run monitor drives it to completion
    } catch (err) {
      setRerunning(false);
      setJobState(`error:${(err as Error).message}`);
    }
  };

  const cancelActiveRun = async () => {
    if (!projectId || !activeRunId) return;
    try {
      await api.cancelAnalysisRun(projectId, activeRunId);
    } catch {
      /* the cooperative cancel flag is the real signal; ignore errors */
    }
  };

  const retryActiveRun = async () => {
    // Re-run terrain analysis with the same DTM/AOI after a failure or a
    // confirmed stall.
    await rerunAnalysis();
  };
  useEffect(() => stopRerunPolling, []);

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
  const areaTooBig = areaKm2 !== null && areaKm2 > MAX_AOI_KM2;

  // The run whose products the Downloads section serves: the active run once
  // it has completed, else the newest completed run for this project.
  const completedActive =
    activeRun &&
    (activeRun.state === "completed" ||
      activeRun.state === "completed_with_warnings")
      ? activeRun
      : null;
  const downloadRun =
    completedActive ??
    runs.find(
      (r) => r.state === "completed" || r.state === "completed_with_warnings"
    ) ??
    null;
  const runInFlight =
    activeRun != null && !TERMINAL_RUN_STATES.includes(activeRun.state);

  const openDownload = (
    product:
      | "dtm"
      | "keylines.geojson"
      | "keylines.kml"
      | "keyline-design-map.tif"
      | "design-package.zip"
  ) => {
    if (!projectId || !downloadRun) return;
    // Normal browser download through the configured API base (works when the
    // Vercel frontend origin differs from the Render backend). Never fetches
    // the file into React memory.
    window.open(api.runDownloadUrl(projectId, downloadRun.id, product), "_blank");
  };

  return (
    <div className="app">
      <div ref={mapContainer} className="map" />

      {warning && (
        <div className="warning-banner">
          <span>⚠ {warning}</span>
          <button onClick={() => setWarning(null)} title="Dismiss">
            ✕
          </button>
        </div>
      )}

      {!warning && resultsProps?.watermark && (
        <div className="warning-banner amber">
          <span>
            ⚠ {resultsProps.watermark}
            {resultsProps.qa?.issues
              ?.filter((i) => i.severity === "error")
              .map((i) => ` [${i.code}]`)
              .join("")}
          </span>
        </div>
      )}

      {mapMeta && (
        <GeorefModal
          meta={mapMeta}
          onMeta={setMapMeta}
          onApply={applyScanOverlay}
          onClose={() => setMapMeta(null)}
        />
      )}

      <div className="toolbar">
        <h1>Keyline Studio</h1>

        <form className="search" onSubmit={doSearch}>
          <input
            type="text"
            placeholder="Search a place…"
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
          />
          <button type="submit" title="Search">
            🔎
          </button>
          {searchResults.length > 0 && (
            <ul className="search-results">
              {searchResults.map((r, i) => (
                <li key={i} onClick={() => goToResult(r)}>
                  {r.display_name}
                </li>
              ))}
            </ul>
          )}
        </form>

        <div className="basemaps">
          <label>
            <input
              type="radio"
              name="basemap"
              checked={basemap === "satellite"}
              onChange={() => switchBasemap("satellite")}
            />
            Satellite
          </label>
          <label>
            <input
              type="radio"
              name="basemap"
              checked={basemap === "streets"}
              onChange={() => switchBasemap("streets")}
            />
            Streets
          </label>
        </div>

        <button className={drawing ? "active" : ""} onClick={startDrawing}>
          ▰ Draw AOI {drawing ? "(click map, click first point to finish)" : ""}
        </button>
        <button disabled={busy} onClick={() => importInputRef.current?.click()}>
          ⬈ Import boundary (KML/KMZ/GeoJSON)
        </button>
        <input
          ref={importInputRef}
          type="file"
          accept=".kml,.kmz,.geojson,.json"
          style={{ display: "none" }}
          onChange={onImportBoundary}
        />
        <button disabled={busy} onClick={() => scanInputRef.current?.click()}>
          🗺 Locate from map scan (PNG/JPG/PDF)
        </button>
        <input
          ref={scanInputRef}
          type="file"
          accept=".png,.jpg,.jpeg,.pdf"
          style={{ display: "none" }}
          onChange={onScanFile}
        />
        {scanOverlay && (
          <div className="scan-controls">
            <div>
              Map overlay (RMS {scanOverlay.rms} m) — opacity{" "}
              {Math.round(scanOpacity * 100)}%
            </div>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={scanOpacity}
              onChange={(e) => setOverlayOpacity(parseFloat(e.target.value))}
            />
            <div className="scan-buttons">
              <button onClick={useScanExtentAsAoi}>Use map extent as AOI</button>
              <button onClick={removeScanOverlay}>Remove overlay</button>
            </div>
          </div>
        )}
        {areaKm2 !== null && (
          <div className={`area ${areaTooBig ? "too-big" : ""}`}>
            Area: {areaKm2 < 10 ? areaKm2.toFixed(2) : areaKm2.toFixed(1)} km²
            {areaTooBig ? ` — exceeds the ${MAX_AOI_KM2} km² limit` : ""}
          </div>
        )}
        <div className="terrain-source">
          <b>Terrain source</b>
          <label>
            <input type="radio" name="tsource"
              checked={terrainSource === "drone"}
              onChange={() => setTerrainSource("drone")} />
            Drone photos — process a new survey
          </label>
          <label>
            <input type="radio" name="tsource"
              checked={terrainSource === "dtm"}
              onChange={() => setTerrainSource("dtm")} />
            Existing DTM — upload a GeoTIFF
          </label>
          <label>
            <input type="radio" name="tsource"
              checked={terrainSource === "satellite"}
              onChange={() => setTerrainSource("satellite")} />
            Satellite preview — Copernicus GLO-30
          </label>
        </div>

        {terrainSource === "drone" && (
          <DroneSurveyPanel
            projectId={projectId}
            initialSurveyId={surveyId}
            onSurveyCreated={setSurveyId}
            onCompleted={() => void surveyCompleted()}
            onError={(msg) => setJobState(`error:${msg}`)}
          />
        )}

        {terrainSource === "dtm" && (
          <DtmPanel
            projectId={projectId}
            initialDtmId={dtmId}
            analyzeDisabledReason={
              busy
                ? "An analysis is already running."
                : areaTooBig
                  ? "The AOI exceeds the size limit."
                  : null
            }
            onLocate={(dtm) => void locateDtm(dtm)}
            onDtmSelected={setDtmId}
            onAnalyze={(id, terrain) =>
              void analyze({ dtmId: id, terrain: terrain as Record<string, number> })}
          />
        )}

        {terrainSource === "satellite" && (
          <div className="muted small-note">
            30 m satellite terrain — reconnaissance-grade only (~4 m vertical
            error).
          </div>
        )}

        {terrainSource === "satellite" && (
          <button
            disabled={!projectId || busy || areaTooBig}
            onClick={() => void analyze({ demMode: "satellite_only" })}
          >
            ▶ Analyze
          </button>
        )}
        {hasResults && projectId && (
          <button disabled={rerunning || busy} onClick={rerunAnalysis}>
            ↻ Re-run terrain analysis {rerunning ? "…" : ""}
          </button>
        )}

        <div className="export-wrap">
          <button
            disabled={!hasResults}
            onClick={() => setExportOpen((o) => !o)}
          >
            ⇩ Export {exportOpen ? "▴" : "▾"}
          </button>
          {exportOpen && hasResults && (
            <div className="export-menu">
              {([
                ["keylines.geojson", "Keylines only (GeoJSON)",
                 exportsAvail?.keylines_geojson ?? false],
                ["keylines.kml", "Keylines only (KML)",
                 exportsAvail?.keylines_kml ?? false],
                ["keylines.dxf", "Keylines (DXF for CAD)",
                 exportsAvail?.keylines_dxf ?? false],
                ["terrain.gpkg", "Full terrain package (GeoPackage)",
                 exportsAvail?.gpkg ?? false],
              ] as [string, string, boolean][]).map(([kind, label, ok]) => (
                <button
                  key={kind}
                  disabled={!ok}
                  title={ok ? label
                    : exportsAvail?.unavailable_reason ??
                      "Not available for this result"}
                  onClick={() => {
                    if (projectId) window.open(api.exportUrl(projectId, kind), "_blank");
                    setExportOpen(false);
                  }}
                >
                  {label}
                </button>
              ))}
              <button
                onClick={() => {
                  void exportGeoJSON();
                  setExportOpen(false);
                }}
              >
                All layers (GeoJSON)
              </button>
              <button
                onClick={() => {
                  if (projectId) window.open(api.exportKmlUrl(projectId), "_blank");
                  setExportOpen(false);
                }}
              >
                All layers (KML)
              </button>
              {!exportsAvail?.keylines_geojson &&
                exportsAvail?.unavailable_reason && (
                <div className="muted export-reason">
                  {exportsAvail.unavailable_reason}
                </div>
              )}
            </div>
          )}
        </div>

        {activeRun && (
          <AnalysisProgressPanel
            run={activeRun}
            onCancel={cancelActiveRun}
            onRetry={retryActiveRun}
            onRerun={rerunAnalysis}
          />
        )}

        {downloadRun && (
          <div className="downloads">
            <div className="downloads-title">Downloads</div>
            <button
              className="dl-btn"
              onClick={() => openDownload("dtm")}
              title="Untouched elevation raster used for analysis."
            >
              Original DTM
            </button>
            <button
              className="dl-btn"
              disabled={!downloadRun.exports.keylines_geojson}
              title={
                downloadRun.exports.keylines_geojson
                  ? "Candidate keylines + keypoints (EPSG:4326)."
                  : "No valid keyline was generated for this analysis."
              }
              onClick={() => openDownload("keylines.geojson")}
            >
              Keylines GeoJSON
            </button>
            <button
              className="dl-btn"
              disabled={!downloadRun.exports.keylines_kml}
              title={
                downloadRun.exports.keylines_kml
                  ? "Candidate keylines for Google Earth."
                  : "No valid keyline was generated for this analysis."
              }
              onClick={() => openDownload("keylines.kml")}
            >
              Keylines KML
            </button>
            <button
              className="dl-btn"
              onClick={() => openDownload("keyline-design-map.tif")}
              title="Visual georeferenced map. This is NOT an elevation raster."
            >
              {downloadRun.exports.keylines_geojson
                ? "DTM + keylines map"
                : "Diagnostic terrain map"}
            </button>
            <button
              className="dl-btn dl-primary"
              onClick={() => openDownload("design-package.zip")}
              title="All terrain, vector, QA, and metadata outputs."
            >
              Complete design package (ZIP)
            </button>
            {!downloadRun.exports.keylines_geojson && (
              <div className="muted dl-note">
                No valid keyline was generated — the design map is a diagnostic
                terrain map, and keyline-only files are unavailable.
              </div>
            )}
          </div>
        )}

        {hasResults && resultsProps?.counts && (
          <div className="counts">
            <div className="counts-title">Results</div>
            <b>Valleys {resultsProps.counts.valleys}</b> ·{" "}
            <b>Ridges {resultsProps.counts.ridges}</b> ·{" "}
            <b>Keypoints {resultsProps.counts.keypoints}</b> ·{" "}
            <b>Keylines {resultsProps.counts.keylines}</b>
            {resultsProps.notices?.includes("NO_VALID_KEYPOINT") && (
              <div className="muted">
                No valid keypoint found in this AOI — no candidate keyline was
                generated.
                {(resultsProps.keypoint_reasons ?? []).map((r, i) => (
                  <div key={i}>• {r}</div>
                ))}
              </div>
            )}
            {resultsProps.notices?.includes("KEYLINE_GENERATION_BLOCKED") && (
              <div className="muted">
                Keyline generation blocked: severe terrain-quality issues
                (strict mode).
              </div>
            )}
            <div className="muted">
              {resultsProps.dem_mode}
              {resultsProps.dem_resolution_m != null &&
                ` · ${resultsProps.dem_resolution_m} m/px`}
              {resultsProps.qa && (
                <> · QA {resultsProps.qa.severe ? "✗ failed"
                  : resultsProps.qa.passed ? "✓ passed" : "⚠ warnings"}</>
              )}
              {resultsProps.analysis_run_id &&
                ` · run ${resultsProps.analysis_run_id}`}
            </div>
            {runs.length > 1 && (
              <div className="muted">
                Previous runs:{" "}
                {runs.slice(0, 4).map((r) => (
                  <span key={r.id}>
                    {r.id.slice(0, 6)} ({r.state}
                    {r.counts ? `, ${r.counts.keylines} keylines` : ""}){" "}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Pre-run status (project setup / validation) — the dedicated
            AnalysisProgressPanel takes over once a run exists. */}
        {!activeRun && (running || error) && (
          <div className="status">
            <div className={`state ${error ? "error" : ""}`}>
              {error ? `Error: ${error}` : `⏳ ${jobState.replace("running:", "")}`}
            </div>
          </div>
        )}

        {(hasResults || orthoInfo) && (
          <div className="layers">
            {orthoInfo && (
              <>
                <label>
                  <input
                    type="checkbox"
                    checked={orthoVisible}
                    onChange={() => {
                      const next = !orthoVisible;
                      setOrthoVisible(next);
                      mapRef.current?.setLayoutProperty(
                        "orthophoto", "visibility",
                        next ? "visible" : "none");
                    }}
                  />
                  <span className="swatch" style={{ background: "#4a7a4a" }} />
                  Orthophoto
                </label>
                {orthoVisible && (
                  <input
                    type="range" min={0} max={1} step={0.05}
                    value={orthoOpacity}
                    onChange={(e) => {
                      const v = parseFloat(e.target.value);
                      setOrthoOpacity(v);
                      mapRef.current?.setPaintProperty(
                        "orthophoto", "raster-opacity", v);
                    }}
                  />
                )}
              </>
            )}
            {hasResults && (
            <label>
              <input
                type="checkbox"
                checked={visible.hillshade}
                onChange={() => toggleLayer("hillshade")}
              />
              <span className="swatch" style={{ background: "#888" }} />
              Hillshade
            </label>
            )}
            {hasResults && (
            <label>
              <input
                type="checkbox"
                checked={visible.contours}
                onChange={() => toggleLayer("contours")}
              />
              <span className="swatch" style={{ background: "#9a938a" }} />
              Contours
            </label>
            )}
            {hasResults && (<>
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
            </>)}
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
