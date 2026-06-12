using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Skype.Bots.Media;
using System.Runtime.InteropServices;
using Azure.Identity;
using Azure.Core;
using System.Net.Http.Json;
using System.Reflection;

namespace EchoBot.Media
{
    /// <summary>
    /// Class SpeechService.
    /// </summary>
    public class SpeechService
    {
        /// <summary>
        /// The is the indicator if the media stream is running
        /// </summary>
        private bool _isRunning = false;
        /// <summary>
        /// The is draining indicator
        /// </summary>
        protected bool _isDraining;
        /// <summary>
        /// Flag to suppress audio input while TTS is playing (prevent bot hearing itself)
        /// </summary>
        private volatile bool _isSpeaking = false;

        /// <summary>
        /// The logger
        /// </summary>
        private readonly ILogger _logger;
        private readonly PushAudioInputStream _audioInputStream = AudioInputStream.CreatePushStream(AudioStreamFormat.GetWaveFormatPCM(16000, 16, 1));
        private readonly AudioOutputStream _audioOutputStream = AudioOutputStream.CreatePullStream();

        private readonly SpeechConfig _speechConfig;
        private SpeechRecognizer _recognizer;
        private readonly SpeechSynthesizer _synthesizer;

        // NEU: HttpClient für Agent-Aufrufe
        private readonly HttpClient _httpClient;
        private readonly string _agentEndpoint;

        // Typing background sound: pre-loaded 20ms PCM chunks from embedded WAV
        private readonly List<byte[]> _typingChunks;

    
        /// <summary>
        /// Calls the LangGraph Invoice Agent and returns the response
        /// Endpoint: /responses
        /// Request:  {"input": "user message"}
        /// Response: {"output": "agent response"}
        /// </summary>
        private async Task<string> CallInvoiceAgent(string userMessage)
        {
            try
            {
                var payload = new { input = userMessage };
                var response = await _httpClient.PostAsJsonAsync(_agentEndpoint, payload);
                response.EnsureSuccessStatusCode();
                
                var result = await response.Content.ReadFromJsonAsync<AgentResponse>();
                return result?.Output ?? "Keine Antwort vom Agent erhalten.";
            }
            catch (HttpRequestException ex)
            {
                _logger.LogError(ex, "HTTP error calling Invoice Agent");
                throw;
            }
        }

        /// <summary>
        /// Response model for LangGraph agent
        /// </summary>
        private class AgentResponse
        {
            public string Output { get; set; }
        }

        /// <summary>
        /// Exchanges an Entra ID token for a Speech Service token via the custom domain token endpoint.
        /// </summary>
        private static async Task<string> ExchangeTokenForSpeechToken(string customDomainHost, string aadToken)
        {
            using var client = new HttpClient();
            var request = new HttpRequestMessage(HttpMethod.Post, $"https://{customDomainHost}/sts/v1.0/issuetoken");
            request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", aadToken);
            request.Content = new StringContent(string.Empty);
            request.Content.Headers.ContentLength = 0;

            var response = await client.SendAsync(request);
            response.EnsureSuccessStatusCode();
            return await response.Content.ReadAsStringAsync();
        }


        /// <summary>
        /// Initializes a new instance of the <see cref="SpeechService" /> class.        
        public SpeechService(AppSettings settings, ILogger logger)
        {
            _logger = logger;

            // Entra ID / Managed Identity Authentication mit Custom Domain
            var speechEndpoint = settings.SpeechEndpoint; // "https://invoice-voice-speech.cognitiveservices.azure.com/"
            var region = settings.SpeechConfigRegion; // "swedencentral"
            var credential = new DefaultAzureCredential();
            _logger.LogWarning($"SpeechService: Getting Entra ID token for {speechEndpoint}");
            var aadToken = credential.GetToken(new TokenRequestContext(new[] { "https://cognitiveservices.azure.com/.default" }));
            _logger.LogWarning($"SpeechService: Got Entra ID token, expires: {aadToken.ExpiresOn}");

            // Token-Tausch: Entra-ID-Token → Speech-Token über Custom Domain
            var customDomain = new Uri(speechEndpoint).Host;
            _logger.LogWarning($"SpeechService: Exchanging token via {customDomain}");
            var speechToken = ExchangeTokenForSpeechToken(customDomain, aadToken.Token).GetAwaiter().GetResult();
            _logger.LogWarning($"SpeechService: Got Speech token ({speechToken.Length} chars)");

            // Regionalen Endpoint nutzen mit dem getauschten Speech-Token
            _speechConfig = SpeechConfig.FromAuthorizationToken(speechToken, region);
            _speechConfig.SpeechSynthesisLanguage = settings.BotLanguage;
            _speechConfig.SpeechRecognitionLanguage = settings.BotLanguage;
            _speechConfig.SpeechSynthesisVoiceName = "en-US-NovaMultilingualNeural";
            _speechConfig.SetSpeechSynthesisOutputFormat(SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm);
            _logger.LogWarning($"SpeechService: Config done. region={region}, language={settings.BotLanguage}, voice=en-US-NovaMultilingualNeural");

            // HttpClient für Agent-Aufrufe
            _httpClient = new HttpClient();
            _agentEndpoint = settings.InvoiceAgentEndpoint;

            var audioConfig = AudioConfig.FromStreamOutput(_audioOutputStream);
            _synthesizer = new SpeechSynthesizer(_speechConfig, audioConfig);

            // Load typing background WAV from embedded resource
            _typingChunks = LoadTypingChunks();
            _logger.LogWarning($"SpeechService: Loaded {_typingChunks.Count} typing sound chunks ({_typingChunks.Count * 20}ms total)");

            _logger.LogInformation($"SpeechService initialized with Entra ID auth via custom domain: {customDomain}");
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
                throw new FileNotFoundException("Embedded resource typing_background.wav not found");

            using var stream = assembly.GetManifestResourceStream(resourceName);
            using var reader = new BinaryReader(stream);

            // Find the "data" chunk — header can be larger than 44 bytes
            stream.Position = 12; // skip RIFF header
            while (stream.Position < stream.Length - 8)
            {
                var chunkId = reader.ReadBytes(4);
                var wavChunkSize = reader.ReadInt32();
                if (chunkId[0] == 'd' && chunkId[1] == 'a' && chunkId[2] == 't' && chunkId[3] == 'a')
                    break; // stream.Position is now right at the audio data
                stream.Position += wavChunkSize;
            }

            var chunks = new List<byte[]>();
            const int chunkSize = 640; // 20ms at 16kHz/16bit/mono
            while (stream.Position + chunkSize <= stream.Length)
            {
                chunks.Add(reader.ReadBytes(chunkSize));
            }
            return chunks;
        }

        /// <summary>
        /// Appends the audio buffer.
        /// </summary>
        /// <param name="audioBuffer"></param>
        public async Task AppendAudioBuffer(AudioMediaBuffer audioBuffer)
        {
            if (!_isRunning)
            {
                Start();
                // Fire-and-forget: ProcessSpeech läuft im Hintergrund,
                // damit Audio-Buffers sofort geschrieben werden können
                _ = Task.Run(() => ProcessSpeech());
            }

            try
            {
                // Skip audio while bot is speaking TTS (prevent self-hearing loop)
                if (_isSpeaking) return;

                // audio for a 1:1 call
                var bufferLength = audioBuffer.Length;
                if (bufferLength > 0)
                {
                    var buffer = new byte[bufferLength];
                    Marshal.Copy(audioBuffer.Data, buffer, 0, (int)bufferLength);

                    _audioInputStream.Write(buffer);
                }
            }
            catch (Exception e)
            {
                _logger.LogError(e, "Exception happend writing to input stream");
            }
        }

        public virtual void OnSendMediaBufferEventArgs(object sender, MediaStreamEventArgs e)
        {
            if (SendMediaBuffer != null)
            {
                SendMediaBuffer(this, e);
            }
        }

        public event EventHandler<MediaStreamEventArgs> SendMediaBuffer;

        /// <summary>
        /// Ends this instance.
        /// </summary>
        /// <returns>Task.</returns>
        public async Task ShutDownAsync()
        {
            if (!_isRunning)
            {
                return;
            }

            if (_isRunning)
            {
                await _recognizer.StopContinuousRecognitionAsync();
                _recognizer.Dispose();
                _audioInputStream.Close();

                _audioInputStream.Dispose();
                _audioOutputStream.Dispose();
                _synthesizer.Dispose();

                _isRunning = false;
            }
        }

        /// <summary>
        /// Starts this instance.
        /// </summary>
        private void Start()
        {
            if (!_isRunning)
            {
                _isRunning = true;
            }
        }

        /// <summary>
        /// Processes this instance.
        /// </summary>
        private async Task ProcessSpeech()
        {
            try
            {
                var stopRecognition = new TaskCompletionSource<int>();

                using (var audioInput = AudioConfig.FromStreamInput(_audioInputStream))
                {
                    if (_recognizer == null)
                    {
                        _logger.LogInformation("init recognizer");
                        _recognizer = new SpeechRecognizer(_speechConfig, audioInput);
                    }
                }

                _recognizer.Recognizing += (s, e) =>
                {
                    _logger.LogInformation($"RECOGNIZING: Text={e.Result.Text}");
                };

                _recognizer.Recognized += async (s, e) =>
                {
                    if (e.Result.Reason == ResultReason.RecognizedSpeech)
                    {
                        if (string.IsNullOrEmpty(e.Result.Text))
                            return;

                        _logger.LogInformation($"RECOGNIZED: Text={e.Result.Text}");
                        _isSpeaking = true;

                        // Start typing background sound loop
                        var typingCts = new CancellationTokenSource();
                        var typingTask = PlayTypingBackgroundAsync(typingCts.Token);

                        try
                        {
                            _logger.LogInformation($"Calling Invoice Agent with: {e.Result.Text}");
                            var agentResponse = await CallInvoiceAgent(e.Result.Text);
                            _logger.LogInformation($"Agent Response: {agentResponse}");

                            // Stop typing background
                            typingCts.Cancel();
                            await typingTask;

                            // Speak the response
                            await TextToSpeech(agentResponse);
                        }
                        catch (Exception ex)
                        {
                            typingCts.Cancel();
                            try { await typingTask; } catch { }
                            _logger.LogError(ex, "Error calling Invoice Agent");
                            await TextToSpeech("Es gab einen Fehler bei der Verarbeitung.");
                        }
                    }
                    else if (e.Result.Reason == ResultReason.NoMatch)
                    {
                        _logger.LogInformation($"NOMATCH: Speech could not be recognized.");
                    }
                };

                _recognizer.Canceled += (s, e) =>
                {
                    _logger.LogInformation($"CANCELED: Reason={e.Reason}");

                    if (e.Reason == CancellationReason.Error)
                    {
                        _logger.LogInformation($"CANCELED: ErrorCode={e.ErrorCode}");
                        _logger.LogInformation($"CANCELED: ErrorDetails={e.ErrorDetails}");
                        _logger.LogInformation($"CANCELED: Did you update the subscription info?");
                    }

                    stopRecognition.TrySetResult(0);
                };

                _recognizer.SessionStarted += async (s, e) =>
                {
                    _logger.LogInformation("Session started event.");
                    await TextToSpeech("Hallo, wie kann ich Ihnen bei Ihren Rechnungen helfen?");
                };

                _recognizer.SessionStopped += (s, e) =>
                {
                    _logger.LogInformation("\nSession stopped event.");
                    _logger.LogInformation("\nStop recognition.");
                    stopRecognition.TrySetResult(0);
                };

                // Starts continuous recognition. Uses StopContinuousRecognitionAsync() to stop recognition.
                _logger.LogInformation("Starting continuous recognition...");
                await _recognizer.StartContinuousRecognitionAsync().ConfigureAwait(false);

                // Waits for completion (async, not blocking a thread)
                await stopRecognition.Task.ConfigureAwait(false);

                _logger.LogInformation("Recognition session ended. Stopping recognizer...");
                // Stops recognition.
                await _recognizer.StopContinuousRecognitionAsync().ConfigureAwait(false);
            }
            catch (ObjectDisposedException ex)
            {
                _logger.LogError(ex, "The queue processing task object has been disposed.");
            }
            catch (Exception ex)
            {
                // Catch all other exceptions and log
                _logger.LogError(ex, "Caught Exception");
            }

            _isDraining = false;
        }

        private async Task TextToSpeech(string text)
        {
            _isSpeaking = true;
            _logger.LogInformation($"TextToSpeech: Synthesizing '{text}'");
            // convert the text to speech
            SpeechSynthesisResult result = await _synthesizer.SpeakTextAsync(text);

            if (result.Reason == ResultReason.Canceled)
            {
                var cancellation = SpeechSynthesisCancellationDetails.FromResult(result);
                _logger.LogError($"TTS CANCELED: Reason={cancellation.Reason}, ErrorCode={cancellation.ErrorCode}, ErrorDetails={cancellation.ErrorDetails}");
                _isSpeaking = false;
                return;
            }

            if (result.Reason != ResultReason.SynthesizingAudioCompleted)
            {
                _logger.LogWarning($"TTS unexpected result: {result.Reason}");
                _isSpeaking = false;
                return;
            }

            _logger.LogInformation($"TTS completed: {result.AudioData.Length} bytes");
            // take the stream of the result
            // create 20ms media buffers of the stream
            // and send to the AudioSocket in the BotMediaStream
            int bufferCount = 0;
            using (var stream = AudioDataStream.FromResult(result))
            {
                var currentTick = DateTime.Now.Ticks;
                MediaStreamEventArgs args = new MediaStreamEventArgs
                {
                    AudioMediaBuffers = Util.Utilities.CreateAudioMediaBuffers(stream, currentTick, _logger)
                };
                bufferCount = args.AudioMediaBuffers.Count;
                OnSendMediaBufferEventArgs(this, args);
            }

            // Wait for TTS playback to finish before accepting audio again
            // Approximate: 20ms per buffer
            var playbackMs = bufferCount * 20;
            await Task.Delay(playbackMs + 500).ConfigureAwait(false);
            _isSpeaking = false;
            _logger.LogInformation($"TTS playback done, listening again (waited {playbackMs + 500}ms)");
        }

        /// <summary>
        /// Plays the typing background sound in a loop until cancelled.
        /// Sends all PCM chunks at once with sequential timestamps (like TTS does).
        /// </summary>
        private async Task PlayTypingBackgroundAsync(CancellationToken ct)
        {
            if (_typingChunks == null || _typingChunks.Count == 0)
            {
                _logger.LogWarning("No typing chunks loaded, skipping background sound");
                return;
            }

            _logger.LogInformation("Typing background started");
            const int batchSize = 50; // 50 chunks = 1 second of audio
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
                        var args = new MediaStreamEventArgs { AudioMediaBuffers = audioMediaBuffers };
                        OnSendMediaBufferEventArgs(this, args);
                    }

                    // Loop back to start of WAV when we reach the end
                    if (chunkIndex >= _typingChunks.Count)
                        chunkIndex = 0;

                    // Wait ~1 second before sending next batch
                    await Task.Delay(sent * 20, ct).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException) { }
            _logger.LogInformation("Typing background stopped");
        }
    }
}
