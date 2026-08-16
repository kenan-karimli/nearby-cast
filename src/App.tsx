import { useState, useEffect, useCallback, useRef } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { canStartScreenCast } from './casting';

interface DisplayTarget {
  id: string;
  name: string;
  platform: string;
  ip_address: string;
  port: number;
  status: string;
  protocol: string;
  screen_cast: boolean;
  media_cast: boolean;
  audio_cast: boolean;
  requires_pin: boolean;
  support_notes: string;
}

interface WlOutput {
  name: string;
  description: string;
  resolution: string;
}

interface WlWindow {
  id: string;
  title: string;
  class_name: string;
  geometry: string;
}

type CastStage =
  | 'idle'
  | 'connecting'
  | 'authenticating'
  | 'negotiating'
  | 'starting_stream'
  | 'waiting_receiver'
  | 'casting'
  | 'reconnecting'
  | 'failed';

interface CastStatus {
  stage: CastStage;
  target: DisplayTarget | null;
  outputName?: string;
  geometry?: string;
  selectionLabel?: string;
  error?: string;
  protocol?: string;
}

interface CastMetrics {
  frame?: number | null;
  fps?: number | null;
  bitrate_kbps?: number | null;
  drop_frames?: number | null;
}

interface CastHealth {
  state: 'starting' | 'negotiating' | 'stream_ready' | 'casting' | 'reconnecting' | 'failed';
  message?: string;
  metrics?: CastMetrics;
  reconnect?: {
    attempt?: number;
    max_attempts?: number;
    next_delay_ms?: number;
  };
}

export default function App() {
  const [targets, setTargets] = useState<DisplayTarget[]>([]);
  const [wfdTargets, setWfdTargets] = useState<DisplayTarget[]>([]);
  const [selectedOutput, setSelectedOutput] = useState<WlOutput | null>(null);

  const [castStatus, setCastStatus] = useState<CastStatus>({ stage: 'idle', target: null });
  const [projectTime, setProjectTime] = useState(0);
  const [isScanning, setIsScanning] = useState(false);
  const [receiverActive, setReceiverActive] = useState(false);
  const [receiverPairingCode, setReceiverPairingCode] = useState<string | null>(null);
  const [deps, setDeps] = useState<Record<string, unknown>>({});
  const [toast, setToast] = useState('');

  // Source selection modal state
  const [showSourceModal, setShowSourceModal] = useState(false);
  const [windows, setWindows] = useState<WlWindow[]>([]);
  const [loadingWindows, setLoadingWindows] = useState(false);
  const [targetForModal, setTargetForModal] = useState<DisplayTarget | null>(null);

  // Direct IP
  const [showManualIp, setShowManualIp] = useState(false);
  const [manualIp, setManualIp] = useState('');

  // Diagnose result
  const [diagResult, setDiagResult] = useState<any>(null);
  const [isDiagnosing, setIsDiagnosing] = useState(false);
  const [localIp, setLocalIp] = useState<string>('...');
  const [audioMode, setAudioMode] = useState<'system' | 'silent'>('system');
  const [castMetrics, setCastMetrics] = useState<CastMetrics | null>(null);

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(''), 5000);
  };

  const refreshAll = useCallback(async () => {
    setIsScanning(true);
    try {
      const [devs, outs, depMap] = await Promise.all([
        invoke<DisplayTarget[]>('get_discovered_devices'),
        invoke<WlOutput[]>('list_outputs'),
        invoke<Record<string, unknown>>('check_dependencies'),
      ]);
      setTargets(devs);
      setDeps(depMap);
      if (outs.length > 0) {
        setSelectedOutput(prev => prev ?? outs[0]);
      }
    } catch (e) {
      console.warn('Refresh error:', e);
    } finally { setIsScanning(false); }
  }, []);

  const scanWirelessDisplays = async () => {
    setIsScanning(true);
    try {
      const result = await invoke<{ ok: boolean; error?: string; devices: DisplayTarget[] }>('scan_miracast_devices');
      setWfdTargets(result.devices);
      if (!result.ok) showToast(result.error || 'Could not scan wireless displays');
      else if (result.devices.length === 0) showToast('No compatible wireless displays found');
    } catch (error) {
      showToast(`Wireless display scan failed: ${String(error)}`);
    } finally {
      setIsScanning(false);
    }
  };

  const visibleTargets = [...targets, ...wfdTargets.filter(wfd => !targets.some(target => target.id === wfd.id))];

  useEffect(() => {
    refreshAll();
    const t = setInterval(refreshAll, 4000);
    return () => clearInterval(t);
  }, [refreshAll]);

  useEffect(() => {
    if (!receiverActive) {
      setReceiverPairingCode(null);
      return undefined;
    }
    let active = true;
    const poll = async () => {
      try {
        const result = await invoke<{ code: string | null }>('nearby_pairing_code');
        if (active) {
          setReceiverPairingCode(result.code);
        }
      } catch {
        if (active) {
          setReceiverPairingCode(null);
        }
      }
    };
    void poll();
    const timer = setInterval(() => { void poll(); }, 1000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [receiverActive]);

  // Stop projection when app window is closed
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    getCurrentWindow().onCloseRequested(async () => {
      await invoke('stop_projection').catch(() => {});
    }).then(fn => { unlisten = fn; });
    return () => { unlisten?.(); };
  }, []);

  // Duration counter
  useEffect(() => {
    if (castStatus.stage === 'casting') {
      timerRef.current = setInterval(() => setProjectTime(s => s + 1), 1000);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
      setProjectTime(0);
    }
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [castStatus.stage]);

  useEffect(() => {
    if (!['connecting', 'authenticating', 'negotiating', 'starting_stream', 'waiting_receiver', 'casting', 'reconnecting'].includes(castStatus.stage)) {
      return undefined;
    }

    let active = true;
    const checkHealth = async () => {
      try {
        const health = await invoke<CastHealth>('check_cast_alive');
        if (!active) return;
        if (health.metrics) setCastMetrics(health.metrics);
        if (health.state === 'reconnecting') {
          setCastStatus(current => ({
            ...current,
            stage: 'reconnecting',
            error: health.message,
          }));
          const delay = Math.max(50, health.reconnect?.next_delay_ms ?? 0);
          if (delay <= 50) {
            const result = await invoke<{ ok: boolean; status?: CastHealth; error?: string }>(
              'attempt_reconnect',
            );
            if (!active) return;
            if (result.ok) {
              setCastStatus(current => ({
                ...current,
                stage: 'starting_stream',
                error: undefined,
              }));
              showToast('Reconnected — restarting stream...');
            } else if (result.status?.state === 'failed') {
              setCastStatus(current => ({
                ...current,
                stage: 'failed',
                error: result.status?.message || result.error || 'Reconnect exhausted',
              }));
            }
          }
        } else if (health.state === 'negotiating' || health.message?.toLowerCase().includes('authenticat')) {
          const stage = health.message?.toLowerCase().includes('authenticat')
            ? 'authenticating'
            : 'negotiating';
          setCastStatus(current => (
            ['connecting', 'authenticating', 'negotiating', 'starting_stream', 'waiting_receiver', 'reconnecting'].includes(current.stage)
              ? { ...current, stage, error: undefined }
              : current
          ));
        } else if (health.state === 'stream_ready') {
          setCastStatus(current => ['connecting', 'authenticating', 'negotiating', 'starting_stream', 'waiting_receiver', 'reconnecting'].includes(current.stage)
            ? { ...current, stage: 'waiting_receiver', error: undefined }
            : current);
        } else if (health.state === 'casting') {
          setCastStatus(current => (
            ['connecting', 'authenticating', 'negotiating', 'starting_stream', 'waiting_receiver', 'reconnecting', 'casting'].includes(current.stage)
              ? { ...current, stage: 'casting', error: undefined }
              : current
          ));
        } else if (health.state === 'failed') {
          setCastStatus(current => ({
            ...current,
            stage: 'failed',
            error: health.message || 'The streaming pipeline stopped unexpectedly.',
          }));
        }
      } catch (error) {
        if (active) {
          setCastStatus(current => ({
            ...current,
            stage: 'failed',
            error: `Could not read stream health: ${String(error)}`,
          }));
        }
      }
    };

    void checkHealth();
    const interval = setInterval(() => void checkHealth(), 500);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [castStatus.protocol, castStatus.stage]);

  const fmtTime = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}:${sec.toString().padStart(2, '0')}`;
  };

  const executeCast = async (
    target: DisplayTarget,
    outputName: string,
    geometry: string,
    selectionLabel: string,
    pairingCode?: string,
  ) => {
    setShowSourceModal(false);
    setCastStatus({
      stage: 'connecting',
      target,
      outputName,
      geometry,
      selectionLabel,
      protocol: target.protocol,
    });

    showToast(`Connecting to ${target.name}...`);

    try {
      const res = await invoke<{ ok: boolean; error?: string; protocol?: string }>('start_projection', {
        targetIp: target.ip_address,
        targetPort: target.port || 29870,
        outputName,
        geometry,
        protocol: target.protocol,
        audioMode,
        pairingCode: pairingCode || null,
      });

      if (res.ok) {
        const targetIpForRoute = target.ip_address;
        invoke<string | null>('get_local_ip', { targetIp: targetIpForRoute })
          .then(ip => setLocalIp(ip ?? ''))
          .catch(() => setLocalIp(''));

        setCastMetrics(null);
        setCastStatus({
          stage: 'starting_stream',
          target,
          outputName,
          geometry,
          selectionLabel,
          protocol: target.protocol,
        });
        showToast(`Starting stream for ${target.name}...`);
      } else if (
        (target.protocol === 'Nearby Cast' || target.protocol === 'AirPlay')
        && !pairingCode
        && String(res.error || '').toLowerCase().includes('pair')
      ) {
        const promptText = target.protocol === 'AirPlay'
          ? 'Enter the AirPlay pairing PIN (lab default is 0000):'
          : 'Enter the pairing code shown on the NearbyCast receiver:';
        const code = window.prompt(promptText);
        if (code && code.trim()) {
          await executeCast(target, outputName, geometry, selectionLabel, code.trim());
          return;
        }
        setCastStatus({ stage: 'failed', target, error: res.error, protocol: target.protocol });
        showToast(`Failed: ${res.error || 'Could not connect to receiver'}`);
      } else {
        setCastStatus({ stage: 'failed', target, error: res.error, protocol: target.protocol });
        showToast(`Failed: ${res.error || 'Could not connect to receiver'}`);
      }
    } catch (e: any) {
      setCastStatus({ stage: 'failed', target, error: String(e), protocol: target.protocol });
      showToast(`Error: ${String(e)}`);
    }
  };

  const openSourceModal = (target: DisplayTarget) => {
    setTargetForModal(target);
    setShowSourceModal(true);
    setLoadingWindows(true);
    invoke<WlWindow[]>('list_windows')
      .then(wins => setWindows(wins))
      .catch(() => setWindows([]))
      .finally(() => setLoadingWindows(false));
  };

  const initiateProjection = (target: DisplayTarget) => {
    openSourceModal(target);
  };

  const handleSelectRegionSlurp = async () => {
    if (!targetForModal) return;
    showToast('Click and drag with mouse to select region...');
    try {
      const res = await invoke<{ ok: boolean; geometry?: string; error?: string }>('select_region_slurp');
      if (res.ok && res.geometry) {
        if (!selectedOutput) {
          showToast('No capture output could be detected on this desktop session.');
          return;
        }
        executeCast(targetForModal, selectedOutput.name, res.geometry, `Region: ${res.geometry}`);
      } else if (res.error) {
        showToast(`Region cancelled: ${res.error}`);
      }
    } catch (e) {
      showToast(`Slurp error: ${e}`);
    }
  };

  const handleManualConnect = async () => {
    const ip = manualIp.trim();
    if (!ip) { showToast('Enter an IP address first'); return; }
    setIsDiagnosing(true);
    try {
      const result = await invoke<{ protocol_detected: string }>('diagnose_device', { targetIp: ip });
      if (result.protocol_detected !== 'Google Cast') {
        showToast('The address was not verified as a Google Cast receiver. No cast was started.');
        return;
      }
    const customTarget: DisplayTarget = {
      id: `custom-${ip}`,
      name: `Display (${ip})`,
      platform: 'Google Cast',
      ip_address: ip,
      port: 8008,
      status: 'available',
      protocol: 'Google Cast',
      screen_cast: true,
      media_cast: true,
      audio_cast: true,
      requires_pin: false,
      support_notes: 'Google Cast verified by DIAL or pychromecast probe',
    };
    initiateProjection(customTarget);
    } catch (error) {
      showToast(`Could not verify receiver: ${String(error)}`);
    } finally {
      setIsDiagnosing(false);
    }
  };

  const handleStopProjection = async () => {
    await invoke('stop_projection').catch(() => {});
    setCastStatus({ stage: 'idle', target: null });
    setCastMetrics(null);
    showToast('Projection stopped');
  };

  const handleDiagnose = async (target: DisplayTarget) => {
    setIsDiagnosing(true);
    setDiagResult(null);
    showToast(`Diagnosing ${target.ip_address}...`);
    try {
      const result = await invoke<any>('diagnose_device', { targetIp: target.ip_address });
      setDiagResult(result);
    } catch (e) {
      showToast(`Diagnose error: ${e}`);
    } finally {
      setIsDiagnosing(false);
    }
  };

  const handleToggleReceiver = async () => {
    if (!receiverActive) {
      const res = await invoke<{ ok: boolean; error?: string; port?: number; message?: string }>('start_receiver', { listenPort: 29870 });
      if (res.ok) {
        setReceiverActive(true);
        setReceiverPairingCode(null);
        showToast(res.message || `Receiver listening on port ${res.port || 29870}`);
      } else {
        showToast(`Receiver error: ${res.error || 'Failed'}`);
      }
    } else {
      await invoke('stop_receiver').catch(() => {});
      setReceiverActive(false);
      setReceiverPairingCode(null);
      showToast('Receiver stopped');
    }
  };

  const copyLiveLink = () => {
    if (!localIp) return;
    const url = `http://${localIp}:8090`;
    navigator.clipboard.writeText(url);
    showToast(`Copied: ${url}`);
  };

  const isActive = ['connecting', 'authenticating', 'negotiating', 'casting', 'starting_stream', 'waiting_receiver', 'reconnecting'].includes(castStatus.stage);
  const isConnecting = ['connecting', 'authenticating', 'negotiating', 'starting_stream', 'waiting_receiver', 'reconnecting'].includes(castStatus.stage);

  // Only surface tools required for Google Cast / core capture. Optional helpers
  // (fluxcast, mpv, nmcli, slurp, wlr-randr) enable Miracast/region features and
  // must not block or alarm users when Cast works.
  const requiredDepKeys = ['wf-recorder', 'ffmpeg', 'python3', 'pychromecast'] as const;
  const requiredMap =
    deps.required && typeof deps.required === 'object' && !Array.isArray(deps.required)
      ? (deps.required as Record<string, boolean>)
      : (deps as Record<string, boolean>);
  const missingRequired = requiredDepKeys.filter((k) => requiredMap[k] === false);

  const stageLabel: Record<CastStage, string> = {
    idle: '',
    connecting: 'Connecting...',
    authenticating: 'Authenticating...',
    negotiating: 'Negotiating...',
    starting_stream: 'Starting stream...',
    waiting_receiver: 'Waiting for receiver...',
    casting: 'Casting',
    reconnecting: 'Reconnecting...',
    failed: 'Connection lost',
  };

  return (
    <div style={{
      minHeight: '100vh',
      background: '#09090b',
      color: '#f4f4f5',
      fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
      display: 'flex',
      flexDirection: 'column',
      userSelect: 'none',
      letterSpacing: '-0.01em',
    }}>
      {/* Title Bar Drag Region */}
      <div data-tauri-drag-region style={{ height: '32px', width: '100%', flexShrink: 0 }} />

      <div style={{
        flex: 1, display: 'flex', flexDirection: 'column',
        maxWidth: '440px', margin: '0 auto', width: '100%',
        padding: '0 20px 24px',
      }}>

        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div style={{
              width: '36px', height: '36px', borderRadius: '8px',
              background: '#18181b', border: '1px solid #27272a',
              display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#f4f4f5',
            }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="2" y="3" width="20" height="14" rx="2"/>
                <line x1="8" y1="21" x2="16" y2="21"/>
                <line x1="12" y1="17" x2="12" y2="21"/>
              </svg>
            </div>
            <div>
              <h1 style={{ fontSize: '15px', fontWeight: 600, color: '#f4f4f5', margin: 0 }}>Nearby Cast</h1>
              <div style={{ fontSize: '11px', color: '#71717a', marginTop: '2px', display: 'flex', alignItems: 'center', gap: '5px' }}>
                <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#22c55e', display: 'inline-block' }} />
                <span>Linux Desktop</span>
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', gap: '6px' }}>
            {deps.fluxcast === true && (
              <button
                onClick={() => void scanWirelessDisplays()}
                disabled={isScanning}
                style={{
                  background: '#18181b', border: '1px solid #27272a', borderRadius: '8px',
                  padding: '6px 10px', fontSize: '12px', fontWeight: 500, color: '#a1a1aa', cursor: 'pointer',
                }}
              >Wireless</button>
            )}
            <button
              onClick={refreshAll}
              style={{
                background: '#18181b', border: '1px solid #27272a',
                borderRadius: '8px', padding: '6px 12px',
                fontSize: '12px', fontWeight: 500, color: '#a1a1aa',
                cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '6px',
              }}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                style={{ animation: isScanning ? 'spin 1s linear infinite' : 'none' }}>
                <path d="M21.5 2v6h-6M2.5 22v-6h6"/>
                <path d="M2 11.5a10 10 0 0 1 18.8-4.3L21.5 8M2.5 16l1.2 1.2a10 10 0 0 0 18.8-4.2"/>
              </svg>
              <span>Scan</span>
            </button>
          </div>
        </div>

        {/* Active Projection Card */}
        {isActive && castStatus.target && (
          <div style={{
            background: '#141417',
            border: '1px solid #3f3f46', borderRadius: '12px',
            padding: '16px', marginBottom: '20px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: castStatus.stage === 'casting' ? '#22c55e' : '#f59e0b', animation: 'blink 1.2s infinite' }} />
                <span style={{ fontSize: '11px', fontWeight: 600, color: castStatus.stage === 'casting' ? '#22c55e' : '#f59e0b', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  {stageLabel[castStatus.stage]}
                </span>
              </div>
              {castStatus.stage === 'casting' && (
                <span style={{ fontSize: '13px', fontWeight: 600, fontFamily: 'monospace', color: '#f4f4f5' }}>
                  {fmtTime(projectTime)}
                </span>
              )}
            </div>

            <div style={{ fontSize: '14px', fontWeight: 600, color: '#f4f4f5', marginBottom: '4px' }}>
              Casting to {castStatus.target.name}
            </div>
            <div style={{ fontSize: '11px', color: '#71717a', marginBottom: '12px' }}>
              {castStatus.protocol}
              {castStatus.selectionLabel ? ` · ${castStatus.selectionLabel}` : ''}
            </div>

            {castStatus.stage === 'casting' && (
              <div style={{
                display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '8px',
                marginBottom: '12px',
              }}>
                <div style={{ background: '#09090b', border: '1px solid #27272a', borderRadius: '7px', padding: '7px 8px' }}>
                  <div style={{ fontSize: '9px', color: '#71717a', textTransform: 'uppercase', fontWeight: 600 }}>Latency</div>
                  <div style={{ fontSize: '11px', color: '#f4f4f5', fontFamily: 'monospace', marginTop: '2px' }}>
                    Latency unavailable
                  </div>
                </div>
                <div style={{ background: '#09090b', border: '1px solid #27272a', borderRadius: '7px', padding: '7px 8px' }}>
                  <div style={{ fontSize: '9px', color: '#71717a', textTransform: 'uppercase', fontWeight: 600 }}>Quality</div>
                  <div style={{ fontSize: '11px', color: '#f4f4f5', fontFamily: 'monospace', marginTop: '2px' }}>
                    {castMetrics?.fps == null ? 'Measuring…' : `${castMetrics.fps.toFixed(0)} FPS`}
                  </div>
                </div>
              </div>
            )}

            {castMetrics && (
              <div style={{
                display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '8px',
                marginBottom: '12px',
              }}>
                {[
                  ['FPS', castMetrics.fps == null ? 'N/A' : castMetrics.fps.toFixed(1)],
                  ['Bitrate', castMetrics.bitrate_kbps == null ? 'N/A' : `${(castMetrics.bitrate_kbps / 1000).toFixed(1)} Mbps`],
                  ['Dropped', castMetrics.drop_frames == null ? 'N/A' : String(castMetrics.drop_frames)],
                ].map(([label, value]) => (
                  <div key={label} style={{
                    background: '#09090b', border: '1px solid #27272a', borderRadius: '7px', padding: '7px 8px',
                  }}>
                    <div style={{ fontSize: '9px', color: '#71717a', textTransform: 'uppercase', fontWeight: 600 }}>{label}</div>
                    <div style={{ fontSize: '11px', color: '#f4f4f5', fontFamily: 'monospace', marginTop: '2px' }}>{value}</div>
                  </div>
                ))}
              </div>
            )}

            {localIp && castStatus.protocol === 'Google Cast' && <div style={{
              background: '#09090b', border: '1px solid #27272a',
              borderRadius: '8px', padding: '8px 12px', marginBottom: '12px',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <div>
                <div style={{ fontSize: '10px', color: '#71717a', textTransform: 'uppercase', fontWeight: 600 }}>
                  Diagnostics stream
                </div>
                <div style={{ fontSize: '12px', fontFamily: 'monospace', color: '#f4f4f5', marginTop: '1px' }}>
                  http://{localIp}:8090
                </div>
              </div>
              <button
                onClick={copyLiveLink}
                style={{
                  background: '#27272a', border: '1px solid #3f3f46',
                  borderRadius: '6px', padding: '5px 10px', color: '#f4f4f5',
                  fontSize: '11px', fontWeight: 500, cursor: 'pointer',
                }}
              >
                Copy
              </button>
            </div>}

            <button
              onClick={handleStopProjection}
              style={{
                width: '100%', padding: '9px',
                background: '#27272a', border: '1px solid #3f3f46', borderRadius: '8px',
                color: '#f87171', fontSize: '12px', fontWeight: 600, cursor: 'pointer',
              }}
            >
              Stop Projection
            </button>
          </div>
        )}

        {/* Audio Source Selector Card on Main Screen */}
        <div style={{
          background: '#141417', border: '1px solid #27272a',
          borderRadius: '12px', padding: '14px', marginBottom: '20px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
            <div style={{ fontSize: '12px', fontWeight: 600, color: '#f4f4f5', display: 'flex', alignItems: 'center', gap: '6px' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                <path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/>
              </svg>
              <span>Audio Output</span>
            </div>
            <span style={{ fontSize: '11px', color: '#71717a' }}>Select destination</span>
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              type="button"
              onClick={() => setAudioMode('system')}
              style={{
                flex: 1, padding: '8px 10px', borderRadius: '8px', fontSize: '11px', fontWeight: 600,
                background: audioMode === 'system' ? '#f4f4f5' : '#09090b',
                border: audioMode === 'system' ? '1px solid #f4f4f5' : '1px solid #27272a',
                color: audioMode === 'system' ? '#09090b' : '#71717a',
                cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px',
                transition: 'all 0.15s ease',
              }}
            >
              <span>Include system audio</span>
            </button>
            <button
              type="button"
              onClick={() => setAudioMode('silent')}
              style={{
                flex: 1, padding: '8px 10px', borderRadius: '8px', fontSize: '11px', fontWeight: 600,
                background: audioMode === 'silent' ? '#f4f4f5' : '#09090b',
                border: audioMode === 'silent' ? '1px solid #f4f4f5' : '1px solid #27272a',
                color: audioMode === 'silent' ? '#09090b' : '#71717a',
                cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px',
                transition: 'all 0.15s ease',
              }}
            >
              <span>Video only</span>
            </button>
          </div>
        </div>

        {/* Failed Banner */}
        {castStatus.stage === 'failed' && castStatus.target && (
          <div style={{
            background: '#18181b', border: '1px solid #3f3f46',
            borderRadius: '10px', padding: '12px', marginBottom: '16px',
          }}>
            <div style={{ fontSize: '12px', fontWeight: 600, color: '#f87171', marginBottom: '4px' }}>
              Connection Failed — {castStatus.target.name}
            </div>
            <div style={{ fontSize: '11px', color: '#71717a', marginBottom: '8px' }}>
              {castStatus.error || 'Could not establish connection.'}
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                onClick={() => castStatus.target && initiateProjection(castStatus.target)}
                style={{
                  background: '#27272a', border: '1px solid #3f3f46',
                  borderRadius: '6px', padding: '6px 12px',
                  color: '#f4f4f5', fontSize: '11px', fontWeight: 500, cursor: 'pointer',
                }}
              >
                Retry
              </button>
              <button
                onClick={() => setCastStatus({ stage: 'idle', target: null })}
                style={{ background: 'none', border: 'none', color: '#71717a', fontSize: '11px', cursor: 'pointer' }}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {/* Missing required tools only — optional Miracast helpers stay quiet */}
        {missingRequired.length > 0 && (
          <div style={{
            background: '#18181b', border: '1px solid #3f3f46',
            borderRadius: '10px', padding: '12px', marginBottom: '16px',
            fontSize: '11px', color: '#f87171',
          }}>
            <div style={{ fontWeight: 600, marginBottom: '2px' }}>Missing required dependencies for casting:</div>
            <div style={{ fontFamily: 'monospace', color: '#a1a1aa' }}>
              {missingRequired.join(', ')}
            </div>
            <div style={{ color: '#71717a', marginTop: '6px' }}>
              Install these with your package manager, or use the Flatpak build which bundles them.
            </div>
          </div>
        )}

        {/* Nearby Displays */}
        <div style={{ marginBottom: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
            <div style={{ fontSize: '11px', fontWeight: 600, color: '#71717a', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Discovered Displays ({visibleTargets.length})
            </div>
            {isScanning && <span style={{ fontSize: '11px', color: '#71717a' }}>Scanning...</span>}
          </div>

          {visibleTargets.length === 0 ? (
            <div style={{
              background: '#141417', border: '1px solid #27272a',
              borderRadius: '12px', padding: '20px', textAlign: 'center',
            }}>
              <div style={{ fontSize: '13px', fontWeight: 500, color: '#a1a1aa' }}>No nearby displays found</div>
              <div style={{ fontSize: '11px', color: '#71717a', marginTop: '4px' }}>
                Ensure your TV is connected to the same network.
              </div>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {visibleTargets.map(target => {
                const canCast = canStartScreenCast(target);
                return (
                  <div
                    key={target.id}
                    style={{
                      background: '#141417', border: '1px solid #27272a',
                      borderRadius: '10px', padding: '12px 14px',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <div style={{
                          width: '34px', height: '34px', borderRadius: '8px',
                          background: '#18181b', border: '1px solid #27272a', color: '#f4f4f5',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                        }}>
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <rect x="2" y="3" width="20" height="14" rx="2"/>
                            <line x1="8" y1="21" x2="16" y2="21"/>
                            <line x1="12" y1="17" x2="12" y2="21"/>
                          </svg>
                        </div>
                        <div>
                          <div style={{ fontSize: '13px', fontWeight: 600, color: '#f4f4f5' }}>{target.name}</div>
                          <div style={{ fontSize: '11px', color: '#71717a', marginTop: '1px' }}>
                            {target.protocol}
                            {canCast ? ' · Available' : ' · Unavailable'}
                          </div>
                          {!canCast && (
                            <div style={{ fontSize: '10px', color: '#52525b', marginTop: '2px' }}>
                              {target.support_notes}
                            </div>
                          )}
                        </div>
                      </div>

                      <div style={{ display: 'flex', gap: '6px', flexShrink: 0 }}>
                        <button
                          onClick={() => handleDiagnose(target)}
                          disabled={isDiagnosing}
                          style={{
                            background: '#18181b', border: '1px solid #27272a',
                            color: '#71717a', borderRadius: '6px',
                            padding: '6px 10px', fontSize: '11px', fontWeight: 500,
                            cursor: 'pointer',
                          }}
                        >
                          {isDiagnosing ? '...' : 'Info'}
                        </button>
                        <button
                          onClick={() => initiateProjection(target)}
                          disabled={isActive || isConnecting || !canCast}
                          style={{
                            background: canCast ? (isActive ? '#27272a' : '#f4f4f5') : '#27272a',
                            color: canCast ? (isActive ? '#71717a' : '#09090b') : '#52525b',
                            border: 'none', borderRadius: '6px',
                            padding: '6px 14px', fontSize: '12px', fontWeight: 600,
                            cursor: (isActive || isConnecting || !canCast) ? 'not-allowed' : 'pointer',
                            transition: 'all 0.15s ease',
                          }}
                        >
                          {isConnecting && castStatus.target?.id === target.id
                            ? 'Connecting...'
                            : canCast ? 'Cast' : 'N/A'}
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Diagnostics Panel */}
        {diagResult && (
          <div style={{
            background: '#141417', border: '1px solid #27272a',
            borderRadius: '10px', padding: '12px', marginBottom: '20px',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
              <div style={{ fontSize: '11px', fontWeight: 600, color: '#71717a', textTransform: 'uppercase' }}>
                Diagnostics — {diagResult.target}
              </div>
              <button
                onClick={() => setDiagResult(null)}
                style={{ background: 'none', border: 'none', color: '#71717a', cursor: 'pointer', fontSize: '12px' }}
              >✕</button>
            </div>
            {(diagResult.checks || []).map((c: any, i: number) => (
              <div key={i} style={{ display: 'flex', gap: '8px', marginBottom: '3px', fontSize: '11px' }}>
                <span style={{ color: c.ok ? '#22c55e' : '#f87171', fontWeight: 600 }}>{c.ok ? 'OK' : 'ERR'}</span>
                <span style={{ color: c.ok ? '#a1a1aa' : '#71717a' }}>{c.check}</span>
              </div>
            ))}
          </div>
        )}

        {/* Direct IP Connect */}
        <div style={{ marginBottom: '20px' }}>
          <button
            onClick={() => setShowManualIp(!showManualIp)}
            style={{
              background: 'none', border: 'none', color: '#71717a',
              fontSize: '11px', fontWeight: 500, cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: '6px', padding: 0,
            }}
          >
            <span>{showManualIp ? 'Hide Direct IP Connect' : 'Connect via Direct IP...'}</span>
          </button>

          {showManualIp && (
            <div style={{ marginTop: '8px' }}>
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  value={manualIp}
                  onChange={e => setManualIp(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleManualConnect()}
                  placeholder="Google Cast receiver IP"
                  style={{
                    flex: 1, background: '#141417', border: '1px solid #27272a',
                    borderRadius: '6px', padding: '8px 12px', color: '#f4f4f5',
                    fontSize: '12px', outline: 'none',
                  }}
                />
                <button
                  onClick={handleManualConnect}
                  disabled={isConnecting}
                  style={{
                    background: '#f4f4f5', border: 'none',
                    borderRadius: '6px', padding: '8px 14px', color: '#09090b',
                    fontSize: '12px', fontWeight: 600, cursor: 'pointer',
                  }}
                >
                  Verify & Cast
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Receiver Mode Card */}
        <div style={{
          background: '#141417', border: '1px solid #27272a',
          borderRadius: '12px', padding: '12px 14px',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <div style={{ fontSize: '12px', fontWeight: 600, color: '#f4f4f5' }}>Allow Projecting to This PC</div>
            <div style={{ fontSize: '11px', color: '#71717a', marginTop: '1px' }}>
              {receiverActive
                ? (receiverPairingCode
                  ? `Pairing code: ${receiverPairingCode}`
                  : 'Listening for authenticated NearbyCast sessions')
                : 'Enable to accept authenticated NearbyCast sessions on this PC'}
            </div>
          </div>
          <button
            onClick={handleToggleReceiver}
            style={{
              background: receiverActive ? '#22c55e' : '#27272a',
              color: receiverActive ? '#000' : '#a1a1aa',
              border: 'none', borderRadius: '16px',
              padding: '5px 12px', fontSize: '11px', fontWeight: 600,
              cursor: 'pointer', transition: 'all 0.15s ease',
            }}
          >
            {receiverActive ? 'Active' : 'Enable'}
          </button>
        </div>

      </div>

      {showSourceModal && targetForModal && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)',
          backdropFilter: 'blur(8px)', display: 'flex', alignItems: 'center', justifyContent: 'center',
          zIndex: 9990, padding: '20px',
        }}>
          <div style={{
            background: '#141417', border: '1px solid #27272a',
            borderRadius: '14px', padding: '18px', maxWidth: '380px', width: '100%',
            boxShadow: '0 20px 50px rgba(0,0,0,0.8)', display: 'flex', flexDirection: 'column',
            maxHeight: '80vh',
          }}>
            {/* Header */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexShrink: 0 }}>
              <div>
                <h3 style={{ fontSize: '14px', fontWeight: 600, color: '#f4f4f5', margin: '0 0 2px' }}>Select Source</h3>
                <div style={{ fontSize: '11px', color: '#71717a' }}>
                  Target: <strong style={{ color: '#e4e4e7' }}>{targetForModal.name}</strong>
                </div>
              </div>
              <button
                onClick={() => setShowSourceModal(false)}
                style={{ background: '#27272a', border: 'none', color: '#a1a1aa', borderRadius: '50%', width: '24px', height: '24px', cursor: 'pointer', fontSize: '12px' }}
              >✕</button>
            </div>

            {/* Full Screen Option */}
            <button
              onClick={() => {
                if (!targetForModal) {
                  return;
                }
                if (!selectedOutput && targetForModal.protocol !== 'Miracast') {
                  showToast('No capture output could be detected on this desktop session.');
                  return;
                }
                const outputName = selectedOutput?.name ?? 'PORTAL';
                executeCast(targetForModal, outputName, '', `Screen: ${outputName}`);
              }}
              style={{
                width: '100%', padding: '11px 14px', borderRadius: '9px',
                background: '#f4f4f5', border: 'none',
                color: '#09090b', cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: '10px',
                textAlign: 'left', marginBottom: '8px', flexShrink: 0,
              }}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <rect x="2" y="3" width="20" height="14" rx="2"/>
                <line x1="8" y1="21" x2="16" y2="21"/>
                <line x1="12" y1="17" x2="12" y2="21"/>
              </svg>
              <span style={{ fontSize: '12px', fontWeight: 700 }}>Full Screen</span>
            </button>

            {/* Crop Region Option */}
            {targetForModal.protocol !== 'Miracast' && <button
              onClick={handleSelectRegionSlurp}
              style={{
                width: '100%', padding: '9px 12px', borderRadius: '8px',
                background: 'transparent', border: '1px solid #27272a',
                color: '#71717a', cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: '8px',
                textAlign: 'left', marginBottom: '12px', flexShrink: 0,
              }}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="22" y1="12" x2="18" y2="12"/>
                <line x1="6" y1="12" x2="2" y2="12"/>
                <line x1="12" y1="6" x2="12" y2="2"/>
                <line x1="12" y1="22" x2="12" y2="18"/>
              </svg>
              <span style={{ fontSize: '11px', fontWeight: 500 }}>Crop Region (Slurp)</span>
            </button>}

            {/* FluxCast's WFD backend negotiates a complete display or portal source. */}
            {targetForModal.protocol !== 'Miracast' && <><div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px', flexShrink: 0 }}>
              <div style={{ flex: 1, height: '1px', background: '#27272a' }} />
              <span style={{ fontSize: '10px', color: '#52525b', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Windows</span>
              <div style={{ flex: 1, height: '1px', background: '#27272a' }} />
            </div>

            {/* Window list */}
            <div style={{ overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: '4px' }}>
              {loadingWindows ? (
                <div style={{ fontSize: '11px', color: '#71717a', textAlign: 'center', padding: '12px' }}>Loading windows...</div>
              ) : windows.length === 0 ? (
                <div style={{ fontSize: '11px', color: '#71717a', textAlign: 'center', padding: '12px' }}>No open windows found</div>
              ) : (
                windows.map(win => (
                  <button
                    key={win.id}
                    onClick={() => {
                      if (!targetForModal || !selectedOutput) {
                        showToast('No capture output could be detected on this desktop session.');
                        return;
                      }
                      executeCast(targetForModal, selectedOutput.name, win.geometry, win.title);
                    }}
                    style={{
                      width: '100%', padding: '9px 12px', borderRadius: '8px',
                      background: '#18181b', border: '1px solid #27272a',
                      color: '#f4f4f5', cursor: 'pointer',
                      display: 'flex', alignItems: 'center', gap: '10px',
                      textAlign: 'left', transition: 'background 0.12s',
                    }}
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#71717a" strokeWidth="2" style={{ flexShrink: 0 }}>
                      <rect x="3" y="3" width="18" height="18" rx="2"/>
                      <line x1="3" y1="9" x2="21" y2="9"/>
                    </svg>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontSize: '12px', fontWeight: 600, color: '#f4f4f5', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {win.title}
                      </div>
                      <div style={{ fontSize: '10px', color: '#52525b', marginTop: '1px' }}>{win.class_name}</div>
                    </div>
                  </button>
                ))
              )}
            </div></>}

          </div>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed', bottom: '20px', left: '50%', transform: 'translateX(-50%)',
          background: '#18181b', border: '1px solid #27272a',
          borderRadius: '8px', padding: '8px 16px',
          fontSize: '11px', fontWeight: 500, color: '#f4f4f5',
          boxShadow: '0 6px 20px rgba(0,0,0,0.5)',
          zIndex: 9999, animation: 'fadeIn 0.2s ease',
          maxWidth: '380px', textAlign: 'center',
        }}>
          {toast}
        </div>
      )}

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * { box-sizing: border-box; }
        body { background: #09090b; margin: 0; padding: 0; }
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        @keyframes fadeIn { from { opacity: 0; transform: translateX(-50%) translateY(4px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }
        button:hover:not(:disabled) { filter: brightness(1.1); }
        button:active:not(:disabled) { transform: scale(0.98); }
        input::placeholder { color: #52525b; letter-spacing: normal; font-size: 13px; }
      `}</style>
    </div>
  );
}
