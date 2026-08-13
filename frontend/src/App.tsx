import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { ApiError, cancelJob, createApiClient, createJob, deleteJob, discardUpload, downloadUrl, getBackends, getConfig, getHealth, getJobLog, getJobs, getUploads } from "./api";
import { outputScales, presetIds, type BackendDescriptor, type ColorCorrection, type Job, type JobStatus, type OutputScale, type PresetId, type RuntimeConfig, type UploadProgress, type UploadSession } from "./types";

const pollMs = 2_000;
const acceptedVideoTypes = "video/*,.mp4,.mov,.mkv,.avi,.webm";

const presets: Array<{
  id: PresetId;
  title: string;
  description: string;
  badge?: string;
  experimental?: boolean;
}> = [
  {
    id: "3b-safe",
    title: "SeedVR2 3B Safe",
    description: "Balanced quality and detail. Recommended for most videos.",
    badge: "Recommended",
  },
  {
    id: "7b-fp8-experimental",
    title: "SeedVR2 7B FP8 Experimental",
    description: "Highest detail. Runs required safety probe first; may fail on this Mac.",
    badge: "Experimental",
    experimental: true,
  },
  {
    id: "7b-fp8-quality",
    title: "SeedVR2 7B FP8 Quality",
    description: "Highest restoration quality tuned for the RTX 4090.",
    badge: "RTX 4090",
  },
  {
    id: "3b-fp8-fast",
    title: "SeedVR2 3B FP8 Fast",
    description: "Faster CUDA restoration using the RTX 4090.",
    badge: "Fast",
  },
];

const activeStatuses: JobStatus[] = ["queued", "preflight", "running"];

const fallbackScaleOptions: RuntimeConfig["output_scales"] = [
  { value: 1, label: "1x Original", description: "Original dimensions; full generative restoration." },
  { value: 0.5, label: "0.5x Balanced", description: "Half width and height; generative restoration with fewer output pixels." },
  { value: 0.25, label: "0.25x Fast", description: "Quarter width and height; experimental generative restoration." },
  { value: 2, label: "2x Upscale", description: "Double width and height; highest processing cost." },
];

type UploadState = UploadProgress & {
  phase: "uploading" | "validating" | "paused";
  filename: string;
  backendDisplayName: string;
};

function icon(name: "upload" | "play" | "x" | "download" | "trash" | "shield" | "check" | "spinner") {
  const paths = {
    upload: <><path d="M12 16V3m0 0-5 5m5-5 5 5" /><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" /></>,
    play: <path d="m9 7 8 5-8 5V7Z" fill="currentColor" stroke="none" />,
    x: <><path d="m7 7 10 10M17 7 7 17" /></>,
    download: <><path d="M12 3v12m0 0-5-5m5 5 5-5" /><path d="M5 18v2a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2" /></>,
    trash: <><path d="M4 7h16m-10 4v6m4-6v6M9 7l1-3h4l1 3m-9 0 1 14h10l1-14" /></>,
    shield: <><path d="M12 3 4 6v5c0 5 3.4 8.8 8 10 4.6-1.2 8-5 8-10V6l-8-3Z" /><path d="m9 12 2 2 4-5" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    spinner: <path d="M20 12a8 8 0 1 1-2.34-5.66" />,
  } satisfies Record<string, ReactElement>;
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "Unknown size";
  const units = ["B", "KB", "MB", "GB"];
  const level = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** level).toFixed(level === 0 ? 0 : 1)} ${units[level]}`;
}

function formatSpeed(bytesPerSecond: number) {
  return bytesPerSecond > 0 ? `${formatBytes(bytesPerSecond)}/s` : "Calculating speed…";
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3_600);
  const minutes = Math.floor((total % 3_600) / 60);
  const remainder = total % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(remainder).padStart(2, "0")}s`;
  return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
}

function formatEtaPoint(seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder > 0 ? `${hours}h ${remainder}m` : `${hours}h`;
}

export function formatEtaRange(low: number, high: number): string {
  if (high <= 3_600) {
    const lowMinutes = Math.max(1, Math.floor(low / 60));
    const highMinutes = Math.max(lowMinutes, Math.ceil(high / 60));
    return `${lowMinutes}–${highMinutes} min`;
  }
  return `${formatEtaPoint(low)}–${formatEtaPoint(high)}`;
}

export function formatScale(scale: number): string {
  return `${Number.isFinite(scale) ? scale : 1}×`;
}

function labelForStatus(status: JobStatus) {
  return {
    queued: "Queued",
    preflight: "7B safety probe",
    running: "Processing",
    completed: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
  }[status];
}

function presetTitle(id: PresetId) {
  return presets.find((preset) => preset.id === id)?.title ?? id;
}

function isActive(job: Job) {
  return activeStatuses.includes(job.status);
}

function sortJobs(jobs: Job[]) {
  return [...jobs].sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at));
}

function jobStageDetail(job: Job) {
  const backendName = job.backend_display_name ?? "Mac M4 Pro";
  if (job.status === "queued") return `Upload validated — waiting for the ${backendName} queue`;
  if (job.status === "preflight") {
    if (job.stage === "preflight-media") return "Preparing the 7B safety sample";
    if (job.stage === "seedvr2-start") return "7B safety probe is rendering";
    if (job.stage === "audio-remux") return "Finalizing the 7B safety probe";
    return "Checking the 7B safety probe";
  }
  if (job.status === "running") {
    if (job.stage === "audio-remux") return "Finalizing MP4 and source audio";
    if (job.stage === "seedvr2-start") return "AI rendering in progress — no reliable percentage";
    return `Preparing SeedVR2 on ${backendName}`;
  }
  return labelForStatus(job.status);
}

function isOutputScale(value: unknown): value is OutputScale {
  return typeof value === "number" && (outputScales as readonly number[]).includes(value);
}

function phaseDetail(job: Job): string {
  const name = job.phase_name
    ? `${job.phase_name.charAt(0).toUpperCase()}${job.phase_name.slice(1)}`
    : jobStageDetail(job);
  const counters = [name];
  if (typeof job.phase_current === "number" && typeof job.phase_total === "number" && job.phase_total > 0) {
    counters.push(`${job.phase_current}/${job.phase_total}`);
  }
  if (typeof job.chunk_current === "number" && typeof job.chunk_total === "number" && job.chunk_total > 0) {
    counters.push(`chunk ${job.chunk_current}/${job.chunk_total}`);
  }
  return counters.join(" · ");
}

function expectedDimension(value: number, scale: OutputScale): number {
  return Math.max(2, Math.floor((value * scale) / 2 + 0.5) * 2);
}

export default function App() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const resumeInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [preset, setPreset] = useState<PresetId>("3b-safe");
  const [availablePresets, setAvailablePresets] = useState<PresetId[]>(["3b-safe", "7b-fp8-experimental"]);
  const [backends, setBackends] = useState<BackendDescriptor[]>([
    { id: "mac", display_name: "Mac M4 Pro", api_base_url: "", preference: 100 },
  ]);
  const [backendChoice, setBackendChoice] = useState("auto");
  const [backendReady, setBackendReady] = useState<Record<string, boolean>>({ mac: true });
  const [colorCorrection, setColorCorrection] = useState<ColorCorrection>("lab");
  const [outputScale, setOutputScale] = useState<OutputScale>(1);
  const [scaleOptions, setScaleOptions] = useState(fallbackScaleOptions);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [sourceDimensions, setSourceDimensions] = useState<{ width: number; height: number } | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [pendingUploads, setPendingUploads] = useState<UploadSession[]>([]);
  const [lastPollReceivedAt, setLastPollReceivedAt] = useState(() => Date.now());
  const [displayNow, setDisplayNow] = useState(() => Date.now());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [uploadState, setUploadState] = useState<UploadState | null>(null);
  const [resumeSessionId, setResumeSessionId] = useState<string | null>(null);
  const [resumeTarget, setResumeTarget] = useState<UploadSession | null>(null);
  const [showDebugConsole, setShowDebugConsole] = useState(false);
  const [debugText, setDebugText] = useState("");
  const debugOffsetRef = useRef(0);

  const clients = useMemo(
    () => new Map(backends.map((backend) => [backend.id, createApiClient(backend)])),
    [backends],
  );
  const multiBackend = backends.length > 1;
  const selectedBackend = useMemo(() => {
    if (backendChoice !== "auto") {
      const explicit = backends.find((backend) => backend.id === backendChoice) ?? null;
      return explicit && backendReady[explicit.id] ? explicit : null;
    }
    return [...backends]
      .sort((left, right) => left.preference - right.preference)
      .find((backend) => backendReady[backend.id]) ?? null;
  }, [backendChoice, backendReady, backends]);

  useEffect(() => {
    void getBackends().then((descriptors) => {
      if (descriptors.length > 0) setBackends(descriptors);
    }).catch(() => {
      // Older backend remains usable as one current-origin Mac service.
    });
  }, []);

  const refresh = useCallback(async () => {
    try {
      if (multiBackend) {
        const snapshots = await Promise.all(backends.map(async (backend) => {
          const client = clients.get(backend.id);
          if (!client) throw new Error("Backend client unavailable");
          try {
            const [health, backendJobs, backendUploads] = await Promise.all([
              client.getHealth(), client.getJobs(), client.getUploads(),
            ]);
            if (health.status.toLowerCase() !== "ok") throw new Error("Backend unavailable");
            return {
              backend,
              ready: true,
              jobs: backendJobs.map((job) => ({ ...job, backend_id: backend.id, backend_display_name: backend.display_name })),
              uploads: backendUploads.map((upload) => ({ ...upload, backend_id: backend.id, backend_display_name: backend.display_name })),
            };
          } catch {
            return { backend, ready: false, jobs: [] as Job[], uploads: [] as UploadSession[] };
          }
        }));
        const receivedAt = Date.now();
        setBackendReady(Object.fromEntries(snapshots.map((snapshot) => [snapshot.backend.id, snapshot.ready])));
        setJobs(sortJobs(snapshots.flatMap((snapshot) => snapshot.jobs)));
        setPendingUploads(snapshots.flatMap((snapshot) => snapshot.uploads));
        setLastPollReceivedAt(receivedAt);
        setDisplayNow(receivedAt);
        setConnectionError(snapshots.some((snapshot) => snapshot.ready) ? null : "Could not reach any Video Upscale backend");
        return;
      }
      const [health, nextJobs, nextUploads] = await Promise.all([getHealth(), getJobs(), getUploads()]);
      if (health.status.toLowerCase() !== "ok") throw new ApiError("Service is not ready");
      const receivedAt = Date.now();
      setJobs(sortJobs(nextJobs));
      setPendingUploads(nextUploads);
      setLastPollReceivedAt(receivedAt);
      setDisplayNow(receivedAt);
      setConnectionError(null);
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : "Could not reach Video Upscale service");
    }
  }, [backends, clients, multiBackend]);

  useEffect(() => {
    void refresh();
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  useEffect(() => {
    const configRequest = multiBackend && selectedBackend
      ? clients.get(selectedBackend.id)?.getConfig()
      : getConfig();
    if (!configRequest) return;
    void configRequest.then((config) => {
      if (presetIds.includes(config.default_profile)) setPreset(config.default_profile);
      setAvailablePresets(config.presets.filter((candidate): candidate is PresetId => presetIds.includes(candidate)));
      if (isOutputScale(config.default_output_scale)) setOutputScale(config.default_output_scale);
      if (Array.isArray(config.output_scales)) {
        const validOptions = config.output_scales.filter((option) => isOutputScale(option.value));
        if (validOptions.length > 0) setScaleOptions(validOptions);
      }
    }).catch(() => {
      // Existing default remains usable while a host is being upgraded.
    });
  }, [clients, multiBackend, selectedBackend]);

  useEffect(() => {
    setSourceDimensions(null);
    if (!selectedFile || typeof URL.createObjectURL !== "function") {
      setPreviewUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(selectedFile);
    setPreviewUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [selectedFile]);

  const hasActiveJob = jobs.some(isActive);
  useEffect(() => {
    if (!hasActiveJob && !showDebugConsole) return;
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [hasActiveJob, refresh, showDebugConsole]);

  const hasTimedActiveJob = jobs.some((job) => isActive(job) && Boolean(job.started_at));
  useEffect(() => {
    if (!hasTimedActiveJob) return;
    const timer = window.setInterval(() => setDisplayNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [hasTimedActiveJob]);

  const activeJobs = useMemo(() => jobs.filter(isActive), [jobs]);
  const finishedJobs = useMemo(() => jobs.filter((job) => !isActive(job)), [jobs]);
  const monitoredJob = uploadState || resumeSessionId ? null : activeJobs[0] ?? finishedJobs[0] ?? null;
  const monitoredJobId = monitoredJob?.id;
  const monitoredJobKey = monitoredJob ? `${monitoredJob.backend_id ?? "mac"}:${monitoredJob.id}` : null;
  const monitoredJobIsActive = monitoredJob ? isActive(monitoredJob) : false;

  useEffect(() => {
    debugOffsetRef.current = 0;
    setDebugText("");
  }, [monitoredJobKey]);

  useEffect(() => {
    if (!showDebugConsole || !monitoredJobId) return;
    let cancelled = false;
    const readLog = async () => {
      try {
        const owner = monitoredJob?.backend_id;
        const log = multiBackend && owner
          ? await clients.get(owner)!.getJobLog(monitoredJobId, debugOffsetRef.current)
          : await getJobLog(monitoredJobId, debugOffsetRef.current);
        if (cancelled) return;
        debugOffsetRef.current = log.next_offset;
        if (log.text) {
          setDebugText((current) => log.truncated ? log.text : `${current}${log.text}`);
        }
      } catch (error) {
        if (!cancelled) setDebugText((current) => current || `Could not read debug log: ${error instanceof Error ? error.message : "unknown error"}`);
      }
    };
    void readLog();
    if (!monitoredJobIsActive) return () => { cancelled = true; };
    const timer = window.setInterval(() => void readLog(), pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [clients, monitoredJob, monitoredJobId, monitoredJobIsActive, multiBackend, showDebugConsole]);

  const chooseFile = (file?: File) => {
    if (!file) return;
    setActionError(null);
    setResumeSessionId(null);
    setResumeTarget(null);
    setUploadState(null);
    setSelectedFile(file);
  };

  const chooseResumeFile = (file?: File) => {
    if (!file || !resumeTarget) return;
    if (file.name !== resumeTarget.filename || file.size !== resumeTarget.total_bytes) {
      setActionError("Selected file does not match this pending upload");
      return;
    }
    setActionError(null);
    setUploadState(null);
    setSelectedFile(file);
    setResumeSessionId(resumeTarget.id);
  };

  const beginResume = (upload: UploadSession) => {
    setActionError(null);
    setResumeTarget(upload);
    if (upload.backend_id) setBackendChoice(upload.backend_id);
    window.setTimeout(() => resumeInputRef.current?.click(), 0);
  };

  const endUpload = async (upload: UploadSession) => {
    if (!window.confirm(`End upload for ${upload.filename}? Confirmed partial data will be deleted.`)) return;
    setActionError(null);
    try {
      if (multiBackend && upload.backend_id) await clients.get(upload.backend_id)!.discardUpload(upload.id);
      else await discardUpload(upload.id);
      setPendingUploads((current) => current.filter((item) => item.id !== upload.id));
      if (resumeSessionId === upload.id) {
        setSelectedFile(null);
        setResumeSessionId(null);
        setResumeTarget(null);
        setUploadState(null);
      }
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not end pending upload");
    }
  };

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedFile || isSubmitting) return;
    setActionError(null);
    setIsSubmitting(true);
    setUploadState({
      phase: "uploading",
      filename: selectedFile.name,
      backendDisplayName: selectedBackend?.display_name ?? "selected backend",
      loaded: 0,
      total: selectedFile.size,
      bytesPerSecond: 0,
      retryAttempt: 0,
    });
    try {
      if (!selectedBackend) throw new ApiError("Selected processing backend is offline");
      const submit = multiBackend ? clients.get(selectedBackend.id)!.createJob : createJob;
      const createdRaw = await submit(selectedFile, preset, colorCorrection, outputScale, {
        resumeSessionId: resumeSessionId ?? undefined,
        onSession: setResumeSessionId,
        onProgress: (progress) => {
          setUploadState({
            phase: "uploading",
            filename: selectedFile.name,
            backendDisplayName: selectedBackend.display_name,
            ...progress,
          });
        },
        onUploadComplete: () => {
          setUploadState((current) => current && {
            ...current,
            phase: "validating",
            loaded: Math.max(current.loaded, current.total),
          });
        },
      });
      const created = multiBackend
        ? { ...createdRaw, backend_id: selectedBackend.id, backend_display_name: selectedBackend.display_name }
        : createdRaw;
      setJobs((current) => sortJobs([created, ...current.filter((job) => !(
        job.id === created.id && job.backend_id === created.backend_id
      ))]));
      setSelectedFile(null);
      setUploadState(null);
      setResumeSessionId(null);
      await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Upload failed");
      setUploadState((current) => current && { ...current, phase: "paused", retryAttempt: 0 });
      await refresh();
    } finally {
      setIsSubmitting(false);
    }
  };

  const onCancel = async (job: Job) => {
    setActionError(null);
    try {
      const updatedRaw = multiBackend && job.backend_id
        ? await clients.get(job.backend_id)!.cancelJob(job.id)
        : await cancelJob(job.id);
      const updated = { ...updatedRaw, backend_id: job.backend_id, backend_display_name: job.backend_display_name };
      setJobs((current) => sortJobs(current.map((candidate) => (
        candidate.id === job.id && candidate.backend_id === job.backend_id ? updated : candidate
      ))));
      await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not cancel job");
    }
  };

  const onDelete = async (job: Job) => {
    setActionError(null);
    try {
      if (multiBackend && job.backend_id) await clients.get(job.backend_id)!.deleteJob(job.id);
      else await deleteJob(job.id);
      setJobs((current) => current.filter((candidate) => !(
        candidate.id === job.id && candidate.backend_id === job.backend_id
      )));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not delete job");
    }
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">{icon("play")}</span>
          <div>
            <h1>Video Upscale</h1>
            <p>Private AI video restoration on your Mac or RTX 4090.</p>
          </div>
        </div>
        <div className="topbar-status" aria-label="Connection information">
          <span className="safe-status">{icon("shield")} Private via Tailscale</span>
          <span className={`service-status ${connectionError ? "service-status--error" : ""}`}>
            <i aria-hidden="true" />
            {connectionError ? "Service unavailable" : selectedBackend?.display_name ?? "No backend ready"}
          </span>
        </div>
      </header>

      {(connectionError || actionError) && (
        <section className="message message--error" role="alert">
          <strong>Action needed</strong>
          <span>{actionError ?? connectionError}</span>
          {connectionError && <button className="text-button" type="button" onClick={() => void refresh()}>Retry</button>}
        </section>
      )}

      <section className="workspace" aria-label="Video upscaling workspace">
        <form className="panel settings-panel" onSubmit={onSubmit}>
          <PanelTitle number="1" title="Upload & Settings" />
          <label className="select-label backend-select">
            <span>Processing host</span>
            <select aria-label="Processing host" value={backendChoice} disabled={isSubmitting} onChange={(event) => setBackendChoice(event.target.value)}>
              <option value="auto">Auto — RTX 4090 preferred</option>
              {backends.map((backend) => (
                <option key={backend.id} value={backend.id}>{backend.display_name}{backendReady[backend.id] ? " — Ready" : " — Offline"}</option>
              ))}
            </select>
            <small>{backendChoice === "auto" ? `Auto selected ${selectedBackend?.display_name ?? "no available backend"}` : selectedBackend?.display_name}</small>
          </label>
          <input
            ref={fileInputRef}
            id="video-file"
            className="visually-hidden"
            aria-label="Choose new video file"
            type="file"
            accept={acceptedVideoTypes}
            disabled={isSubmitting}
            onChange={(event) => chooseFile(event.currentTarget.files?.[0])}
          />
          <input
            ref={resumeInputRef}
            className="visually-hidden"
            aria-label={resumeTarget ? `Choose file to resume ${resumeTarget.filename}` : "Choose file to resume upload"}
            type="file"
            accept={acceptedVideoTypes}
            disabled={isSubmitting}
            onChange={(event) => {
              chooseResumeFile(event.currentTarget.files?.[0]);
              event.currentTarget.value = "";
            }}
          />
          <label
            className={`drop-zone ${isDragging ? "drop-zone--active" : ""}`}
            htmlFor="video-file"
            onDragOver={(event) => { event.preventDefault(); setIsDragging(true); }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setIsDragging(false);
              chooseFile(event.dataTransfer.files[0]);
            }}
          >
            <span className="upload-icon">{icon("upload")}</span>
            <strong className="upload-copy upload-copy--desktop">Upload a new video</strong>
            <span className="drop-copy drop-copy--desktop">or <em>click to browse</em></span>
            <strong className="upload-copy upload-copy--mobile">Upload a new video</strong>
            <span className="drop-copy drop-copy--mobile">MP4, MOV, MKV, AVI, or WebM</span>
            <span className="photo-action">Choose from Photos</span>
          </label>

          {pendingUploads.length > 0 && (
            <section className="pending-uploads" aria-label="Pending uploads">
              <h3>Pending uploads</h3>
              {pendingUploads.map((upload) => {
                const percent = Math.min(100, Math.round((upload.accepted_bytes / upload.total_bytes) * 100));
                return (
                  <article className="pending-upload" key={upload.id}>
                    <div>
                      <strong>{upload.filename}</strong>
                      <span>{formatBytes(upload.accepted_bytes)} / {formatBytes(upload.total_bytes)} confirmed · {percent}%</span>
                      <small>{upload.backend_display_name ?? "Mac M4 Pro"} · Available until {new Date(upload.expires_at).toLocaleString()}</small>
                    </div>
                    <div className="pending-upload__actions">
                      <button type="button" disabled={isSubmitting} aria-label={`Resume ${upload.filename}`} onClick={() => beginResume(upload)}>Resume</button>
                      <button type="button" disabled={isSubmitting} aria-label={`End upload ${upload.filename}`} onClick={() => void endUpload(upload)}>End upload</button>
                    </div>
                  </article>
                );
              })}
            </section>
          )}

          {selectedFile && (
            <section className="selected-file" aria-label="Selected video">
              <span className="file-icon">{icon("play")}</span>
              <div>
                <strong>{selectedFile.name}</strong>
                <span>{formatBytes(selectedFile.size)}</span>
                {sourceDimensions && (
                  <span>Expected output: {expectedDimension(sourceDimensions.width, outputScale)} × {expectedDimension(sourceDimensions.height, outputScale)}</span>
                )}
              </div>
              <button type="button" className="icon-button" aria-label="Remove selected video" disabled={isSubmitting} onClick={() => { setSelectedFile(null); setResumeSessionId(null); setResumeTarget(null); setUploadState(null); }}>{icon("x")}</button>
            </section>
          )}

          {previewUrl && (
            <video
              className="visually-hidden"
              src={previewUrl}
              preload="metadata"
              aria-hidden="true"
              onLoadedMetadata={(event) => {
                const video = event.currentTarget;
                if (video.videoWidth > 0 && video.videoHeight > 0) {
                  setSourceDimensions({ width: video.videoWidth, height: video.videoHeight });
                }
              }}
              onError={() => setSourceDimensions(null)}
            />
          )}

          {uploadState && <UploadMonitor upload={uploadState} />}

          <fieldset className="preset-fieldset">
            <legend>Upscale model</legend>
            {presets.filter((option) => availablePresets.includes(option.id)).map((option) => (
              <label key={option.id} className={`preset ${preset === option.id ? "preset--selected" : ""} ${option.experimental ? "preset--experimental" : ""}`}>
                <input type="radio" name="preset" value={option.id} checked={preset === option.id} disabled={isSubmitting} onChange={() => setPreset(option.id)} />
                <span className="radio-mark" aria-hidden="true" />
                <span className="preset-copy"><strong>{option.title}</strong><small>{option.description}</small></span>
                {option.badge && <span className={`badge ${option.experimental ? "badge--warn" : ""}`}>{option.badge}</span>}
              </label>
            ))}
          </fieldset>

          <fieldset className="scale-fieldset">
            <legend>Output resolution</legend>
            {scaleOptions.map((option) => (
              <label key={option.value} className={`scale-option ${outputScale === option.value ? "scale-option--selected" : ""}`}>
                <input type="radio" name="output-scale" value={option.value} checked={outputScale === option.value} disabled={isSubmitting} onChange={() => setOutputScale(option.value)} />
                <span className="radio-mark" aria-hidden="true" />
                <span className="scale-copy"><strong>{option.label}</strong><small>{option.description}</small></span>
              </label>
            ))}
          </fieldset>

          <label className="select-label">
            <span>Source color correction</span>
            <select value={colorCorrection} disabled={isSubmitting} onChange={(event) => setColorCorrection(event.target.value as ColorCorrection)}>
              <option value="lab">LAB color match (recommended)</option>
              <option value="none">None — preserve source color</option>
            </select>
          </label>

          {preset === "7b-fp8-experimental" && (
            <p className="experimental-note"><strong>7B safety probe required.</strong> First run checks a ≤10-second, ≤480p sample. Full job starts only after probe passes.</p>
          )}

          <button className="primary-button" type="submit" disabled={!selectedFile || isSubmitting}>
            {isSubmitting ? icon("spinner") : icon("play")}
            {isSubmitting ? uploadState?.phase === "validating" ? "Validating video…" : "Uploading video…" : resumeSessionId ? "Resume upload" : "Start processing"}
          </button>
          <p className="settings-footnote">{formatScale(outputScale)} output. One job processes at a time. Results stay until you delete them.</p>
        </form>

        <section className="panel jobs-panel" aria-live="polite">
          <PanelTitle number="2" title={`Job Queue & Progress${activeJobs.length ? ` (${activeJobs.length})` : ""}`} />
          {activeJobs.length > 0 ? (
            <div className="job-list">
              {activeJobs.map((job) => <JobCard key={`${job.backend_id ?? "mac"}:${job.id}`} job={job} onCancel={onCancel} now={displayNow} lastPollReceivedAt={lastPollReceivedAt} />)}
            </div>
          ) : (
            <div className="empty-state">
              <span>{icon("upload")}</span>
              <h2>Nothing processing</h2>
              <p>Choose a video, select a model, then start a job.</p>
            </div>
          )}
          <div className="monitor-controls">
            <button type="button" className="monitor-toggle" onClick={() => setShowDebugConsole((current) => !current)}>
              {showDebugConsole ? "Hide debug console" : "Show debug console"}
            </button>
            <span>Live local status and adapter output</span>
          </div>
          {showDebugConsole && (
            <DebugConsole upload={uploadState} job={monitoredJob} text={debugText} />
          )}
          <div className="queue-footnote">
            <span><i className="pulse-dot" /> {selectedBackend?.display_name ?? "No backend ready"}</span>
            <span>Single-job queue protects memory</span>
          </div>
        </section>

        <section className="panel results-panel">
          <PanelTitle number="3" title="Result & Details" />
          {finishedJobs.length > 0 ? (
            <div className="result-list">
              {finishedJobs.map((job) => <ResultCard key={`${job.backend_id ?? "mac"}:${job.id}`} job={job} onDelete={onDelete} client={job.backend_id ? clients.get(job.backend_id) : undefined} />)}
            </div>
          ) : (
            <div className="empty-state empty-state--result">
              <span>{icon("check")}</span>
              <h2>Results appear here</h2>
              <p>Finished MP4 files remain available until you delete them.</p>
            </div>
          )}
        </section>
      </section>

      <footer>
          <span>Private tailnet processing. Videos go only to your selected machine.</span>
        <span>MP4 output with source audio preserved when compatible.</span>
      </footer>
    </main>
  );
}

function PanelTitle({ number, title }: { number: string; title: string }) {
  return <h2 className="panel-title"><span>{number}</span>{title}</h2>;
}

function UploadMonitor({ upload }: { upload: UploadState }) {
  const progress = upload.total > 0 ? Math.min(100, Math.round((upload.loaded / upload.total) * 100)) : 0;
  const isValidating = upload.phase === "validating";
  const isPaused = upload.phase === "paused";
  return (
    <section className="upload-monitor" aria-live="polite">
      <div>
        <strong>{isValidating ? `Validating on ${upload.backendDisplayName}` : isPaused ? "Upload paused — resume available" : upload.retryAttempt > 0 ? `Retrying upload (${upload.retryAttempt}/3)` : `Uploading to ${upload.backendDisplayName}`}</strong>
        <span>{isValidating ? "Transfer complete. Checking this video before queuing it." : `${formatBytes(upload.loaded)} / ${formatBytes(upload.total)} · ${isPaused ? "waiting to resume" : formatSpeed(upload.bytesPerSecond)} · server confirmed`}</span>
      </div>
      <div className="upload-progress" aria-label={`Upload ${progress}%`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div>
    </section>
  );
}

function DebugConsole({ upload, job, text }: { upload: UploadState | null; job: Job | null; text: string }) {
  const localStatus = upload
    ? upload.phase === "validating"
      ? `[Browser] Transfer complete. ${upload.backendDisplayName} is validating the video.`
      : upload.phase === "paused"
        ? `[Browser] Upload paused at server-confirmed ${formatBytes(upload.loaded)} / ${formatBytes(upload.total)}. Resume is available.`
      : `[Browser] ${upload.retryAttempt > 0 ? `Retry ${upload.retryAttempt}/3; ` : ""}server confirmed ${formatBytes(upload.loaded)} / ${formatBytes(upload.total)} at ${formatSpeed(upload.bytesPerSecond)}.`
    : null;
  const jobStatus = job ? `[Job ${job.id}] ${jobStageDetail(job)}.` : "[Job] No server job yet";
  const title = upload ? "Upload status" : job && isActive(job) ? "Live job log" : "Historical job log";
  const subtitle = upload
    ? "Waiting for upload finalization"
    : job && isActive(job)
      ? "Polling every 2 seconds"
      : "Fetched once · processing is not active";
  const completion = job?.status === "completed" ? "[WebUI] Backend validated output and recorded 100% completion." : null;
  return (
    <section className="debug-console" aria-label={title}>
      <div className="debug-console__head">
        <h3>{title}</h3>
        <span>{subtitle}</span>
      </div>
      <pre>{[localStatus, jobStatus, text || (upload ? null : "[Adapter] No output yet."), completion].filter(Boolean).join("\n")}</pre>
    </section>
  );
}

function JobCard({
  job,
  onCancel,
  now,
  lastPollReceivedAt,
}: {
  job: Job;
  onCancel: (job: Job) => Promise<void>;
  now: number;
  lastPollReceivedAt: number;
}) {
  const staleWarning = job.heartbeat_stale
    ? "Progress signal stale — processing may still be active"
    : job.progress_stale
      ? "Process is alive, but measured work has not advanced"
      : null;
  const measuredProgress = job.progress_source === "measured"
    && Number.isFinite(job.progress)
    && staleWarning === null;
  const progress = Math.max(0, Math.min(99, Math.round(job.progress)));
  const detail = phaseDetail(job);
  const elapsed = job.started_at && typeof job.elapsed_seconds === "number" && Number.isFinite(job.elapsed_seconds)
    ? Math.max(0, job.elapsed_seconds + Math.floor(Math.max(0, now - lastPollReceivedAt) / 1_000))
    : null;
  const hasEta = staleWarning === null
    && typeof job.eta_low_seconds === "number"
    && Number.isFinite(job.eta_low_seconds)
    && typeof job.eta_high_seconds === "number"
    && Number.isFinite(job.eta_high_seconds)
    && job.eta_low_seconds >= 0
    && job.eta_high_seconds >= job.eta_low_seconds
    && job.eta_confidence !== "none";
  const progressValueText = staleWarning ?? "Calibrating progress";
  return (
    <article className="job-card">
      <div className="job-card__top">
        <div className="video-tile">{icon("play")}</div>
        <div className="job-card__copy">
          <strong>{job.original_filename}</strong>
          <span className="job-state"><i />{labelForStatus(job.status)}</span>
          <small>{detail}</small>
          <small>{job.backend_display_name ?? "Mac M4 Pro"} · {presetTitle(job.preset)} · {formatScale(job.output_scale)} · MP4</small>
        </div>
        <strong className="job-percent">{measuredProgress ? `${progress}%` : staleWarning ? "Attention" : "Live"}</strong>
      </div>
      <div className="progress-row">
        {measuredProgress ? (
          <div className="progress-bar" aria-label={`${progress}% complete`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div>
        ) : (
          <div className="progress-bar progress-bar--indeterminate" aria-label="Processing status" role="progressbar" aria-valuetext={progressValueText}><span /></div>
        )}
        <button type="button" className="cancel-button" onClick={() => void onCancel(job)}>Cancel</button>
      </div>
      <div className="job-timing">
        {elapsed !== null && <span>Elapsed {formatDuration(elapsed)}</span>}
        {staleWarning ? (
          <p className="progress-warning" role="status">{staleWarning}</p>
        ) : hasEta ? (
          <>
            <span>ETA {formatEtaRange(job.eta_low_seconds as number, job.eta_high_seconds as number)}</span>
            <span className="eta-confidence">{job.eta_confidence.charAt(0).toUpperCase() + job.eta_confidence.slice(1)} confidence</span>
          </>
        ) : (
          <span>Calibrating…</span>
        )}
        {job.last_heartbeat_at && <small>Heartbeat <time dateTime={job.last_heartbeat_at}>{job.last_heartbeat_at}</time></small>}
        {job.last_progress_at && <small>Last measured progress <time dateTime={job.last_progress_at}>{job.last_progress_at}</time></small>}
      </div>
    </article>
  );
}

function ResultCard({
  job,
  onDelete,
  client,
}: {
  job: Job;
  onDelete: (job: Job) => Promise<void>;
  client?: ReturnType<typeof createApiClient>;
}) {
  const completed = job.status === "completed";
  return (
    <article className={`result-card ${completed ? "result-card--success" : "result-card--failed"}`}>
      <div className="result-card__head">
        <span className="result-icon">{completed ? icon("check") : icon("x")}</span>
        <div>
          <strong>{completed ? job.output_filename ?? job.original_filename : job.original_filename}</strong>
          <p>{completed ? <>{job.backend_display_name ?? "Mac M4 Pro"} · {presetTitle(job.preset)} · {formatScale(job.output_scale)} MP4 · <span>100%</span></> : labelForStatus(job.status)}</p>
        </div>
        <button type="button" className="icon-button" aria-label={`Delete ${job.original_filename}`} onClick={() => void onDelete(job)}>{icon("trash")}</button>
      </div>
      {completed ? (
        <a className="download-button" href={client ? client.downloadUrl(job.id) : downloadUrl(job.id)} download>{icon("download")}Download MP4</a>
      ) : (
        <p className="job-error" role="alert">{job.error ?? "This job did not produce a video."}</p>
      )}
    </article>
  );
}
