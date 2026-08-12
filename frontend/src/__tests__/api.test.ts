import { afterEach, describe, expect, it, vi } from "vitest";
import { createJob } from "../api";

const createdJob = {
  id: "job-1",
  original_filename: "lake.mov",
  preset: "3b-safe",
  color_correction: "lab",
  status: "queued",
  progress: 0,
  stage: "queued",
  created_at: "2026-07-29T10:00:00Z",
  updated_at: "2026-07-29T10:00:00Z",
  output_filename: null,
  error: null,
  requires_preflight: false,
};

class FakeUpload {
  onprogress: ((event: ProgressEvent<EventTarget>) => void) | null = null;
}

class FakeXmlHttpRequest {
  static instances: FakeXmlHttpRequest[] = [];
  upload = new FakeUpload();
  status = 0;
  responseText = "";
  onload: ((event: ProgressEvent<EventTarget>) => void) | null = null;
  onerror: ((event: ProgressEvent<EventTarget>) => void) | null = null;
  onabort: ((event: ProgressEvent<EventTarget>) => void) | null = null;
  open = vi.fn();
  setRequestHeader = vi.fn();
  send = vi.fn();

  constructor() {
    FakeXmlHttpRequest.instances.push(this);
  }
}

afterEach(() => {
  FakeXmlHttpRequest.instances = [];
  vi.unstubAllGlobals();
});

describe("createJob", () => {
  it("uses browser upload events to report measured transfer progress", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXmlHttpRequest);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => createdJob }));
    const onProgress = vi.fn();

    const pending = createJob(
      new File(["movie"], "lake.mov", { type: "video/quicktime" }),
      "3b-safe",
      "lab",
      { onProgress },
    );
    const request = FakeXmlHttpRequest.instances[0];
    request.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent<EventTarget>);
    request.status = 201;
    request.responseText = JSON.stringify(createdJob);
    request.onload?.(new ProgressEvent("load"));

    await expect(pending).resolves.toMatchObject({ id: "job-1" });
    expect(onProgress).toHaveBeenCalledWith(expect.objectContaining({ loaded: 50, total: 100 }));
    expect(request.setRequestHeader).toHaveBeenCalledWith("X-Video-Upscale-Request", "1");
  });
});
