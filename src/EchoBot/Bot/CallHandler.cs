using EchoBot.Models;
using EchoBot.Util;
using Microsoft.Graph;
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using System.Timers;

namespace EchoBot.Bot
{
    /// <summary>
    /// Call Handler Logic.
    /// </summary>
    public class CallHandler : HeartbeatHandler
    {
        // Diagnostic logger that survives EventLogLogger crashes.
        private readonly ILogger _logger;
        private void SafeLog(LogLevel level, string msg)
        {
            try { _logger?.Log(level, msg); }
            catch (Exception ex)
            {
                try { Console.Error.WriteLine($"[CallHandler.SafeLog] {level}: {msg} :: {ex.Message}"); } catch { }
            }
        }

        /// <summary>
        /// Gets the call.
        /// </summary>
        /// <value>The call.</value>
        public ICall Call { get; }

        /// <summary>
        /// Gets the bot media stream.
        /// </summary>
        /// <value>The bot media stream.</value>
        public BotMediaStream BotMediaStream { get; private set; }

        /// <summary>
        /// Initializes a new instance of the <see cref="CallHandler" /> class.
        /// </summary>
        /// <param name="statefulCall">The stateful call.</param>
        /// <param name="settings">The settings.</param>
        /// <param name="logger"></param>
        public CallHandler(
            ICall statefulCall,
            AppSettings settings,
            ILogger logger,
            JoinCallBody? joinContext = null
        )
            : base(TimeSpan.FromMinutes(10), statefulCall?.GraphLogger)
        {
            this._logger = logger;
            this.Call = statefulCall;
            try { Console.Error.WriteLine($"[CH-DIAG] ctor enter callId={this.Call?.Id} scenarioId={this.Call?.ScenarioId} loggerNull={(logger==null)}"); } catch {}
            SafeLog(LogLevel.Warning, $"[bot-mic-diag][callhandler] ctor enter callId={this.Call?.Id} scenarioId={this.Call?.ScenarioId}");
            this.Call.OnUpdated += this.CallOnUpdated;
            this.Call.Participants.OnUpdated += this.ParticipantsOnUpdated;
            try { Console.Error.WriteLine($"[CH-DIAG] events subscribed callId={this.Call.Id}"); } catch {}
            SafeLog(LogLevel.Warning, $"[bot-mic-diag][callhandler] events subscribed callId={this.Call.Id}");

            this.BotMediaStream = new BotMediaStream(this.Call.GetLocalMediaSession(), this.Call.Id, this.GraphLogger, logger, settings, joinContext);
            try { Console.Error.WriteLine($"[CH-DIAG] ctor done callId={this.Call.Id}"); } catch {}
            SafeLog(LogLevel.Warning, $"[bot-mic-diag][callhandler] ctor done callId={this.Call.Id}");
        }

        /// <inheritdoc/>
        protected override Task HeartbeatAsync(ElapsedEventArgs args)
        {
            return this.Call.KeepAliveAsync();
        }

        /// <inheritdoc />
        protected override void Dispose(bool disposing)
        {
            base.Dispose(disposing);
            this.Call.OnUpdated -= this.CallOnUpdated;
            this.Call.Participants.OnUpdated -= this.ParticipantsOnUpdated;

            this.BotMediaStream?.ShutdownAsync().ForgetAndLogExceptionAsync(this.GraphLogger);
        }

        /// <summary>
        /// Event fired when the call has been updated.
        /// </summary>
        /// <param name="sender">The call.</param>
        /// <param name="e">The event args containing call changes.</param>
        private async void CallOnUpdated(ICall sender, ResourceEventArgs<Call> e)
        {
            try { Console.Error.WriteLine($"[CH-DIAG] call state={e.OldResource?.State}->{e.NewResource?.State} mediaAudio={e.NewResource?.MediaState?.Audio} callId={sender?.Id}"); } catch {}
            SafeLog(LogLevel.Warning, $"[bot-mic-diag][call] state={e.OldResource?.State}->{e.NewResource?.State} subject={e.NewResource?.Subject} mediaState.audio={e.NewResource?.MediaState?.Audio} resultInfo={e.NewResource?.ResultInfo?.Message} callId={sender?.Id}");

            if (e.OldResource.State != e.NewResource.State && e.NewResource.State == CallState.Established)
            {
                SafeLog(LogLevel.Warning, $"[bot-mic-diag][call] established — notifying sidecar callId={sender?.Id}");
                if (BotMediaStream != null)
                {
                    // async void caller — wrap in Task.Run + try/catch so a
                    // sidecar outage can't crash the call event loop.
                    _ = Task.Run(async () =>
                    {
                        try { await BotMediaStream.NotifyCallEstablishedAsync(); }
                        catch (Exception ex)
                        {
                            SafeLog(LogLevel.Error, $"[bot-mic-diag][call] NotifyCallEstablishedAsync failed: {ex.Message}");
                        }
                    });
                }
            }

            if ((e.OldResource.State == CallState.Established) && (e.NewResource.State == CallState.Terminated))
            {
                if (BotMediaStream != null)
                {
                    await BotMediaStream.ShutdownAsync().ForgetAndLogExceptionAsync(GraphLogger);
                }
            }
        }

        /// <summary>
        /// Creates the participant update json.
        /// </summary>
        /// <param name="participantId">The participant identifier.</param>
        /// <param name="participantDisplayName">Display name of the participant.</param>
        /// <returns>System.String.</returns>
        private string createParticipantUpdateJson(string participantId, string participantDisplayName = "")
        {
            if (participantDisplayName.Length == 0)
                return "{" + String.Format($"\"Id\": \"{participantId}\"") + "}";
            else
                return "{" + String.Format($"\"Id\": \"{participantId}\", \"DisplayName\": \"{participantDisplayName}\"") + "}";
        }

        /// <summary>
        /// Updates the participant.
        /// </summary>
        /// <param name="participants">The participants.</param>
        /// <param name="participant">The participant.</param>
        /// <param name="added">if set to <c>true</c> [added].</param>
        /// <param name="participantDisplayName">Display name of the participant.</param>
        /// <returns>System.String.</returns>
        private string updateParticipant(List<IParticipant> participants, IParticipant participant, bool added, string participantDisplayName = "")
        {
            if (added)
            {
                if (!participants.Exists(existing => existing?.Id == participant.Id))
                {
                    participants.Add(participant);
                }
            }
            else
            {
                participants.RemoveAll(existing => existing?.Id == participant.Id);
            }
            return createParticipantUpdateJson(participant.Id, participantDisplayName);
        }

        /// <summary>
        /// Updates the participants.
        /// </summary>
        /// <param name="eventArgs">The event arguments.</param>
        /// <param name="added">if set to <c>true</c> [added].</param>
        private void updateParticipants(ICollection<IParticipant> eventArgs, bool added = true)
        {
            foreach (var participant in eventArgs)
            {
                var json = string.Empty;

                // todo remove the cast with the new graph implementation,
                // for now we want the bot to only subscribe to "real" participants
                var participantDetails = participant.Resource.Info.Identity.User;

                if (participantDetails != null)
                {
                    json = updateParticipant(this.BotMediaStream.participants, participant, added, participantDetails.DisplayName);
                }
                else if (participant.Resource.Info.Identity.AdditionalData?.Count > 0)
                {
                    if (CheckParticipantIsUsable(participant))
                    {
                        json = updateParticipant(this.BotMediaStream.participants, participant, added);
                    }
                }
            }
        }

        /// <summary>
        /// Event fired when the participants collection has been updated.
        /// </summary>
        /// <param name="sender">Participants collection.</param>
        /// <param name="args">Event args containing added and removed participants.</param>
        public void ParticipantsOnUpdated(IParticipantCollection sender, CollectionEventArgs<IParticipant> args)
        {
            // Diagnostic: log added/removed participants with identity + media-source IDs
            // so we can correlate ReceiveUnmixedMeetingAudio buffers and detect lobby state.
            try
            {
                foreach (var p in args.AddedResources)
                {
                    var u = p?.Resource?.Info?.Identity?.User;
                    var ad = p?.Resource?.Info?.Identity?.AdditionalData;
                    string adKeys = ad == null ? "<none>" : string.Join(",", ad.Keys);
                    try { Console.Error.WriteLine($"[CH-DIAG] participant ADDED id={p?.Id} userId={u?.Id} display={u?.DisplayName} idTypes=[{adKeys}]"); } catch {}
                    SafeLog(LogLevel.Warning, $"[bot-mic-diag][participants] ADDED id={p?.Id} userId={u?.Id} display={u?.DisplayName} idTypes=[{adKeys}]");
                }
                foreach (var p in args.RemovedResources)
                {
                    var u = p?.Resource?.Info?.Identity?.User;
                    try { Console.Error.WriteLine($"[CH-DIAG] participant REMOVED id={p?.Id} userId={u?.Id} display={u?.DisplayName}"); } catch {}
                    SafeLog(LogLevel.Warning, $"[bot-mic-diag][participants] REMOVED id={p?.Id} userId={u?.Id} display={u?.DisplayName}");
                }
            }
            catch (Exception ex) { SafeLog(LogLevel.Error, $"[bot-mic-diag][participants] log failed: {ex.Message}"); }

            updateParticipants(args.AddedResources);
            updateParticipants(args.RemovedResources, false);

            // Auto-hangup: if all human participants have left, end the call so we
            // don't keep paying for an empty meeting. Only check on REMOVAL events to
            // avoid hanging up before any human has joined (guest/bot is added first).
            try
            {
                bool participantLeft = false;
                foreach (var p in args.RemovedResources)
                {
                    var u = p?.Resource?.Info?.Identity?.User;
                    if ((u != null && !string.IsNullOrEmpty(u.Id)) || (p != null && CheckParticipantIsUsable(p)))
                    {
                        participantLeft = true;
                        break;
                    }
                }
                int participantCount = this.BotMediaStream.participants.Count;
                int aadUserCount = this.BotMediaStream.participants.Count(CheckParticipantHasUser);
                if (participantLeft)
                {
                    SafeLog(LogLevel.Warning, $"[bot-autohangup] participant left - remaining participantCount={participantCount} aadUserCount={aadUserCount}");
                }
                if (participantLeft && (participantCount == 0 || aadUserCount == 0))
                {
                    SafeLog(LogLevel.Warning, "[bot-autohangup] no human AAD participants remain - terminating call");
                    try { Console.Error.WriteLine($"[CH-DIAG] AUTO-HANGUP callId={this.Call?.Id}"); } catch {}
                    _ = Task.Run(async () =>
                    {
                        try
                        {
                            await Task.Delay(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
                            await this.Call.DeleteAsync().ConfigureAwait(false);
                        }
                        catch (Exception ex)
                        {
                            SafeLog(LogLevel.Error, $"[bot-autohangup] DeleteAsync failed: {ex.Message}");
                        }
                    });
                }
            }
            catch (Exception ex)
            {
                SafeLog(LogLevel.Error, $"[bot-autohangup] check failed: {ex.Message}");
            }
        }

        /// <summary>
        /// Checks the participant is usable.
        /// </summary>
        /// <param name="p">The p.</param>
        /// <returns><c>true</c> if XXXX, <c>false</c> otherwise.</returns>
        private bool CheckParticipantIsUsable(IParticipant p)
        {
            var additionalData = p?.Resource?.Info?.Identity?.AdditionalData;
            if (additionalData == null)
            {
                return false;
            }

            foreach (var i in additionalData)
                if (i.Key != "applicationInstance" && i.Value is Identity)
                    return true;

            return false;
        }

        private bool CheckParticipantHasUser(IParticipant p)
        {
            return !string.IsNullOrEmpty(p?.Resource?.Info?.Identity?.User?.Id);
        }
    }
}

