using Microsoft.Skype.Bots.Media;

namespace EchoBot.Media
{
    public class MediaStreamEventArgs
    {
        public List<AudioMediaBuffer> AudioMediaBuffers { get; set; }
        public List<VideoMediaBuffer> VideoMediaBuffers { get; set; }
        public string? LatencySource { get; set; }
        public string? VoiceLiveResponseId { get; set; }
        public long? SidecarSentTicks { get; set; }
        public long? BotDispatchTicks { get; set; }
    }
}
