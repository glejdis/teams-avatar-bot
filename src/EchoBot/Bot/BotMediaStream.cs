// ***********************************************************************
// Assembly         : EchoBot.Services
// Author           : JasonTheDeveloper
// Created          : 09-07-2020
//
// Last Modified By : bcage29
// Last Modified On : 10-17-2023
// ***********************************************************************
// <copyright file="BotMediaStream.cs" company="Microsoft Corporation">
//     Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT license.
// </copyright>
// <summary>The bot media stream.</summary>
// ***********************************************************************-
using EchoBot.Media;
using EchoBot.Models;
using EchoBot.Util;
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Common;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Skype.Bots.Media;
using Microsoft.Skype.Internal.Media.Services.Common;
using System.Linq;
using System.Runtime.InteropServices;

namespace EchoBot.Bot
{
    /// <summary>
    /// Class responsible for streaming audio and video.
    /// </summary>
    public class BotMediaStream : ObjectRootDisposable
    {
        private AppSettings _settings;

        /// <summary>
        /// The participants
        /// </summary>
        internal List<IParticipant> participants;

        /// <summary>
        /// The audio socket
        /// </summary>
        private readonly IAudioSocket _audioSocket;
        /// <summary>
        /// The video socket (for avatar output)
        /// </summary>
        private readonly IVideoSocket _videoSocket;
        /// <summary>
        /// The media stream
        /// </summary>
        private readonly ILogger _logger;
        private AudioVideoFramePlayer audioVideoFramePlayer;
        private readonly TaskCompletionSource<bool> audioSendStatusActive;
        private readonly TaskCompletionSource<bool> startVideoPlayerCompleted;
        private AudioVideoFramePlayerSettings audioVideoFramePlayerSettings;
        private List<AudioMediaBuffer> audioMediaBuffers = new List<AudioMediaBuffer>();
        private List<VideoMediaBuffer> videoMediaBuffers = new List<VideoMediaBuffer>();
        private int shutdown;
        private readonly SpeechService _languageService;
        private readonly object _sendQueueLock = new object();
        private Task _sendQueueTail = Task.CompletedTask;

        // Diagnostic counters for OnAudioMediaReceived energy logging.
        private long _diagFrameCount;
        private long _diagBytesTotal;
        private long _diagSampleAbsSum;
        private int _diagSamplePeak;
        private int _diagNonZeroSamples;
        private long _diagWindowStartTicks;
        private int _diagFirstFrameLogsRemaining = 5;
        private long _sendDiagAudioBuffers;
        private long _sendDiagVideoBuffers;
        private long _sendDiagWindowStartTicks;
        private static readonly long DiagWindowTicks = TimeSpan.FromSeconds(5).Ticks;
        private readonly bool _latencyDiag;
        private readonly int _mediaPlayoutBufferMs;
        private long _latencyInboundAudioFrames;
        private long _latencyEnqueueRequests;

        /// <summary>
        /// Safe logging wrapper. EventLogLogger on stripped Windows VMs throws
        /// AggregateException("RPC server is unavailable") whenever any provider in
        /// the chain dies. Provider-removal in Program.cs / BotHost.cs has not been
        /// 100% reliable, so we wrap every log call from inside async-void media
        /// handlers and route to Console.Error on failure to keep the process alive.
        /// </summary>
        private void SafeLog(Microsoft.Extensions.Logging.LogLevel level, string message, Exception? ex = null)
        {
            try
            {
                if (ex != null) _logger.Log(level, ex, message);
                else _logger.Log(level, message);
            }
            catch (Exception logEx)
            {
                try { Console.Error.WriteLine($"[bot-mic-diag][SafeLog] {level} {message} | logger-failed: {logEx.GetType().Name}: {logEx.Message}"); } catch { }
                if (ex != null)
                {
                    try { Console.Error.WriteLine($"[bot-mic-diag][SafeLog] inner: {ex}"); } catch { }
                }
            }
        }

        /// <summary>
        /// Initializes a new instance of the <see cref="BotMediaStream" /> class.
        /// </summary>
        /// <param name="mediaSession">The media session.</param>
        /// <param name="callId">The call identity</param>
        /// <param name="graphLogger">The Graph logger.</param>
        /// <param name="logger">The logger.</param>
        /// <param name="settings">Azure settings</param>
        /// <exception cref="InvalidOperationException">A mediaSession needs to have at least an audioSocket</exception>
        public BotMediaStream(
            ILocalMediaSession mediaSession,
            string callId,
            IGraphLogger graphLogger,
            ILogger logger,
            AppSettings settings,
            JoinCallBody? joinContext = null
        )
            : base(graphLogger)
        {
            ArgumentVerifier.ThrowOnNullArgument(mediaSession, nameof(mediaSession));
            ArgumentVerifier.ThrowOnNullArgument(logger, nameof(logger));
            ArgumentVerifier.ThrowOnNullArgument(settings, nameof(settings));

            _settings = settings;
            _logger = logger;
            _latencyDiag = ReadBoolEnv("LISA_LATENCY_DIAG", false);
            _mediaPlayoutBufferMs = NormalizeMediaPlayoutBufferMs(_settings.MediaPlayoutBufferMs);

            this.participants = new List<IParticipant>();

            this.audioSendStatusActive = new TaskCompletionSource<bool>();
            this.startVideoPlayerCompleted = new TaskCompletionSource<bool>();

            // Subscribe to the audio media.
            this._audioSocket = mediaSession.AudioSocket;
            if (this._audioSocket == null)
            {
                throw new InvalidOperationException("A mediaSession needs to have at least an audioSocket");
            }

            // Subscribe to the video socket (first one) for avatar output.
            if (mediaSession.VideoSockets != null && mediaSession.VideoSockets.Any())
            {
                this._videoSocket = mediaSession.VideoSockets.First();
                _logger.LogWarning("BotMediaStream: VideoSocket acquired for avatar output");
            }

            var ignoreTask = this.StartAudioVideoFramePlayerAsync().ForgetAndLogExceptionAsync(this.GraphLogger, "Failed to start the player");

            this._audioSocket.AudioSendStatusChanged += OnAudioSendStatusChanged;            

            this._audioSocket.AudioMediaReceived += this.OnAudioMediaReceived;

            if (_settings.UseSpeechService)
            {
                try
                {
                    _logger.LogWarning("BotMediaStream: Creating SpeechService...");
                    _languageService = new SpeechService(_settings, _logger, joinContext);
                    _languageService.SendMediaBuffer += this.OnSendMediaBuffer;
                    _logger.LogWarning("BotMediaStream: SpeechService created successfully");
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "BotMediaStream: FAILED to create SpeechService - falling back to echo mode");
                    _languageService = null;
                }
            }
            else
            {
                _logger.LogWarning("BotMediaStream: UseSpeechService is FALSE - echo mode");
            }
        }

        /// <summary>
        /// Gets the participants.
        /// </summary>
        /// <returns>List&lt;IParticipant&gt;.</returns>
        public List<IParticipant> GetParticipants()
        {
            return participants;
        }

        /// <summary>
        /// Forwards a call-established notification to the SpeechService so
        /// the sidecar can release the greeting gate (when
        /// LISA_WAIT_FOR_CALL_ESTABLISHED=1). No-op if SpeechService is null
        /// (UseSpeechService=false / echo mode).
        /// </summary>
        public async Task NotifyCallEstablishedAsync()
        {
            if (_languageService != null)
            {
                await _languageService.NotifyCallEstablishedAsync();
            }
        }

        /// <summary>
        /// Shut down.
        /// </summary>
        /// <returns><see cref="Task" />.</returns>
        public async Task ShutdownAsync()
        {
            if (Interlocked.CompareExchange(ref this.shutdown, 1, 0) != 0)
            {
                return;
            }

            await this.startVideoPlayerCompleted.Task.ConfigureAwait(false);

            if (this._languageService != null)
            {
                this._languageService.SendMediaBuffer -= this.OnSendMediaBuffer;
                try
                {
                    await this._languageService.ShutDownAsync().ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    SafeLog(Microsoft.Extensions.Logging.LogLevel.Error, "SpeechService shutdown failed", ex);
                }
            }

            // unsubscribe
            if (this._audioSocket != null)
            {
                this._audioSocket.AudioSendStatusChanged -= this.OnAudioSendStatusChanged;
                this._audioSocket.AudioMediaReceived -= this.OnAudioMediaReceived;
            }

            Task sendQueueTail;
            lock (_sendQueueLock)
            {
                sendQueueTail = _sendQueueTail;
            }

            try
            {
                await Task.WhenAny(sendQueueTail, Task.Delay(TimeSpan.FromSeconds(2))).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning, "Send queue drain failed during shutdown", ex);
            }

            // shutting down the players
            if (this.audioVideoFramePlayer != null)
            {
                await this.audioVideoFramePlayer.ShutdownAsync().ConfigureAwait(false);
            }

            // make sure all the audio and video buffers are disposed, it can happen that,
            // the buffers were not enqueued but the call was disposed if the caller hangs up quickly
            DisposeMediaBuffers(this.audioMediaBuffers, this.videoMediaBuffers);

            _logger.LogInformation($"disposed {this.audioMediaBuffers.Count} audioMediaBuffers and {this.videoMediaBuffers.Count} videoMediaBuffers.");

            this.audioMediaBuffers.Clear();
            this.videoMediaBuffers.Clear();
        }

        private static void DisposeMediaBuffers(IEnumerable<AudioMediaBuffer>? audioBuffers, IEnumerable<VideoMediaBuffer>? videoBuffers)
        {
            if (audioBuffers != null)
            {
                foreach (var audioBuffer in audioBuffers)
                {
                    audioBuffer?.Dispose();
                }
            }

            if (videoBuffers != null)
            {
                foreach (var videoBuffer in videoBuffers)
                {
                    videoBuffer?.Dispose();
                }
            }
        }

        private static bool ReadBoolEnv(string name, bool fallback)
        {
            var raw = Environment.GetEnvironmentVariable(name);
            if (string.IsNullOrWhiteSpace(raw)) return fallback;
            return raw.Trim().ToLowerInvariant() is "1" or "true" or "yes" or "on";
        }

        private static int NormalizeMediaPlayoutBufferMs(int configuredValue)
        {
            if (configuredValue <= 0) return 1000;
            return Math.Clamp(configuredValue, 100, 5000);
        }

        private static bool ShouldLogLatency(long count)
        {
            return count <= 5 || count % 50 == 0;
        }

        /// <summary>
        /// Initialize AV frame player.
        /// </summary>
        /// <returns>Task denoting creation of the player with initial frames enqueued.</returns>
        private async Task StartAudioVideoFramePlayerAsync()
        {
            try
            {
                _logger.LogInformation("Send status active for audio and video Creating the audio video player");
                this.audioVideoFramePlayerSettings =
                    new AudioVideoFramePlayerSettings(new AudioSettings(20), new VideoSettings(), (uint)_mediaPlayoutBufferMs);
                this.audioVideoFramePlayer = new AudioVideoFramePlayer(
                    (AudioSocket)_audioSocket,
                    _videoSocket != null ? (VideoSocket)_videoSocket : null,
                    this.audioVideoFramePlayerSettings);

                _logger.LogWarning(
                    "created the audio video player with mediaPlayoutBufferMs={MediaPlayoutBufferMs}, LISA_LATENCY_DIAG={LatencyDiag}",
                    _mediaPlayoutBufferMs,
                    _latencyDiag);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to create the audioVideoFramePlayer with exception");
            }
            finally
            {
                this.startVideoPlayerCompleted.TrySetResult(true);
            }
        }

        /// <summary>
        /// Callback for informational updates from the media plaform about audio status changes.
        /// Once the status becomes active, audio can be loopbacked.
        /// </summary>
        /// <param name="sender">The audio socket.</param>
        /// <param name="e">Event arguments.</param>
        private void OnAudioSendStatusChanged(object? sender, AudioSendStatusChangedEventArgs e)
        {
            SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                $"[bot-send-diag] AudioSendStatusChanged MediaSendStatus={e.MediaSendStatus}");

            if (e.MediaSendStatus == MediaSendStatus.Active)
            {
                this.audioSendStatusActive.TrySetResult(true);
            }
        }

        /// <summary>
        /// Receive audio from subscribed participant.
        /// </summary>
        /// <param name="sender">The sender.</param>
        /// <param name="e">The audio media received arguments.</param>
        private async void OnAudioMediaReceived(object? sender, AudioMediaReceivedEventArgs e)
        {
            // Outer guard: this is async void; any uncaught exception (including
            // exceptions thrown by ILogger providers) will tear down the process
            // via the thread pool. Keep this whole method swallowing.
            try
            {
            _logger.LogTrace($"Received Audio: [AudioMediaReceivedEventArgs(Data=<{e.Buffer.Data.ToString()}>, Length={e.Buffer.Length}, Timestamp={e.Buffer.Timestamp})]");

            // ----- First-N-frames diagnostic dump --------------------------------
            // Logs MediaSourceId / ActiveSpeakers / OriginalSenderTimestamp on the
            // very first audio frames so we can tell why mixed buffers come back
            // silent: bot in lobby, no participants visible, or unmixed-policy not
            // granted by the tenant. Decrement counter atomically to log only first 5.
            if (_diagFirstFrameLogsRemaining > 0)
            {
                int remaining = Interlocked.Decrement(ref _diagFirstFrameLogsRemaining);
                if (remaining >= 0)
                {
                    try
                    {
                        var unmixedSnap = e.Buffer.UnmixedAudioBuffers?.ToArray();
                        var activeSpeakers = e.Buffer.ActiveSpeakers;
                        string activeStr = activeSpeakers == null
                            ? "<null>"
                            : "[" + string.Join(",", activeSpeakers) + "]";
                        SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                            $"[bot-mic-diag][first-frame {5 - remaining}/5] mixed: Length={e.Buffer.Length} DataPtrZero={(e.Buffer.Data == IntPtr.Zero)} Timestamp={e.Buffer.Timestamp} ActiveSpeakers={activeStr} unmixedCount={(unmixedSnap?.Length ?? 0)} participants={participants.Count}");
                        if (unmixedSnap != null)
                        {
                            for (int i = 0; i < unmixedSnap.Length; i++)
                            {
                                var ub = unmixedSnap[i];
                                SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                                    $"[bot-mic-diag][first-frame {5 - remaining}/5]   unmixed[{i}]: ActiveSpeakerId={ub.ActiveSpeakerId} Length={ub.Length} DataPtrZero={(ub.Data == IntPtr.Zero)} OriginalSenderTimestamp={ub.OriginalSenderTimestamp}");
                            }
                        }
                        // Participants snapshot (helps detect lobby/empty-room state)
                        for (int p = 0; p < participants.Count; p++)
                        {
                            try
                            {
                                var part = participants[p];
                                var ident = part.Resource?.Info?.Identity?.User?.DisplayName
                                    ?? part.Resource?.Info?.Identity?.User?.Id
                                    ?? "<no-user-identity>";
                                SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                                    $"[bot-mic-diag][first-frame {5 - remaining}/5]   participant[{p}]: id={part.Id} display={ident}");
                            }
                            catch { }
                        }
                    }
                    catch (Exception fdEx)
                    {
                        SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning, "[bot-mic-diag][first-frame] dump failed", fdEx);
                    }
                }
            }
            // ----- end first-N-frames dump --------------------------------------

            // ReceiveUnmixedMeetingAudio=true: prefer per-participant buffers and
            // sum-mix them into a single PCM stream. The bot's own audio is NOT
            // emitted as a participant buffer here, so this avoids the AEC loopback
            // that zeroed the mixed-mode mic input.
            byte[]? pcmBytes = null;
            int unmixedCount = 0;
            try
            {
                // UnmixedAudioBuffers is IEnumerable<UnmixedAudioBuffer> in this SDK
                // version; materialize to an array so we can index + length-check.
                var unmixed = e.Buffer.UnmixedAudioBuffers?.ToArray();
                if (unmixed != null && unmixed.Length > 0)
                {
                    unmixedCount = unmixed.Length;
                    // Find longest buffer; assume all share Pcm16K format.
                    int maxLen = 0;
                    for (int i = 0; i < unmixed.Length; i++)
                    {
                        int len = (int)unmixed[i].Length;
                        if (len > maxLen) maxLen = len;
                    }
                    if (maxLen > 0)
                    {
                        // Sum-mix as int16 with clipping.
                        int sampleCount = maxLen / 2;
                        var mix = new int[sampleCount];
                        for (int i = 0; i < unmixed.Length; i++)
                        {
                            var ub = unmixed[i];
                            int len = (int)ub.Length;
                            if (len <= 0 || ub.Data == IntPtr.Zero) continue;
                            var tmp = new byte[len];
                            try { Marshal.Copy(ub.Data, tmp, 0, len); }
                            catch (Exception copyEx)
                            {
                                SafeLog(Microsoft.Extensions.Logging.LogLevel.Error, $"[bot-mic-diag] Marshal.Copy unmixed[{i}] failed", copyEx);
                                continue;
                            }
                            int n = len / 2;
                            for (int j = 0; j < n; j++)
                            {
                                short s = (short)(tmp[2 * j] | (tmp[2 * j + 1] << 8));
                                mix[j] += s;
                            }
                        }
                        pcmBytes = new byte[maxLen];
                        for (int j = 0; j < sampleCount; j++)
                        {
                            int v = mix[j];
                            if (v > short.MaxValue) v = short.MaxValue;
                            else if (v < short.MinValue) v = short.MinValue;
                            short s = (short)v;
                            pcmBytes[2 * j] = (byte)(s & 0xFF);
                            pcmBytes[2 * j + 1] = (byte)((s >> 8) & 0xFF);
                        }
                    }
                }
                else
                {
                    // Fallback: mixed buffer (e.g. when no participants in unmixed list).
                    int pcmLen = (int)e.Buffer.Length;
                    if (pcmLen > 0 && e.Buffer.Data != IntPtr.Zero)
                    {
                        pcmBytes = new byte[pcmLen];
                        try { Marshal.Copy(e.Buffer.Data, pcmBytes, 0, pcmLen); }
                        catch (Exception copyEx)
                        {
                            SafeLog(Microsoft.Extensions.Logging.LogLevel.Error, "[bot-mic-diag] Marshal.Copy mixed-fallback failed", copyEx);
                            pcmBytes = null;
                        }
                    }
                }
            }
            catch (Exception unmixEx)
            {
                SafeLog(Microsoft.Extensions.Logging.LogLevel.Error, "[bot-mic-diag] unmixed mix-down failed", unmixEx);
                pcmBytes = null;
            }
            // surface unmixed source count occasionally so we can confirm mode in logs
            if (_diagFrameCount % 250 == 0)
            {
                SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning, $"[bot-mic-diag] unmixed source count={unmixedCount}");
            }

            // --- Diagnostic energy probe (raw, before any forwarding) ---
            try
            {
                var diagLen = pcmBytes?.Length ?? 0;
                if (diagLen > 0 && pcmBytes != null)
                {
                    var diagBuf = pcmBytes;
                    int sampleCount = diagLen / 2;
                    int peak = 0;
                    long absSum = 0;
                    int nonZero = 0;
                    for (int i = 0; i < sampleCount; i++)
                    {
                        short s = (short)(diagBuf[2 * i] | (diagBuf[2 * i + 1] << 8));
                        int a = s < 0 ? -s : s;
                        absSum += a;
                        if (a > peak) peak = a;
                        if (s != 0) nonZero++;
                    }
                    _diagFrameCount++;
                    _diagBytesTotal += diagLen;
                    _diagSampleAbsSum += absSum;
                    _diagNonZeroSamples += nonZero;
                    if (peak > _diagSamplePeak) _diagSamplePeak = peak;

                    long now = DateTime.UtcNow.Ticks;
                    if (_diagWindowStartTicks == 0) _diagWindowStartTicks = now;
                    if (now - _diagWindowStartTicks >= DiagWindowTicks)
                    {
                        long totalSamples = _diagBytesTotal / 2;
                        double meanAbs = totalSamples > 0 ? (double)_diagSampleAbsSum / totalSamples : 0.0;
                        SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                            $"[bot-mic-diag] frames={_diagFrameCount} bytes={_diagBytesTotal} samples={totalSamples} nonZero={_diagNonZeroSamples} peak={_diagSamplePeak} meanAbs={meanAbs:F1} (5s window)");
                        _diagFrameCount = 0;
                        _diagBytesTotal = 0;
                        _diagSampleAbsSum = 0;
                        _diagNonZeroSamples = 0;
                        _diagSamplePeak = 0;
                        _diagWindowStartTicks = now;
                    }
                }
                else
                {
                    SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                        $"[bot-mic-diag] empty buffer: Length={diagLen} DataPtrZero={(e.Buffer.Data == IntPtr.Zero)}");
                }
            }
            catch (Exception diagEx)
            {
                SafeLog(Microsoft.Extensions.Logging.LogLevel.Error, "[bot-mic-diag] failed", diagEx);
            }
            // --- end diagnostic probe ---

            try
            {
                if (!startVideoPlayerCompleted.Task.IsCompleted) { return; }

                if (_languageService != null)
                {
                    // send audio buffer to language service for processing
                    // the particpant talking will hear the bot repeat what they said
                    if (pcmBytes != null)
                    {
                        if (_latencyDiag && pcmBytes.Length > 0)
                        {
                            var count = Interlocked.Increment(ref _latencyInboundAudioFrames);
                            if (ShouldLogLatency(count))
                            {
                                SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                                    $"[latency][bot] teams_audio_received count={count} pcm_bytes={pcmBytes.Length} unmixed_sources={unmixedCount}");
                            }
                        }
                        await _languageService.AppendAudioBuffer(pcmBytes);
                    }
                    e.Buffer.Dispose();
                }
                else
                {
                    // send audio buffer back on the audio socket
                    // the particpant talking will hear themselves
                    if (pcmBytes != null && pcmBytes.Length > 0)
                    {
                        var currentTick = DateTime.Now.Ticks;
                        this.audioMediaBuffers = Util.Utilities.CreateAudioMediaBuffers(pcmBytes, currentTick, _logger);
                        await this.audioVideoFramePlayer.EnqueueBuffersAsync(this.audioMediaBuffers, new List<VideoMediaBuffer>());
                    }
                }
            }
            catch (Exception ex)
            {
                try { this.GraphLogger.Error(ex); } catch { }
                SafeLog(Microsoft.Extensions.Logging.LogLevel.Error, "OnAudioMediaReceived error", ex);
            }
            finally
            {
                e.Buffer.Dispose();
            }
            }
            catch (Exception outerEx)
            {
                // Last-resort guard. Logger itself may be broken; try GraphLogger
                // and Console as fallbacks. Never let this method throw.
                try { this.GraphLogger?.Error(outerEx); } catch { }
                try { Console.Error.WriteLine($"[bot-mic-diag] outer guard caught: {outerEx}"); } catch { }
            }
        }

        private void OnSendMediaBuffer(object? sender, Media.MediaStreamEventArgs e)
        {
            var audioBuffers = e.AudioMediaBuffers ?? new List<AudioMediaBuffer>();
            var videoBuffers = e.VideoMediaBuffers ?? new List<VideoMediaBuffer>();
            if (Volatile.Read(ref this.shutdown) == 1)
            {
                DisposeMediaBuffers(audioBuffers, videoBuffers);
                return;
            }

            this.audioMediaBuffers = audioBuffers;
            this.videoMediaBuffers = videoBuffers;
            var requestTicks = DateTime.UtcNow.Ticks;
            var latencySequence = Interlocked.Increment(ref _latencyEnqueueRequests);
            if (_latencyDiag && ShouldLogLatency(latencySequence))
            {
                var sidecarToRequestMs = e.SidecarSentTicks.HasValue
                    ? (requestTicks - e.SidecarSentTicks.Value) / TimeSpan.TicksPerMillisecond
                    : (long?)null;
                var dispatchToRequestMs = e.BotDispatchTicks.HasValue
                    ? (requestTicks - e.BotDispatchTicks.Value) / TimeSpan.TicksPerMillisecond
                    : (long?)null;
                SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                    $"[latency][bot] enqueue_request seq={latencySequence} source={e.LatencySource ?? ""} response_id={e.VoiceLiveResponseId ?? ""} audio={audioBuffers.Count} video={videoBuffers.Count} sidecar_to_request_ms={sidecarToRequestMs} dispatch_to_request_ms={dispatchToRequestMs}");
            }
            try
            {
                _sendDiagAudioBuffers += audioBuffers.Count;
                _sendDiagVideoBuffers += videoBuffers.Count;
                long now = DateTime.UtcNow.Ticks;
                if (_sendDiagWindowStartTicks == 0) _sendDiagWindowStartTicks = now;
                if (now - _sendDiagWindowStartTicks >= DiagWindowTicks)
                {
                    SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                        $"[bot-send-diag] enqueue-request audioBuffers={_sendDiagAudioBuffers} videoBuffers={_sendDiagVideoBuffers} audioSendActive={audioSendStatusActive.Task.IsCompleted}");
                    _sendDiagAudioBuffers = 0;
                    _sendDiagVideoBuffers = 0;
                    _sendDiagWindowStartTicks = now;
                }
            }
            catch { }

            Task enqueueTask;
            lock (_sendQueueLock)
            {
                _sendQueueTail = _sendQueueTail.ContinueWith(
                    _ => EnqueueMediaBuffersInOrderAsync(
                        audioBuffers,
                        videoBuffers,
                        requestTicks,
                        latencySequence,
                        e.LatencySource,
                        e.VoiceLiveResponseId,
                        e.SidecarSentTicks),
                    CancellationToken.None,
                    TaskContinuationOptions.None,
                    TaskScheduler.Default).Unwrap();
                enqueueTask = _sendQueueTail;
            }

            _ = enqueueTask;
        }

        private async Task EnqueueMediaBuffersInOrderAsync(
            List<AudioMediaBuffer> audioBuffers,
            List<VideoMediaBuffer> videoBuffers,
            long requestTicks,
            long latencySequence,
            string? latencySource,
            string? responseId,
            long? sidecarSentTicks)
        {
            try
            {
                if (Volatile.Read(ref this.shutdown) == 1)
                {
                    DisposeMediaBuffers(audioBuffers, videoBuffers);
                    return;
                }

                var enqueueStartTicks = DateTime.UtcNow.Ticks;
                await this.audioVideoFramePlayer.EnqueueBuffersAsync(audioBuffers, videoBuffers).ConfigureAwait(false);
                if (_latencyDiag && ShouldLogLatency(latencySequence))
                {
                    var enqueueEndTicks = DateTime.UtcNow.Ticks;
                    var queueWaitMs = (enqueueStartTicks - requestTicks) / TimeSpan.TicksPerMillisecond;
                    var enqueueMs = (enqueueEndTicks - enqueueStartTicks) / TimeSpan.TicksPerMillisecond;
                    var sidecarToEnqueuedMs = sidecarSentTicks.HasValue
                        ? (enqueueEndTicks - sidecarSentTicks.Value) / TimeSpan.TicksPerMillisecond
                        : (long?)null;
                    SafeLog(Microsoft.Extensions.Logging.LogLevel.Warning,
                        $"[latency][bot] enqueue_complete seq={latencySequence} source={latencySource ?? ""} response_id={responseId ?? ""} audio={audioBuffers.Count} video={videoBuffers.Count} queue_wait_ms={queueWaitMs} enqueue_ms={enqueueMs} sidecar_to_enqueued_ms={sidecarToEnqueuedMs}");
                }
            }
            catch (Exception ex)
            {
                SafeLog(Microsoft.Extensions.Logging.LogLevel.Error, "[bot-send-diag] EnqueueBuffersAsync failed", ex);
            }
        }
    }
}

