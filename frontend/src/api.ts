import type { ColorCorrection, Health, Job, JobLogTail, OutputScale, PresetId, RuntimeConfig, UploadProgress } from "./types";

const apiRoot = "/api";

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
  } = {},
): Promise<Job> {
  const form = new FormData();
  form.append("video", video);
  form.append("preset", preset);
  form.append("color_correction", colorCorrection);
  form.append("output_scale", String(outputScale));

  return new Promise<Job>((resolve, reject) => {
    const startedAt = performance.now();
    const request = new XMLHttpRequest();
    request.open("POST", `${apiRoot}/jobs`);
    request.setRequestHeader("Accept", "application/json");
    request.setRequestHeader("X-Video-Upscale-Request", "1");

    request.upload.onprogress = (event) => {
      const total = event.lengthComputable ? event.total : video.size;
      const elapsedSeconds = Math.max((performance.now() - startedAt) / 1_000, 0.001);
      callbacks.onProgress?.({
        loaded: event.loaded,
        total,
        bytesPerSecond: event.loaded / elapsedSeconds,
      });
    };
    request.upload.onloadend = () => callbacks.onUploadComplete?.();
    request.onerror = () => reject(new ApiError("Upload connection failed"));
    request.onabort = () => reject(new ApiError("Upload cancelled"));
    request.onload = () => {
      let payload: { detail?: string; message?: string } | Job | undefined;
      try {
        payload = request.responseText ? JSON.parse(request.responseText) as { detail?: string; message?: string } | Job : undefined;
      } catch {
        // A proxy or server error does not have to return JSON.
      }
      if (request.status < 200 || request.status >= 300) {
        const errorPayload = payload as { detail?: string; message?: string } | undefined;
        reject(new ApiError(errorPayload?.detail ?? errorPayload?.message ?? `Request failed (${request.status})`, request.status));
        return;
      }
      resolve(payload as Job);
    };
    request.send(form);
  });
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
