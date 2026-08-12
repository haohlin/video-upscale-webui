export const presetIds = [
  "3b-safe",
  "7b-fp8-experimental",
] as const;

export type PresetId = (typeof presetIds)[number];
export type ColorCorrection = "lab" | "none";
export type JobStatus =
  | "queued"
  | "preflight"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface Job {
  id: string;
  original_filename: string;
  preset: PresetId;
  color_correction: ColorCorrection;
  status: JobStatus;
  progress: number;
  stage: string;
  created_at: string;
  updated_at: string;
  output_filename: string | null;
  error: string | null;
  requires_preflight: boolean;
}

export interface Health {
  status: string;
  runner?: "ready" | "unavailable";
  detail?: string;
}

export interface RuntimeConfig {
  default_profile: PresetId;
  presets: PresetId[];
}

export interface UploadProgress {
  loaded: number;
  total: number;
  bytesPerSecond: number;
}

export interface JobLogTail {
  text: string;
  next_offset: number;
  size: number;
  truncated: boolean;
}
