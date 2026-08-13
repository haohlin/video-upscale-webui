import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import type { Job, RuntimeConfig, UploadSession } from "../types";

vi.mock("../api", () => ({
  createApiClient: vi.fn(),
  getBackends: vi.fn(),
  createJob: vi.fn(),
  deleteJob: vi.fn(),
  getJobs: vi.fn(),
  getUploads: vi.fn(),
  getHealth: vi.fn(),
  getConfig: vi.fn(),
  getJobLog: vi.fn(),
  cancelJob: vi.fn(),
  discardUpload: vi.fn(),
  downloadUrl: vi.fn((id: string) => `/api/jobs/${id}/download`),
}));

const getJobs = vi.mocked(api.getJobs);
const getUploads = vi.mocked(api.getUploads);
const getHealth = vi.mocked(api.getHealth);
const getConfig = vi.mocked(api.getConfig);
const getJobLog = vi.mocked(api.getJobLog);
const createJob = vi.mocked(api.createJob);
const discardUpload = vi.mocked(api.discardUpload);
const getBackends = vi.mocked(api.getBackends);
const createApiClient = vi.mocked(api.createApiClient);

const pendingUpload: UploadSession = {
  id: "upload-pending",
  filename: "lake.mov",
  total_bytes: 60,
  accepted_bytes: 12,
  expires_at: "2026-08-14T06:00:00Z",
};

const runtimeConfig: RuntimeConfig = {
  default_profile: "3b-safe",
  presets: ["3b-safe", "7b-fp8-experimental"],
  default_output_scale: 1,
  output_scales: [
    { value: 1, label: "1x Original", description: "Original dimensions; full generative restoration." },
    { value: 0.5, label: "0.5x Balanced", description: "Half width and height; generative restoration with fewer output pixels." },
    { value: 0.25, label: "0.25x Fast", description: "Quarter width and height; experimental generative restoration." },
    { value: 2, label: "2x Upscale", description: "Double width and height; highest processing cost." },
  ],
};

function jobFixture(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    original_filename: "lake.mov",
    preset: "3b-safe",
    color_correction: "lab",
    output_scale: 1,
    target_width: 1920,
    target_height: 1080,
    status: "queued",
    progress: 0,
    stage: "queued",
    created_at: "2026-08-12T06:00:00Z",
    updated_at: "2026-08-12T06:00:00Z",
    started_at: null,
    finished_at: null,
    elapsed_seconds: null,
    last_heartbeat_at: null,
    last_progress_at: null,
    progress_source: "none",
    phase_name: null,
    phase_current: null,
    phase_total: null,
    chunk_current: null,
    chunk_total: null,
    eta_low_seconds: null,
    eta_high_seconds: null,
    eta_confidence: "none",
    heartbeat_stale: false,
    progress_stale: false,
    output_filename: null,
    error: null,
    requires_preflight: false,
    ...overrides,
  };
}

function runningJob(overrides: Partial<Job> = {}): Job {
  return jobFixture({
    status: "running",
    progress: 38,
    progress_source: "measured",
    stage: "decoding",
    phase_name: "decoding",
    phase_current: 5,
    phase_total: 10,
    chunk_current: 2,
    chunk_total: 4,
    started_at: "2026-08-12T06:00:00Z",
    elapsed_seconds: 120,
    eta_low_seconds: 2700,
    eta_high_seconds: 3600,
    eta_confidence: "medium",
    last_heartbeat_at: "2026-08-12T06:02:00Z",
    last_progress_at: "2026-08-12T06:01:55Z",
    ...overrides,
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("App", () => {
  beforeEach(() => {
    getBackends.mockReset();
    createApiClient.mockReset();
    getJobs.mockReset();
    getUploads.mockReset();
    getHealth.mockReset();
    getConfig.mockReset();
    getJobLog.mockReset();
    getJobs.mockResolvedValue([]);
    getUploads.mockResolvedValue([]);
    getHealth.mockResolvedValue({ status: "ok" });
    getConfig.mockResolvedValue(runtimeConfig);
    getJobLog.mockResolvedValue({ text: "", next_offset: 0, size: 0, truncated: false });
    getBackends.mockResolvedValue([{ id: "mac", display_name: "Mac M4 Pro", api_base_url: "", preference: 100 }]);
    createJob.mockReset();
    discardUpload.mockReset();
  });

  it("prefers healthy Windows in Auto mode and keeps both job histories visible", async () => {
    const windowsJob = jobFixture({ id: "win-job", original_filename: "windows.mov" });
    const macJob = jobFixture({ id: "mac-job", original_filename: "mac.mov", status: "completed" });
    getBackends.mockResolvedValue([
      { id: "windows-4090", display_name: "Windows RTX 4090", api_base_url: "https://windows.ts.net", preference: 10 },
      { id: "mac", display_name: "Mac M4 Pro", api_base_url: "", preference: 100 },
    ]);
    createApiClient.mockImplementation((descriptor) => ({
      descriptor,
      getHealth: vi.fn().mockResolvedValue({ status: "ok", backend_id: descriptor.id }),
      getConfig: vi.fn().mockResolvedValue(descriptor.id === "windows-4090" ? { ...runtimeConfig, default_profile: "7b-fp8-quality", presets: ["7b-fp8-quality", "3b-fp8-fast"] } : runtimeConfig),
      getJobs: vi.fn().mockResolvedValue(descriptor.id === "windows-4090" ? [windowsJob] : [macJob]),
      getUploads: vi.fn().mockResolvedValue([]),
      createJob: vi.fn(), discardUpload: vi.fn(), cancelJob: vi.fn(), deleteJob: vi.fn(), getJobLog: vi.fn(),
      downloadUrl: vi.fn((id: string) => `${descriptor.api_base_url}/api/jobs/${id}/download`),
    }));

    render(<App />);

    expect(await screen.findByRole("combobox", { name: "Processing host" })).toHaveValue("auto");
    expect(await screen.findByText("windows.mov")).toBeVisible();
    expect(screen.getByText("mac.mov")).toBeVisible();
    expect(screen.getAllByText("Windows RTX 4090").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Mac M4 Pro/).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getByRole("radio", { name: /SeedVR2 7B FP8 Quality/i })).toBeChecked());
  });

  it("falls back to Mac for new jobs when Windows is offline", async () => {
    getBackends.mockResolvedValue([
      { id: "windows-4090", display_name: "Windows RTX 4090", api_base_url: "https://windows.ts.net", preference: 10 },
      { id: "mac", display_name: "Mac M4 Pro", api_base_url: "", preference: 100 },
    ]);
    createApiClient.mockImplementation((descriptor) => ({
      descriptor,
      getHealth: descriptor.id === "windows-4090" ? vi.fn().mockRejectedValue(new Error("offline")) : vi.fn().mockResolvedValue({ status: "ok" }),
      getConfig: vi.fn().mockResolvedValue(runtimeConfig), getJobs: vi.fn().mockResolvedValue([]), getUploads: vi.fn().mockResolvedValue([]),
      createJob: vi.fn(), discardUpload: vi.fn(), cancelJob: vi.fn(), deleteJob: vi.fn(), getJobLog: vi.fn(), downloadUrl: vi.fn(),
    }));

    render(<App />);

    expect(await screen.findByText(/Auto selected Mac M4 Pro/)).toBeVisible();
  });

  it("shows upload controls and server-configured output scale choices", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Video Upscale" })).toBeVisible();
    expect(screen.getByLabelText("Choose new video file")).toHaveAttribute(
      "accept",
      "video/*,.mp4,.mov,.mkv,.avi,.webm",
    );
    expect(screen.getByRole("group", { name: "Output resolution" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /1x Original.*Original dimensions; full generative restoration/i })).toBeChecked();
    expect(screen.getByRole("radio", { name: /0.5x Balanced.*Half width and height; generative restoration with fewer output pixels/i })).toBeVisible();
    expect(screen.getByRole("radio", { name: /0.25x Fast.*Quarter width and height; experimental generative restoration/i })).toBeVisible();
    expect(screen.getByRole("radio", { name: /2x Upscale.*Double width and height; highest processing cost/i })).toBeVisible();
  });

  it("restores pending uploads after refresh and resumes only the exact file", async () => {
    const user = userEvent.setup();
    getUploads.mockResolvedValue([pendingUpload]);
    createJob.mockResolvedValue(jobFixture({ id: pendingUpload.id }));
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Pending uploads" })).toBeVisible();
    expect(screen.getByText(/12 B \/ 60 B confirmed/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Resume lake.mov" }));
    await user.upload(
      screen.getByLabelText("Choose file to resume lake.mov"),
      new File(["x".repeat(60)], "wrong.mov", { type: "video/quicktime" }),
    );
    expect(await screen.findByText("Selected file does not match this pending upload")).toBeVisible();
    expect(createJob).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Resume lake.mov" }));
    await user.upload(
      screen.getByLabelText("Choose file to resume lake.mov"),
      new File(["x".repeat(60)], "lake.mov", { type: "video/quicktime" }),
    );
    await user.click(screen.getByRole("button", { name: "Resume upload" }));

    expect(createJob).toHaveBeenCalledWith(
      expect.objectContaining({ name: "lake.mov", size: 60 }),
      "3b-safe",
      "lab",
      1,
      expect.objectContaining({ resumeSessionId: pendingUpload.id }),
    );
  });

  it("ends one pending upload and keeps upload-new separate", async () => {
    const user = userEvent.setup();
    getUploads.mockResolvedValue([pendingUpload]);
    discardUpload.mockResolvedValue();
    createJob.mockResolvedValue(jobFixture());
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<App />);

    await screen.findByRole("heading", { name: "Pending uploads" });
    await user.click(screen.getByRole("button", { name: "End upload lake.mov" }));
    expect(discardUpload).toHaveBeenCalledWith(pendingUpload.id);
    await waitFor(() => expect(screen.queryByText(/12 B \/ 60 B confirmed/)).not.toBeInTheDocument());

    await user.upload(
      screen.getByLabelText("Choose new video file"),
      new File(["new"], "new.mov", { type: "video/quicktime" }),
    );
    await user.click(screen.getByRole("button", { name: "Start processing" }));
    expect(createJob).toHaveBeenLastCalledWith(
      expect.objectContaining({ name: "new.mov" }),
      "3b-safe",
      "lab",
      1,
      expect.objectContaining({ resumeSessionId: undefined }),
    );
  });

  it("submits selected video and server-configured output scale", async () => {
    const user = userEvent.setup();
    createJob.mockResolvedValue(jobFixture({ output_scale: 0.5, target_width: 960, target_height: 540 }));
    render(<App />);

    await user.click(await screen.findByRole("radio", { name: /0.5x Balanced/i }));
    await user.upload(screen.getByLabelText("Choose new video file"), new File(["movie"], "lake.mov", { type: "video/quicktime" }));
    await user.click(screen.getByRole("button", { name: "Start processing" }));

    expect(createJob).toHaveBeenCalledWith(
      expect.objectContaining({ name: "lake.mov" }),
      "3b-safe",
      "lab",
      0.5,
      expect.any(Object),
    );
  });

  it("uses runtime-configured defaults instead of hard-coded UI defaults", async () => {
    getConfig.mockResolvedValue({
      ...runtimeConfig,
      default_profile: "7b-fp8-experimental",
      default_output_scale: 0.25,
    });
    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /SeedVR2 7B FP8 Experimental/i })).toBeChecked();
      expect(screen.getByRole("radio", { name: /0.25x Fast/i })).toBeChecked();
    });
  });

  it("previews local output dimensions and revokes object URLs on replacement and unmount", async () => {
    const createObjectURL = vi.fn()
      .mockReturnValueOnce("blob:preview-one")
      .mockReturnValueOnce("blob:preview-two");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
    vi.spyOn(HTMLVideoElement.prototype, "videoWidth", "get").mockReturnValue(1920);
    vi.spyOn(HTMLVideoElement.prototype, "videoHeight", "get").mockReturnValue(1080);
    const user = userEvent.setup();
    const view = render(<App />);
    const input = await screen.findByLabelText("Choose new video file");

    await user.upload(input, new File(["one"], "one.mov", { type: "video/quicktime" }));
    await waitFor(() => expect(document.querySelector("video")).not.toBeNull());
    fireEvent.loadedMetadata(document.querySelector("video") as HTMLVideoElement);
    await user.click(screen.getByRole("radio", { name: /0.5x Balanced/i }));
    expect(screen.getByText("Expected output: 960 × 540")).toBeVisible();

    await user.upload(input, new File(["two"], "two.mov", { type: "video/quicktime" }));
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-one"));
    view.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-two");
  });

  it("shows measured progress, phase counters, server ETA, and a live elapsed timer", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-12T06:02:00Z"));
    getJobs
      .mockResolvedValueOnce([runningJob()])
      .mockImplementation(() => new Promise(() => undefined));
    render(<App />);

    expect(await screen.findByText("38%")).toBeVisible();
    expect(screen.getByText("Decoding · 5/10 · chunk 2/4")).toBeVisible();
    expect(screen.getByText("Elapsed 2m 00s")).toBeVisible();
    expect(screen.getByText("ETA 45–60 min")).toBeVisible();
    expect(screen.getByText("Medium confidence")).toBeVisible();
    expect(screen.getByText("2026-08-12T06:02:00Z").closest("small")).toHaveTextContent("Heartbeat 2026-08-12T06:02:00Z");
    expect(screen.getByText("2026-08-12T06:01:55Z").closest("small")).toHaveTextContent("Last measured progress 2026-08-12T06:01:55Z");
    expect(screen.getByRole("progressbar", { name: "38% complete" })).toHaveAttribute("aria-valuenow", "38");

    act(() => vi.advanceTimersByTime(5_000));
    expect(screen.getByText("Elapsed 2m 05s")).toBeVisible();
  });

  it("calibrates without inventing a percentage when progress source is none", async () => {
    getJobs.mockResolvedValue([runningJob({ progress_source: "none", eta_low_seconds: null, eta_high_seconds: null, eta_confidence: "none" })]);
    render(<App />);

    expect(await screen.findByText("Calibrating…")).toBeVisible();
    expect(screen.queryByText("38%")).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Processing status" })).toHaveAttribute("aria-valuetext", "Calibrating progress");
  });

  it("keeps historical estimates separate from measured percentage", async () => {
    getJobs.mockResolvedValue([runningJob({ progress_source: "historical" })]);
    render(<App />);

    expect(await screen.findByText("ETA 45–60 min")).toBeVisible();
    expect(screen.queryByText("38%")).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Processing status" })).toHaveAttribute("aria-valuetext", "Calibrating progress");
  });

  it("warns when heartbeat is stale and hides ETA", async () => {
    getJobs.mockResolvedValue([runningJob({ heartbeat_stale: true })]);
    render(<App />);

    expect(await screen.findByRole("status")).toHaveTextContent("Progress signal stale — processing may still be active");
    expect(screen.queryByText(/ETA 45/)).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Processing status" })).toHaveAttribute(
      "aria-valuetext",
      "Progress signal stale — processing may still be active",
    );
  });

  it("distinguishes live heartbeat from stale measured work and hides ETA", async () => {
    getJobs.mockResolvedValue([runningJob({ progress_stale: true })]);
    render(<App />);

    expect(await screen.findByRole("status")).toHaveTextContent("Process is alive, but measured work has not advanced");
    expect(screen.queryByText(/ETA 45/)).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Processing status" })).toHaveAttribute(
      "aria-valuetext",
      "Process is alive, but measured work has not advanced",
    );
  });

  it("renders a legacy active job with missing optional timing fields", async () => {
    const legacy = runningJob();
    delete (legacy as Partial<Job>).started_at;
    delete (legacy as Partial<Job>).finished_at;
    delete (legacy as Partial<Job>).elapsed_seconds;
    delete (legacy as Partial<Job>).last_heartbeat_at;
    delete (legacy as Partial<Job>).last_progress_at;
    delete (legacy as Partial<Job>).phase_name;
    delete (legacy as Partial<Job>).phase_current;
    delete (legacy as Partial<Job>).phase_total;
    delete (legacy as Partial<Job>).chunk_current;
    delete (legacy as Partial<Job>).chunk_total;
    delete (legacy as Partial<Job>).eta_low_seconds;
    delete (legacy as Partial<Job>).eta_high_seconds;
    getJobs.mockResolvedValue([legacy]);
    render(<App />);

    expect(await screen.findByText("lake.mov")).toBeVisible();
    expect(screen.getByRole("progressbar", { name: "38% complete" })).toBeVisible();
    expect(screen.queryByText(/^Elapsed /)).not.toBeInTheDocument();
  });

  it("renders exact 100% only for backend-completed state", async () => {
    getJobs.mockResolvedValue([
      jobFixture({ id: "complete", status: "completed", progress: 17, output_filename: "lake-restored.mp4", finished_at: "2026-08-12T07:00:00Z" }),
      jobFixture({ id: "failed", original_filename: "broken.mp4", status: "failed", progress: 100, error: "MPS out of memory" }),
    ]);
    render(<App />);

    expect(await screen.findByText("100%")).toBeVisible();
    expect(screen.getAllByText("100%")).toHaveLength(1);
    expect(screen.getByText("MPS out of memory")).toBeVisible();
  });

  it("shows measured browser upload progress before a server job exists", async () => {
    const user = userEvent.setup();
    createJob.mockImplementation((async (...args: unknown[]) => {
      const callbacks = args[4] as { onProgress?: (progress: { loaded: number; total: number; bytesPerSecond: number; retryAttempt: number }) => void } | undefined;
      callbacks?.onProgress?.({ loaded: 50 * 1024 * 1024, total: 100 * 1024 * 1024, bytesPerSecond: 5 * 1024 * 1024, retryAttempt: 0 });
      return new Promise(() => undefined);
    }) as typeof api.createJob);
    render(<App />);

    const input = await screen.findByLabelText("Choose new video file");
    await user.upload(input, new File(["movie"], "lake.mov", { type: "video/quicktime" }));
    await user.click(screen.getByRole("button", { name: "Start processing" }));

    expect(await screen.findByText("Uploading to your Mac")).toBeVisible();
    expect(screen.getByText("50.0 MB / 100.0 MB · 5.0 MB/s · server confirmed")).toBeVisible();
    expect(screen.getByRole("progressbar", { name: "Upload 50%" })).toHaveAttribute("aria-valuenow", "50");
  });

  it("shows a toggleable live debug console for an active job", async () => {
    getJobs.mockResolvedValue([runningJob({ id: "running-1", stage: "seedvr2-start", phase_name: null })]);
    getJobLog.mockResolvedValue({
      text: "PROGRESS 0 seedvr2-start\nSeedVR2 rendering started\n",
      next_offset: 52,
      size: 52,
      truncated: false,
    });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Show debug console" }));

    expect(await screen.findByText("Live job log")).toBeVisible();
    expect(screen.getByText(/Job running-1/)).toBeVisible();
    expect(screen.getByText("Polling every 2 seconds")).toBeVisible();
    expect(await screen.findByText(/SeedVR2 rendering started/)).toBeVisible();
    expect(getJobLog).toHaveBeenCalledWith("running-1", 0);
  });

  it("does not mix a current upload with an older completed job log", async () => {
    getJobs.mockResolvedValue([jobFixture({ id: "old-job", status: "completed", output_filename: "old.mp4" })]);
    createJob.mockImplementation((async (...args: unknown[]) => {
      const callbacks = args[4] as { onProgress?: (progress: { loaded: number; total: number; bytesPerSecond: number; retryAttempt: number }) => void };
      callbacks.onProgress?.({ loaded: 1, total: 5, bytesPerSecond: 1, retryAttempt: 0 });
      return new Promise(() => undefined);
    }) as typeof api.createJob);
    const user = userEvent.setup();
    render(<App />);

    await user.upload(await screen.findByLabelText("Choose new video file"), new File(["movie"], "new.mov"));
    await user.click(screen.getByRole("button", { name: "Start processing" }));
    await user.click(screen.getByRole("button", { name: "Show debug console" }));

    expect(await screen.findByText("Upload status")).toBeVisible();
    expect(screen.getByText(/No server job yet/)).toBeVisible();
    expect(screen.queryByText("Completed.")).not.toBeInTheDocument();
    expect(getJobLog).not.toHaveBeenCalled();
  });

  it("labels completed output as historical and states backend validation reached 100", async () => {
    getJobs.mockResolvedValue([jobFixture({ id: "done-1", status: "completed", output_filename: "done.mp4" })]);
    getJobLog.mockResolvedValue({ text: "PROGRESS 99 audio-remux\n", next_offset: 24, size: 24, truncated: false });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Show debug console" }));

    expect(await screen.findByText("Historical job log")).toBeVisible();
    expect(screen.getByText("Fetched once · processing is not active")).toBeVisible();
    expect(screen.getByText(/Backend validated output and recorded 100% completion/)).toBeVisible();
    expect(screen.queryByText("Polling every 2 seconds")).not.toBeInTheDocument();
  });

  it("offers resume with the same server session after retries are exhausted", async () => {
    const user = userEvent.setup();
    createJob
      .mockImplementationOnce((async (...args: unknown[]) => {
        const callbacks = args[4] as { onSession?: (id: string) => void };
        callbacks.onSession?.("upload-resume");
        throw new Error("Upload connection failed after 3 attempts");
      }) as typeof api.createJob)
      .mockResolvedValueOnce(jobFixture({ id: "upload-resume" }));
    render(<App />);

    await user.upload(await screen.findByLabelText("Choose new video file"), new File(["movie"], "new.mov"));
    await user.click(screen.getByRole("button", { name: "Start processing" }));
    expect(await screen.findByRole("button", { name: "Resume upload" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Resume upload" }));
    expect(createJob).toHaveBeenLastCalledWith(
      expect.objectContaining({ name: "new.mov" }), "3b-safe", "lab", 1,
      expect.objectContaining({ resumeSessionId: "upload-resume" }),
    );
  });
});
