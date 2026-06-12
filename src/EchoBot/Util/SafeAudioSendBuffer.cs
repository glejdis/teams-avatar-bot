using Microsoft.Skype.Bots.Media;

namespace EchoBot.Util
{
    /// <summary>
    /// Replacement for the stock <c>AudioSendBuffer</c> that intentionally does NOT
    /// free its native memory on <see cref="Dispose"/>.
    ///
    /// The stock implementation calls <c>Marshal.FreeHGlobal(Data)</c> in
    /// <see cref="Dispose(bool)"/>, but <c>AudioFramePlayer.CopyAndSendData</c>
    /// invokes a native <c>CopyMemory</c> asynchronously after the buffer has been
    /// dequeued, racing with Dispose and producing
    /// <see cref="System.AccessViolationException"/> in
    /// <c>Microsoft.Skype.Bots.Media.AudioFramePlayer.CopyMemory(IntPtr, IntPtr, UInt32)</c>.
    ///
    /// We accept a small leak (~32 KB/s while the avatar is speaking) in exchange
    /// for crash-free playback. The process is restarted on each call anyway via
    /// NSSM, so the leak is bounded in practice.
    /// </summary>
    internal sealed class SafeAudioSendBuffer : AudioMediaBuffer
    {
        public SafeAudioSendBuffer(IntPtr data, long length, AudioFormat format, long timestamp)
        {
            Data = data;
            Length = length;
            AudioFormat = format;
            Timestamp = timestamp;
        }

        protected override void Dispose(bool disposing)
        {
            // Intentionally NOT calling Marshal.FreeHGlobal(Data) and not
            // calling base.Dispose either (it is abstract).
            // See class summary for rationale.
        }
    }
}
