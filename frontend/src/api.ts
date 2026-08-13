import type { ColorCorrection, Health, Job, JobLogTail, OutputScale, PresetId, RuntimeConfig, UploadProgress } from "./types";

const apiRoot = "/api";
const uploadChunkBytes = 4 * 1024 * 1024;
const maxChunkAttempts = 3;

interface UploadSession {
  id: string;
  filename: string;
  total_bytes: number;
  accepted_bytes: number;
  expires_at: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiRoot}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...init?.headers,
      "X-Video-Upscale-Request": "1",
    },
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: string; message?: string };
      message = payload.detail ?? payload.message ?? message;
    } catch {
      // Error response need not be JSON.
    }
    throw new ApiError(message, response.status);
  }

  return (await response.json()) as T;
}

export async function getHealth(): Promise<Health> {
  return request<Health>("/health");
}

export async function getConfig(): Promise<RuntimeConfig> {
  return request<RuntimeConfig>("/config");
}

export async function getJobs(): Promise<Job[]> {
  const result = await request<{ jobs: Job[] }>("/jobs");
  return result.jobs;
}

export async function createJob(
  video: File,
  preset: PresetId,
  colorCorrection: ColorCorrection,
  outputScale: OutputScale,
  callbacks: {
    onProgress?: (progress: UploadProgress) => void;
    onUploadComplete?: () => void;
    onSession?: (id: string) => void;
    resumeSessionId?: string;
  } = {},
): Promise<Job> {
  const startedAt = performance.now();
  const createSession = () => request<UploadSession>("/uploads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: video.name,
        total_bytes: video.size,
        options: { preset, color_correction: colorCorrection, output_scale: outputScale },
      }),
    });
  let session: UploadSession;
  if (callbacks.resumeSessionId) {
    try {
      session = await request<UploadSession>(`/uploads/${encodeURIComponent(callbacks.resumeSessionId)}`, { method: "GET" });
    } catch (error) {
      if (!(error instanceof ApiError) || ![404, 410].includes(error.status ?? 0)) throw error;
      session = await createSession();
    }
  } else {
    session = await createSession();
  }
  if (session.total_bytes !== video.size || session.filename !== video.name) {
    throw new ApiError("Selected file does not match resumable upload session", 409);
  }
  callbacks.onSession?.(session.id);
  let confirmed = session.accepted_bytes;
  const initialConfirmed = confirmed;
  callbacks.onProgress?.({ loaded: confirmed, total: video.size, bytesPerSecond: 0, retryAttempt: 0 });
  let conflicts = 0;

  while (confirmed < video.size) {
    const end = Math.min(video.size, confirmed + uploadChunkBytes);
    let uploaded = false;
    for (let attempt = 0; attempt < maxChunkAttempts && !uploaded; attempt += 1) {
      try {
        const next = await request<UploadSession>(`/uploads/${encodeURIComponent(session.id)}`, {
          method: "PUT",
          headers: {
            "Content-Type": "application/octet-stream",
            "Upload-Offset": String(confirmed),
          },
          body: video.slice(confirmed, end),
        });
        if (!Number.isInteger(next.accepted_bytes) || next.accepted_bytes <= confirmed || next.accepted_bytes > end) {
          throw new ApiError("Server returned an invalid upload offset");
        }
        confirmed = next.accepted_bytes;
        uploaded = true;
        const elapsedSeconds = Math.max((performance.now() - startedAt) / 1_000, 0.001);
        callbacks.onProgress?.({ loaded: confirmed, total: video.size, bytesPerSecond: (confirmed - initialConfirmed) / elapsedSeconds, retryAttempt: 0 });
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          conflicts += 1;
          if (conflicts > maxChunkAttempts) throw error;
          const current = await request<UploadSession>(`/uploads/${encodeURIComponent(session.id)}`, { method: "GET" });
          if (!Number.isInteger(current.accepted_bytes) || current.accepted_bytes < 0 || current.accepted_bytes > video.size) {
            throw new ApiError("Server returned an invalid upload offset");
          }
          confirmed = current.accepted_bytes;
          uploaded = true;
          const elapsedSeconds = Math.max((performance.now() - startedAt) / 1_000, 0.001);
          callbacks.onProgress?.({ loaded: confirmed, total: video.size, bytesPerSecond: (confirmed - initialConfirmed) / elapsedSeconds, retryAttempt: 0 });
          continue;
        }
        const retryable = !(error instanceof ApiError) || error.status === undefined || error.status === 408 || error.status === 429 || error.status >= 500;
        if (!retryable || attempt + 1 >= maxChunkAttempts) throw error;
        callbacks.onProgress?.({
          loaded: confirmed,
          total: video.size,
          bytesPerSecond: (confirmed - initialConfirmed) / Math.max((performance.now() - startedAt) / 1_000, 0.001),
          retryAttempt: attempt + 1,
        });
        await new Promise((resolve) => window.setTimeout(resolve, 250 * (attempt + 1)));
      }
    }
  }
  callbacks.onUploadComplete?.();
  return request<Job>(`/uploads/${encodeURIComponent(session.id)}/finalize`, { method: "POST" });
}

export async function cancelJob(id: string): Promise<Job> {
  return request<Job>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export async function deleteJob(id: string): Promise<void> {
  await request<unknown>(`/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function getJobLog(id: string, offset: number): Promise<JobLogTail> {
  return request<JobLogTail>(`/jobs/${encodeURIComponent(id)}/log?offset=${Math.max(0, Math.floor(offset))}`);
}

export function downloadUrl(id: string): string {
  return `${apiRoot}/jobs/${encodeURIComponent(id)}/download`;
}
