import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";

vi.mock("../api", () => ({
  createJob: vi.fn(),
  deleteJob: vi.fn(),
  getJobs: vi.fn(),
  getHealth: vi.fn(),
  getConfig: vi.fn(),
  getJobLog: vi.fn(),
  cancelJob: vi.fn(),
  downloadUrl: vi.fn((id: string) => `/api/jobs/${id}/download`),
}));

const getJobs = vi.mocked(api.getJobs);
const getHealth = vi.mocked(api.getHealth);
const getConfig = vi.mocked(api.getConfig);
const getJobLog = vi.mocked(api.getJobLog);
const createJob = vi.mocked(api.createJob);

describe("App", () => {
  beforeEach(() => {
    getJobs.mockResolvedValue([]);
    getHealth.mockResolvedValue({ status: "ok" });
    getConfig.mockResolvedValue({ default_profile: "3b-safe", presets: ["3b-safe", "7b-fp8-experimental"] });
    getJobLog.mockResolvedValue({ text: "", next_offset: 0, size: 0, truncated: false });
    createJob.mockReset();
  });

  it("shows real upload controls and marks 7B as experimental", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Video Upscale" })).toBeVisible();
    expect(screen.getByLabelText("Choose video file")).toHaveAttribute(
      "accept",
      "video/*,.mp4,.mov,.mkv,.avi,.webm",
    );
    expect(screen.getByText("Choose from Photos")).toBeVisible();
    expect(screen.getByText("Experimental")).toBeVisible();
    expect(screen.queryByText("Real-ESRGAN Conservative")).not.toBeInTheDocument();
    expect(screen.getByText("Finished MP4 files remain available until you delete them.")).toBeVisible();
  });

  it("submits selected video and current processing settings", async () => {
    const user = userEvent.setup();
    createJob.mockResolvedValue({
      id: "job-1",
      original_filename: "lake.mov",
      preset: "3b-safe",
      color_correction: "lab",
      status: "queued",
      progress: 0,
      stage: "Queued",
      created_at: "2026-07-29T10:00:00Z",
      updated_at: "2026-07-29T10:00:00Z",
      output_filename: null,
      error: null,
      requires_preflight: false,
    });
    render(<App />);

    const input = await screen.findByLabelText("Choose video file");
    await user.upload(input, new File(["movie"], "lake.mov", { type: "video/quicktime" }));
    await user.click(screen.getByRole("button", { name: "Start processing" }));

    expect(createJob).toHaveBeenCalledWith(
      expect.objectContaining({ name: "lake.mov" }),
      "3b-safe",
      "lab",
      expect.any(Object),
    );
  });

  it("shows measured browser upload progress before a server job exists", async () => {
    const user = userEvent.setup();
    createJob.mockImplementation((async (...args: unknown[]) => {
      const callbacks = args[3] as { onProgress?: (progress: { loaded: number; total: number; bytesPerSecond: number }) => void } | undefined;
      callbacks?.onProgress?.({ loaded: 50 * 1024 * 1024, total: 100 * 1024 * 1024, bytesPerSecond: 5 * 1024 * 1024 });
      return new Promise(() => undefined);
    }) as typeof api.createJob);
    render(<App />);

    const input = await screen.findByLabelText("Choose video file");
    await user.upload(input, new File(["movie"], "lake.mov", { type: "video/quicktime" }));
    await user.click(screen.getByRole("button", { name: "Start processing" }));

    expect(await screen.findByText("Uploading to your Mac")).toBeVisible();
    expect(screen.getByText("50.0 MB / 100.0 MB · 5.0 MB/s")).toBeVisible();
    expect(screen.getByRole("progressbar", { name: "Upload 50%" })).toHaveAttribute("aria-valuenow", "50");
  });

  it("shows a toggleable live debug console for an active job", async () => {
    getJobs.mockResolvedValue([
      {
        id: "running-1",
        original_filename: "lake.mov",
        preset: "3b-safe",
        color_correction: "lab",
        status: "running",
        progress: 0,
        stage: "seedvr2-start",
        created_at: "2026-07-29T10:00:00Z",
        updated_at: "2026-07-29T10:01:00Z",
        output_filename: null,
        error: null,
        requires_preflight: false,
      },
    ]);
    getJobLog.mockResolvedValue({
      text: "PROGRESS 0 seedvr2-start\nSeedVR2 rendering started\n",
      next_offset: 52,
      size: 52,
      truncated: false,
    });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Show debug console" }));

    expect(await screen.findByText("Live debug console")).toBeVisible();
    expect(await screen.findByText(/SeedVR2 rendering started/)).toBeVisible();
    expect(getJobLog).toHaveBeenCalledWith("running-1", 0);
  });

  it("uses runtime-configured default profile instead of a hard-coded UI default", async () => {
    getConfig.mockResolvedValue({
      default_profile: "7b-fp8-experimental",
      presets: ["3b-safe", "7b-fp8-experimental"],
    });
    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /SeedVR2 7B FP8 Experimental/i })).toBeChecked();
    });
  });

  it("renders backend failure instead of inventing success", async () => {
    getJobs.mockResolvedValue([
      {
        id: "failed-1",
        original_filename: "broken.mp4",
        preset: "3b-safe",
        color_correction: "lab",
        status: "failed",
        progress: 12,
        stage: "Processing",
        created_at: "2026-07-29T10:00:00Z",
        updated_at: "2026-07-29T10:01:00Z",
        output_filename: null,
        error: "MPS out of memory",
        requires_preflight: false,
      },
    ]);
    render(<App />);

    expect(await screen.findByText("MPS out of memory")).toBeVisible();
    expect(screen.queryByRole("link", { name: /download mp4/i })).not.toBeInTheDocument();
  });
});
