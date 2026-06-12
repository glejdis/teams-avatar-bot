using System;
using System.Collections.Generic;
using Microsoft.Extensions.Logging;

namespace EchoBot.Tests;

/// <summary>
/// Minimal ILogger that records messages by level. Used by tests that need to
/// assert on canary log emission (e.g. PTS-missing one-shot warning).
/// </summary>
internal sealed class RecordingLogger : ILogger
{
    public List<(LogLevel Level, string Message)> Records { get; } = new();

    public IDisposable BeginScope<TState>(TState state) where TState : notnull => NullScope.Instance;
    public bool IsEnabled(LogLevel logLevel) => true;

    public void Log<TState>(
        LogLevel logLevel, EventId eventId, TState state, Exception? exception,
        Func<TState, Exception?, string> formatter)
    {
        Records.Add((logLevel, formatter(state, exception)));
    }

    public int WarningCount(Func<string, bool> predicate)
    {
        int n = 0;
        foreach (var r in Records)
            if (r.Level == LogLevel.Warning && predicate(r.Message)) n++;
        return n;
    }

    private sealed class NullScope : IDisposable
    {
        public static NullScope Instance { get; } = new();
        public void Dispose() { }
    }
}
