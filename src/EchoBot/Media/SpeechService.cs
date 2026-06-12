using EchoBot.Models;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Skype.Bots.Media;
using System.Collections.Generic;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Reflection;

namespace EchoBot.Media
{
    /// <summary>
    /// SpeechService — Avatar Sidecar Proxy.
    /// Replaces the Azure Speech SDK with a local Python sidecar that runs Voice Live API.
    /// Audio is forwarded to the sidecar via WebSocket; it returns audio + video frames.
    /// Original Speech SDK version preserved in SpeechService_speechSDK.cs.
    /// </summary>
    public class SpeechService
    {
        private bool _isRunning = false;
        protected bool _isDraining;
        private volatile bool _isSpeaking = false;
        private long _speakingSinceTicks = 0;

        private readonly ILogger _logger;
        private readonly string _sidecarUrl;
        private readonly Dictionary<string, string> _interviewContext;
        private readonly int _maxSpeakingGateMs;
        private readonly bool _audioPipelineDiag;
        private readonly bool _latencyDiag;
        private long _audioPipelineMessageCount;
        private long _audioPipelineAudioMessageCount;
        private long _latencyBotAudioMessages;
        private long _latencySidecarAudioMessages;
        private long _latencySidecarVideoMessages;
        private ClientWebSocket _sidecarWs;
        private CancellationTokenSource _receiveCts;
        // Serializes EnsureConnectedAsync so concurrent callers (first inbound
        // audio frame in AppendAudioBuffer + CallState.Established notification)
        // don't each open their own /stream WebSocket to the sidecar. The prior
        // `_sidecarWs.State == Open` guard didn't catch the in-flight Connecting
        // state, so two parallel ConnectAsync calls produced two VL sessions and
        // tripped the per-account avatar concurrency rate limit.
        private readonly SemaphoreSlim _connectGate = new SemaphoreSlim(1, 1);

        // Typing background sound: pre-loaded 20ms PCM chunks from embedded WAV
        private readonly List<byte[]> _typingChunks;

        // ── Phase 2: PTS propagation (lipsync) ──────────────────────────────
        // Per design doc Phase 2 v1.1 (LOCKED 2026-05-06).
        // Wire format from sidecar: optional `pts` (int64) + `tb_num`/`tb_den`
        // (rational time_base) on each `audio` and `video` message. Bot anchors
        // a SINGLE wall-clock origin on the first AUDIO buffer (Z4: audio is
        // master clock per AudioVideoFramePlayer xmldoc) and converts every
        // subsequent buffer's PTS into ticks relative to that anchor.
        //
        // Feature-flagged via LISA_USE_PTS env var. Default: 0 (legacy wall-
        // clock-at-receive behavior, bit-for-bit Phase-1-equivalent). Flip to
        // 1 only after Phase 1 production validation. Provides a fast revert
        // path if the conversion math turns out to be wrong in production.
        private readonly bool _usePts;
        private long? _anchorTicks;             // wall-clock ticks at first audio buffer
        private long? _anchorAudioPtsTicks;     // converted PTS of first audio buffer
        private bool _ptsFallbackWarned;        // single-shot canary log
        // Diagnostic counters (§9.3 mitigation): log first 10 PTS per stream.
        private int _audioPtsLogged;
        private int _videoPtsLogged;
        private bool _firstOffsetLogged;        // log first (video_ticks - audio_ticks) once both anchors set
        private long _audioPtsDriftWindowStartTicks;
        private long _audioPtsDriftSamples;
        private long _audioPtsDriftSumMs;
        private long _audioPtsDriftMinMs = long.MaxValue;
        private long _audioPtsDriftMaxMs = long.MinValue;
        private long _audioPtsDriftLastMs;

        // Test-only clock injection. Production path uses DateTime.UtcNow.Ticks;
        // unit tests swap this for a deterministic source so anchor / fallback
        // / first-offset logic can be asserted on exact values.
        internal Func<long> Clock { get; set; } = () => DateTime.UtcNow.Ticks;

        /// <summary>
        /// Event fired when audio (and optionally video) buffers should be sent to Teams.
        /// </summary>
        public event EventHandler<MediaStreamEventArgs> SendMediaBuffer;

        public SpeechService(AppSettings settings, ILogger logger, JoinCallBody? joinContext = null)
        {
            _logger = logger;
            _sidecarUrl = settings.AvatarEndpoint?.TrimEnd('/') ?? "ws://localhost:5001";
            _interviewContext = BuildInterviewContext(joinContext);
            _typingChunks = LoadTypingChunks();
            // Phase 2 feature flag. Read once at ctor; service restart required
            // to flip. Accept 1/true/yes (case-insensitive).
            var raw = Environment.GetEnvironmentVariable("LISA_USE_PTS") ?? "0";
            _usePts = raw.Trim().ToLowerInvariant() is "1" or "true" or "yes";
            _maxSpeakingGateMs = ReadIntEnv("LISA_MAX_SPEAKING_GATE_MS", 12000);
            _audioPipelineDiag = ReadBoolEnv("LISA_AUDIO_PIPELINE_DIAG", false);
            _latencyDiag = ReadBoolEnv("LISA_LATENCY_DIAG", false);
            _logger.LogWarning($"SpeechService [Avatar]: sidecar={_sidecarUrl}, typing chunks={_typingChunks.Count}, LISA_USE_PTS={_usePts}, LISA_MAX_SPEAKING_GATE_MS={_maxSpeakingGateMs}, LISA_AUDIO_PIPELINE_DIAG={_audioPipelineDiag}, LISA_LATENCY_DIAG={_latencyDiag}");
        }

        /// <summary>
        /// Test-only ctor: bypasses AppSettings and embedded resource loading
        /// so unit tests can construct an instance in isolation without the
        /// EchoBot assembly's typing_background.wav resource being available.
        /// </summary>
        internal SpeechService(ILogger logger, bool usePts)
        {
            _logger = logger;
            _sidecarUrl = "ws://localhost:5001";
            _interviewContext = new Dictionary<string, string>();
            _typingChunks = new List<byte[]>();
            _usePts = usePts;
            _maxSpeakingGateMs = 12000;
            _audioPipelineDiag = false;
            _latencyDiag = false;
        }

        private static int ReadIntEnv(string name, int fallback)
        {
            var raw = Environment.GetEnvironmentVariable(name);
            return int.TryParse(raw, out var value) && value >= 0 ? value : fallback;
        }

        private static bool ReadBoolEnv(string name, bool fallback)
        {
            var raw = Environment.GetEnvironmentVariable(name);
            if (string.IsNullOrWhiteSpace(raw)) return fallback;
            return raw.Trim().ToLowerInvariant() is "1" or "true" or "yes" or "on";
        }

        private static long? TryGetInt64(JsonElement root, string propertyName)
        {
            if (!root.TryGetProperty(propertyName, out var value)) return null;
            return value.ValueKind switch
            {
                JsonValueKind.Number when value.TryGetInt64(out var number) => number,
                JsonValueKind.String when long.TryParse(value.GetString(), out var number) => number,
                _ => null,
            };
        }

        private static string? TryGetString(JsonElement root, string propertyName)
        {
            if (!root.TryGetProperty(propertyName, out var value)) return null;
            return value.ValueKind == JsonValueKind.String ? value.GetString() : null;
        }

        private static bool ShouldLogLatency(long count)
        {
            return count <= 5 || count % 50 == 0;
        }

        private static Dictionary<string, string> BuildInterviewContext(JoinCallBody? joinContext)
        {
            var payload = new Dictionary<string, string>();
            if (joinContext == null) return payload;

            void AddIfPresent(string key, string? value)
            {
                if (!string.IsNullOrWhiteSpace(value))
                {
                    payload[key] = value.Trim();
                }
            }

            AddIfPresent("candidate_id", joinContext.CandidateId);
            AddIfPresent("candidate_name", joinContext.CandidateName);
            AddIfPresent("position", joinContext.Position);
            AddIfPresent("session_id", joinContext.SessionId);
            return payload;
        }

        /// <summary>
        /// Convert a stream-relative PTS (in `pts * tb_num / tb_den` seconds)
        /// to 100-ns ticks. Uses decimal arithmetic to avoid Int64 overflow on
        /// long sessions (`pts * 10_000_000` would overflow at ~3 hours for a
        /// 1/90000 video time-base).
        ///
        /// Pure math: accepts and returns signed values. Caller is responsible
        /// for deciding policy on negative PTS (ResolveTimestamp guards).
        /// </summary>
        internal long ConvertPtsToTicks(long pts, int tbNum, int tbDen)
        {
            if (tbDen <= 0 || tbNum <= 0)
                throw new ArgumentException($"Invalid time_base: {tbNum}/{tbDen}");
            // decimal has 28-29 significant digits — comfortably handles
            // pts up to ~9e18 even with the 10_000_000 multiplier.
            var ticks = (decimal)pts * TimeSpan.TicksPerSecond * tbNum / tbDen;
            return (long)ticks;
        }

        /// <summary>
        /// Phase 2 timestamp resolver. Returns the buffer.Timestamp value to
        /// hand to the AudioVideoFramePlayer.
        ///
        /// Fallback chain (per design doc §3, refined per review):
        ///   1. `pts` property present (any value incl. 0 is legitimate) → use it.
        ///   2. `timestamp` present AND non-zero → use as-is (legacy compat).
        ///   3. Else wall-clock NOW + ONE-SHOT warning canary.
        ///
        /// Anchor logic (Z4 — audio is master clock):
        ///   The very first AUDIO buffer that yields a usable pts establishes
        ///   the wall-clock origin: anchor = NOW - ticks(first_audio_pts).
        ///   All subsequent buffers (audio AND video) use the SAME anchor so
        ///   their playout times share a single timeline. Video frames that
        ///   arrive before any audio fall back to wall-clock NOW (player
        ///   strategy 2 paces them at fps cadence; this is fine for the
        ///   typically-tiny pre-roll window).
        /// </summary>
        internal long ResolveTimestamp(JsonElement root, bool isAudio)
        {
            long? ptsTicks = null;
            int tbNum = 0, tbDen = 0;
            bool ptsPresent = false;

            if (root.TryGetProperty("pts", out var ptsEl) && ptsEl.ValueKind == JsonValueKind.Number)
            {
                ptsPresent = true;
                var pts = ptsEl.GetInt64();
                tbNum = root.TryGetProperty("tb_num", out var nEl) ? nEl.GetInt32() : 1;
                tbDen = root.TryGetProperty("tb_den", out var dEl) ? dEl.GetInt32() : 0;
                // Negative PTS is malformed (sidecar drops <0 by design). Treat
                // as missing rather than honoring a negative anchor offset.
                if (tbDen > 0 && pts >= 0)
                    ptsTicks = ConvertPtsToTicks(pts, tbNum, tbDen);
            }
            else if (root.TryGetProperty("timestamp", out var tsEl) && tsEl.ValueKind == JsonValueKind.Number)
            {
                var ts = tsEl.GetInt64();
                // Treat 0 as "missing" — current sidecar hardcodes timestamp:0
                // which we explicitly do NOT want to honor as a buffer time of
                // year 1 AD. Non-zero legacy timestamp values are ABSOLUTE
                // tick values (not stream-relative PTS) so they bypass the
                // anchor entirely — returned as-is. (§3 fallback chain.)
                if (ts != 0) return ts;
            }

            // Diagnostic: log first 10 PTS values per stream + their tb (§9.3).
            if (ptsPresent && tbDen > 0)
            {
                if (isAudio && _audioPtsLogged < 10)
                {
                    _audioPtsLogged++;
                    _logger.LogInformation(
                        "SpeechService [Avatar]: audio pts[{N}]={Pts} tb={TbNum}/{TbDen} -> ticks={Ticks}",
                        _audioPtsLogged, ptsEl.GetInt64(), tbNum, tbDen, ptsTicks);
                }
                else if (!isAudio && _videoPtsLogged < 10)
                {
                    _videoPtsLogged++;
                    _logger.LogInformation(
                        "SpeechService [Avatar]: video pts[{N}]={Pts} tb={TbNum}/{TbDen} -> ticks={Ticks}",
                        _videoPtsLogged, ptsEl.GetInt64(), tbNum, tbDen, ptsTicks);
                }
            }

            if (ptsTicks == null)
            {
                if (!_ptsFallbackWarned)
                {
                    _ptsFallbackWarned = true;
                    _logger.LogWarning(
                        "SpeechService [Avatar]: PTS missing on {Stream}; falling back to wall-clock (canary: Phase 2 regression?)",
                        isAudio ? "audio" : "video");
                }
                // Per SDK xmldoc, MediaPlatform.GetCurrentTimestamp() is the
                // canonical source. SpeechService doesn't currently hold a
                // platform reference; DateTime.UtcNow.Ticks is what the SDK
                // returns under the hood and is functionally equivalent here.
                return Clock();
            }

            // Anchor on first AUDIO buffer (Z4: audio is master clock).
            if (isAudio && _anchorTicks == null)
            {
                _anchorTicks = Clock();
                _anchorAudioPtsTicks = ptsTicks.Value;
                _logger.LogInformation(
                    "SpeechService [Avatar]: PTS anchor set on first audio. anchor_ticks={Anchor} first_audio_pts_ticks={Pts}",
                    _anchorTicks.Value, _anchorAudioPtsTicks.Value);
            }

            // Video that arrives before any audio: fall back to wall-clock for
            // these (rare; player strategy 2 will pace at fps cadence).
            if (_anchorTicks == null) return Clock();

            var resolved = _anchorTicks.Value + (ptsTicks.Value - _anchorAudioPtsTicks!.Value);
            if (isAudio)
            {
                RecordAudioPtsVsWallClockDrift(resolved);
            }

            // Diagnostic: log the FIRST computed (video - audio) offset once both
            // anchors are established. A misconfigured tb on the sidecar shows up
            // here long before LowOnFrames(Video) does (§9.3 mitigation, faster
            // signal than waiting for strategy-3 video drops).
            if (!_firstOffsetLogged && !isAudio)
            {
                _firstOffsetLogged = true;
                var offsetTicks = resolved - Clock();
                var offsetMs = offsetTicks / TimeSpan.TicksPerMillisecond;
                _logger.LogInformation(
                    "SpeechService [Avatar]: first video frame offset vs wall-clock-NOW = {OffsetMs} ms (resolved_ticks={Resolved})",
                    offsetMs, resolved);
                if (Math.Abs(offsetMs) > 100)
                {
                    _logger.LogWarning(
                        "SpeechService [Avatar]: |first video offset| > 100ms — likely tb/PTS conversion bug; expect strategy-3 video drops");
                }
            }

            return resolved;
        }

        private void RecordAudioPtsVsWallClockDrift(long resolvedTicks)
        {
            var now = Clock();
            var driftMs = (resolvedTicks - now) / TimeSpan.TicksPerMillisecond;
            if (_audioPtsDriftWindowStartTicks == 0)
                _audioPtsDriftWindowStartTicks = now;

            _audioPtsDriftSamples++;
            _audioPtsDriftSumMs += driftMs;
            _audioPtsDriftLastMs = driftMs;
            if (driftMs < _audioPtsDriftMinMs) _audioPtsDriftMinMs = driftMs;
            if (driftMs > _audioPtsDriftMaxMs) _audioPtsDriftMaxMs = driftMs;

            if (now - _audioPtsDriftWindowStartTicks < TimeSpan.FromSeconds(5).Ticks)
                return;

            var average = _audioPtsDriftSamples > 0 ? (double)_audioPtsDriftSumMs / _audioPtsDriftSamples : 0.0;
            _logger.LogInformation(
                "SpeechService [Avatar]: audio_pts_vs_wallclock_drift_ms last={Last} avg={Average:F1} min={Min} max={Max} samples={Samples}",
                _audioPtsDriftLastMs, average, _audioPtsDriftMinMs, _audioPtsDriftMaxMs, _audioPtsDriftSamples);

            _audioPtsDriftWindowStartTicks = now;
            _audioPtsDriftSamples = 0;
            _audioPtsDriftSumMs = 0;
            _audioPtsDriftMinMs = long.MaxValue;
            _audioPtsDriftMaxMs = long.MinValue;
            _audioPtsDriftLastMs = 0;
        }

        /// <summary>
        /// Loads the embedded typing_background.wav and splits it into 640-byte (20ms) PCM chunks.
        /// </summary>
        private List<byte[]> LoadTypingChunks()
        {
            var assembly = Assembly.GetExecutingAssembly();
            var resourceName = assembly.GetManifestResourceNames()
                .FirstOrDefault(n => n.EndsWith("typing_background.wav"));
            if (resourceName == null)
            {
                _logger.LogWarning("Embedded resource typing_background.wav not found");
                return new List<byte[]>();
            }

            using var stream = assembly.GetManifestResourceStream(resourceName);
            using var reader = new BinaryReader(stream);

            // Find the "data" chunk
            stream.Position = 12;
            while (stream.Position < stream.Length - 8)
            {
                var chunkId = reader.ReadBytes(4);
                var wavChunkSize = reader.ReadInt32();
                if (chunkId[0] == 'd' && chunkId[1] == 'a' && chunkId[2] == 't' && chunkId[3] == 'a')
                    break;
                stream.Position += wavChunkSize;
            }

            var chunks = new List<byte[]>();
            const int chunkSize = 640;
            while (stream.Position + chunkSize <= stream.Length)
            {
                chunks.Add(reader.ReadBytes(chunkSize));
            }
            return chunks;
        }

        /// <summary>
        /// Connect to the Python sidecar WebSocket and start receiving frames.
        /// </summary>
        private async Task EnsureConnectedAsync()
        {
            // Fast path: already open. Cheap check before taking the gate.
            if (_sidecarWs != null && _sidecarWs.State == WebSocketState.Open)
                return;

            await _connectGate.WaitAsync().ConfigureAwait(false);
            try
            {
                // Re-check inside the gate. A concurrent caller may have just
                // finished the handshake, OR may still be connecting (state =
                // Connecting). Either way we must not start a second connect.
                if (_sidecarWs != null &&
                    (_sidecarWs.State == WebSocketState.Open ||
                     _sidecarWs.State == WebSocketState.Connecting))
                {
                    return;
                }

                _sidecarWs?.Dispose();
                _sidecarWs = new ClientWebSocket();
                _receiveCts = new CancellationTokenSource();

                var wsUri = _sidecarUrl.Replace("http://", "ws://").Replace("https://", "wss://");
                if (!wsUri.EndsWith("/stream"))
                    wsUri += "/stream";

                _logger.LogWarning($"SpeechService [Avatar]: Connecting to sidecar at {wsUri}");
                await _sidecarWs.ConnectAsync(new Uri(wsUri), CancellationToken.None);
                _logger.LogWarning("SpeechService [Avatar]: Connected to sidecar");

                // Start background receive loop
                _ = Task.Run(() => ReceiveLoop(_receiveCts.Token));
            }
            finally
            {
                _connectGate.Release();
            }
        }

        /// <summary>
        /// Background loop that receives audio + video frames from the sidecar.
        /// Protocol:
        ///   {"type":"audio","data":"base64 PCM"}
        ///   {"type":"video","data":"base64 NV12","width":1920,"height":1080,"timestamp":123456}
        ///   {"type":"speaking","value":true/false}
        ///   {"type":"greeting","text":"..."}
        /// </summary>
        private async Task ReceiveLoop(CancellationToken ct)
        {
            // Per-receive chunk buffer; messages can be much larger (NV12 1080p
            // base64-encoded ≈ 4 MB). We accumulate fragments into `message`
            // until EndOfMessage, then parse.
            var chunk = new byte[64 * 1024];
            using var message = new MemoryStream();
            try
            {
                while (!ct.IsCancellationRequested && _sidecarWs.State == WebSocketState.Open)
                {
                    message.SetLength(0);
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await _sidecarWs.ReceiveAsync(new ArraySegment<byte>(chunk), ct);
                        if (result.MessageType == WebSocketMessageType.Close)
                        {
                            _logger.LogWarning("SpeechService [Avatar]: Sidecar closed connection");
                            return;
                        }
                        message.Write(chunk, 0, result.Count);
                    } while (!result.EndOfMessage && !ct.IsCancellationRequested);

                    if (message.Length == 0) continue;

                    string type;
                    JsonElement root;
                    JsonDocument doc;
                    try
                    {
                        doc = JsonDocument.Parse(message.GetBuffer().AsMemory(0, (int)message.Length));
                        root = doc.RootElement;
                        type = root.GetProperty("type").GetString();
                    }
                    catch (Exception parseEx)
                    {
                        _logger.LogWarning(parseEx, "SpeechService [Avatar]: malformed sidecar message ({Bytes} bytes)", message.Length);
                        continue;
                    }

                    if (_audioPipelineDiag)
                    {
                        var messageNumber = Interlocked.Increment(ref _audioPipelineMessageCount);
                        _logger.LogWarning(
                            "SpeechService [Avatar][audio-pipeline]: sidecar message #{Number} type={Type} bytes={Bytes}",
                            messageNumber,
                            type,
                            message.Length);
                    }

                    using (doc)
                    {
                        switch (type)
                        {
                            case "audio":
                                HandleAudioFrame(root);
                                break;
                            case "video":
                                HandleVideoFrame(root);
                                break;
                            case "speaking":
                                _isSpeaking = root.GetProperty("value").GetBoolean();
                                Interlocked.Exchange(ref _speakingSinceTicks, _isSpeaking ? Clock() : 0);
                                _logger.LogInformation($"SpeechService [Avatar]: speaking={_isSpeaking}");
                                break;
                            case "greeting":
                                _logger.LogInformation("SpeechService [Avatar]: Greeting sent by sidecar");
                                break;
                            default:
                                if (_audioPipelineDiag)
                                {
                                    _logger.LogWarning(
                                        "SpeechService [Avatar][audio-pipeline]: unknown sidecar message type={Type} bytes={Bytes}",
                                        type,
                                        message.Length);
                                }
                                break;
                        }
                    }
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
            {
                _logger.LogError(ex, "SpeechService [Avatar]: ReceiveLoop error");
            }
        }

        /// <summary>
        /// Handles incoming PCM audio frame from sidecar and sends to Teams via AudioSocket.
        /// </summary>
        private void HandleAudioFrame(JsonElement root)
        {
            var receivedTicks = Clock();
            var sidecarSentTicks = TryGetInt64(root, "sidecar_sent_ticks");
            var responseId = TryGetString(root, "vl_response_id");
            var pcmBase64 = root.GetProperty("data").GetString();
            var pcmData = Convert.FromBase64String(pcmBase64);

            if (_latencyDiag)
            {
                var count = Interlocked.Increment(ref _latencySidecarAudioMessages);
                if (ShouldLogLatency(count))
                {
                    var sidecarToBotMs = sidecarSentTicks.HasValue
                        ? (receivedTicks - sidecarSentTicks.Value) / TimeSpan.TicksPerMillisecond
                        : (long?)null;
                    _logger.LogWarning(
                        "[latency][bot] sidecar_audio_received count={Count} response_id={ResponseId} pcm_bytes={PcmBytes} sidecar_to_bot_ms={SidecarToBotMs}",
                        count,
                        responseId ?? "",
                        pcmData.Length,
                        sidecarToBotMs);
                }
            }

            var audioMediaBuffers = new List<AudioMediaBuffer>();
            // Phase 2: PTS-anchored timeline when LISA_USE_PTS=1, else legacy
            // wall-clock-at-receive (bit-for-bit Phase-1-equivalent).
            var referenceTime = _usePts ? ResolveTimestamp(root, isAudio: true) : DateTime.Now.Ticks;
            var firstReferenceTime = referenceTime;
            const int chunkSize = 640; // 20ms at 16kHz/16bit/mono
            const int ticksPerChunk = 20 * 10000;

            for (int offset = 0; offset + chunkSize <= pcmData.Length; offset += chunkSize)
            {
                IntPtr unmanagedBuffer = Marshal.AllocHGlobal(chunkSize);
                Marshal.Copy(pcmData, offset, unmanagedBuffer, chunkSize);
                // Use SafeAudioSendBuffer (leaks intentionally) to avoid
                // AccessViolationException race in AudioFramePlayer.CopyMemory.
                var audioBuffer = new EchoBot.Util.SafeAudioSendBuffer(unmanagedBuffer, chunkSize, AudioFormat.Pcm16K, referenceTime);
                audioMediaBuffers.Add(audioBuffer);
                referenceTime += ticksPerChunk;
            }

            if (_audioPipelineDiag)
            {
                var audioMessageNumber = Interlocked.Increment(ref _audioPipelineAudioMessageCount);
                _logger.LogWarning(
                    "SpeechService [Avatar][audio-pipeline]: audio message #{Number} pcmBytes={PcmBytes} base64Chars={Base64Chars} chunks={Chunks} remainderBytes={Remainder} firstReferenceTicks={ReferenceTicks}",
                    audioMessageNumber,
                    pcmData.Length,
                    pcmBase64?.Length ?? 0,
                    audioMediaBuffers.Count,
                    pcmData.Length % chunkSize,
                    firstReferenceTime);
                if (audioMediaBuffers.Count == 0)
                {
                    _logger.LogWarning(
                        "SpeechService [Avatar][audio-pipeline]: audio received but produced zero AudioMediaBuffers pcmBytes={PcmBytes} chunkSize={ChunkSize}",
                        pcmData.Length,
                        chunkSize);
                }
            }

            if (audioMediaBuffers.Count > 0)
            {
                var args = new MediaStreamEventArgs
                {
                    AudioMediaBuffers = audioMediaBuffers,
                    VideoMediaBuffers = new List<VideoMediaBuffer>(),
                    LatencySource = "sidecar_audio",
                    VoiceLiveResponseId = responseId,
                    SidecarSentTicks = sidecarSentTicks,
                    BotDispatchTicks = Clock(),
                };
                if (_audioPipelineDiag)
                {
                    _logger.LogWarning(
                        "SpeechService [Avatar][audio-pipeline]: dispatching audio buffers count={Count}",
                        audioMediaBuffers.Count);
                }
                OnSendMediaBufferEventArgs(this, args);
            }
        }

        /// <summary>
        /// Handles incoming NV12 video frame from sidecar and sends to Teams via VideoSocket.
        /// </summary>
        private void HandleVideoFrame(JsonElement root)
        {
            var receivedTicks = Clock();
            var sidecarSentTicks = TryGetInt64(root, "sidecar_sent_ticks");
            var responseId = TryGetString(root, "vl_response_id");
            var nv12Base64 = root.GetProperty("data").GetString();
            var nv12Data = Convert.FromBase64String(nv12Base64);
            var width = root.GetProperty("width").GetInt32();
            var height = root.GetProperty("height").GetInt32();

            if (_latencyDiag)
            {
                var count = Interlocked.Increment(ref _latencySidecarVideoMessages);
                if (ShouldLogLatency(count))
                {
                    var sidecarToBotMs = sidecarSentTicks.HasValue
                        ? (receivedTicks - sidecarSentTicks.Value) / TimeSpan.TicksPerMillisecond
                        : (long?)null;
                    _logger.LogWarning(
                        "[latency][bot] sidecar_video_received count={Count} response_id={ResponseId} nv12_bytes={Nv12Bytes} size={Width}x{Height} sidecar_to_bot_ms={SidecarToBotMs}",
                        count,
                        responseId ?? "",
                        nv12Data.Length,
                        width,
                        height,
                        sidecarToBotMs);
                }
            }
            // Phase 2: PTS-anchored timeline when LISA_USE_PTS=1, else preserve
            // legacy behavior (which fed `timestamp:0` literal Tick=0 to the
            // player — see design doc §1, treated by the player's strategy 2).
            long timestamp;
            if (_usePts)
            {
                timestamp = ResolveTimestamp(root, isAudio: false);
            }
            else
            {
                timestamp = root.TryGetProperty("timestamp", out var ts) ? ts.GetInt64() : DateTime.Now.Ticks;
            }

            IntPtr unmanagedBuffer = Marshal.AllocHGlobal(nv12Data.Length);
            Marshal.Copy(nv12Data, 0, unmanagedBuffer, nv12Data.Length);

            var videoFormat = VideoFormat.NV12_1920x1080_15Fps;
            if (width == 1280 && height == 720)
                videoFormat = VideoFormat.NV12_1280x720_15Fps;
            else if (width == 640 && height == 360)
                videoFormat = VideoFormat.NV12_640x360_15Fps;

            var videoBuffer = new VideoSendBuffer(unmanagedBuffer, (uint)nv12Data.Length, videoFormat, timestamp);

            var args = new MediaStreamEventArgs
            {
                AudioMediaBuffers = new List<AudioMediaBuffer>(),
                VideoMediaBuffers = new List<VideoMediaBuffer> { videoBuffer },
                LatencySource = "sidecar_video",
                VoiceLiveResponseId = responseId,
                SidecarSentTicks = sidecarSentTicks,
                BotDispatchTicks = Clock(),
            };
            OnSendMediaBufferEventArgs(this, args);
        }

        /// <summary>
        /// Appends the audio buffer from Teams and forwards to sidecar.
        /// </summary>
        public async Task AppendAudioBuffer(AudioMediaBuffer audioBuffer)
        {
            var bufferLength = audioBuffer.Length;
            if (bufferLength <= 0) return;
            var buffer = new byte[bufferLength];
            Marshal.Copy(audioBuffer.Data, buffer, 0, (int)bufferLength);
            await AppendAudioBuffer(buffer);
        }

        /// <summary>
        /// Appends already-copied PCM bytes and forwards to sidecar.
        /// </summary>
        public async Task AppendAudioBuffer(byte[] buffer)
        {
            if (!_isRunning)
            {
                Start();
                _ = Task.Run(async () =>
                {
                    try
                    {
                        await EnsureConnectedAsync();
                    }
                    catch (Exception ex)
                    {
                        _logger.LogError(ex, "SpeechService [Avatar]: Failed to connect to sidecar");
                    }
                });
            }

            try
            {
                // Skip audio while avatar is speaking (prevent self-hearing loop)
                if (_isSpeaking)
                {
                    var since = Interlocked.Read(ref _speakingSinceTicks);
                    var elapsedMs = since > 0 ? (Clock() - since) / TimeSpan.TicksPerMillisecond : 0;
                    if (_maxSpeakingGateMs <= 0 || elapsedMs < _maxSpeakingGateMs)
                    {
                        return;
                    }

                    _isSpeaking = false;
                    Interlocked.Exchange(ref _speakingSinceTicks, 0);
                    _logger.LogWarning(
                        "SpeechService [Avatar]: speaking gate exceeded {MaxMs}ms; reopening candidate mic",
                        _maxSpeakingGateMs);
                }

                if (_sidecarWs == null || _sidecarWs.State != WebSocketState.Open)
                    return;

                if (buffer != null && buffer.Length > 0)
                {
                    // Send PCM audio to sidecar as base64 JSON message
                    var audioBase64 = Convert.ToBase64String(buffer);
                    string msg;
                    if (_latencyDiag)
                    {
                        var sentTicks = Clock();
                        var count = Interlocked.Increment(ref _latencyBotAudioMessages);
                        msg = JsonSerializer.Serialize(new Dictionary<string, object>
                        {
                            ["type"] = "audio",
                            ["data"] = audioBase64,
                            ["sent_at_ticks"] = sentTicks,
                            ["client_audio_seq"] = count,
                        });
                        if (ShouldLogLatency(count))
                        {
                            _logger.LogWarning(
                                "[latency][bot] candidate_audio_to_sidecar count={Count} pcm_bytes={PcmBytes} sent_ticks={SentTicks}",
                                count,
                                buffer.Length,
                                sentTicks);
                        }
                    }
                    else
                    {
                        msg = JsonSerializer.Serialize(new
                        {
                            type = "audio",
                            data = audioBase64
                        });
                    }
                    var bytes = Encoding.UTF8.GetBytes(msg);
                    await _sidecarWs.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);
                }
            }
            catch (Exception e)
            {
                _logger.LogError(e, "SpeechService [Avatar]: Exception forwarding audio to sidecar");
            }
        }

        /// <summary>
        /// Force-opens the sidecar WS (if not already open) and sends a
        /// {"type":"call_established"} signal so the sidecar can release
        /// the greeting gate (LISA_WAIT_FOR_CALL_ESTABLISHED=1) and prime
        /// the Voice Live agent. Called from CallHandler when CallState
        /// transitions to Established. Safe to call multiple times — the
        /// sidecar idempotently gates on first receipt per connection.
        /// Behavior shift: previously the sidecar WS opened lazily on first
        /// inbound audio frame in AppendAudioBuffer. With this method the
        /// WS may open earlier in calls where Teams Media doesn't deliver
        /// early-media frames before CallState.Established.
        /// </summary>
        public async Task NotifyCallEstablishedAsync()
        {
            if (!_isRunning)
            {
                Start();
            }

            try
            {
                await EnsureConnectedAsync();
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "SpeechService [Avatar]: NotifyCallEstablishedAsync EnsureConnected failed");
                return;
            }

            try
            {
                if (_sidecarWs == null || _sidecarWs.State != WebSocketState.Open)
                {
                    _logger.LogWarning("SpeechService [Avatar]: NotifyCallEstablishedAsync — WS not open after EnsureConnected");
                    return;
                }
                var callEstablishedPayload = new Dictionary<string, string>(_interviewContext)
                {
                    ["type"] = "call_established",
                };
                var msg = JsonSerializer.Serialize(callEstablishedPayload);
                var bytes = Encoding.UTF8.GetBytes(msg);
                await _sidecarWs.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);
                _logger.LogWarning("SpeechService [Avatar]: call_established sent to sidecar");
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "SpeechService [Avatar]: failed to send call_established");
            }
        }

        public virtual void OnSendMediaBufferEventArgs(object sender, MediaStreamEventArgs e)
        {
            SendMediaBuffer?.Invoke(this, e);
        }

        public async Task ShutDownAsync()
        {
            if (!_isRunning && _sidecarWs == null) return;

            _isRunning = false;

            _receiveCts?.Cancel();

            await _connectGate.WaitAsync().ConfigureAwait(false);
            try
            {
                if (_sidecarWs != null)
                {
                    try
                    {
                        if (_sidecarWs.State == WebSocketState.Open || _sidecarWs.State == WebSocketState.CloseReceived)
                        {
                            await _sidecarWs.CloseAsync(WebSocketCloseStatus.NormalClosure, "shutdown", CancellationToken.None).ConfigureAwait(false);
                        }
                    }
                    catch { }

                    _sidecarWs.Dispose();
                    _sidecarWs = null;
                }
            }
            finally
            {
                _connectGate.Release();
            }
        }

        private void Start()
        {
            if (!_isRunning)
            {
                _isRunning = true;
            }
        }

        /// <summary>
        /// Plays the typing background sound in a loop until cancelled.
        /// </summary>
        public async Task PlayTypingBackgroundAsync(CancellationToken ct)
        {
            if (_typingChunks == null || _typingChunks.Count == 0) return;

            _logger.LogInformation("Typing background started");
            const int batchSize = 50;
            const int numberOfTicksInOneAudioBuffer = 20 * 10000;

            try
            {
                int chunkIndex = 0;
                while (!ct.IsCancellationRequested)
                {
                    var audioMediaBuffers = new List<AudioMediaBuffer>();
                    var referenceTime = DateTime.Now.Ticks;
                    int sent = 0;

                    for (int i = 0; i < batchSize && chunkIndex < _typingChunks.Count; i++, chunkIndex++, sent++)
                    {
                        var chunk = _typingChunks[chunkIndex];
                        IntPtr unmanagedBuffer = Marshal.AllocHGlobal(640);
                        Marshal.Copy(chunk, 0, unmanagedBuffer, 640);
                        var audioBuffer = new AudioSendBuffer(unmanagedBuffer, 640, AudioFormat.Pcm16K, referenceTime);
                        audioMediaBuffers.Add(audioBuffer);
                        referenceTime += numberOfTicksInOneAudioBuffer;
                    }

                    if (audioMediaBuffers.Count > 0)
                    {
                        var args = new MediaStreamEventArgs
                        {
                            AudioMediaBuffers = audioMediaBuffers,
                            VideoMediaBuffers = new List<VideoMediaBuffer>()
                        };
                        OnSendMediaBufferEventArgs(this, args);
                    }

                    if (chunkIndex >= _typingChunks.Count)
                        chunkIndex = 0;

                    await Task.Delay(sent * 20, ct).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException) { }
            _logger.LogInformation("Typing background stopped");
        }
    }
}
