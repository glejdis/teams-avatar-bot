using EchoBot;
using Microsoft.Extensions.DependencyInjection.Extensions;

IHost host = Host.CreateDefaultBuilder(args)
    .UseWindowsService(options =>
    {
        options.ServiceName = "Echo Bot Service";
    })
    .ConfigureLogging(logging =>
    {
        // EventLog provider on stripped Windows VMs throws RPC-unavailable from
        // inside async-void media callbacks and can crash the process. Filter +
        // descriptor-removal proved unreliable (provider still got instantiated).
        // Bulletproof fix: nuke ALL providers registered by CreateDefaultBuilder
        // and re-add only the safe ones.
        logging.ClearProviders();
        logging.AddConsole();
        logging.AddDebug();
    })
    .ConfigureServices(services =>
    {
        services.AddSingleton<IBotHost, BotHost>();

        services.AddHostedService<EchoBotWorker>();
    })
    .Build();

await host.RunAsync();
