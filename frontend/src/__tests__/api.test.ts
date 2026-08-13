import { afterEach, describe, expect, it, vi } from "vitest";
import { createJob } from "../api";

const session = {
  id: "upload-1",
  filename: "lake.mov",
  total_bytes: 5,
  accepted_bytes: 0,
  expires_at: "2026-08-14T00:00:00Z",
};

const createdJob = {
  id: "upload-1",
  original_filename: "lake.mov",
  preset: "3b-safe",
  color_correction: "lab",
  output_scale: 1,
  status: "queued",
};

function response(status: number, payload: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("createJob", () => {
  it("uploads fixed chunks and reports only server-confirmed bytes", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(201, session))
      .mockResolvedValueOnce(response(200, { ...session, accepted_bytes: 5 }))
      .mockResolvedValueOnce(response(201, createdJob));
    vi.stubGlobal("fetch", fetchMock);
    const onProgress = vi.fn();

    await expect(createJob(
      new File(["movie"], "lake.mov", { type: "video/quicktime" }),
      "3b-safe", "lab", 1, { onProgress },
    )).resolves.toMatchObject({ id: "upload-1" });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/uploads", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/uploads/upload-1", expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "Upload-Offset": "0" }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/uploads/upload-1/finalize", expect.objectContaining({ method: "POST" }));
    expect(onProgress).toHaveBeenLastCalledWith(expect.objectContaining({ loaded: 5, total: 5, retryAttempt: 0 }));
  });

  it("retries a transient chunk failure without advancing confirmed offset", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(201, session))
      .mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValueOnce(response(200, { ...session, accepted_bytes: 5 }))
      .mockResolvedValueOnce(response(201, createdJob));
    vi.stubGlobal("fetch", fetchMock);
    const onProgress = vi.fn();

    const pending = createJob(new File(["movie"], "lake.mov"), "3b-safe", "lab", 1, { onProgress });
    await vi.runAllTimersAsync();
    await pending;

    expect(onProgress).toHaveBeenCalledWith(expect.objectContaining({ loaded: 0, retryAttempt: 1 }));
    const puts = fetchMock.mock.calls.filter(([, init]) => (init as RequestInit).method === "PUT");
    expect(puts).toHaveLength(2);
  });

  it("refreshes server offset after conflict and resumes from accepted bytes", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(201, session))
      .mockResolvedValueOnce(response(409, { detail: "offset mismatch" }))
      .mockResolvedValueOnce(response(200, { ...session, accepted_bytes: 2 }))
      .mockResolvedValueOnce(response(200, { ...session, accepted_bytes: 5 }))
      .mockResolvedValueOnce(response(201, createdJob));
    vi.stubGlobal("fetch", fetchMock);

    await createJob(new File(["movie"], "lake.mov"), "3b-safe", "lab", 1);

    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/uploads/upload-1", expect.objectContaining({ method: "GET" }));
    expect(fetchMock).toHaveBeenNthCalledWith(4, "/api/uploads/upload-1", expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "Upload-Offset": "2" }),
    }));
  });

  it("resumes an existing session instead of creating another upload", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, { ...session, accepted_bytes: 2 }))
      .mockResolvedValueOnce(response(200, { ...session, accepted_bytes: 5 }))
      .mockResolvedValueOnce(response(201, createdJob));
    vi.stubGlobal("fetch", fetchMock);

    await createJob(new File(["movie"], "lake.mov"), "3b-safe", "lab", 1, { resumeSessionId: "upload-1" });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/uploads/upload-1", expect.objectContaining({ method: "GET" }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/uploads/upload-1", expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "Upload-Offset": "2" }),
    }));
  });
});
