# EchoBot Deployment to a Windows VM

This guide documents the end-to-end steps to deploy the EchoBot or custom calling bot code to a Windows VM so that the Microsoft Teams calling endpoint works through Azure Bot Service.

It is written for the architecture used in this repository:

- Microsoft Teams
- Azure Bot Service
- Public HTTPS calling endpoint
- Windows VM running EchoBot
- Optional downstream services such as Azure AI Speech, Voice Live API, Container Apps, or databases

## Target Architecture

The request flow is:

1. A user interacts with the bot in Microsoft Teams.
2. Azure Bot Service receives the bot traffic.
3. Azure Bot Service calls your public HTTPS endpoint.
4. DNS resolves your custom domain to the public IP of the Windows VM.
5. The VM accepts traffic on port 443 and forwards it to the EchoBot process.
6. EchoBot handles the calling logic and optional downstream integrations.

## Prerequisites

You need the following before you start:

- An Azure subscription where you can create networking and compute resources.
- A Microsoft 365 tenant with Teams enabled.
- Permission to create or manage an Entra app registration.
- Tenant admin rights to grant Microsoft Graph admin consent.
- A public DNS domain that you control.
- A Windows VM in Azure, or permission to create one.
- RDP access to the VM.
- A certificate for the public bot domain, exported as a PFX.
- The .NET runtime required by the EchoBot build.

## Deployment Order

The order matters. Follow the steps in this sequence:

1. Create the Entra app registration.
2. Add Microsoft Graph calling permissions and grant admin consent.
3. Create or configure the Azure Bot Service to use that app registration.
4. Provision the Windows VM and public networking.
5. Configure DNS for the custom domain.
6. Create and import the TLS certificate.
7. Publish and copy the EchoBot build to the VM.
8. Configure HTTPS, firewall rules, and optional port proxy on the VM.
9. Start EchoBot on the VM.
10. Point Azure Bot Service to the final HTTPS endpoint.
11. Test the calling path end to end.

## 1. Create the Entra App Registration

Create the app registration first because the bot identity is required by Azure Bot Service and by Microsoft Graph.

Azure CLI example:

```bash
az ad app create \
  --display-name "lisa-hr-bot" \
  --sign-in-audience AzureADMyOrg
```

Create a client secret and store it securely:

```bash
az ad app credential reset \
  --id <APP_ID> \
  --append \
  --display-name "echo-bot-secret"
```

Record these values:

- Application (client) ID
- Directory (tenant) ID
- Client secret value

## 2. Add Microsoft Graph Calling Permissions

The bot cannot handle Teams calling until the app registration has the required Microsoft Graph application permissions.

At minimum, this sample documentation has historically used:

- `Calls.AccessMedia.All`
- `Calls.JoinGroupCall.All`

Depending on your final scenario, additional permissions can be required. Always confirm against the current calling bot documentation for your use case.

Portal flow:

1. Open Entra ID.
2. Open the app registration.
3. Go to API permissions.
4. Add Microsoft Graph application permissions.
5. Add the calling permissions.
6. Click Grant admin consent.

This step happens before the bot can successfully join or process calls.

## 3. Create or Configure the Azure Bot Service

Create the Azure Bot Service after the app registration exists, because the bot service needs to be bound to the app identity.

Typical information used here:

- Microsoft App ID: the Entra app client ID
- Microsoft App Type: single tenant or multi-tenant, depending on your environment
- Messaging endpoint: can be set later, after the VM endpoint is ready

If you already have the Azure Bot Service, confirm that it uses the same App ID as the app registration from step 1.

## 4. Provision the Windows VM

Create a Windows VM that will host EchoBot.

Typical Azure CLI example:

```bash
az vm create \
  --resource-group <RESOURCE_GROUP> \
  --name <VM_NAME> \
  --image MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest \
  --admin-username <ADMIN_USER> \
  --admin-password '<ADMIN_PASSWORD>' \
  --public-ip-sku Standard \
  --size Standard_D4s_v3
```

Why the VM needs a public ingress in this architecture:

- Azure Bot Service must reach the calling endpoint from outside your private network.
- In the current design, the Windows VM is the public ingress.
- If you later place Application Gateway or another public reverse proxy in front of the VM, the VM itself can become private, but some public ingress is still required.

## 5. Open the Required Network Paths

Configure inbound network rules on the Azure NSG and on Windows Firewall.

At minimum, you typically need:

- `443` for public HTTPS
- `3389` for RDP administration
- Optional internal ports used by the app, such as `8445`, `9441`, or `9442`

Azure NSG examples:

```bash
az network nsg rule create \
  --resource-group <RESOURCE_GROUP> \
  --nsg-name <NSG_NAME> \
  --name Allow-HTTPS \
  --priority 200 \
  --access Allow \
  --direction Inbound \
  --protocol Tcp \
  --destination-port-ranges 443

az network nsg rule create \
  --resource-group <RESOURCE_GROUP> \
  --nsg-name <NSG_NAME> \
  --name Allow-RDP \
  --priority 210 \
  --access Allow \
  --direction Inbound \
  --protocol Tcp \
  --destination-port-ranges 3389
```

Important:

- Check the NIC-level NSG.
- Check the subnet-level NSG.
- Check Windows Firewall on the VM.

## 6. Connect to the VM with Remote Desktop

Once the VM is deployed and port 3389 is allowed, connect by RDP.

Typical flow:

1. Look up the public IP.
2. Open Remote Desktop.
3. Connect to the VM using the administrator credentials.

Useful command to confirm the public IP:

```bash
az vm show \
  --resource-group <RESOURCE_GROUP> \
  --name <VM_NAME> \
  --show-details \
  --query publicIps \
  --output tsv
```

## 7. Configure the Custom Domain

Choose a stable public DNS name, for example:

```text
bot.example.com
```

Create an `A` record that points to the public IP of the VM.

Azure DNS example:

```bash
az network dns zone create \
  --resource-group <RESOURCE_GROUP> \
  --name example.com

az network dns record-set a add-record \
  --resource-group <RESOURCE_GROUP> \
  --zone-name example.com \
  --record-set-name bot \
  --ipv4-address <PUBLIC_IP>
```

Verify DNS before moving on:

```bash
nslookup bot.example.com
```

You should only continue once the domain resolves to the correct public IP.

## 8. Create and Prepare the TLS Certificate

The calling endpoint must be available over HTTPS. The certificate must match the public host name exactly.

Requirements:

- Subject name or SAN must include `bot.example.com`
- You need the private key
- Export the certificate as a `.pfx`

## 9. Import the Certificate on the Windows VM

Copy the PFX file to the VM and import it into the `LocalMachine\My` certificate store.

PowerShell example on the VM:

```powershell
$password = ConvertTo-SecureString "<PFX_PASSWORD>" -AsPlainText -Force
Import-PfxCertificate \
  -FilePath "C:\Certs\bot.example.com.pfx" \
  -CertStoreLocation Cert:\LocalMachine\My \
  -Password $password
```

List the imported certificate and copy the thumbprint:

```powershell
Get-ChildItem Cert:\LocalMachine\My | Select Subject, Thumbprint, NotAfter
```

The thumbprint is needed later for HTTPS binding.

## 10. Install the Required .NET Runtime on the VM

The EchoBot process is started with:

```powershell
dotnet EchoBot.dll
```

So the correct .NET runtime must be installed.

Verify:

```powershell
dotnet --info
```

If `dotnet` is not found in the current shell, you can temporarily add it:

```powershell
$env:Path += ";C:\Program Files\dotnet"
```

## 11. Publish the EchoBot Build

Publish the application from your development machine.

Example:

```bash
cd Samples/PublicSamples/EchoBot/src/EchoBot
dotnet publish -c Release
```

Why use `publish`:

- It produces a deployable output.
- It is more reliable than copying only a single DLL from a build folder.
- It keeps all required runtime files together.

## 12. Copy the Published Output to the VM

Create a folder on the VM, for example:

```text
C:\EchoBot
```

Copy the published files there. The folder should contain:

- `EchoBot.dll`
- dependent DLLs
- configuration files
- other runtime assets

You can copy the files with RDP file transfer, a ZIP archive, `az vm run-command`, or another approved transfer mechanism.

## 13. Configure the EchoBot Settings

Configure the app with the values from your environment.

Typical values include:

- Microsoft App ID
- Microsoft App Password or other bot auth settings
- Tenant ID
- Certificate thumbprint
- Public bot domain
- Azure AI Speech settings or other speech provider settings
- Agent or backend URLs

Store these in your configuration file or in environment variables, depending on how your custom code is implemented.

## 14. Bind the Certificate to HTTPS on Port 443

On the Windows VM, bind the certificate to the public HTTPS listener.

Example:

```powershell
netsh http add sslcert \
  ipport=0.0.0.0:443 \
  certhash=<CERT_THUMBPRINT_WITHOUT_SPACES> \
  appid="{7e162b8d-b6b0-4565-9925-d785742e6502}"
```

To inspect the current bindings:

```powershell
netsh http show sslcert
```

This step is required because importing the certificate alone does not make HTTPS work.

## 15. Configure Port Proxy if the App Does Not Listen on 443

If EchoBot listens on an internal port such as `8445` or `9441`, configure a Windows port proxy so that public HTTPS traffic on port 443 reaches the internal app port.

Example:

```powershell
netsh interface portproxy add v4tov4 \
  listenport=443 \
  listenaddress=0.0.0.0 \
  connectport=9441 \
  connectaddress=127.0.0.1
```

Inspect port proxy rules:

```powershell
netsh interface portproxy show all
```

If the app already listens directly on 443, you do not need this step.

## 16. Start EchoBot on the Windows VM

Open PowerShell on the VM and start the bot:

```powershell
cd C:\EchoBot
dotnet EchoBot.dll
```

Run it in the foreground the first time so that you can immediately see startup logs and errors.

Once the bot works, you can later convert this into a more permanent startup model such as a Windows service.

## 17. Test the Endpoint Before Configuring Azure Bot Service

Verify these items before the end-to-end bot test:

1. DNS resolves the domain correctly.
2. The certificate is valid for the domain.
3. Port 443 is reachable.
4. The EchoBot process is running.

Examples:

```bash
curl -vk https://bot.example.com/
curl -vk https://bot.example.com/api/calling
```

If the endpoint is not reachable at this stage, fix DNS, TLS, firewall, binding, or port proxy before moving on.

## 18. Set the Final Azure Bot Service Endpoint

Once the VM endpoint is live, set the final bot endpoint in Azure Bot Service.

For calling, the endpoint must be the public HTTPS URL of your bot, for example:

```text
https://bot.example.com/api/calling
```

This must be a public HTTPS URL that Azure Bot Service can reach.

## 19. Test the Teams Calling Path End to End

Now validate the full chain:

1. Teams sends traffic to Azure Bot Service.
2. Azure Bot Service calls the public endpoint.
3. The endpoint reaches the Windows VM.
4. EchoBot handles the calling logic.
5. The response path returns through Azure Bot Service to Teams.

At this point you can test:

- bot join flow
- calling webhook delivery
- audio path
- downstream services such as speech or agent backends

## Deployment Checklist

Use this list when repeating the setup:

1. Create the Entra app registration.
2. Add Microsoft Graph calling application permissions.
3. Grant admin consent.
4. Create or confirm the Azure Bot Service using the same App ID.
5. Create the Windows VM.
6. Ensure the VM has a public IP.
7. Open NSG and Windows Firewall rules.
8. Connect by RDP.
9. Create the DNS record for the custom domain.
10. Create the TLS certificate.
11. Import the PFX into `LocalMachine\My`.
12. Note the certificate thumbprint.
13. Install the required .NET runtime.
14. Publish EchoBot.
15. Copy the published files to `C:\EchoBot`.
16. Apply app configuration.
17. Bind the certificate to port 443.
18. Add port proxy if the app uses an internal listener port.
19. Start `dotnet EchoBot.dll`.
20. Test the HTTPS endpoint.
21. Point Azure Bot Service to the final endpoint.
22. Test Teams calling.

## Troubleshooting

If the bot does not work, check these items first:

### DNS problems

- `bot.example.com` resolves to the wrong IP
- DNS changes have not propagated yet

### TLS problems

- the certificate does not match the domain
- the certificate is not imported into `LocalMachine\My`
- the thumbprint used in `netsh` is wrong
- no HTTPS binding exists on port 443

### Network problems

- NSG on the NIC blocks the traffic
- NSG on the subnet blocks the traffic
- Windows Firewall blocks the traffic
- public port 443 is not reachable

### Routing problems

- no port proxy exists even though the app listens internally
- port proxy forwards to the wrong internal port

### Identity problems

- Azure Bot Service uses a different App ID than the VM app config
- Microsoft Graph permissions are missing
- admin consent was not granted

### Runtime problems

- the .NET runtime is missing
- the published files are incomplete
- the bot process starts but fails because configuration values are missing

## Notes for Custom Code

If you are deploying custom bot code rather than the sample EchoBot, the infrastructure steps remain the same. What changes is only the application payload:

- publish your custom application
- copy it to the VM
- provide the correct configuration
- make sure the app exposes the required public HTTPS bot endpoint

As long as Azure Bot Service can reach the HTTPS endpoint and the app registration and Graph permissions are correct, the deployment pattern stays the same.