using System;
using System.Text.Json;
using EchoBot.Media;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace EchoBot.Tests;

/// <summary>
/// Phase 2 (LOCKED v1.2 2026-05-06) unit tests for SpeechService PTS handling.
/// Covers ConvertPtsToTicks arithmetic + ResolveTimestamp branching matrix.
///
/// Per design doc §6 — these tests must be green before flipping
/// LISA_USE_PTS=1 in production.
/// </summary>
public class SpeechServicePtsTests
{
    private const long TICKS_PER_SECOND = 10_000_000L;

    private static SpeechService NewSvc(long fakeNowTicks)
    {
        var svc = new SpeechService(NullLogger.Instance, usePts: true);
        svc.Clock = () => fakeNowTicks;
        return svc;
    }

    private static SpeechService NewSvcWithMutableClock(out Func<long, long> setNow)
    {
        long current = 0;
        var svc = new SpeechService(NullLogger.Instance, usePts: true);
        svc.Clock = () => current;
        setNow = (newVal) => { current = newVal; return current; };
        return svc;
    }

    private static JsonElement Json(string json) => JsonDocument.Parse(json).RootElement;

    // ── ConvertPtsToTicks ───────────────────────────────────────────────────

    [Fact]
    public void ConvertPtsToTicks_PtsZero_AudioBase_ReturnsZero()
    {
        // Regression: PTS 0 is legitimate (encoder time origin), NOT "missing".
        // The §3 fallback chain refinement depends on this.
        var svc = NewSvc(0);
        Assert.Equal(0L, svc.ConvertPtsToTicks(pts: 0, tbNum: 1, tbDen: 16000));
    }

    [Fact]
    public void ConvertPtsToTicks_OneSecond_AudioBase16k()
    {
        var svc = NewSvc(0);
        Assert.Equal(TICKS_PER_SECOND, svc.ConvertPtsToTicks(16000, 1, 16000));
    }

    [Fact]
    public void ConvertPtsToTicks_OneSecond_AudioBase24k_VoiceLiveDomain()
    {
        // Voice Live native audio rate. Resampler reduces to 16k pre-emit, but
        // proves the conversion math is rate-agnostic.
        var svc = NewSvc(0);
        Assert.Equal(TICKS_PER_SECOND, svc.ConvertPtsToTicks(24000, 1, 24000));
    }

    [Fact]
    public void ConvertPtsToTicks_OneSecond_VideoBase90k()
    {
        var svc = NewSvc(0);
        Assert.Equal(TICKS_PER_SECOND, svc.ConvertPtsToTicks(90000, 1, 90000));
    }

    [Fact]
    public void ConvertPtsToTicks_NegativePts_PureMath_ReturnsNegative()
    {
        // Documented contract: ConvertPtsToTicks is pure math; policy on
        // negative PTS lives in ResolveTimestamp (which guards). Sidecar drops
        // pts<0 by design — this test pins behavior in case that changes.
        var svc = NewSvc(0);
        Assert.Equal(-625L, svc.ConvertPtsToTicks(-1, 1, 16000));
    }

    [Fact]
    public void ConvertPtsToTicks_TbDenZero_Throws()
    {
        var svc = NewSvc(0);
        Assert.Throws<ArgumentException>(() => svc.ConvertPtsToTicks(100, 1, 0));
    }

    [Fact]
    public void ConvertPtsToTicks_TbNumZero_Throws()
    {
        var svc = NewSvc(0);
        Assert.Throws<ArgumentException>(() => svc.ConvertPtsToTicks(100, 0, 16000));
    }

    [Fact]
    public void ConvertPtsToTicks_LargePts_DecimalPathDoesNotOverflow()
    {
        // Boundary that proves decimal arithmetic is in use.
        // long.MaxValue / 1000 ≈ 9.22e15. With tb=1/90000:
        //   pts * 10_000_000 = 9.22e22  →  overflows Int64 (max 9.22e18)
        // With decimal:
        //   pts * 10_000_000 / 90000 ≈ 1.025e18  (fits in Int64)
        // Result must be positive and not overflow.
        var svc = NewSvc(0);
        long bigPts = long.MaxValue / 1000;
        long ticks = svc.ConvertPtsToTicks(bigPts, 1, 90000);
        Assert.True(ticks > 0, $"expected positive ticks, got {ticks}");
        // Sanity: result ≈ bigPts * 10M / 90000 ≈ 1.025e18
        Assert.InRange(ticks, 1_000_000_000_000_000_000L, 1_100_000_000_000_000_000L);
    }

    // ── ResolveTimestamp: branching matrix (§3 + §6) ────────────────────────

    [Fact]
    public void ResolveTimestamp_FirstAudio_SetsAnchorAndReturnsItExactly()
    {
        const long FAKE_NOW = 638_500_000_000_000_000L;
        var svc = NewSvc(FAKE_NOW);
        // pts=1000 @ 1/16000 → 625_000 ticks
        var msg = Json("""{"pts":1000,"tb_num":1,"tb_den":16000,"data":"AA=="}""");

        var ts = svc.ResolveTimestamp(msg, isAudio: true);

        // First audio: returned ts = anchor + (pts_ticks - first_audio_pts_ticks)
        //                          = FAKE_NOW + 0
        Assert.Equal(FAKE_NOW, ts);
    }

    [Fact]
    public void ResolveTimestamp_VideoBeforeAnyAudio_ReturnsWallClock_DoesNotSetAnchor()
    {
        const long FAKE_NOW = 638_500_000_000_000_000L;
        var svc = NewSvc(FAKE_NOW);
        var video = Json("""{"pts":3000,"tb_num":1,"tb_den":90000,"data":"AA=="}""");

        var ts = svc.ResolveTimestamp(video, isAudio: false);

        Assert.Equal(FAKE_NOW, ts);
        // Anchor still null → next audio buffer is what sets it.
        // Verified indirectly: if anchor were set, the audio test below would
        // produce a different value than its own NOW.
        var svc2 = NewSvc(FAKE_NOW + 1_000_000);
        svc2.ResolveTimestamp(video, isAudio: false);
        var audio = Json("""{"pts":0,"tb_num":1,"tb_den":16000,"data":"AA=="}""");
        var auTs = svc2.ResolveTimestamp(audio, isAudio: true);
        Assert.Equal(FAKE_NOW + 1_000_000, auTs); // anchor set by audio, not by prior video
    }

    [Fact]
    public void ResolveTimestamp_VideoAfterAudio_UsesSharedAnchor()
    {
        var svc = NewSvcWithMutableClock(out var setNow);
        // Audio at NOW=A_NOW with pts=1000 @ 1/16000 → audio_pts_ticks = 625_000.
        // Anchor = A_NOW. anchor_audio_pts_ticks = 625_000.
        const long A_NOW = 638_500_000_000_000_000L;
        setNow(A_NOW);
        svc.ResolveTimestamp(Json("""{"pts":1000,"tb_num":1,"tb_den":16000}"""), isAudio: true);

        // Video pts=270000 @ 1/90000 → video_pts_ticks = 30_000_000 (3 s)
        // Expected: anchor + (video_pts_ticks - audio_pts_ticks)
        //         = A_NOW + (30_000_000 - 625_000) = A_NOW + 29_375_000
        setNow(A_NOW + 99_999_999); // wall-clock has moved a lot — must be ignored
        var ts = svc.ResolveTimestamp(
            Json("""{"pts":270000,"tb_num":1,"tb_den":90000}"""), isAudio: false);

        Assert.Equal(A_NOW + 29_375_000L, ts);
    }

    [Fact]
    public void ResolveTimestamp_AudioMissingPts_FallsBackToWallClock_WarnsOnce()
    {
        // Use a recording logger to count warnings.
        var rec = new RecordingLogger();
        var svc = new SpeechService(rec, usePts: true);
        svc.Clock = () => 12345L;
        var msg = Json("""{"data":"AA=="}""");

        var t1 = svc.ResolveTimestamp(msg, isAudio: true);
        var t2 = svc.ResolveTimestamp(msg, isAudio: true);

        Assert.Equal(12345L, t1);
        Assert.Equal(12345L, t2);
        Assert.Equal(1, rec.WarningCount(s => s.Contains("PTS missing")));
    }

    [Fact]
    public void ResolveTimestamp_LegacyTimestampZero_FallsBackToWallClock()
    {
        // Sidecar's current hardcoded timestamp:0 must NOT be honored as an
        // actual buffer time of 0 ticks. §3 fallback chain refinement (v1.1).
        var svc = NewSvc(98765L);
        var msg = Json("""{"timestamp":0,"data":"AA=="}""");
        var ts = svc.ResolveTimestamp(msg, isAudio: false);
        Assert.Equal(98765L, ts);
    }

    [Fact]
    public void ResolveTimestamp_LegacyTimestampNonZero_UsedDirectly()
    {
        // Hypothetical legacy non-zero timestamp passes through as-is.
        var svc = NewSvc(98765L);
        var msg = Json("""{"timestamp":42,"data":"AA=="}""");
        var ts = svc.ResolveTimestamp(msg, isAudio: false);
        Assert.Equal(42L, ts);
    }

    [Fact]
    public void ResolveTimestamp_NegativePts_TreatedAsMissing()
    {
        // Defense-in-depth: malformed message with pts<0 should NOT corrupt
        // the anchor by setting it to a negative offset. Falls through to
        // wall-clock + canary.
        var rec = new RecordingLogger();
        var svc = new SpeechService(rec, usePts: true);
        svc.Clock = () => 555L;
        var msg = Json("""{"pts":-100,"tb_num":1,"tb_den":16000,"data":"AA=="}""");

        var ts = svc.ResolveTimestamp(msg, isAudio: true);

        Assert.Equal(555L, ts);
        Assert.Equal(1, rec.WarningCount(s => s.Contains("PTS missing")));
    }

    [Fact]
    public void ResolveTimestamp_AudioPts_LogsDriftMetric()
    {
        var rec = new RecordingLogger();
        var svc = new SpeechService(rec, usePts: true);
        long now = 100_000_000L;
        svc.Clock = () => now;

        svc.ResolveTimestamp(Json("""{"pts":0,"tb_num":1,"tb_den":16000,"data":"AA=="}"""), isAudio: true);
        now += TimeSpan.FromSeconds(6).Ticks;
        svc.ResolveTimestamp(Json("""{"pts":16000,"tb_num":1,"tb_den":16000,"data":"AA=="}"""), isAudio: true);

        Assert.Contains(rec.Records, r =>
            r.Level == LogLevel.Information &&
            r.Message.Contains("audio_pts_vs_wallclock_drift_ms") &&
            r.Message.Contains("last=-5000"));
    }
}
