using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;
using EchoBot.Controllers;
using EchoBot.Models;
using Microsoft.AspNetCore.Mvc;
using Xunit;

namespace EchoBot.Tests;

public class CallsControllerContractTests
{
    [Fact]
    public void JoinCallAsync_ExposesCanonicalAndCompatibilityRoutes()
    {
        var method = typeof(CallsController).GetMethod(nameof(CallsController.JoinCallAsync));
        Assert.NotNull(method);

        var templates = method!
            .GetCustomAttributes<HttpPostAttribute>()
            .Select(attribute => attribute.Template)
            .ToArray();

        Assert.Contains(templates, template => template is null);
        Assert.Contains("joinCall", templates);
        Assert.Contains("/joinCall", templates);
    }

    [Fact]
    public void JoinCallBody_UsesLegacyJoinUrlJsonName()
    {
        var property = typeof(JoinCallBody).GetProperty(nameof(JoinCallBody.JoinUrl));
        Assert.NotNull(property);

        var jsonName = property!.GetCustomAttribute<JsonPropertyNameAttribute>();
        Assert.NotNull(jsonName);
        Assert.Equal("joinURL", jsonName!.Name);
    }

    [Fact]
    public void JoinCallBody_DeserializesLegacyPayload()
    {
        var body = JsonSerializer.Deserialize<JoinCallBody>(
            """
            {
              "joinURL": "https://teams.microsoft.com/l/meetup-join/example",
              "displayName": "Lisa HR"
            }
            """,
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true });

        Assert.NotNull(body);
        Assert.Equal("https://teams.microsoft.com/l/meetup-join/example", body!.JoinUrl);
        Assert.Equal("Lisa HR", body.DisplayName);
    }
}