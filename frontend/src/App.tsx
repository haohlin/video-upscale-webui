import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { ApiError, cancelJob, createJob, deleteJob, downloadUrl, getConfig, getHealth, getJobLog, getJobs } from "./api";
import { presetIds, type ColorCorrection, type Job, type JobStatus, type PresetId, type UploadProgress } from "./types";

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
];

const activeStatuses: JobStatus[] = ["queued", "preflight", "running"];

type UploadState = UploadProgress & {
  phase: "uploading" | "validating";
  filename: string;
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
  if (job.status === "queued") return "Upload validated — waiting for the Mac queue";
  if (job.status === "preflight") {
    if (job.stage === "preflight-media") return "Preparing the 7B safety sample";
    if (job.stage === "seedvr2-start") return "7B safety probe is rendering";
    if (job.stage === "audio-remux") return "Finalizing the 7B safety probe";
    return "Checking the 7B safety probe";
  }
  if (job.status === "running") {
    if (job.stage === "audio-remux") return "Finalizing MP4 and source audio";
    if (job.stage === "seedvr2-start") return "AI rendering in progress — no reliable percentage";
    return "Preparing SeedVR2 on the Mac";
  }
  return labelForStatus(job.status);
}

function hasMeasuredJobProgress(job: Job) {
  return job.stage === "preflight-media" || job.stage === "audio-remux";
}

export default function App() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [preset, setPreset] = useState<PresetId>("3b-safe");
  const [colorCorrection, setColorCorrection] = useState<ColorCorrection>("lab");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [uploadState, setUploadState] = useState<UploadState | null>(null);
  const [showDebugConsole, setShowDebugConsole] = useState(false);
  const [debugText, setDebugText] = useState("");
  const debugOffsetRef = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const [health, nextJobs] = await Promise.all([getHealth(), getJobs()]);
      if (health.status.toLowerCase() !== "ok") throw new ApiError("Service is not ready");
      setJobs(sortJobs(nextJobs));
      setConnectionError(null);
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : "Could not reach Video Upscale service");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  useEffect(() => {
    void getConfig().then((config) => {
      if (presetIds.includes(config.default_profile)) setPreset(config.default_profile);
    }).catch(() => {
      // Existing default remains usable while a host is being upgraded.
    });
  }, []);

  const hasActiveJob = jobs.some(isActive);
  useEffect(() => {
    if (!hasActiveJob) return;
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [hasActiveJob, refresh]);

  const activeJobs = useMemo(() => jobs.filter(isActive), [jobs]);
  const finishedJobs = useMemo(() => jobs.filter((job) => !isActive(job)), [jobs]);
  const monitoredJob = activeJobs[0] ?? finishedJobs[0] ?? null;
  const monitoredJobId = monitoredJob?.id;
  const monitoredJobIsActive = monitoredJob ? isActive(monitoredJob) : false;

  useEffect(() => {
    debugOffsetRef.current = 0;
    setDebugText("");
  }, [monitoredJobId]);

  useEffect(() => {
    if (!showDebugConsole || !monitoredJobId) return;
    let cancelled = false;
    const readLog = async () => {
      try {
        const log = await getJobLog(monitoredJobId, debugOffsetRef.current);
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
  }, [monitoredJobId, monitoredJobIsActive, showDebugConsole]);

  const chooseFile = (file?: File) => {
    if (!file) return;
    setActionError(null);
    setSelectedFile(file);
  };

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedFile || isSubmitting) return;
    setActionError(null);
    setIsSubmitting(true);
    setUploadState({
      phase: "uploading",
      filename: selectedFile.name,
      loaded: 0,
      total: selectedFile.size,
      bytesPerSecond: 0,
    });
    try {
      const created = await createJob(selectedFile, preset, colorCorrection, {
        onProgress: (progress) => {
          setUploadState({ phase: "uploading", filename: selectedFile.name, ...progress });
        },
        onUploadComplete: () => {
          setUploadState((current) => current && {
            ...current,
            phase: "validating",
            loaded: Math.max(current.loaded, current.total),
          });
        },
      });
      setJobs((current) => sortJobs([created, ...current.filter((job) => job.id !== created.id)]));
      setSelectedFile(null);
      setUploadState(null);
      await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Upload failed");
      setUploadState(null);
    } finally {
      setIsSubmitting(false);
    }
  };

  const onCancel = async (id: string) => {
    setActionError(null);
    try {
      const updated = await cancelJob(id);
      setJobs((current) => sortJobs(current.map((job) => (job.id === id ? updated : job))));
      await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not cancel job");
    }
  };

  const onDelete = async (id: string) => {
    setActionError(null);
    try {
      await deleteJob(id);
      setJobs((current) => current.filter((job) => job.id !== id));
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
            <p>Local AI upscaling for your videos. Private. All on your Mac.</p>
          </div>
        </div>
        <div className="topbar-status" aria-label="Connection information">
          <span className="safe-status">{icon("shield")} Private via Tailscale</span>
          <span className={`service-status ${connectionError ? "service-status--error" : ""}`}>
            <i aria-hidden="true" />
            {connectionError ? "Service unavailable" : "Mac processing service"}
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
          <input
            ref={fileInputRef}
            id="video-file"
            className="visually-hidden"
            aria-label="Choose video file"
            type="file"
            accept={acceptedVideoTypes}
            disabled={isSubmitting}
            onChange={(event) => chooseFile(event.currentTarget.files?.[0])}
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
            <strong className="upload-copy upload-copy--desktop">Drag & drop a video here</strong>
            <span className="drop-copy drop-copy--desktop">or <em>click to browse</em></span>
            <strong className="upload-copy upload-copy--mobile">Upload a video to get started</strong>
            <span className="drop-copy drop-copy--mobile">MP4, MOV, MKV, AVI, or WebM</span>
            <span className="photo-action">Choose from Photos</span>
          </label>

          {selectedFile && (
            <section className="selected-file" aria-label="Selected video">
              <span className="file-icon">{icon("play")}</span>
              <div>
                <strong>{selectedFile.name}</strong>
                <span>{formatBytes(selectedFile.size)}</span>
              </div>
              <button type="button" className="icon-button" aria-label="Remove selected video" disabled={isSubmitting} onClick={() => setSelectedFile(null)}>{icon("x")}</button>
            </section>
          )}

          {uploadState && <UploadMonitor upload={uploadState} />}

          <fieldset className="preset-fieldset">
            <legend>Upscale model</legend>
            {presets.map((option) => (
              <label key={option.id} className={`preset ${preset === option.id ? "preset--selected" : ""} ${option.experimental ? "preset--experimental" : ""}`}>
                <input type="radio" name="preset" value={option.id} checked={preset === option.id} disabled={isSubmitting} onChange={() => setPreset(option.id)} />
                <span className="radio-mark" aria-hidden="true" />
                <span className="preset-copy"><strong>{option.title}</strong><small>{option.description}</small></span>
                {option.badge && <span className={`badge ${option.experimental ? "badge--warn" : ""}`}>{option.badge}</span>}
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
            {isSubmitting ? uploadState?.phase === "validating" ? "Validating video…" : "Uploading video…" : "Start processing"}
          </button>
          <p className="settings-footnote">2× output. One job processes at a time. Work files clear automatically.</p>
        </form>

        <section className="panel jobs-panel" aria-live="polite">
          <PanelTitle number="2" title={`Job Queue & Progress${activeJobs.length ? ` (${activeJobs.length})` : ""}`} />
          {activeJobs.length > 0 ? (
            <div className="job-list">
              {activeJobs.map((job) => <JobCard key={job.id} job={job} onCancel={onCancel} />)}
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
            <span><i className="pulse-dot" /> MacBook Pro</span>
            <span>Single-job queue protects memory</span>
          </div>
        </section>

        <section className="panel results-panel">
          <PanelTitle number="3" title="Result & Details" />
          {finishedJobs.length > 0 ? (
            <div className="result-list">
              {finishedJobs.map((job) => <ResultCard key={job.id} job={job} onDelete={onDelete} />)}
            </div>
          ) : (
            <div className="empty-state empty-state--result">
              <span>{icon("check")}</span>
              <h2>Results appear here</h2>
              <p>Finished MP4 files stay available for 24 hours.</p>
            </div>
          )}
        </section>
      </section>

      <footer>
        <span>Private local processing. Videos never leave this Mac.</span>
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
  return (
    <section className="upload-monitor" aria-live="polite">
      <div>
        <strong>{isValidating ? "Validating on your Mac" : "Uploading to your Mac"}</strong>
        <span>{isValidating ? "Transfer complete. Checking this video before queuing it." : `${formatBytes(upload.loaded)} / ${formatBytes(upload.total)} · ${formatSpeed(upload.bytesPerSecond)}`}</span>
      </div>
      <div className="upload-progress" aria-label={`Upload ${progress}%`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div>
    </section>
  );
}

function DebugConsole({ upload, job, text }: { upload: UploadState | null; job: Job | null; text: string }) {
  const localStatus = upload
    ? upload.phase === "validating"
      ? "[Browser] Transfer complete. Mac is validating the video."
      : `[Browser] Uploading ${formatBytes(upload.loaded)} / ${formatBytes(upload.total)} at ${formatSpeed(upload.bytesPerSecond)}.`
    : null;
  const jobStatus = job ? `[Job] ${jobStageDetail(job)}.` : "[Job] No server job yet. A job appears after upload and validation.";
  return (
    <section className="debug-console" aria-label="Live debug console">
      <div className="debug-console__head">
        <h3>Live debug console</h3>
        <span>{job ? "Polling job log every 2 seconds" : "Waiting for job"}</span>
      </div>
      <pre>{[localStatus, jobStatus, text || "[Adapter] No output yet."].filter(Boolean).join("\n")}</pre>
    </section>
  );
}

function JobCard({ job, onCancel }: { job: Job; onCancel: (id: string) => Promise<void> }) {
  const progress = Math.max(0, Math.min(100, job.progress));
  const measuredProgress = hasMeasuredJobProgress(job);
  const detail = jobStageDetail(job);
  return (
    <article className="job-card">
      <div className="job-card__top">
        <div className="video-tile">{icon("play")}</div>
        <div className="job-card__copy">
          <strong>{job.original_filename}</strong>
          <span className="job-state"><i />{labelForStatus(job.status)}</span>
          <small>{detail}</small>
          <small>{presetTitle(job.preset)} · 2× · MP4</small>
        </div>
        <strong className="job-percent">{measuredProgress ? `${progress}%` : "Live"}</strong>
      </div>
      <div className="progress-row">
        {measuredProgress ? (
          <div className="progress-bar" aria-label={`${progress}% complete`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div>
        ) : (
          <div className="progress-bar progress-bar--indeterminate" aria-label="Processing status" role="progressbar" aria-valuetext={detail}><span /></div>
        )}
        <button type="button" className="cancel-button" onClick={() => void onCancel(job.id)}>Cancel</button>
      </div>
    </article>
  );
}

function ResultCard({ job, onDelete }: { job: Job; onDelete: (id: string) => Promise<void> }) {
  const completed = job.status === "completed";
  return (
    <article className={`result-card ${completed ? "result-card--success" : "result-card--failed"}`}>
      <div className="result-card__head">
        <span className="result-icon">{completed ? icon("check") : icon("x")}</span>
        <div>
          <strong>{completed ? job.output_filename ?? job.original_filename : job.original_filename}</strong>
          <p>{completed ? `${presetTitle(job.preset)} · 2× MP4` : labelForStatus(job.status)}</p>
        </div>
        <button type="button" className="icon-button" aria-label={`Delete ${job.original_filename}`} onClick={() => void onDelete(job.id)}>{icon("trash")}</button>
      </div>
      {completed ? (
        <a className="download-button" href={downloadUrl(job.id)} download>{icon("download")}Download MP4</a>
      ) : (
        <p className="job-error" role="alert">{job.error ?? "This job did not produce a video."}</p>
      )}
    </article>
  );
}
