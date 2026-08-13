export const presetIds = [
  "3b-safe",
  "7b-fp8-experimental",
] as const;

export type PresetId = (typeof presetIds)[number];
export type ColorCorrection = "lab" | "none";
export const outputScales = [0.25, 0.5, 1, 2] as const;
export type OutputScale = (typeof outputScales)[number];
export type EtaConfidence = "none" | "low" | "medium" | "high";
export type ProgressSource = "none" | "measured" | "historical";
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
  output_scale: OutputScale;
  target_width: number;
  target_height: number;
  status: JobStatus;
  progress: number;
  stage: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  elapsed_seconds: number | null;
  last_heartbeat_at: string | null;
  last_progress_at: string | null;
  progress_source: ProgressSource;
  phase_name: string | null;
  phase_current: number | null;
  phase_total: number | null;
  chunk_current: number | null;
  chunk_total: number | null;
  eta_low_seconds: number | null;
  eta_high_seconds: number | null;
  eta_confidence: EtaConfidence;
  heartbeat_stale: boolean;
  progress_stale: boolean;
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
  default_output_scale: OutputScale;
  output_scales: Array<{ value: OutputScale; label: string; description: string }>;
}

export interface UploadProgress {
  loaded: number;
  total: number;
  bytesPerSecond: number;
  retryAttempt: number;
}

export interface JobLogTail {
  text: string;
  next_offset: number;
  size: number;
  truncated: boolean;
}
