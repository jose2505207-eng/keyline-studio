import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Dtm } from "./api";
import DtmPanel from "./DtmPanel";

const READY_DTM: Dtm = {
  id: "dtm_abc123",
  display_name: "caliterra-dtm.tif",
  original_filename: "caliterra-dtm.tif",
  source_type: "upload",
  status: "ready",
  size_bytes: 134217728,
  created_at: 1784000000,
  crs: "EPSG:32613",
  width: 4096,
  height: 4096,
  nodata: -9999,
  survey_id: null,
  project_id: null,
  resolution_m: [0.1, 0.1],
};

function mockFetchList(items: Dtm[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const u = String(input);
      if (u.includes("/api/dtms")) {
        return new Response(JSON.stringify({ items }), { status: 200 });
      }
      throw new Error(`unexpected fetch ${u}`);
    })
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DtmPanel", () => {
  it("shows the empty state when no DTMs exist", async () => {
    mockFetchList([]);
    render(<DtmPanel projectId="p1" onAnalyze={() => {}} />);
    expect(
      await screen.findByText(
        /No saved DTMs yet\. Upload a GeoTIFF or generate one from a drone survey\./
      )
    ).toBeInTheDocument();
  });

  it("lists DTMs and shows selected metadata + Ready status", async () => {
    mockFetchList([READY_DTM]);
    const user = userEvent.setup();
    render(<DtmPanel projectId="p1" onAnalyze={() => {}} />);
    const select = await screen.findByLabelText("Saved DTMs");
    await user.selectOptions(select, "dtm_abc123");
    const card = await screen.findByTestId("dtm-selected");
    expect(card).toHaveTextContent("Selected: caliterra-dtm.tif");
    expect(card).toHaveTextContent("128.0 MB");
    expect(card).toHaveTextContent("EPSG:32613");
    expect(card).toHaveTextContent("Status: Ready");
  });

  it("Analyze is disabled with a reason until a valid DTM is selected, then enables", async () => {
    mockFetchList([READY_DTM]);
    const onAnalyze = vi.fn();
    const user = userEvent.setup();
    render(<DtmPanel projectId="p1" onAnalyze={onAnalyze} />);
    const analyze = await screen.findByRole("button", {
      name: /Analyze with selected DTM/,
    });
    expect(analyze).toBeDisabled();
    expect(
      screen.getByText("Select or upload a valid DTM before analyzing.")
    ).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Saved DTMs"), "dtm_abc123");
    expect(analyze).toBeEnabled();
    await user.click(analyze);
    expect(onAnalyze).toHaveBeenCalledWith("dtm_abc123");
  });

  it("upload button opens the hidden file input", async () => {
    mockFetchList([]);
    const user = userEvent.setup();
    render(<DtmPanel projectId="p1" onAnalyze={() => {}} />);
    const input = (await screen.findByTestId(
      "dtm-file-input"
    )) as HTMLInputElement;
    expect(input).toHaveAttribute("accept", ".tif,.tiff,image/tiff,image/geotiff");
    const clickSpy = vi.spyOn(input, "click");
    const button = screen.getByRole("button", { name: /Upload new DTM/ });
    expect(button).toHaveAttribute("type", "button");
    await user.click(button);
    expect(clickSpy).toHaveBeenCalled();
  });

  it("rejects a non-TIFF file before uploading", async () => {
    mockFetchList([]);
    const user = userEvent.setup({ applyAccept: false });
    render(<DtmPanel projectId="p1" onAnalyze={() => {}} />);
    const input = (await screen.findByTestId(
      "dtm-file-input"
    )) as HTMLInputElement;
    const bad = new File(["x"], "photo.jpg", { type: "image/jpeg" });
    await user.upload(input, bad);
    expect(
      await screen.findByText(/"photo\.jpg" is not a \.tif\/\.tiff GeoTIFF/)
    ).toBeInTheDocument();
    expect(input.value).toBe(""); // same file can be picked again
  });

  it("uploads a valid file, shows the name, and auto-selects the new DTM", async () => {
    const uploaded: Dtm = { ...READY_DTM, id: "dtm_new", display_name: "new.tif" };
    let items: Dtm[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const u = String(input);
        if (u.includes("/api/dtms")) {
          return new Response(JSON.stringify({ items }), { status: 200 });
        }
        throw new Error(`unexpected fetch ${u}`);
      })
    );
    // stub XHR used by uploadDtmWithProgress
    class FakeXHR {
      upload = { onprogress: null as null | ((e: ProgressEvent) => void) };
      onload: null | (() => void) = null;
      onerror: null | (() => void) = null;
      status = 200;
      responseText = JSON.stringify(uploaded);
      open() {}
      send() {
        items = [uploaded]; // refresh after upload sees the new record
        setTimeout(() => this.onload?.(), 0);
      }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXHR as unknown as typeof XMLHttpRequest);

    const user = userEvent.setup();
    render(<DtmPanel projectId="p1" onAnalyze={() => {}} />);
    const input = (await screen.findByTestId(
      "dtm-file-input"
    )) as HTMLInputElement;
    const good = new File(["tiffdata"], "new.tif", { type: "image/tiff" });
    await user.upload(input, good);
    const card = await screen.findByTestId("dtm-selected");
    expect(card).toHaveTextContent("Selected: new.tif");
    expect(card).toHaveTextContent("Status: Ready");
  });

  it("keeps the custom server path section collapsed by default", async () => {
    mockFetchList([]);
    const user = userEvent.setup();
    render(<DtmPanel projectId="p1" onAnalyze={() => {}} />);
    const toggle = await screen.findByRole("button", {
      name: /Advanced: use a custom server filepath/,
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByText(/This path must already exist on the Keyline server/)
    ).not.toBeInTheDocument();
    await user.click(toggle);
    expect(
      screen.getByText(/This path must already exist on the Keyline server/)
    ).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: /Import into DTM library/ })
    ).toBeChecked();
  });

  it("blocks analysis when the selected DTM's file is missing", async () => {
    // a missing DTM is not selectable from the dropdown; a stale selection
    // that later goes missing must clear on refresh
    mockFetchList([{ ...READY_DTM, status: "missing" }]);
    render(<DtmPanel projectId="p1" onAnalyze={() => {}} />);
    await waitFor(() =>
      expect(
        screen.getByText(/Some DTMs reference files that no longer exist/)
      ).toBeInTheDocument()
    );
    const option = screen.getByRole("option", {
      name: /caliterra-dtm\.tif.*file missing/,
    }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    expect(
      screen.getByRole("button", { name: /Analyze with selected DTM/ })
    ).toBeDisabled();
  });

  it("passes non-DTM disabled reasons through (no AOI)", async () => {
    mockFetchList([READY_DTM]);
    const user = userEvent.setup();
    render(
      <DtmPanel
        projectId={null}
        analyzeDisabledReason="Draw or import an AOI first."
        onAnalyze={() => {}}
      />
    );
    await user.selectOptions(
      await screen.findByLabelText("Saved DTMs"), "dtm_abc123");
    expect(
      screen.getByRole("button", { name: /Analyze with selected DTM/ })
    ).toBeDisabled();
    expect(screen.getByText("Draw or import an AOI first.")).toBeInTheDocument();
  });
});
